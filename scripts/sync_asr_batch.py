#!/usr/bin/env python3
"""Mirror closed local-ASR attempts from Mac mini, preserving local changes.

The remote state and attempt files are read-only. No credentials or media URLs
are consumed. Running attempts are never copied; declared SHA256s are checked
after rsync --ignore-existing. Processed remains pending listening review.
"""
from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, timezone
import fcntl
import hashlib
import html
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import sys
sys.dont_write_bytecode = True
import tempfile
import time
from typing import Any, Callable
from urllib.parse import quote
from tool_paths import data_root, external_path


DEFAULT_JOB_ROOT = data_root() / 'asr_batches/yuncong_202608_202609'
HOST = 'macmini'
CLOSED = {'processed_pending_review', 'partial_needs_review', 'failed'}
TERMINAL = {'paused', 'paused_low_disk', 'queued_remaining', 'finished_needs_review'}
SHA = re.compile(r'[0-9a-f]{64}')
ID = re.compile(r'[A-Za-z0-9_-]{11}')
LABELS = {
    'processed_pending_review': '处理完成 · 待听音验收',
    'partial_needs_review': '部分结果 · 待复核', 'failed': '处理失败',
    'running': '处理中', 'queued': '排队中', 'pending': '排队中',
}


class SyncError(Exception):
    """Short, fixed diagnostic codes; raw SSH stderr is not user-facing."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def relative_remote_path(value: str, remote_root: Path | None = None) -> Path:
    remote_root = remote_root or DEFAULT_JOB_ROOT
    if not isinstance(value, str) or not value or any(c in value for c in '\x00\r\n\\'):
        raise SyncError('remote_path_invalid')
    parsed = PurePosixPath(value)
    if '..' in parsed.parts:
        raise SyncError('remote_path_escape')
    if parsed.is_absolute():
        try:
            parsed = parsed.relative_to(PurePosixPath(str(remote_root)))
        except ValueError:
            raise SyncError('remote_path_escape') from None
    if not parsed.parts or parsed == PurePosixPath('.'):
        raise SyncError('remote_path_is_root')
    return Path(*parsed.parts)


def local_path(root: Path, relative: Path, *, create_parent: bool = False) -> Path:
    """Reject traversal and any existing symlink inside the selected root."""
    if root.is_symlink() or relative.is_absolute() or '..' in relative.parts:
        raise SyncError('local_path_escape')
    root = root.resolve()
    destination = root / relative
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise SyncError('local_symlink_rejected')
    if not destination.resolve().is_relative_to(root):
        raise SyncError('local_path_escape')
    if create_parent:
        destination.parent.mkdir(parents=True, exist_ok=True)
    return destination


def atomic_json(path: Path, value: Any) -> None:
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')


def atomic_text(path: Path, value: str) -> None:
    if path.is_symlink():
        raise SyncError('local_symlink_rejected')
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.asr-mirror-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def read_snapshot() -> dict[str, Any]:
    """Read only the fixed batch's state.json; suppress raw remote failures."""
    source = (
        "import pathlib,sys; p=pathlib.Path(" + repr(str(DEFAULT_JOB_ROOT / 'state.json')) + "); "
        "b=p.read_bytes(); "
        "sys.exit(3) if len(b)>16777216 else sys.stdout.buffer.write(b)"
    )
    command = 'python3 -c ' + shlex.quote(source)
    try:
        result = subprocess.run(
            ['/usr/bin/ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', HOST, command],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=40, check=False,
        )
        if result.returncode or len(result.stdout) > 16777216:
            raise SyncError('remote_state_unavailable')
        state = json.loads(result.stdout)
        if not isinstance(state, dict) or not isinstance(state.get('items'), dict):
            raise SyncError('remote_state_invalid')
        return state
    except SyncError:
        raise
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired):
        raise SyncError('remote_state_unavailable') from None


def copy_attempt(remote_directory: str, destination: Path) -> None:
    """Retain local files and resumable rsync partials; never follow unsafe links."""
    destination.mkdir(parents=True, exist_ok=True)
    source = HOST + ':' + shlex.quote(remote_directory.rstrip('/') + '/')
    command = ['/usr/bin/rsync', '-a', '--ignore-existing', '--safe-links',
               '--partial-dir=.rsync-partial', '-e',
               '/usr/bin/ssh -o BatchMode=yes -o ConnectTimeout=10', source, str(destination) + '/']
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=1800, check=False)
        if result.returncode:
            raise SyncError('rsync_incomplete')
    except SyncError:
        raise
    except (OSError, subprocess.TimeoutExpired):
        raise SyncError('rsync_incomplete') from None


