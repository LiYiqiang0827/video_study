#!/usr/bin/env python3
"""Collect validated local videos, original audio, and five subtitle artifacts.

No network, ASR, or source mutation. Existing destination edits are never replaced.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
sys.dont_write_bytecode = True
import tempfile
import unicodedata
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from tool_paths import data_root, external_path

BASE = data_root()
JOB = BASE / 'asr_batches/yuncong_202608_202609'
FINAL_FILES = ('transcript.srt', 'transcript.vtt', 'transcript.md',
               'transcript.json', 'AUTO_EXPORT_PROVENANCE.json')
COLLECTION_SUFFIXES = {
    'transcript.srt': '.srt', 'transcript.vtt': '.vtt',
    'transcript.md': '.md', 'transcript.json': '.json',
    'AUTO_EXPORT_PROVENANCE.json': '_来源记录.json',
    'audio_original.m4a': '.m4a',
}


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                     dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    os.replace(temporary, path)


def sha256(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError(f'Expected a regular file: {path}')
    before = path.stat()
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    after = path.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError(f'File changed during verification: {path}')
    return digest.hexdigest()


def directory_name(item):
    if item.get('upload_time_precision') != 'second':
        raise ValueError(f"Missing exact upload time: {item['video_id']}")
    instant = datetime.fromisoformat(item['upload_time_beijing'])
    if instant.tzinfo is None:
        raise ValueError('Upload time must include a timezone')
    prefix = instant.astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d_%H%M%S_')
    title = unicodedata.normalize('NFC', item['title'])
    title = re.sub(r'[\\/:\x00-\x1f\x7f]', '_', title)
    # Match the downloader's whitespace normalization for the shared stem.
    title = re.sub(r'\s+', ' ', title).strip().rstrip('.')
    if not title or title in ('.', '..'):
        raise ValueError('Video title has no usable characters')
    name = prefix + title
    # Leave room for the longest artifact suffix in a 255-byte filename.
    max_stem_bytes = 255 - len('_来源记录.json'.encode('utf-8'))
    if len(name.encode('utf-8')) > max_stem_bytes:
        suffix = '_' + item['video_id']
        available = max_stem_bytes - len((prefix + suffix).encode('utf-8'))
        title = title.encode('utf-8')[:available].decode('utf-8', errors='ignore')
        name = prefix + title + suffix
    return name


def load_items(job_root, extra_items_json=None, export_status=None, media_manifest=None):
    snapshot = job_root / 'state.json'
    if not snapshot.exists():
        snapshot = job_root / 'REMOTE_STATE.json'
    if snapshot.exists():
        remote = read_json(snapshot)['items']
    elif extra_items_json:
        # Standalone videos need only their explicit, verified item list.
        remote = {}
    else:
        raise ValueError('No batch state exists; supply an explicit --extra-items-json for standalone videos')
    if export_status is None:
        choices = [job_root / 'subtitle_exports/SUBTITLE_AUTO_STATUS.json',
                   job_root / 'SUBTITLE_AUTO_STATUS.json']
        export_status = next((p for p in choices if p.exists()), choices[0])
    exports = read_json(export_status)['items'] if remote else {}
    mirror_path = job_root / 'local_sync_status.json'
    mirror = read_json(mirror_path).get('attempts', {}) if mirror_path.exists() else {}
    media = {v['video_id']: v for v in read_json(media_manifest)['items']} if media_manifest else {}
    items = []
    for video_id, source in remote.items():
        exported = exports.get(video_id, {})
        if exported.get('state') != 'exported_auto':
            raise ValueError(f'Final automatic outputs are unavailable: {video_id}')
        production = external_path(exported['production_directory']) if exported.get('production_directory') else (
            job_root / exported['derivative_relative']).resolve()
        if not exported.get('production_directory') and not production.is_relative_to(job_root.resolve()):
            raise ValueError('Production path escapes the batch directory')
        record = mirror.get(source['attempt_directory'])
        if record is not None:
            if record.get('state') != 'synced':
                raise ValueError(f'Current attempt is not fully mirrored: {video_id}')
            attempt = (job_root / record['relative_directory']).resolve()
        else:
            attempt = Path(source['attempt_directory']).resolve()
        if not attempt.is_relative_to(job_root.resolve()):
            raise ValueError(f'Current local attempt path is outside the batch: {video_id}')
        if Path(exported['source_attempt_directory']).resolve() != attempt:
            raise ValueError(f'Subtitles and audio use different attempts: {video_id}')
        audio = attempt / 'audio_original.m4a'
        source = dict(source)
        if video_id in media:
            local_media = media[video_id]
            if any(local_media.get(key) != source[key] for key in ('video_sha256', 'bytes')):
                raise ValueError(f'Local/remote video identity mismatch: {video_id}')
            source['video_path'] = local_media['video_path']
        items.append({**source, 'video_id': video_id,
                      'production_directory': str(production),
                      'provenance_sha256': exported['provenance_sha256'],
                      'audio_path': str(audio),
                      'audio_sha256': source['artifact_hashes']['audio_original.m4a']})
    if extra_items_json:
        extra = read_json(extra_items_json)
        if not isinstance(extra, list):
            raise ValueError('Extra items must be an explicit JSON list')
        items.extend(extra)
    ids = set()
    names = set()
    for item in items:
        video_id = item['video_id']
        name = directory_name(item)
        normalized = unicodedata.normalize('NFC', name).casefold()
        if video_id in ids or normalized in names:
            raise ValueError(f'Duplicate video ID or directory: {video_id}')
        ids.add(video_id)
        names.add(normalized)
        item['collection_directory_name'] = name
    return sorted(items, key=lambda item: item['collection_directory_name'])


def validate_source(item):
    production = Path(item['production_directory'])
    provenance = read_json(production / 'AUTO_EXPORT_PROVENANCE.json')
    if provenance.get('video_id') != item['video_id']:
        raise ValueError('Production provenance video ID mismatch')
    sources = {name: production / name for name in FINAL_FILES}
    video = Path(item['video_path'])
    if video.suffix.lower() != '.mp4' or video.name in sources:
        raise ValueError('Expected a distinct source MP4')
    sources[video.name] = video
    hashes = dict(provenance['artifact_hashes'])
    hashes['AUTO_EXPORT_PROVENANCE.json'] = item.get('provenance_sha256') or sha256(
        production / 'AUTO_EXPORT_PROVENANCE.json')
    hashes[video.name] = item['video_sha256']
    audio = Path(item['audio_path'])
    if audio.suffix.lower() != '.m4a':
        raise ValueError('Expected original M4A audio')
    sources['audio_original.m4a'] = audio
    hashes['audio_original.m4a'] = item['audio_sha256']
    if item.get('audio_bytes') is not None and audio.stat().st_size != item['audio_bytes']:
        raise ValueError('Source audio size mismatch')
    if video.stat().st_size != item['bytes']:
        raise ValueError('Source video size mismatch')
    for name, source in sources.items():
        if name not in hashes or sha256(source) != hashes[name]:
            raise ValueError(f'Source artifact hash mismatch: {source}')
    return sources, {name: hashes[name] for name in sources}


def content_entries(directory):
    # Finder may naturally add its private display metadata while browsing.
    return [p for p in directory.iterdir() if not (
        p.name == '.DS_Store' and p.is_file() and not p.is_symlink())]


def verify_directory(directory, hashes, sources=None, allow_missing=()):
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError(f'Expected an independent directory: {directory}')
    actual_names = {path.name for path in content_entries(directory)}
    missing = set(hashes) - actual_names
    if actual_names - set(hashes) or missing - set(allow_missing):
        raise ValueError(f'Directory must contain the seven expected files: {directory}')
    for name, expected in hashes.items():
        if name in missing:
            continue
        target = directory / name
        if sha256(target) != expected:
            raise ValueError(f'Destination differs; preserving existing content: {target}')
        if sources and os.path.samefile(target, sources[name]):
            raise ValueError(f'Destination must not hardlink the source: {target}')


def copy_file(source, target):
    # macOS APFS clone: independent writable file, shared initial storage blocks.
    if sys.platform == 'darwin':
        cloned = subprocess.run(['/bin/cp', '-c', str(source), str(target)],
                                capture_output=True, text=True)
        if cloned.returncode == 0:
            return 'apfs_clone'
        if target.exists():
            target.unlink()  # Only this newly created private staging file.
    shutil.copy2(source, target)
    return 'copy'


def collection_file_plan(item, original_sources, original_hashes):
    stem = item['collection_directory_name']
    mapping = {}
    for original in original_sources:
        suffix = COLLECTION_SUFFIXES.get(original)
        if suffix is None:
            if Path(original).suffix.lower() != '.mp4':
                raise ValueError(f'Unrecognized collection artifact: {original}')
            suffix = '.mp4'
        mapping[original] = stem + suffix
    if len(set(mapping.values())) != len(mapping):
        raise ValueError('Collection filename collision')
    return ({mapping[name]: path for name, path in original_sources.items()},
            {mapping[name]: digest for name, digest in original_hashes.items()}, mapping)


def migrate_filenames(directory, sources, hashes, mapping):
    """Rename only verified legacy files; tolerate an interrupted local rename."""
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError(f'Expected an independent directory: {directory}')
    allowed = set(mapping) | set(hashes)
    unexpected = {p.name for p in content_entries(directory)} - allowed
    if unexpected:
        raise ValueError(f'Unexpected files preserved: {directory}: {sorted(unexpected)}')
    # Validate the whole directory before changing any names.
    for original, renamed in mapping.items():
        old, new = directory / original, directory / renamed
        existing = {p for p in (old, new) if p.exists() or p.is_symlink()}
        if not existing and original != 'audio_original.m4a':
            raise ValueError(f'Missing artifact: {directory / renamed}')
        for path in existing:
            if sha256(path) != hashes[renamed] or os.path.samefile(path, sources[renamed]):
                raise ValueError(f'Existing artifact differs; preserving it: {path}')
        if old != new and old.exists() and new.exists() and not os.path.samefile(old, new):
            raise ValueError(f'Ambiguous filename collision; preserving both: {new}')
    updated = False
    methods = {}
    for original, renamed in mapping.items():
        old, new = directory / original, directory / renamed
        if old != new and old.exists():
            if not new.exists():
                # Exclusive publication, followed immediately by removal of
                # the old name. No source file is linked or changed.
                os.link(old, new)
            old.unlink()
            updated = True
        elif not new.exists():
            # Compatibility with the original six-file collection layout.
            with tempfile.TemporaryDirectory(prefix='.yuncong-audio-',
                                             dir=directory.parent.parent) as temporary:
                staged = Path(temporary) / renamed
                methods[renamed] = copy_file(sources[renamed], staged)
                if sha256(staged) != hashes[renamed]:
                    raise ValueError('Staged audio hash mismatch')
                os.link(staged, new)
                staged.unlink()
            updated = True
    verify_directory(directory, hashes, sources)
    return ('filenames_updated' if updated else 'verified_existing'), methods


def collect_item(item, output_root):
    original_sources, original_hashes = validate_source(item)
    sources, hashes, filename_map = collection_file_plan(item, original_sources, original_hashes)
    destination = output_root / item['collection_directory_name']
    methods = {}
    if destination.exists() or destination.is_symlink():
        state, methods = migrate_filenames(destination, sources, hashes, filename_map)
    else:
        with tempfile.TemporaryDirectory(prefix='.yuncong-collection-',
                                         dir=output_root.parent) as temporary:
            staging = Path(temporary) / 'video'
            staging.mkdir()
            for name, source in sources.items():
                methods[name] = copy_file(source, staging / name)
            verify_directory(staging, hashes, sources)
            if destination.exists() or destination.is_symlink():
                raise ValueError(f'Destination appeared during copy: {destination}')
            staging.rename(destination)
        state = 'created'
    return {'video_id': item['video_id'], 'title': item['title'],
            'upload_time_beijing': item['upload_time_beijing'],
            'upload_time_precision': item['upload_time_precision'],
            'directory': str(destination), 'state': state,
            'common_filename_stem': item['collection_directory_name'],
            'filename_map': filename_map,
            'source_video_bytes': item['bytes'], 'copy_methods': methods,
            'files': {new: {'source': str(sources[new]), 'sha256': hashes[new],
                            'original_filename': original}
                      for original, new in filename_map.items()}}


def organize(job_root, output_root, extra_items_json=None, export_status=None, media_manifest=None):
    job_root = external_path(job_root)
    output_root = external_path(output_root)
    items = load_items(job_root, extra_items_json, export_status, media_manifest)
    output_root.parent.mkdir(parents=True, exist_ok=True)
    if output_root.is_symlink():
        raise ValueError('Collection root must not be a symlink')
    output_root.mkdir(exist_ok=True)
    manifest = {'schema_version': 2, 'output_root': str(output_root),
                'date_basis': 'actual YouTube upload time converted to Asia/Shanghai',
                'file_naming': 'YYYYMMDD_HHMMSS_title + extension; provenance uses _来源记录.json',
                'source_directories_preserved': True, 'expected_files_per_video': 7,
                'state': 'running', 'items': [], 'issues': [],
                'counts': {'total': len(items), 'completed': 0, 'issues': 0}}
    manifest_path = job_root / 'COLLECTION_LAYOUT.json'
    for item in items:
        try:
            manifest['items'].append(collect_item(item, output_root))
            manifest['counts']['completed'] += 1
        except Exception as error:
            manifest['issues'].append({'video_id': item['video_id'], 'error': str(error)})
            manifest['counts']['issues'] += 1
        manifest['updated_at'] = datetime.now(timezone.utc).isoformat()
        write_json(manifest_path, manifest)
    planned = {item['collection_directory_name'] for item in items}
    unexpected = [p.name for p in content_entries(output_root) if p.name not in planned]
    if unexpected:
        manifest['issues'].append({'unexpected_root_entries': unexpected})
        manifest['counts']['issues'] += 1
    manifest['state'] = 'complete' if not manifest['issues'] else 'issues'
    write_json(manifest_path, manifest)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job-root', type=Path, default=JOB)
    default_collection_root = Path(os.environ.get(
        'YUNCONG_VIDEO_COLLECTION_ROOT', str(Path.home() / '云聪视频全集')))
    parser.add_argument('--output-root', type=Path, default=default_collection_root)
    parser.add_argument('--extra-items-json', type=Path)
    parser.add_argument('--export-status', type=Path, help='Use a specific subtitle export status')
    parser.add_argument('--media-manifest', type=Path, help='Local verified media paths when remote paths differ')
    args = parser.parse_args()
    args.output_root = external_path(args.output_root)
    args.job_root = external_path(args.job_root)
    lock_path = args.output_root.parent / ('.' + args.output_root.name + '.lock')
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = organize(args.job_root, args.output_root, args.extra_items_json,
                          args.export_status, args.media_manifest)
    print(json.dumps({'state': result['state'], 'counts': result['counts'],
                      'output_root': result['output_root'], 'issues': result['issues']},
                     ensure_ascii=False))
    return 0 if result['state'] == 'complete' else 1


if __name__ == '__main__':
    raise SystemExit(main())
