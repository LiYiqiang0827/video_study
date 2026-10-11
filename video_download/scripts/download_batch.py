#!/usr/bin/env python3
"""Freeze and resume explicitly selected videos, with private bounded batch state."""
import argparse
import contextlib
import csv
import fcntl
import hashlib
import io
import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

SCHEMA = 1
ITEM_KEYS = {'url', 'video_id', 'expected_duration_seconds'}
ITEM_STATES = {'pending', 'active', 'interrupted', 'complete', 'failed', 'review'}
CATEGORIES = {
    'network_timeout', 'network_connection', 'dns', 'tls_certificate', 'tls_connection',
    'login_or_verification', 'membership_required', 'unavailable_or_private', 'http_403',
    'http_429', 'po_token', 'javascript_challenge', 'format_unavailable', 'fragment_skipped',
}
BLOCK_CATEGORIES = {'login_or_verification', 'membership_required', 'http_429'}
TRANSPORT_CATEGORIES = {'network_timeout', 'network_connection', 'dns', 'tls_connection'}
BLOCK_STATUSES = {
    'missing_dependency', 'missing_python_dependencies', 'invalid_cookie_export',
    'cookie_export_missing_or_symlink', 'nonempty_youtube_only_export_required',
    'existing_proxy_without_embedded_credentials_required', 'job_locked',
}
REVIEW_STATUSES = {
    'duration_requires_review', 'stream_timing_requires_review', 'duration_reference_missing',
    'sample_checks_passed_full_decode_pending', 'hash_mismatch', 'job_metadata_missing', 'job_media_missing',
}
WORKER_STATUSES = BLOCK_STATUSES | REVIEW_STATUSES | {
    'started', 'technical_checks_passed', 'interrupted', 'operation_failed',
    'local_operation_failed', 'unexpected_video_id', 'live_or_scheduled_video_not_supported',
    'merged_media_not_found', 'fragment_skipped_not_acceptable', 'media_file_missing_or_symlink',
    'invalid_media_duration', 'audio_or_video_track_missing', 'ffprobe_failed', 'decode_failed',
    'frame_extraction_failed', 'job_directory_invalid', 'job_locked', 'job_identity_mismatch',
    'job_state_invalid', 'job_identity_missing', 'job_directory_download_only',
    'metadata_accessible_media_not_downloaded', 'access_or_download_failed',
}


class BatchError(Exception):
    def __init__(self, status):
        self.status = status


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def emit(record):
    print(json.dumps(record, ensure_ascii=False, allow_nan=False), flush=True)


def canonical_bytes(record):
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def atomic_json(path, record):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(canonical_bytes(record))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BatchError('duplicate_json_field')
        result[key] = value
    return result