def item_state(item: dict[str, Any]) -> str:
    return str(item.get('state', item.get('status', 'unknown')))


def issue(code: str, **details: Any) -> dict[str, Any]:
    return {'code': code, **details}


def verify_attempt(root: Path, relative: Path, declared: Any, previous: dict[str, Any] | None = None) -> tuple[dict[str, str], list[dict[str, Any]]]:
    verified, problems = {}, []
    if not isinstance(declared, dict) or not declared:
        return verified, [issue('artifact_hashes_missing')]
    prior_hashes = (previous or {}).get('expected_hashes', {})
    for name, expected in declared.items():
        try:
            if not isinstance(name, str) or not isinstance(expected, str) or not SHA.fullmatch(expected):
                raise SyncError('artifact_hash_invalid')
            artifact_relative = relative_remote_path(name)
            if PurePosixPath(name).is_absolute():
                raise SyncError('artifact_path_not_relative')
            if name in prior_hashes and prior_hashes[name] != expected:
                problems.append(issue('remote_artifact_hash_changed', file=name))
                continue
            artifact = local_path(root, relative / artifact_relative)
            if not artifact.is_file():
                problems.append(issue('artifact_missing', file=name))
            elif sha256_file(artifact) != expected:
                problems.append(issue('local_sha_changed_preserved', file=name))
            else:
                verified[name] = expected
        except (SyncError, OSError) as error:
            problems.append(issue(str(error) if isinstance(error, SyncError) else 'artifact_unreadable', file=str(name)))
    return verified, problems


def read_local_status(root: Path) -> dict[str, Any]:
    try:
        path = local_path(root, Path('local_sync_status.json'))
        result = json.loads(path.read_text(encoding='utf-8'))
        return result if isinstance(result, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, SyncError):
        raise SyncError('local_status_invalid') from None


def _closed_attempts(items: dict[str, Any]):
    """Include frozen historical diagnostics, and skip all active attempts."""
    for ident, item in items.items():
        if not isinstance(ident, str) or not ID.fullmatch(ident) or not isinstance(item, dict):
            yield str(ident), {}, 'invalid_item'
            continue
        history = item.get('attempt_history', [])
        if not isinstance(history, list):
            yield ident, {}, 'invalid_attempt_history'
            history = []
        for attempt in history + [item]:
            if not isinstance(attempt, dict):
                yield ident, {}, 'invalid_attempt'
            elif item_state(attempt) in CLOSED:
                yield ident, attempt, None


def _index_link(root: Path, attempt: dict[str, Any], record: dict[str, Any], kind: str) -> str | None:
    verified = record.get('verified_hashes', {})
    if kind == 'review':
        directory = attempt.get('review_directory')
        if not directory:
            return None
        try:
            relative = relative_remote_path(directory) / 'review.html'
            attempt_relative = Path(record['relative_directory'])
            name = relative.relative_to(attempt_relative).as_posix()
            if name not in verified:
                return None
        except (SyncError, ValueError, KeyError):
            return None
    else:
        candidates = [name for name in verified if (
            kind == 'text' and name.endswith(('.md', '.txt')) and 'transcript' in name
            or kind == 'text' and name.endswith('raw_text.txt')
            or kind == 'audio' and name.endswith(('sample.wav', 'audio_asr_16k.wav', 'audio_original.m4a'))
        )]
        if not candidates:
            return None
        candidates.sort(key=lambda name: ('aggregated.provisional' not in name, 'transcript' not in name, len(name), name))
        relative = Path(record['relative_directory']) / candidates[0]
    try:
        if not local_path(root, relative).is_file():
            return None
    except SyncError:
        return None
    return quote(relative.as_posix(), safe='/')


