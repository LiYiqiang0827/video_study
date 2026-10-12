#!/usr/bin/env python3
"""Resume an explicit frozen batch with the previously validated curl profile."""
import argparse
import hashlib
import os
import time
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import download_batch as batch
from download_resilient_worker import get_retry_policy


def frozen_proxies(manifest, directory, requested):
    batch.proxy_valid(requested)
    choices = [requested]
    if requested:
        parsed = urlparse(requested)
        if parsed.scheme in {'http', 'socks5h'} and parsed.hostname in {'127.0.0.1', 'localhost', '::1'}:
            choices += [urlunparse(parsed._replace(scheme=s)) for s in ('http', 'socks5h')]
    known = {(hashlib.sha256(p.encode()).hexdigest() if p else None): p for p in choices}
    selected = {}
    for item in manifest['items']:
        ident = item['video_id']
        job = directory / 'items' / ident
        path = job / 'job.json'
        if job.is_symlink() or path.is_symlink():
            raise batch.BatchError('job_directory_symlink_not_allowed')
        if path.exists():
            identity = batch.load_json(path)
            fingerprint = identity.get('proxy_fingerprint')
            if fingerprint not in known:
                raise batch.BatchError('frozen_proxy_configuration_mismatch')
            selected[ident] = known[fingerprint]
        else:
            selected[ident] = requested
    return selected


def install_adapters(args, manifest, directory, proxies):
    original_command, original_process, original_save = batch.worker_command, batch.worker_process, batch.save_state
    manifest_hash = manifest['manifest_sha256']

    def command(run_args, frozen, item, job):
        result = original_command(run_args, frozen, item, job)
        result[1] = str(Path(__file__).with_name('download_resilient_worker.py'))
        proxy = proxies[item['video_id']]
        if '--proxy' in result:
            result[result.index('--proxy') + 1] = proxy
        elif proxy:
            result.extend(['--proxy', proxy])
        result.extend(['--batch-dir', str(directory), '--manifest-sha256', manifest_hash])
        return result

    def save(path, state):
        original_save(path, state)
        if state.get('batch_state') == 'running':
            batch.atomic_json(path / 'resilient_runtime_policy.json', {
                'manifest_sha256': manifest_hash, 'pid': os.getpid(), 'updated_at': batch.now(),
                'transport': 'system_curl_googlevideo_https',
                'per_request_extra_tls_retries': 6, 'total_extra_tls_retries_min': 60,
                'total_extra_tls_retries_max': 600, 'per_video_timeout_min_seconds': 600,
                'per_video_timeout_max_seconds': 3600, 'batch_runtime_minutes': args.max_runtime_minutes,
                'max_lifetime_download_attempts_per_item': args.max_attempts,
                'one_attempt_per_item_this_run': True})

    def process(cmd, should_stop, on_started, root_lock_fd=None):
        ident, _ = batch.parse_video(cmd[cmd.index('--url') + 1])
        job = Path(cmd[cmd.index('--job-dir') + 1])
        ref = float(cmd[cmd.index('--expected-duration') + 1])
        limit = get_retry_policy(ref)['wall_time_limit_seconds']
        started, deadline = time.time(), time.monotonic() + limit
        flags = {'timeout': False, 'external': False}

        def stop():
            if should_stop():
                flags['external'] = True
                return True
            if time.monotonic() >= deadline:
                flags['timeout'] = True
                return True
            return False

        code, captured, interrupted = original_process(cmd, stop, on_started, root_lock_fd)
        if not flags['timeout'] or flags['external']:
            return code, captured, interrupted
        safe = batch.read_worker_result(job, captured, ident, minimum_mtime=started)
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
                    'final_error_categories': sorted(set(safe.get('final_error_categories', [])) | {'network_timeout'})})
        batch.atomic_json(job / 'result.json', raw)
        return 1, batch.canonical_bytes(batch.safe_worker_record(raw, ident, job)) + b'\n', False

    batch.worker_command, batch.worker_process, batch.save_state = command, process, save
    batch.BLOCK_CATEGORIES |= {'http_403', 'tls_certificate'}
    scope_failures = {'resilient_batch_scope_invalid', 'resilient_batch_validation_failed',
                      'frozen_reference_duration_required'}
    batch.BLOCK_STATUSES |= scope_failures
    batch.WORKER_STATUSES |= scope_failures


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--batch-dir', required=True, type=Path)
    parser.add_argument('--cookie-file', type=Path)
    parser.add_argument('--proxy')
    parser.add_argument('--retry-failed', action='store_true')
    parser.add_argument('--interval', type=batch.nonnegative_number, default=7)
    parser.add_argument('--max-attempts', type=batch.positive_integer, default=3)
    parser.add_argument('--max-runtime-minutes', type=batch.positive_number, default=300)
    parser.set_defaults(command='run', tls_connection_retries=2, impersonate_chrome=True,
                        native_media_network=False, player_client='web_safari', format_mode='hls')
    return parser.parse_args(argv)


def main():
    os.umask(0o077)
    args = parse_args()
    directory = args.batch_dir.expanduser().resolve()
    manifest, _, _ = batch.load_batch(args.batch_dir)
    proxies = frozen_proxies(manifest, directory, args.proxy)
    install_adapters(args, manifest, directory, proxies)
    result = batch.run_batch(args)
    batch.emit(result)
    batch.atomic_json(directory / 'resilient_run_final_status.json', result)
    return 0 if result['batch_state'] == 'completed' else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (batch.BatchError, OSError, ValueError) as exc:
        batch.emit({'status': getattr(exc, 'status', 'resilient_batch_launch_failed'), 'error_type': type(exc).__name__})
        raise SystemExit(1)