def load_json(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
        raise BatchError('json_file_missing_symlink_or_too_large')
    try:
        return json.loads(path.read_text(encoding='utf-8-sig'), object_pairs_hook=reject_duplicate_keys,
                          parse_constant=lambda _: (_ for _ in ()).throw(BatchError('nonfinite_json_number')))
    except (UnicodeError, json.JSONDecodeError):
        raise BatchError('invalid_json_file') from None


def positive_number(value):
    try:
        number = float(value)
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError('A positive finite number is required') from None
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError('A positive finite number is required')
    return number


def nonnegative_number(value):
    try:
        number = float(value)
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError('A nonnegative finite number is required') from None
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError('A nonnegative finite number is required')
    return number


def positive_integer(value):
    try:
        number = int(value)
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError('A positive integer is required') from None
    if number < 1:
        raise argparse.ArgumentTypeError('A positive integer is required')
    return number


def parse_video(value):
    if not isinstance(value, str):
        raise BatchError('video_url_or_id_required')
    value = value.strip()
    if re.fullmatch(r'[A-Za-z0-9_-]{11}', value):
        ident = value
    else:
        try:
            parsed = urlparse(value)
            # Never carry embedded credentials, arbitrary query data or a fragment
            # into the frozen list or the worker command.
            if parsed.scheme != 'https' or parsed.username is not None or parsed.password is not None:
                raise BatchError('https_youtube_url_without_credentials_required')
            if parsed.port not in (None, 443):
                raise BatchError('standard_https_port_required')
            if parsed.hostname == 'youtu.be':
                ident = parsed.path.strip('/')
            elif parsed.hostname in {'youtube.com', 'www.youtube.com', 'm.youtube.com'}:
                if parsed.path == '/watch':
                    values = parse_qs(parsed.query).get('v', [])
                    if len(values) != 1:
                        raise BatchError('single_video_id_required')
                    ident = values[0]
                elif re.fullmatch(r'/live/[A-Za-z0-9_-]{11}/?', parsed.path):
                    ident = parsed.path.split('/')[2]
                else:
                    raise BatchError('single_video_id_required')
            else:
                raise BatchError('youtube_video_required')
        except ValueError:
            raise BatchError('invalid_video_url') from None
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', ident):
        raise BatchError('single_video_id_required')
    return ident, f'https://www.youtube.com/watch?v={ident}'


def expected_duration(value):
    if value is None or value == '':
        return None
    if isinstance(value, bool):
        raise BatchError('positive_expected_duration_required')
    try:
        number = float(value)
    except (ValueError, TypeError):
        raise BatchError('positive_expected_duration_required') from None
    if not math.isfinite(number) or number <= 0:
        raise BatchError('positive_expected_duration_required')
    return number


def normalize_item(value):
    if isinstance(value, str):
        ident, url = parse_video(value)
        return {'video_id': ident, 'url': url, 'expected_duration_seconds': None}
    if not isinstance(value, dict) or not value or set(value) - ITEM_KEYS:
        raise BatchError('unknown_or_invalid_video_fields')
    choices = [value[key] for key in ['url', 'video_id'] if value.get(key)]
    if not choices:
        raise BatchError('video_url_or_id_required')
    parsed = [parse_video(choice) for choice in choices]
    if len({ident for ident, _ in parsed}) != 1:
        raise BatchError('video_url_id_mismatch')
    ident, url = parsed[0]
    return {'video_id': ident, 'url': url,
            'expected_duration_seconds': expected_duration(value.get('expected_duration_seconds'))}


def load_list(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
        raise BatchError('input_list_missing_symlink_or_too_large')
    suffix = path.suffix.lower()
    if suffix == '.json':
        raw = load_json(path)
        if not isinstance(raw, list):
            raise BatchError('json_video_list_required')
    elif suffix == '.txt':
        raw = [line.strip() for line in path.read_text(encoding='utf-8-sig').splitlines()
               if line.strip() and not line.lstrip().startswith('#')]
    elif suffix == '.csv':
        with path.open(encoding='utf-8-sig', newline='') as stream:
            reader = csv.DictReader(stream)
            names = reader.fieldnames or []
            if not names or len(names) != len(set(names)) or set(names) - ITEM_KEYS:
                raise BatchError('unknown_or_duplicate_csv_fields')
            raw = list(reader)
    else:
        raise BatchError('json_txt_or_csv_list_required')
    frozen, indices = [], {}
    for value in raw:
        item = normalize_item(value)
        ident = item['video_id']
        if ident not in indices:
            indices[ident] = len(frozen)
            frozen.append(item)
            continue
        old = frozen[indices[ident]]
        a, b = old['expected_duration_seconds'], item['expected_duration_seconds']
        if a is not None and b is not None and a != b:
            raise BatchError('conflicting_expected_durations')
        if a is None and b is not None:
            old['expected_duration_seconds'] = b
    if not frozen:
        raise BatchError('nonempty_video_list_required')
    return frozen


@contextlib.contextmanager
def root_lock(root, batch_dir):
    path = root / '.download_batch.lock'
    if path.is_symlink():
        raise BatchError('batch_lock_symlink_not_allowed')
    flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BatchError('another_batch_is_running') from None
        owner = {'pid': os.getpid(), 'batch_directory': str(batch_dir), 'locked_at': now()}
        os.ftruncate(fd, 0)
        os.write(fd, canonical_bytes(owner))
        yield fd
    finally:
        os.close(fd)


def manifest_body(root, name, height, items):
    return {'schema_version': SCHEMA, 'root': str(root), 'name': name, 'height': height, 'items': items}


def summary(state):
    counts = {status: 0 for status in ITEM_STATES}
    for item in state['items'].values():
        counts[item['status']] += 1
    return {'schema_version': SCHEMA, 'batch_directory': state['batch_directory'],
            'batch_state': state['batch_state'], 'total': len(state['items']),
            'completed': counts['complete'], 'failed': counts['failed'], 'review': counts['review'],
            'pending': counts['pending'] + counts['interrupted'], 'interrupted': counts['interrupted'],
            'active': counts['active'], 'active_video_id': state.get('active_video_id'),
            'updated_at': state['updated_at'], 'created_at': state['created_at'],
            'run_started_at': state.get('run_started_at'), 'run_finished_at': state.get('run_finished_at'),
            'blocked_reason': state.get('blocked_reason'), 'pause_reason': state.get('pause_reason'),
            'run_newly_completed': state.get('run_newly_completed', 0),
            'run_skipped_completed': state.get('run_skipped_completed', 0),
            'run_skipped_failed_or_review': state.get('run_skipped_failed_or_review', 0),
            'worker_attempts': sum(i.get('worker_attempts', 0) for i in state['items'].values()),
            'download_attempts': sum(i.get('download_attempts', 0) for i in state['items'].values()),
            'assumed_download_attempts': sum(i.get('assumed_download_attempts', 0) for i in state['items'].values())}


def save_state(batch_dir, state):
    state['updated_at'] = now()
    atomic_json(batch_dir / 'state.json', state)
    atomic_json(batch_dir / 'status.json', summary(state))


def initialize(args):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', args.name):
        raise BatchError('ascii_batch_name_required')
    items = load_list(args.list)
    root = args.root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    batch_dir = root / 'batches' / args.name
    body = manifest_body(root, args.name, args.height, items)
    digest = hashlib.sha256(canonical_bytes(body)).hexdigest()
    with root_lock(root, batch_dir):
        if batch_dir.exists():
            manifest, state, _ = load_batch(batch_dir)
            if manifest['manifest_sha256'] != digest:
                raise BatchError('frozen_batch_list_or_configuration_changed')
            return {'status': 'batch_already_initialized', **summary(state)}
        batch_dir.mkdir(parents=True)
        (batch_dir / 'items').mkdir()
        created = now()
        manifest = {**body, 'manifest_sha256': digest, 'created_at': created}
        state = {'schema_version': SCHEMA, 'manifest_sha256': digest, 'batch_directory': str(batch_dir),
                 'created_at': created, 'updated_at': created, 'batch_state': 'ready',
                 'active_video_id': None, 'items': {item['video_id']: {
                     'status': 'pending', 'worker_attempts': 0, 'download_attempts': 0,
                     'accounted_worker_attempt': 0, 'assumed_download_attempts': 0,
                     'updated_at': created} for item in items}}
        atomic_json(batch_dir / 'manifest.json', manifest)
        save_state(batch_dir, state)
        return {'status': 'batch_initialized', **summary(state)}


def load_batch(directory):
    requested = Path(directory).expanduser().absolute()
    if requested.is_symlink():
        raise BatchError('batch_directory_symlink_not_allowed')
    batch_dir = requested.resolve()
    manifest = load_json(batch_dir / 'manifest.json')
    if not isinstance(manifest, dict) or set(manifest) != {
            'schema_version', 'root', 'name', 'height', 'items', 'manifest_sha256', 'created_at'}:
        raise BatchError('invalid_batch_manifest')
    if manifest['schema_version'] != SCHEMA or manifest['height'] not in [720, 1080]:
        raise BatchError('invalid_batch_manifest')
    name = manifest['name']
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', name):
        raise BatchError('invalid_batch_manifest')
    root = Path(manifest['root']).expanduser().resolve()
    if batch_dir != root / 'batches' / name or (batch_dir / 'items').is_symlink():
        raise BatchError('batch_directory_manifest_mismatch')
    raw_items = manifest['items']
    if not isinstance(raw_items, list) or not raw_items:
        raise BatchError('invalid_batch_manifest')
    items = [normalize_item(item) for item in raw_items]
    if items != raw_items or len({item['video_id'] for item in items}) != len(items):
        raise BatchError('invalid_or_changed_batch_items')
    digest = hashlib.sha256(canonical_bytes(manifest_body(root, name, manifest['height'], items))).hexdigest()
    if digest != manifest['manifest_sha256']:
        raise BatchError('batch_manifest_hash_mismatch')
    state = load_json(batch_dir / 'state.json')
    if (not isinstance(state, dict) or state.get('schema_version') != SCHEMA
            or state.get('manifest_sha256') != digest or state.get('batch_directory') != str(batch_dir)
            or not isinstance(state.get('items'), dict)
            or set(state['items']) != {item['video_id'] for item in items}):
        raise BatchError('invalid_batch_state')
    for item in state['items'].values():
        if (not isinstance(item, dict) or item.get('status') not in ITEM_STATES
                or any(not isinstance(item.get(key), int) or isinstance(item.get(key), bool) or item[key] < 0
                       for key in ['worker_attempts', 'download_attempts'])):
            raise BatchError('invalid_batch_item_state')
        for key in ['accounted_worker_attempt', 'assumed_download_attempts']:
            if key in item and (not isinstance(item[key], int) or isinstance(item[key], bool) or item[key] < 0):
                raise BatchError('invalid_batch_item_state')
        if item.get('accounted_worker_attempt', 0) > item['worker_attempts']:
            raise BatchError('invalid_batch_item_state')
    return manifest, state, root


def file_receipt_valid(record, job_dir, reference=None):
    """Recheck bytes and SHA, never accept a completed label or filename alone."""
    if not isinstance(record, dict) or record.get('status') != 'technical_checks_passed':
        return False
    if record.get('decode_check') != {'mode': 'full', 'status': 'passed'}:
        return False
    checks = record.get('duration_checks')
    if (not isinstance(checks, dict) or checks.get('streams_match_container') is not True
            or not (checks.get('listed_matches_actual') is True or checks.get('expected_matches_actual') is True)
            or checks.get('listed_matches_actual') is False or checks.get('expected_matches_actual') is False):
        return False
    byte_count, expected_hash, filename = record.get('bytes'), record.get('sha256'), record.get('file')
    if (not isinstance(byte_count, int) or isinstance(byte_count, bool) or byte_count <= 0
            or not isinstance(expected_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', expected_hash)
            or not isinstance(filename, str)):
        return False
    try:
        path = Path(filename)
        if (path.is_symlink() or not path.is_file() or path.resolve().parent != job_dir.resolve()
                or path.stat().st_size != byte_count):
            return False
        if reference is not None:
            actual = record.get('local_duration_seconds')
            if not isinstance(actual, (int, float)) or not math.isfinite(actual) or abs(actual - reference) > 2:
                return False
        with path.open('rb') as stream:
            actual_hash = hashlib.file_digest(stream, 'sha256').hexdigest()
        return actual_hash == expected_hash
    except (OSError, ValueError):
        return False


def safe_worker_record(record, ident, job_dir):
    if not isinstance(record, dict):
        return {'status': 'worker_result_missing_or_invalid', 'final_error_categories': []}
    raw_status = record.get('status')
    status = raw_status if isinstance(raw_status, str) and raw_status in WORKER_STATUSES else 'worker_result_unrecognized'
    result = {'status': status, 'video_id': ident}
    for key in ['diagnostic_categories', 'final_error_categories']:
        values = record.get(key, [])
        result[key] = sorted({v for v in values if isinstance(v, str) and v in CATEGORIES}) if isinstance(values, list) else []
    if isinstance(record.get('download_attempted'), bool):
        result['download_attempted'] = record['download_attempted']
    if isinstance(record.get('tls_retry_budget_exhausted'), bool):
        result['tls_retry_budget_exhausted'] = record['tls_retry_budget_exhausted']
    if isinstance(record.get('resume_mode'), str) and record['resume_mode'] in {'local_revalidation', 'download_or_resume'}:
        result['resume_mode'] = record['resume_mode']
    if record.get('video_id') not in (None, ident):
        result['status'] = 'unexpected_video_id'
    for key in ['bytes', 'local_duration_seconds']:
        value = record.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            result[key] = value
    value = record.get('sha256')
    if isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value):
        result['sha256'] = value
    value = record.get('file')
    if isinstance(value, str):
        try:
            path = Path(value)
            if path.is_absolute() and path.resolve().parent == job_dir.resolve():
                result['file'] = str(path)
        except (OSError, ValueError):
            pass
    decode = record.get('decode_check')
    if isinstance(decode, dict):
        result['decode_check'] = {key: value for key, value in decode.items()
                                  if isinstance(value, str) and ((key == 'mode' and value in {'full', 'sample'})
                                  or (key == 'status' and value in {'not_started', 'in_progress', 'interrupted', 'failed', 'passed'}))}
    checks = record.get('duration_checks')
    if isinstance(checks, dict):
        result['duration_checks'] = {key: checks.get(key) for key in
                                    ['listed_matches_actual', 'expected_matches_actual', 'streams_match_container']
                                    if checks.get(key) is None or isinstance(checks.get(key), bool)}
    return result


def read_worker_result(job_dir, captured, ident, minimum_mtime=None):
    # Prefer this invocation's final JSON over a previous result left behind
    # when the worker rejects a job before it can safely update that job.
    # Neither raw stdout nor stderr is printed or persisted.
    for line in reversed(captured.decode('utf-8', errors='replace').splitlines()):
        try:
            record = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(record, dict) and 'status' in record:
            return safe_worker_record(record, ident, job_dir)
    try:
        result_path = job_dir / 'result.json'
        if minimum_mtime is not None and result_path.stat().st_mtime < minimum_mtime:
            raise BatchError('stale_worker_result')
        return safe_worker_record(load_json(result_path), ident, job_dir)
    except (BatchError, OSError):
        pass
    return {'status': 'worker_result_missing_or_invalid', 'final_error_categories': []}


def proxy_valid(value):
    if value is None:
        return
    try:
        parsed = urlparse(value)
        if (parsed.scheme not in {'http', 'https', 'socks5', 'socks5h'} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment):
            raise BatchError('existing_proxy_without_credentials_required')
        parsed.port
    except ValueError:
        raise BatchError('existing_proxy_without_credentials_required') from None


def account_worker_attempt(saved, result):
    """Charge each persisted invocation at most once, including crash recovery."""
    number = saved['worker_attempts']
    if saved.get('accounted_worker_attempt', 0) >= number:
        return
    attempted = result.get('download_attempted')
    if attempted is True or not isinstance(attempted, bool):
        saved['download_attempts'] += 1
        if not isinstance(attempted, bool):
            saved['assumed_download_attempts'] = saved.get('assumed_download_attempts', 0) + 1
    saved['accounted_worker_attempt'] = number


def worker_command(args, manifest, item, job_dir):
    command = [sys.executable, str(Path(__file__).resolve().with_name('download_video.py')),
               '--root', manifest['root'], '--job-dir', str(job_dir), '--url', item['url'],
               '--height', str(manifest['height']), '--player-client', args.player_client,
               '--format-mode', args.format_mode, '--tls-connection-retries', str(args.tls_connection_retries),
               '--decode', 'full', '--frames']
    if item['expected_duration_seconds'] is not None:
        command.extend(['--expected-duration', str(item['expected_duration_seconds'])])
    if args.cookie_file is not None:
        command.extend(['--cookie-file', str(args.cookie_file.expanduser().absolute())])
    if args.proxy:
        command.extend(['--proxy', args.proxy])
    if args.impersonate_chrome:
        command.append('--impersonate-chrome')
    if getattr(args, 'native_media_network', False):
        command.append('--native-media-network')
    return command


def worker_process(command, should_stop, on_started, root_lock_fd=None):
    """Bounded in-memory capture and stop the whole owned worker process group."""
    captured = bytearray()
    # An owned worker must retain the same open-file-description flock after
    # a hard-killed scheduler exits. A new scheduler cannot then start another
    # batch while that worker is still using the authenticated download path.
    inherited = (root_lock_fd,) if root_lock_fd is not None else ()
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               stdin=subprocess.DEVNULL, start_new_session=True, pass_fds=inherited)
    def drain():
        try:
            while True:
                chunk = process.stdout.read(4096)
                if not chunk:
                    break
                captured.extend(chunk)
                if len(captured) > 131072:
                    del captured[:-131072]
        finally:
            process.stdout.close()
    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    interrupted, stopping_at, sent_term, sent_kill = False, None, False, False
    try:
        # Even a local state-write failure must stop the already launched
        # worker; otherwise the batch could abandon a live download process.
        on_started(process.pid)
        while process.poll() is None:
            if should_stop() and stopping_at is None:
                interrupted, stopping_at = True, time.monotonic()
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGINT)
            if stopping_at is not None:
                elapsed = time.monotonic() - stopping_at
                if elapsed >= 10 and not sent_term:
                    sent_term = True
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGTERM)
                if elapsed >= 15 and not sent_kill:
                    sent_kill = True
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
            time.sleep(0.2)
        process.wait()
    except BaseException:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        raise
    finally:
        reader.join(timeout=2)
    return process.returncode, bytes(captured), interrupted