def render_index(root: Path, snapshot: dict[str, Any], status: dict[str, Any]) -> str:
    delivery_note = '所有转写均为模型初稿，需听音验收。处理完成、自动校验和人工验收是不同状态；聚合字幕不具备逐字时间精度。活动任务只显示进度，不镜像半成品。'
    revised_link = ''
    if (root / 'SUBTITLE_AUTO_INDEX.html').is_file():
        revised_link = '<p><a href="SUBTITLE_AUTO_INDEX.html">查看自动生成字幕</a></p>'
        delivery_note = '自动字幕可直接使用；短口头语可能采用近似显示时间。原始转写记录保留，模型识别准确率未经人工计量。'
    rows = []
    for ident, item in snapshot['items'].items():
        if not isinstance(item, dict):
            continue
        remote_state = item_state(item)
        record = status['attempts'].get(str(item.get('attempt_directory')), {})
        sync_label = '本机已同步' if record.get('state') == 'synced' else ('同步有待处理项' if record else '尚未同步')
        links = []
        for kind, label in [('text', '完整正文'), ('review', '字幕试听'), ('audio', '音频')]:
            href = _index_link(root, item, record, kind)
            if href:
                links.append('<a href="' + html.escape(href, quote=True) + '">' + label + '</a>')
        date = item.get('upload_date', item.get('upload_time', item.get('date', '未知')))
        title = item.get('title', ident)
        duration = item.get('duration_seconds', item.get('expected_duration_seconds', item.get('duration', '未知')))
        try:
            duration = f'{float(duration) / 60:.1f} 分钟'
        except (ValueError, TypeError, OverflowError):
            duration = '未知'
        cells = [date, title, duration, LABELS.get(remote_state, remote_state), sync_label]
        rows.append('<tr>' + ''.join('<td>' + html.escape(str(value)) + '</td>' for value in cells) + '<td>' + ' · '.join(links) + '</td></tr>')
    counts = status['counts']
    info = '；'.join(f'{html.escape(key)}：{value}' for key, value in counts.items())
    return (
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
        '<title>云聪视频 · 本地转写批次</title><style>body{font-family:system-ui,sans-serif;margin:28px;line-height:1.6;color:#253044}table{border-collapse:collapse;width:100%}th,td{padding:12px;border-bottom:1px solid #dde2e9;text-align:left}a{color:#176aaa}p{max-width:1100px}.note{background:#fff4dc;padding:14px}td:nth-child(2){max-width:420px}</style>'
        '<h1>云聪视频 · 本地转写批次</h1>' + revised_link + '<p class="note">' + delivery_note + '</p>'
        '<p>远端批次：' + html.escape(str(snapshot.get('state', snapshot.get('batch_state', 'unknown')))) + '；更新时间：' + html.escape(status['updated_at']) + '</p><p>' + info + '</p>'
        '<table><thead><tr><th>日期</th><th>标题</th><th>时长</th><th>处理状态</th><th>同步状态</th><th>查看</th></tr></thead><tbody>' + ''.join(rows) + '</tbody></table></html>\n'
    )


