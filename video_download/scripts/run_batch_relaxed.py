#!/usr/bin/env python3
"""Resume only the already-authorized 34-video manifest, with bounded transport retries."""
import json
import os
import pathlib
import sys
import time

ROOT = pathlib.Path('/Users/nickzhang/YuncongVideoAnalysis')
BATCH = ROOT / 'batches/yuncong_202608_202609'
TOOLS = pathlib.Path(__file__).resolve().parent
SCRIPTS = TOOLS
MANIFEST_HASH = '57c9c5484efece61d621e7304e8c9953196d37486006e5f52c200a30764fe849'
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(TOOLS))
import download_batch as batch
from download_batch_worker import get_retry_policy


def install_adapters():
    original_command = batch.worker_command
    original_process = batch.worker_process
    original_save = batch.save_state

    def command(args, manifest, item, job_dir):
        result = original_command(args, manifest, item, job_dir)
        result[1] = str(TOOLS / 'download_batch_worker.py')
        return result

    def save(directory, state):
        original_save(directory, state)
        if state.get('batch_state') == 'running':
            batch.atomic_json(directory / 'relaxed_runtime_policy.json', {
                'authorization': 'current_user_request_retry_existing_aug_sep_batch',
                'manifest_sha256': MANIFEST_HASH, 'pid': os.getpid(),
                'updated_at': batch.now(), 'transport': 'system_curl_googlevideo_https',
                'per_request_extra_tls_retries': 6, 'total_extra_tls_retries_min': 60,
                'total_extra_tls_retries_max': 600, 'per_video_timeout_min_seconds': 600,
                'per_video_timeout_max_seconds': 3600, 'batch_runtime_minutes': 300,
                'max_lifetime_download_attempts_per_item': 5,
                'one_attempt_per_item_this_run': True, 'video_interval_seconds': 7})

    def process(cmd, should_stop, on_started, root_lock_fd=None):
        ident, _ = batch.parse_video(cmd[cmd.index('--url') + 1])
        job = pathlib.Path(cmd[cmd.index('--job-dir') + 1])
        ref = float(cmd[cmd.index('--expected-duration') + 1])
        limit = get_retry_policy(ref)['wall_time_limit_seconds']
        started_epoch, deadline = time.time(), time.monotonic() + limit
        flags = {'timeout': False, 'external_stop': False}

        def stop():
            if should_stop():
                flags['external_stop'] = True
                return True
            if time.monotonic() >= deadline:
                flags['timeout'] = True
                return True
            return False

        code, captured, interrupted = original_process(cmd, stop, on_started, root_lock_fd)
        if not flags['timeout'] or flags['external_stop']:
            return code, captured, interrupted
        safe = batch.read_worker_result(job, captured, ident, minimum_mtime=started_epoch)
        if code == 0 and batch.file_receipt_valid(safe, job, ref):
            return code, captured, False
        try:
            raw = batch.load_json(job / 'result.json')
            if not isinstance(raw, dict) or raw.get('video_id') != ident:
                raw = dict(safe)
        except (OSError, batch.BatchError):
            raw = dict(safe)
        raw.update({'status': 'operation_failed', 'video_id': ident,
                    'error_type': 'PerVideoRuntimeLimit', 'per_video_timeout': True,
                    'final_error_categories': sorted(set(safe.get('final_error_categories', []))
                                                     | {'network_timeout'})})
        batch.atomic_json(job / 'result.json', raw)
        safe = batch.safe_worker_record(raw, ident, job)
        return 1, batch.canonical_bytes(safe) + b'\n', False

    batch.worker_command = command
    batch.worker_process = process
    batch.save_state = save
    batch.BLOCK_CATEGORIES = set(batch.BLOCK_CATEGORIES) | {'http_403', 'tls_certificate'}


def main():
    os.umask(0o077)
    if len(sys.argv) != 1:
        batch.emit({'status': 'fixed_batch_launcher_no_arguments'})
        return 2
    manifest, _, _ = batch.load_batch(BATCH)
    if manifest['manifest_sha256'] != MANIFEST_HASH or len(manifest['items']) != 34:
        batch.emit({'status': 'frozen_manifest_mismatch'})
        return 2
    install_adapters()
    sys.argv = ['download_batch.py', 'run', '--batch-dir', str(BATCH),
                '--cookie-file', str(ROOT / 'local_auth/www.youtube.com_cookies.txt'),
                '--proxy', 'socks5h://127.0.0.1:7890', '--impersonate-chrome',
                '--tls-connection-retries', '2', '--retry-failed', '--max-attempts', '5',
                '--max-runtime-minutes', '300', '--interval', '7']
    args = batch.parse_args()
    result = batch.run_batch(args)
    batch.emit(result)
    batch.atomic_json(BATCH / 'relaxed_run_final_status.json', result)
    return 0 if result['batch_state'] == 'completed' else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        batch.emit({'status': 'batch_launch_failed', 'error_type': type(exc).__name__})
        raise SystemExit(1)