def run_batch(args):
    proxy_valid(args.proxy)
    batch_dir = args.batch_dir.expanduser().resolve()
    manifest, _, root = load_batch(args.batch_dir)
    with root_lock(root, batch_dir) as root_lock_fd:
        # Re-read after acquiring the cross-batch lock; an earlier runner may
        # have completed just before this lock was obtained.
        manifest, state, root = load_batch(batch_dir)
        pause_path = batch_dir / 'PAUSE.json'
        if pause_path.exists():
            pause_path.unlink()
        flags = {'signal': False}
        started = time.monotonic()
        deadline = started + args.max_runtime_minutes * 60 if args.max_runtime_minutes is not None else None
        def handler(signum, frame):
            flags['signal'] = True
        previous = {sig: signal.signal(sig, handler) for sig in [signal.SIGINT, signal.SIGTERM]}
        def stop_reason():
            if flags['signal']:
                return 'termination_signal'
            if pause_path.exists():
                return 'pause_requested'
            if deadline is not None and time.monotonic() >= deadline:
                return 'runtime_limit'
            return None
        def should_stop():
            return stop_reason() is not None
        try:
            for ident, saved in state['items'].items():
                if saved['status'] == 'active':
                    # The inherited root lock guarantees an earlier owned
                    # worker is no longer running. Recover its durable attempt
                    # marker before local validation can replace that marker.
                    prior = read_worker_result(batch_dir / 'items' / ident, b'', ident,
                                               minimum_mtime=saved.get('attempt_started_epoch'))
                    account_worker_attempt(saved, prior)
                    saved['status'] = 'interrupted'
                    saved['updated_at'] = now()
            state.update({'batch_state': 'running', 'active_video_id': None, 'run_started_at': now(),
                          'run_finished_at': None, 'blocked_reason': None, 'pause_reason': None,
                          'run_newly_completed': 0, 'run_skipped_completed': 0,
                          'run_skipped_failed_or_review': 0, 'consecutive_transport_failures': 0})
            save_state(batch_dir, state)
            for item in manifest['items']:
                ident = item['video_id']
                saved = state['items'][ident]
                job_dir = batch_dir / 'items' / ident
                if should_stop():
                    state.update({'batch_state': 'paused', 'pause_reason': stop_reason()})
                    break
                if job_dir.is_symlink():
                    saved.update({'status': 'failed', 'last_error': 'job_directory_symlink_not_allowed', 'updated_at': now()})
                    save_state(batch_dir, state)
                    continue
                if saved['status'] == 'complete':
                    if file_receipt_valid(saved.get('receipt'), job_dir, item['expected_duration_seconds']):
                        state['run_skipped_completed'] += 1
                        save_state(batch_dir, state)
                        continue
                    saved.update({'status': 'interrupted', 'last_error': 'completed_file_missing_or_changed'})
                if saved['status'] in {'failed', 'review'} and not args.retry_failed:
                    state['run_skipped_failed_or_review'] += 1
                    save_state(batch_dir, state)
                    continue
                # Local revalidation must remain possible after the download
                # budget is exhausted. A known merged file is not itself proof
                # of completion; the worker must revalidate it in full.
                prior = read_worker_result(job_dir, b'', ident)
                local_revalidation = prior.get('file') is not None and Path(prior['file']).is_file()
                if saved['download_attempts'] >= args.max_attempts and not local_revalidation:
                    saved.update({'status': 'failed', 'last_error': 'download_attempt_limit', 'updated_at': now()})
                    save_state(batch_dir, state)
                    continue
                job_dir.mkdir(parents=True, exist_ok=True)
                saved.update({'status': 'active', 'worker_attempts': saved['worker_attempts'] + 1,
                              'attempt_started_at': now(), 'attempt_started_epoch': time.time(),
                              'updated_at': now(), 'last_error': None})
                state['active_video_id'] = ident
                save_state(batch_dir, state)
                def on_started(pid):
                    saved['worker_pid'] = pid
                    save_state(batch_dir, state)
                try:
                    attempt_wall_time = time.time()
                    returncode, captured, interrupted = worker_process(
                        worker_command(args, manifest, item, job_dir), should_stop, on_started,
                        root_lock_fd=root_lock_fd)
                    result = read_worker_result(job_dir, captured, ident, minimum_mtime=attempt_wall_time)
                except OSError:
                    returncode, interrupted = 1, False
                    result = {'status': 'missing_dependency', 'download_attempted': False,
                              'final_error_categories': []}
                account_worker_attempt(saved, result)
                saved.pop('worker_pid', None)
                saved.update({'last_worker_status': result['status'], 'last_worker_returncode': returncode,
                              'last_result': result, 'updated_at': now()})
                state['active_video_id'] = None
                if not interrupted and returncode == 0 and file_receipt_valid(result, job_dir, item['expected_duration_seconds']):
                    saved.update({'status': 'complete', 'receipt': result, 'completed_at': now()})
                    state['run_newly_completed'] += 1
                    state['consecutive_transport_failures'] = 0
                elif interrupted or result['status'] == 'interrupted' or should_stop():
                    saved.update({'status': 'interrupted', 'last_error': 'interrupted'})
                    state.update({'batch_state': 'paused', 'pause_reason': stop_reason() or 'worker_interrupted'})
                    save_state(batch_dir, state)
                    break
                else:
                    categories = set(result.get('final_error_categories', []))
                    saved.update({'status': 'review' if result['status'] in REVIEW_STATUSES or returncode == 2
                                  or result['status'] == 'technical_checks_passed' else 'failed',
                                  'last_error': result['status'] if result['status'] != 'technical_checks_passed'
                                  else 'completed_file_missing_or_changed'})
                    if result['status'] in BLOCK_STATUSES or categories & BLOCK_CATEGORIES:
                        state.update({'batch_state': 'blocked', 'blocked_reason': result['status'] if result['status'] in BLOCK_STATUSES
                                      else sorted(categories & BLOCK_CATEGORIES)[0]})
                        save_state(batch_dir, state)
                        break
                    # A terminal format error can follow exhausted TLS retries
                    # while the extractor tries alternative requests. Require
                    # the worker's explicit boolean evidence, never infer this
                    # from accumulated warnings. Successful items take the
                    # earlier completion branch and clear the failure streak.
                    if categories & TRANSPORT_CATEGORIES or result.get('tls_retry_budget_exhausted') is True:
                        state['consecutive_transport_failures'] += 1
                    else:
                        state['consecutive_transport_failures'] = 0
                    if state['consecutive_transport_failures'] >= 2:
                        state.update({'batch_state': 'paused', 'pause_reason': 'consecutive_transport_failures'})
                        save_state(batch_dir, state)
                        break
                save_state(batch_dir, state)
                if args.interval:
                    wait_until = time.monotonic() + args.interval
                    while time.monotonic() < wait_until and not should_stop():
                        time.sleep(min(0.2, max(0, wait_until - time.monotonic())))
            if state['batch_state'] == 'running':
                if should_stop():
                    state.update({'batch_state': 'paused', 'pause_reason': stop_reason()})
                else:
                    complete = all(i['status'] == 'complete' for i in state['items'].values())
                    state['batch_state'] = 'completed' if complete else 'completed_with_issues'
            state.update({'active_video_id': None, 'run_finished_at': now()})
            save_state(batch_dir, state)
            return summary(state)
        finally:
            for sig, old in previous.items():
                signal.signal(sig, old)


