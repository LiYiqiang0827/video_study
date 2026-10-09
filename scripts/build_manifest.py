#!/usr/bin/env python3
"""Build a frozen ASR input list from completed local download results."""
from __future__ import annotations
import argparse
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
import sys
sys.dont_write_bytecode = True
from zoneinfo import ZoneInfo
from tool_paths import external_path


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def manifest_item(result_path):
    result_path = Path(result_path).expanduser().resolve(strict=True)
    result = json.loads(result_path.read_text(encoding='utf-8'))
    if result.get('status') != 'technical_checks_passed':
        raise ValueError(f'Download is not complete and verified: {result_path}')
    ident = result.get('video_id', '')
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', ident):
        raise ValueError('A valid video ID is required')
    candidates = []
    if result.get('file'):
        declared = Path(result['file'])
        candidates.extend([declared if declared.is_absolute() else result_path.parent / declared,
                           result_path.parent / declared.name])
    if result.get('canonical_filename'):
        filename = result['canonical_filename']
        if Path(filename).name != filename:
            raise ValueError('Invalid canonical filename')
        candidates.append(result_path.parent / filename)
    candidates = list(dict.fromkeys(p.resolve() for p in candidates if p.is_file() and not p.is_symlink()))
    video = next((p for p in candidates if p.suffix.lower() == '.mp4'
                  and p.stat().st_size == result.get('bytes') and sha256(p) == result.get('sha256')), None)
    if video is None:
        raise ValueError(f'No local MP4 matches the completed download: {ident}')
    if result.get('upload_time_precision') != 'second':
        raise ValueError(f'Exact upload time is unavailable: {ident}')
    instant = datetime.fromisoformat(result['upload_time_local'])
    if instant.tzinfo is None:
        raise ValueError('Upload time requires a timezone')
    if result.get('timestamp') is not None and abs(instant.timestamp() - result['timestamp']) > .01:
        raise ValueError('Upload timestamp and formatted time disagree')
    instant = instant.astimezone(ZoneInfo('Asia/Shanghai'))
    duration = result.get('local_duration_seconds')
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
        raise ValueError('Verified video duration is required')
    title = result.get('title')
    if not isinstance(title, str) or not title.strip():
        raise ValueError('Original video title is required')
    return {'video_id': ident, 'video_path': str(video), 'video_sha256': result['sha256'],
            'bytes': result['bytes'], 'duration_seconds': duration, 'title': title,
            'upload_date': instant.strftime('%Y%m%d'), 'upload_time_beijing': instant.isoformat(),
            'upload_time_precision': 'second', 'download_result': str(result_path),
            'download_result_sha256': sha256(result_path)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--result', type=Path, action='append', help='Completed result.json; repeat for several videos')
    parser.add_argument('--download-root', type=Path, help='Discover completed results only under this local download directory')
    parser.add_argument('--output', type=Path, required=True, help='Frozen JSON manifest outside the code project')
    args = parser.parse_args()
    results = list(args.result or [])
    if args.download_root:
        for p in sorted(args.download_root.expanduser().resolve(strict=True).rglob('result.json')):
            try:
                if json.loads(p.read_text()).get('status') == 'technical_checks_passed':
                    results.append(p)
            except (OSError, ValueError):
                continue
    results = list(dict.fromkeys(p.expanduser().resolve(strict=True) for p in results))
    if not results:
        parser.error('Supply at least one complete --result or --download-root')
    items = [manifest_item(p) for p in results]
    if len({v['video_id'] for v in items}) != len(items):
        raise ValueError('Duplicate video IDs; select the completed result for each video explicitly')
    items.sort(key=lambda v: v['upload_time_beijing'])
    manifest = {'schema_version': 1, 'name': args.output.stem,
                'total_duration_seconds': sum(v['duration_seconds'] for v in items), 'items': items}
    output = external_path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(manifest, ensure_ascii=False, indent=2) + '\n'
    if output.exists():
        if output.read_text() != text:
            raise ValueError('Existing manifest is different; use a new output filename')
        state = 'verified_existing'
    else:
        with output.open('x', encoding='utf-8') as stream:
            stream.write(text)
        state = 'created'
    print(json.dumps({'state': state, 'count': len(items), 'manifest': str(output)}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
