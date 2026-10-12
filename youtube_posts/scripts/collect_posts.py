#!/usr/bin/env python3
"""Archive posts from a specified YouTube channel Posts tab."""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import mimetypes
import re
import subprocess
import sys
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
            thumbnails = value.get("thumbnails")
            if image_context and isinstance(thumbnails, list):
                candidates = []
                for index, item in enumerate(thumbnails):
                    if not isinstance(item, dict) or not isinstance(item.get("url"), str):
                        continue
                    url = item["url"]
                    host = (urlparse(url).hostname or "").lower()
                    if url.startswith("https://") and any(
                        host == domain or host.endswith("." + domain)
                        for domain in ("ytimg.com", "googleusercontent.com", "ggpht.com")
                    ):
                        width, height = item.get("width", 0), item.get("height", 0)
                        area = width * height if isinstance(width, int) and isinstance(height, int) else 0
                        candidates.append((area, index, url))
                if candidates:
                    urls.append(max(candidates)[2])
            for key, child in value.items():
                in_image = image_context or key.lower() in {"image", "imagerenderer", "backstageimagerenderer", "multiimagerenderer"}
                if in_image and key == "thumbnails":
                    continue
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


def safe_component(value):
    value = re.sub(r"[^A-Za-z0-9_-]+", "_", str(value)).strip("._")
    return value[:100] or "post"


def download_image(url, target, proxy=None):
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not any(host == domain or host.endswith("." + domain)
                                            for domain in ("ytimg.com", "googleusercontent.com", "ggpht.com")):
        raise ValueError("image_url_host_not_allowed")
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".part")
    command = ["/usr/bin/curl", "--silent", "--show-error", "--fail", "--location", "--proto-redir", "=https",
               "--max-time", "30", "--max-filesize", str(25 * 1024 * 1024), "--retry", "2", "--output", str(temp),
               "--write-out", "%{content_type}"]
    if proxy:
        command.extend(["--proxy", proxy])
    command.append(url)
    try:
        result = subprocess.run(command, capture_output=True, timeout=40)
        if result.returncode:
            raise ValueError("image_download_failed")
        content_type = result.stdout.decode("ascii", errors="replace").split(";", 1)[0].strip() or "application/octet-stream"
        if not temp.is_file() or temp.stat().st_size > 25 * 1024 * 1024:
            raise ValueError("image_too_large_or_missing")
        data = temp.read_bytes()
    finally:
        temp.unlink(missing_ok=True)
    ext = mimetypes.guess_extension(content_type) or Path(parsed.path).suffix or ".bin"
    if ext == ".jpe":
        ext = ".jpg"
    target = target.with_suffix(ext)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as f:
        f.write(data)
    return {"path": str(target), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "content_type": content_type}


def collect(channel_url, cookie_file=None, max_posts=None, proxy=None, impersonate_chrome=False):
    try:
        import yt_dlp
        from yt_dlp.extractor.youtube._tab import YoutubeTabIE
    except ImportError as exc:
        raise RuntimeError("yt_dlp_with_youtube_tab_extractor_required") from exc

    original = YoutubeTabIE._post_thread_entries

    def capture(self, thread):
        record = normalize_post(thread, channel_url)
        if record:
            yield record

    YoutubeTabIE._post_thread_entries = capture
    options = {"quiet": True, "no_warnings": True, "skip_download": True, "cachedir": False,
               "retries": 2, "extractor_retries": 2,
               "tls_connection_retries": 2, "socket_timeout": 30}
    if cookie_file:
        options["cookiefile"] = str(cookie_file)
    if proxy:
        options["proxy"] = proxy
    if impersonate_chrome:
        from yt_dlp.networking.impersonate import ImpersonateTarget
        options["impersonate"] = ImpersonateTarget.from_str("chrome")
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = YoutubeTabIE(ydl).extract(channel_url.rstrip("/") + "/posts")
            iterator = (info or {}).get("entries") or []
            entries = list(itertools.islice(iterator, max_posts)) if max_posts is not None else list(iterator)
    finally:
        YoutubeTabIE._post_thread_entries = original
    records, seen = [], set()
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("post_id"):
            continue
        if entry["post_id"] not in seen:
            seen.add(entry["post_id"])
            records.append(entry)
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
    parser.add_argument("--proxy", help="Existing proxy on the execution machine")
    parser.add_argument("--impersonate-chrome", action="store_true")
    args = parser.parse_args(argv)
    url = args.channel_url.rstrip("/")
    if not re.fullmatch(r"https://www\.youtube\.com/(?:@[^/]+|channel/UC[A-Za-z0-9_-]{22}|c/[^/]+|user/[^/]+)", url):
        parser.error("Use an explicit https://www.youtube.com channel URL or handle")
    if args.max_posts is not None and args.max_posts < 1:
        parser.error("--max-posts must be positive")
    if args.proxy:
        proxy = urlparse(args.proxy)
        if proxy.scheme not in {"http", "https", "socks5", "socks5h"} or not proxy.hostname or proxy.username or proxy.password:
            parser.error("Proxy must be an existing URL without embedded credentials")
    cookie_arg = args.cookie_file.expanduser() if args.cookie_file else None
    if cookie_arg and cookie_arg.is_symlink():
        parser.error("Cookie path must not be a symlink")
    cookie = cookie_arg.resolve(strict=True) if cookie_arg else None
    if cookie and not cookie.is_file():
        parser.error("Cookie path must be a regular file")
    output = args.output_dir.expanduser().resolve()
    if output.exists():
        parser.error("Output directory already exists; choose a fresh path to preserve prior snapshots")
    output.mkdir(parents=True, mode=0o700)
    try:
        posts = collect(url, cookie, args.max_posts, args.proxy, args.impersonate_chrome)
        if args.download_images:
            for post in posts:
                for index, image_url in enumerate(post["image_urls"], 1):
                    try:
                        saved = download_image(image_url, output / "media" / safe_component(post["post_id"]) / f"image_{index:02d}", args.proxy)
                        saved["path"] = str(Path(saved["path"]).relative_to(output))
                        post["downloaded_images"].append(saved)
                    except Exception as exc:
                        post["downloaded_images"].append({"url": image_url, "error": type(exc).__name__})
        image_failures = sum(1 for post in posts for image in post["downloaded_images"] if image.get("error"))
        value = {"schema_version": 1, "channel_url": url, "collected_at": datetime.now(timezone.utc).isoformat(),
                 "collector": "yt-dlp YoutubeTabIE Posts renderer adapter", "post_count": len(posts),
                 "image_download_failures": image_failures, "posts": posts}
        data = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
        path = output / "posts.json"
        with path.open("xb") as stream:
            stream.write(data)
        files = []
        for item in sorted(p for p in output.rglob("*") if p.is_file()):
            files.append({"path": str(item.relative_to(output)), "bytes": item.stat().st_size,
                          "sha256": hashlib.sha256(item.read_bytes()).hexdigest()})
        (output / "MANIFEST.json").write_text(json.dumps({"schema_version": 1, "files": files}, indent=2), encoding="utf-8")
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
