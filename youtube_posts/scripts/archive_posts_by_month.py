#!/usr/bin/env python3
"""Merge a verified Posts snapshot into monthly JSON and media archives."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import calendar
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

from collect_posts import safe_component


def _month(post):
    raw = post.get("published_timestamp")
    if raw is None:
        return "未识别日期"
    try:
        micros = int(raw)
        return datetime.fromtimestamp(micros / 1_000_000, timezone.utc).astimezone(
            ZoneInfo("Asia/Shanghai")
        ).strftime("%Y-%m")
    except (ValueError, TypeError, OverflowError, OSError):
        return "未识别日期"


def _month_assignment(post, reference):
    exact = _month(post)
    if exact != "未识别日期":
        return exact, "exact"
    raw = str(post.get("published_time_raw") or "").strip().lower()
    match = re.match(r"^(\d+)\s+(minute|hour|day|week|month|year)s?\s+ago\b", raw)
    if not match:
        return "未识别日期", "unknown"
    try:
        anchor = datetime.fromisoformat(reference.replace("Z", "+00:00"))
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=timezone.utc)
        anchor = anchor.astimezone(ZoneInfo("Asia/Shanghai"))
        amount, unit = int(match.group(1)), match.group(2)
        if unit in {"minute", "hour", "day", "week"}:
            days = {"minute": 0, "hour": 0, "day": 1, "week": 7}[unit] * amount
            hours = amount if unit == "hour" else 0
            minutes = amount if unit == "minute" else 0
            estimated = anchor - timedelta(days=days, hours=hours, minutes=minutes)
        elif unit == "month":
            months_back = amount
            month_index = anchor.year * 12 + anchor.month - 1 - months_back
            year, month0 = divmod(month_index, 12)
            month = month0 + 1
            day = min(anchor.day, calendar.monthrange(year, month)[1])
            estimated = anchor.replace(year=year, month=month, day=day)
        else:
            return "未识别日期", "unknown"
        return estimated.strftime("%Y-%m"), "estimated_from_relative_display"
    except (ValueError, TypeError, OverflowError):
        return "未识别日期", "unknown"


def _relative_time_estimate(post, reference):
    """Return a transparent interval estimate for minute/hour/day/week labels."""
    if post.get("published_timestamp") is not None:
        return {}
    raw = str(post.get("published_time_raw") or "").strip().lower()
    match = re.match(r"^(\d+)\s+(minute|hour|day|week)s?\s+ago\b", raw)
    if not match:
        return {}
    try:
        anchor = datetime.fromisoformat(reference.replace("Z", "+00:00"))
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=timezone.utc)
        anchor = anchor.astimezone(ZoneInfo("Asia/Shanghai"))
        amount, unit = int(match.group(1)), match.group(2)
        duration = {
            "minute": timedelta(minutes=1),
            "hour": timedelta(hours=1),
            "day": timedelta(days=1),
            "week": timedelta(weeks=1),
        }[unit]
        # Relative labels are bucketed by YouTube. Preserve an interval rather
        # than presenting the label's lower edge as an exact post date.
        start = anchor - duration * (amount + 1)
        end = anchor - duration * amount
        return {
            "estimated_published_at_start": start.isoformat(timespec="minutes"),
            "estimated_published_at_end": end.isoformat(timespec="minutes"),
            "estimated_published_at_precision": f"relative_{unit}",
            "estimated_published_at_reference": reference,
            "estimated_published_at_note": "按页面相对时间区间和采集基准推算；不是精确发布时间。",
        }
    except (ValueError, TypeError, OverflowError):
        return {}


def _safe_source_file(snapshot, value):
    rel = PurePosixPath(value)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        raise ValueError("downloaded_image_path_invalid")
    source = snapshot.joinpath(*rel.parts).resolve()
    if not source.is_relative_to(snapshot.resolve()) or not source.is_file() or source.is_symlink():
        raise ValueError("downloaded_image_missing_or_unsafe")
    return source


def _atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _manifest(folder):
    files = []
    for path in sorted(p for p in folder.rglob("*") if p.is_file() and p.name != "MANIFEST.json"):
        data = path.read_bytes()
        files.append({"path": str(path.relative_to(folder)), "bytes": len(data),
                      "sha256": hashlib.sha256(data).hexdigest()})
    _atomic_json(folder / "MANIFEST.json", {"schema_version": 1, "files": files})


def refresh_manifests(archive_root: Path):
    updated = []
    for folder in sorted(p for p in archive_root.iterdir() if p.is_dir() and not p.is_symlink()):
        if (folder / "posts.json").is_file():
            _manifest(folder)
            updated.append(folder.name)
    return updated


def refresh_estimates(archive_root: Path):
    """Backfill relative-time estimate ranges using each archive's saved anchor."""
    updated = []
    estimate_keys = {
        "estimated_published_at_start", "estimated_published_at_end",
        "estimated_published_at_precision", "estimated_published_at_reference",
        "estimated_published_at_note",
    }
    for folder in sorted(p for p in archive_root.iterdir() if p.is_dir() and not p.is_symlink()):
        path = folder / "posts.json"
        if not path.is_file():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        reference = data.get("month_assignment_reference")
        if not reference:
            continue
        for post in data.get("posts", []):
            for key in estimate_keys:
                post.pop(key, None)
            post.update(_relative_time_estimate(post, reference))
        _atomic_json(path, data)
        _manifest(folder)
        updated.append(folder.name)
    return updated