def mirror_once(root: Path, snapshot: dict[str, Any], *, copier: Callable[[str, Path], None] = copy_attempt) -> dict[str, Any]:
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get('items'), dict):
        raise SyncError('remote_state_invalid')
    external_path(root)
    if root.is_symlink():
        raise SyncError('local_symlink_rejected')
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve()
    prior = read_local_status(root)
    records = dict(prior.get('attempts', {}))
    problems = []
    atomic_json(local_path(root, Path('REMOTE_STATE.json')), snapshot)
    for ident, attempt, invalid in _closed_attempts(snapshot['items']):
        if invalid:
            problems.append(issue(invalid, video_id=ident))
            continue
        directory = attempt.get('attempt_directory')
        finished_at = attempt.get('attempt_finished_at')
        if not isinstance(finished_at, str) or not finished_at:
            problems.append(issue('attempt_not_closed', video_id=ident))
            continue
        try:
            relative = relative_remote_path(directory)
            destination = local_path(root, relative, create_parent=True)
            if attempt.get('review_directory'):
                review = relative_remote_path(attempt['review_directory'])
                if not review.is_relative_to(relative):
                    raise SyncError('review_path_escape')
            previous = records.get(directory)
            copy_problem = None
            try:
                copier(str(DEFAULT_JOB_ROOT / relative), destination)
            except (SyncError, OSError) as error:
                copy_problem = issue(str(error) if isinstance(error, SyncError) else 'rsync_incomplete')
            verified, artifact_problems = verify_attempt(root, relative, attempt.get('artifact_hashes'), previous)
            attempt_problems = ([copy_problem] if copy_problem else []) + artifact_problems
            records[directory] = {
                'video_id': ident, 'remote_state': item_state(attempt), 'state': 'issues' if attempt_problems else 'synced',
                'relative_directory': relative.as_posix(), 'attempt_finished_at': finished_at,
                'expected_hashes': (previous or {}).get('expected_hashes') or attempt.get('artifact_hashes', {}),
                'verified_hashes': verified, 'issues': attempt_problems, 'checked_at': now(),
            }
            problems.extend({**problem, 'video_id': ident, 'attempt_directory': directory} for problem in attempt_problems)
        except (SyncError, OSError, ValueError, TypeError) as error:
            problems.append(issue(str(error) if isinstance(error, SyncError) else 'attempt_invalid', video_id=ident))
    current = [item for item in snapshot['items'].values() if isinstance(item, dict)]
    counts = {
        'total': len(snapshot['items']),
        'processed': sum(item_state(item) == 'processed_pending_review' for item in current),
        'partial': sum(item_state(item) == 'partial_needs_review' for item in current),
        'failed': sum(item_state(item) == 'failed' for item in current),
        'running': sum(item_state(item) == 'running' for item in current),
        'local_synced': sum(records.get(str(item.get('attempt_directory')), {}).get('state') == 'synced' and item_state(item) in CLOSED for item in current),
        'synced_attempts': sum(record.get('state') == 'synced' for record in records.values()),
        'issues': len(problems),
    }
    status = {'schema_version': 1, 'updated_at': now(), 'state': 'issues' if problems else 'synced_snapshot',
              'remote_batch_state': snapshot.get('state', snapshot.get('batch_state', 'unknown')),
              'counts': counts, 'attempts': records, 'issues': problems,
              'review_note': 'Model drafts only; processed and mirrored do not mean human accepted.'}
    atomic_json(local_path(root, Path('local_sync_status.json')), status)
    atomic_text(local_path(root, Path('INDEX.html')), render_index(root, snapshot, status))
    return status


def main(argv: list[str] | None = None) -> int:
    global DEFAULT_JOB_ROOT, HOST
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job-root', type=Path, default=DEFAULT_JOB_ROOT, help='Local mirror destination')
    parser.add_argument('--remote-root', type=Path, default=DEFAULT_JOB_ROOT, help='Absolute remote batch directory')
    parser.add_argument('--host', default=HOST, help='Configured SSH host alias, e.g. macmini')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--interval', type=float, default=60)
    args = parser.parse_args(argv)
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', args.host) or args.host.startswith('-'):
        parser.error('Use a configured SSH host alias')
    if not args.remote_root.is_absolute():
        parser.error('--remote-root must be absolute')
    HOST = args.host
    DEFAULT_JOB_ROOT = args.remote_root
    if args.interval < 1 or args.interval > 3600:
        parser.error('interval must be between 1 and 3600 seconds')
    root = external_path(args.job_root)
    if root.is_symlink():
        print('local_symlink_rejected', file=sys.stderr)
        return 1
    root.mkdir(parents=True, exist_ok=True)
    with local_path(root, Path('.mirror.lock')).open('a+', encoding='utf-8') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('mirror_already_running', file=sys.stderr)
            return 3
        while True:
            try:
                snapshot = read_snapshot()
                status = mirror_once(root, snapshot)
                print(json.dumps({'updated_at': status['updated_at'], 'remote_batch_state': status['remote_batch_state'], 'counts': status['counts']}, ensure_ascii=False), flush=True)
                if args.once or status['remote_batch_state'] in TERMINAL:
                    return 2 if status['issues'] else 0
            except (SyncError, OSError, ValueError, TypeError) as error:
                code = str(error) if isinstance(error, SyncError) else 'mirror_cycle_failed'
                previous = read_local_status(root)
                previous.update({'updated_at': now(), 'state': 'sync_error', 'last_sync_error': code})
                atomic_json(local_path(root, Path('local_sync_status.json')), previous)
                print(code, file=sys.stderr, flush=True)
                if args.once:
                    return 1
            time.sleep(args.interval)


if __name__ == '__main__':
    raise SystemExit(main())
