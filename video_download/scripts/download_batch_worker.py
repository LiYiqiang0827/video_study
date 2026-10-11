#!/usr/bin/env python3
"""Private worker for the frozen Yuncong 34-video batch; never start a scheduler."""
import math
from pathlib import Path

from system_curl_transport import SystemCurlYDL, TRANSPORTS, worker
import download_batch as batch

ROOT = Path('/Users/nickzhang/YuncongVideoAnalysis')
BATCH = ROOT / 'batches/yuncong_202608_202609'
FROZEN_MANIFEST_SHA256 = '57c9c5484efece61d621e7304e8c9953196d37486006e5f52c200a30764fe849'
PRIVATE_FIELDS = {'retry_override', 'transport_trial', 'system_curl_requests',
                  'system_curl_exit_codes', 'private_manifest_sha256'}


def get_retry_policy(expected_seconds):
    if (isinstance(expected_seconds, bool) or expected_seconds is None
            or not math.isfinite(float(expected_seconds)) or float(expected_seconds) <= 0):
        raise worker.SafeFailure('private_batch_reference_duration_required')
    duration = float(expected_seconds)
    return {'authorization': 'current_user_batch_resume',
            'baseline_per_request_extra_retries': 2, 'baseline_total_extra_retries': 6,
            'effective_per_request_extra_retries': 6,
            'effective_total_extra_retries': min(600, 60 * math.ceil(duration / 600)),
            'wall_time_limit_seconds': 600 if duration <= 1200 else min(3600, 600 + math.ceil(duration * .3)),
            'reference_duration_seconds': duration}


def authorize(args):
    if (args.check or args.probe_only or args.verify_file or not args.job_dir or not args.url
            or args.decode != 'full' or not args.frames or args.tls_connection_retries != 2
            or args.player_client != 'web_safari' or args.format_mode != 'hls'
            or not args.impersonate_chrome or args.proxy != 'socks5h://127.0.0.1:7890'
            or args.root.expanduser().resolve() != ROOT.resolve()):
        raise worker.SafeFailure('private_batch_scope_invalid')
    manifest, _, root = batch.load_batch(BATCH)
    if (root != ROOT.resolve() or len(manifest['items']) != 34
            or args.height != manifest['height'] or manifest['manifest_sha256'] != FROZEN_MANIFEST_SHA256):
        raise worker.SafeFailure('private_batch_scope_invalid')
    ident, canonical_url = batch.parse_video(args.url)
    item = next((item for item in manifest['items'] if item['video_id'] == ident), None)
    if (item is None or canonical_url != item['url'] or args.url != canonical_url
            or args.job_dir.expanduser().resolve() != (BATCH / 'items' / ident).resolve()
            or args.expected_duration != item['expected_duration_seconds']):
        raise worker.SafeFailure('private_batch_scope_invalid')
    return (BATCH / 'items' / ident).resolve(), manifest['manifest_sha256']


def main():
    args = worker.parse_args()
    try:
        job, manifest_hash = authorize(args)
    except (worker.SafeFailure, batch.BatchError, OSError, ValueError) as exc:
        worker.emit({'status': getattr(exc, 'status', 'private_batch_validation_failed'),
                     'error_type': type(exc).__name__})
        return 1
    try:
        retry_override = get_retry_policy(args.expected_duration)
    except (worker.SafeFailure, ValueError, TypeError) as exc:
        worker.emit({'status': getattr(exc, 'status', 'private_batch_reference_duration_required')})
        return 1

    class AuthorizedBatchYDL(SystemCurlYDL):
        def __init__(self, *options, **kwargs):
            super().__init__(*options, **kwargs)
            self.TLS_RETRY_BUDGET = retry_override['effective_total_extra_retries']

    original_write, original_emit = worker.atomic_write_json, worker.emit

    def annotate(record):
        record.update({'retry_override': dict(retry_override),
                       'transport_trial': 'system_curl_googlevideo_https',
                       'private_manifest_sha256': manifest_hash,
                       'system_curl_requests': sum(t.requests for t in TRANSPORTS),
                       'system_curl_exit_codes': [code for t in TRANSPORTS for code in t.exit_codes][-20:]})
        return record

    def atomic_write(path, record):
        if Path(path) == job / 'result.json':
            annotate(record)
        return original_write(path, record)

    def emit(record):
        original_emit(annotate(record))

    # The shared transport has no fixed-video trial or automatic execution.
    # The original worker still owns identity/hash checks, job locking and HLS resume.
    worker.RESULT_FIELDS.update(PRIVATE_FIELDS)
    worker.parse_args = lambda: args
    worker.atomic_write_json = atomic_write
    worker.emit = emit
    worker.BoundedConnectionYDL = AuthorizedBatchYDL
    return worker.main()


if __name__ == '__main__':
    raise SystemExit(main())