def archive_snapshot(snapshot: Path, archive_root: Path):
    snapshot = snapshot.resolve(strict=True)
    archive_root.mkdir(parents=True, exist_ok=True)
    if archive_root.is_symlink():
        raise ValueError("archive_root_must_not_be_symlink")
    incoming = json.loads((snapshot / "posts.json").read_text(encoding="utf-8"))
    buckets = {}
    reference_time = incoming.get("collected_at") or incoming.get("updated_at") or datetime.now(timezone.utc).isoformat()
    for post in incoming.get("posts", []):
        month, precision = _month_assignment(post, reference_time)
        item = dict(post)
        item["archive_month_precision"] = precision
        if precision == "estimated_from_relative_display":
            item["month_assignment_reference"] = reference_time
        item.update(_relative_time_estimate(item, reference_time))
        buckets.setdefault(month, []).append(item)

    results = []
    for month, posts in sorted(buckets.items()):
        folder = archive_root / month
        if folder.is_symlink():
            raise ValueError("monthly_archive_must_not_be_symlink")
        folder.mkdir(parents=True, exist_ok=True)
        json_path = folder / "posts.json"
        previous = json.loads(json_path.read_text(encoding="utf-8")) if json_path.exists() else {
            "schema_version": 1, "archive_month": month, "channel_url": incoming.get("channel_url"),
            "posts": []
        }
        indexed = {str(item.get("post_id")): item for item in previous.get("posts", []) if item.get("post_id")}
        for source_post in posts:
            post = dict(source_post)
            post["downloaded_images"] = []
            post_key = safe_component(post.get("post_id", "post"))
            for index, image in enumerate(source_post.get("downloaded_images", []), 1):
                if image.get("error"):
                    post["downloaded_images"].append({"url": image.get("url"), "error": image["error"]})
                    continue
                try:
                    source = _safe_source_file(snapshot, image.get("path", ""))
                except ValueError:
                    if image.get("path"):
                        raise
                    continue
                relative = Path("media") / post_key / f"image_{index:02d}{source.suffix.lower()}"
                target = folder / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(target.name + ".tmp")
                shutil.copyfile(source, temporary)
                os.replace(temporary, target)
                data = target.read_bytes()
                post["downloaded_images"].append({
                    "path": relative.as_posix(), "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "content_type": image.get("content_type"),
                })
            indexed[str(post["post_id"])] = post

        merged = sorted(indexed.values(), key=lambda item: (
            int(item.get("published_timestamp") or 0), str(item.get("post_id", ""))))
        result = {"schema_version": 1, "archive_month": month,
                  "channel_url": incoming.get("channel_url"),
                  "updated_at": datetime.now(timezone.utc).isoformat(),
                  "month_assignment_reference": reference_time,
                  "post_count": len(merged), "posts": merged}
        _atomic_json(json_path, result)
        _manifest(folder)
        results.append({"month": month, "post_count": len(merged), "folder": str(folder)})

    return {"post_count": sum(len(items) for items in buckets.values()),
            "image_download_failures": incoming.get("image_download_failures", 0), "months": results}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--archive-root", required=True, type=Path)
    parser.add_argument("--refresh-manifests", action="store_true")
    parser.add_argument("--refresh-estimates", action="store_true")
    args = parser.parse_args()
    if args.refresh_estimates:
        result = {"updated_months": refresh_estimates(args.archive_root)}
    elif args.refresh_manifests:
        result = {"updated_months": refresh_manifests(args.archive_root)}
    elif args.snapshot:
        result = archive_snapshot(args.snapshot, args.archive_root)
    else:
        parser.error("provide --snapshot or --refresh-manifests")
    print(json.dumps(result, ensure_ascii=False))