def pause_batch(args):
    batch_dir = args.batch_dir.expanduser().resolve()
    _, state, _ = load_batch(args.batch_dir)
    atomic_json(batch_dir / 'PAUSE.json', {'requested_at': now(), 'request': 'pause'})
    return {'status': 'pause_requested', **summary(state)}


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    initialize_parser = commands.add_parser('init', help='Freeze a JSON/TXT/CSV list; never discover more videos.')
    initialize_parser.add_argument('--list', type=Path, required=True)
    initialize_parser.add_argument('--root', type=Path, default=Path.home() / 'YuncongVideoAnalysis')
    initialize_parser.add_argument('--name', required=True)
    initialize_parser.add_argument('--height', type=int, choices=[720, 1080], default=720)
    run_parser = commands.add_parser('run', help='Resume one frozen batch using stable per-video jobs.')
    run_parser.add_argument('--batch-dir', type=Path, required=True)
    run_parser.add_argument('--cookie-file', type=Path)
    run_parser.add_argument('--proxy')
    run_parser.add_argument('--impersonate-chrome', action='store_true')
    run_parser.add_argument('--native-media-network', action='store_true',
                            help='Use native TLS for googlevideo media while preserving the player/authentication client.')
    run_parser.add_argument('--player-client', choices=['web_safari', 'web_creator'], default='web_safari')
    run_parser.add_argument('--format-mode', choices=['hls', 'auto'], default='hls')
    run_parser.add_argument('--tls-connection-retries', type=int, choices=[0, 1, 2], default=0)
    run_parser.add_argument('--retry-failed', action='store_true')
    run_parser.add_argument('--interval', type=nonnegative_number, default=7)
    run_parser.add_argument('--max-attempts', type=positive_integer, default=3)
    run_parser.add_argument('--max-runtime-minutes', type=positive_number)
    for name in ['status', 'pause']:
        sub = commands.add_parser(name)
        sub.add_argument('--batch-dir', type=Path, required=True)
    return parser.parse_args()


def main():
    os.umask(0o077)
    args = parse_args()
    try:
        if args.command == 'init':
            record = initialize(args)
        elif args.command == 'run':
            record = run_batch(args)
        elif args.command == 'pause':
            record = pause_batch(args)
        else:
            _, state, _ = load_batch(args.batch_dir)
            record = summary(state)
            record['pause_requested'] = (args.batch_dir.expanduser().resolve() / 'PAUSE.json').exists()
        emit(record)
        return 0 if record.get('batch_state') not in {'blocked', 'completed_with_issues'} else 2
    except BatchError as exc:
        emit({'status': exc.status})
        return 3 if exc.status == 'another_batch_is_running' else 1
    except (OSError, ValueError, TypeError, KeyError, UnicodeError) as exc:
        emit({'status': 'batch_local_operation_failed', 'error_type': type(exc).__name__})
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
