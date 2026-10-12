#!/usr/bin/env python3
"""Archive posts from a specified YouTube channel Posts tab."""
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


def _text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if isinstance(value.get("simpleText"), str):
            return value["simpleText"]
        if isinstance(value.get("runs"), list):
            return "".join(run.get("text", "") for run in value["runs"] if isinstance(run, dict))
    return ""


def _walk(obj):
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _walk(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk(value)


def _image_urls(attachment):
    urls = []

    def visit(value, image_context=False):
        if isinstance(value, dict):
            for key, child in value.items():
                in_image = image_context or key.lower() in {"image", "imagerenderer", "backstageimagerenderer", "multiimagerenderer"}
                if in_image and key == "url" and isinstance(child, str) and child.startswith("https://"):
                    host = (urlparse(child).hostname or "").lower()
                    if any(host == domain or host.endswith("." + domain)
                           for domain in ("ytimg.com", "googleusercontent.com", "ggpht.com")):
                        urls.append(child)
                else:
                    visit(child, in_image)
        elif isinstance(value, list):
            for child in value:
                visit(child, image_context)

    visit(attachment)
    return list(dict.fromkeys(urls))


def normalize_post(thread, channel_url):
    """Convert yt-dlp's backstagePostThreadRenderer into a stable record."""
    post = (thread.get("post") or {}).get("backstagePostRenderer")
    if not isinstance(post, dict):
        return None
    text = _text(post.get("contentText"))
    post_id = post.get("postId") or post.get("backstagePostId")
    post_url = None
    for node in _walk(thread):
        url = node.get("url")
        if isinstance(url, str) and "/post/" in url:
            post_url = "https://www.youtube.com" + url if url.startswith("/") else url
            match = re.search(r"/post/([^?&/]+)", url)
            if not post_id and match:
                post_id = match.group(1)
            break
    if post_id:
        post_url = post_url or f"https://www.youtube.com/post/{post_id}"
    if not post_id:
        stable = json.dumps(post, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        post_id = "sha256-" + hashlib.sha256(stable.encode()).hexdigest()[:20]
    attachment = post.get("backstageAttachment") or {}
    video_ids = []
    playlist_ids = []
    for node in _walk(attachment):
        if isinstance(node.get("videoId"), str):
            video_ids.append(node["videoId"])
        if isinstance(node.get("playlistId"), str):
            playlist_ids.append(node["playlistId"])
    published = post.get("publishedTimeText") or post.get("publishedTime")
    return {
        "post_id": str(post_id),
        "url": post_url,
        "channel_url": channel_url,
        "published_time_raw": _text(published),
        "published_timestamp": post.get("timestampUsec"),
        "text": text,
        "attachment_type": [key for key in (attachment or {}) if key.endswith("Renderer")],
        "image_urls": _image_urls(attachment),
        "video_ids": list(dict.fromkeys(video_ids)),
        "playlist_ids": list(dict.fromkeys(playlist_ids)),
        "downloaded_images": [],
    }


def download_image(url, target):
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not any(host == domain or host.endswith("." + domain)
                                            for domain in ("ytimg.com", "googleusercontent.com", "ggpht.com")):
        raise ValueError("image_url_host_not_allowed")
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read(25 * 1024 * 1024 + 1)
        if len(data) > 25 * 1024 * 1024:
            raise ValueError("image_too_large")
        content_type = response.headers.get_content_type()
    ext = mimetypes.guess_extension(content_type) or Path(parsed.path).suffix or ".bin"
    if ext == ".jpe":
        ext = ".jpg"
    target = target.with_suffix(ext)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as f:
        f.write(data)
    return {"path": str(target), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "content_type": content_type}


def collect(channel_url, cookie_file=None, max_posts=None):
    try:
        import yt_dlp
        from yt_dlp.extractor.youtube._tab import YoutubeTabIE
    except ImportError as exc:
        raise RuntimeError("yt_dlp_with_youtube_tab_extractor_required") from exc

    original = YoutubeTabIE._post_thread_entries

    def capture(self, thread):
        record = normalize_post(thread, channel_url)
        if record:
            yield {"_type": "url", "ie_key": "YoutubeTab", "id": record["post_id"],
                   "url": record["url"] or channel_url, "title": record["text"][:120],
                   "description": json.dumps(record, ensure_ascii=False)}

    YoutubeTabIE._post_thread_entries = capture
    options = {"quiet": True, "no_warnings": True, "skip_download": True, "extract_flat": "in_playlist",
               "cachedir": False, "playlistend": max_posts}
    if cookie_file:
        options["cookiefile"] = str(cookie_file)
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(channel_url.rstrip("/") + "/posts", download=False)
    finally:
        YoutubeTabIE._post_thread_entries = original
    entries = (info or {}).get("entries") or []
    records, seen = [], set()
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("description"):
            continue
        try:
            record = json.loads(entry["description"])
        except (TypeError, json.JSONDecodeError):
            continue
        if record["post_id"] not in seen:
            seen.add(record["post_id"])
            records.append(record)
    if not records:
        raise RuntimeError("posts_not_extracted_check_channel_url_access_and_ytdlp_compatibility")
    return records


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel-url", required=True, help="Explicit channel URL or handle URL")
    parser.add_argument("--output-dir", required=True, type=Path, help="New directory outside the code tree")
    parser.add_argument("--cookie-file", type=Path, help="Optional user-provided cookie export stored outside the repository")
    parser.add_argument("--max-posts", type=int, help="Upper limit for posts returned by the extractor")
    parser.add_argument("--download-images", action="store_true")
    args = parser.parse_args(argv)
    url = args.channel_url.rstrip("/")
    if not re.fullmatch(r"https://www\.youtube\.com/(?:@[^/]+|channel/UC[A-Za-z0-9_-]{22}|c/[^/]+|user/[^/]+)", url):
        parser.error("Use an explicit https://www.youtube.com channel URL or handle")
    if args.max_posts is not None and args.max_posts < 1:
        parser.error("--max-posts must be positive")
    cookie_arg = args.cookie_file.expanduser() if args.cookie_file else None
    if cookie_arg and cookie_arg.is_symlink():
        parser.error("Cookie path must not be a symlink")
    cookie = cookie_arg.resolve(strict=True) if cookie_arg else None
    if cookie and not cookie.is_file():
        parser.error("Cookie path must be a regular file")
    project_root = Path(__file__).resolve().parents[2]
    if cookie and cookie.is_relative_to(project_root):
        parser.error("Cookie file must remain outside the project directory")
    output = args.output_dir.expanduser().resolve()
    if output.exists():
        parser.error("Output directory already exists; choose a fresh path to preserve prior snapshots")
    output.mkdir(parents=True, mode=0o700)
    try:
        posts = collect(url, cookie, args.max_posts)
        if args.download_images:
            for post in posts:
                for index, image_url in enumerate(post["image_urls"], 1):
                    try:
                        saved = download_image(image_url, output / "media" / post["post_id"] / f"image_{index:02d}")
                        post["downloaded_images"].append(saved)
                    except Exception as exc:
                        post["downloaded_images"].append({"url": image_url, "error": type(exc).__name__})
        value = {"schema_version": 1, "channel_url": url, "collected_at": datetime.now(timezone.utc).isoformat(),
                 "collector": "yt-dlp YoutubeTabIE Posts renderer adapter", "post_count": len(posts), "posts": posts}
        data = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
        path = output / "posts.json"
        with path.open("xb") as stream:
            stream.write(data)
        print(json.dumps({"state": "collected", "post_count": len(posts), "path": str(path),
                          "sha256": hashlib.sha256(data).hexdigest()}, ensure_ascii=False))
        return 0
    except Exception as exc:
        marker = output / "COLLECTION_ERROR.json"
        marker.write_text(json.dumps({"state": "failed", "error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"state": "failed", "error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
