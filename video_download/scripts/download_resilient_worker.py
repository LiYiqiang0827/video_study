#!/usr/bin/env python3
"""Scoped worker for a frozen batch using verified system-curl media transport."""
import argparse
import math
import re
import sys
from pathlib import Path

import download_batch as batch
from system_curl_transport import SystemCurlYDL, TRANSPORTS, worker

FIELDS = {'retry_override', 'transport_trial', 'system_curl_requests',
          'system_curl_exit_codes', 'private_manifest_sha256'}


def get_retry_policy(seconds):
    if isinstance(seconds, bool) or seconds is None:
        raise worker.SafeFailure('frozen_reference_duration_required')
    duration = float(seconds)
    if not math.isfinite(duration) or duration <= 0:
        raise worker.SafeFailure('frozen_reference_duration_required')
    return {'authorization': 'user_authorized_frozen_batch',
            'baseline_per_request_extra_retries': 2, 'baseline_total_extra_retries': 6,
            'effective_per_request_extra_retries': 6,
            'effective_total_extra_retries': min(600, 60 * math.ceil(duration / 600)),
            'wall_time_limit_seconds': 600 if duration <= 1200 else min(3600, 600 + math.ceil(duration * .3)),
            'reference_duration_seconds': duration}


def authorize(args, directory, expected_hash):
    if (not re.fullmatch(r'[a-f0-9]{64}', expected_hash)
            or args.check or args.probe_only or args.verify_file or not args.job_dir or not args.url
            or args.decode != 'full' or not args.frames or args.tls_connection_retries != 2
            or args.player_client != 'web_safari' or args.format_mode != 'hls'
            or not args.impersonate_chrome or args.native_media_network):
        raise worker.SafeFailure('resilient_batch_scope_invalid')
    manifest, _, root = batch.load_batch(directory)
    ident, canonical = batch.parse_video(args.url)
    item = next((x for x in manifest['items'] if x['video_id'] == ident), None)
    job = directory.expanduser().resolve() / 'items' / ident
    if (manifest['manifest_sha256'] != expected_hash or item is None
            or args.root.expanduser().resolve() != root
            or args.job_dir.expanduser().resolve() != job or args.url != canonical
            or args.height != manifest['height']
            or args.expected_duration != item['expected_duration_seconds']):
        raise worker.SafeFailure('resilient_batch_scope_invalid')
    batch.proxy_valid(args.proxy)
    return job, manifest['manifest_sha256']


def main():
    scope = argparse.ArgumentParser(add_help=False)
    scope.add_argument('--batch-dir', required=True, type=Path)
    scope.add_argument('--manifest-sha256', required=True)
    scoped, remainder = scope.parse_known_args()
    sys.argv = [sys.argv[0], *remainder]
    args = worker.parse_args()
    try:
        job, manifest_hash = authorize(args, scoped.batch_dir, scoped.manifest_sha256)
        policy = get_retry_policy(args.expected_duration)
    except (worker.SafeFailure, batch.BatchError, OSError, ValueError, TypeError) as exc:
        worker.emit({'status': getattr(exc, 'status', 'resilient_batch_validation_failed'),
                     'error_type': type(exc).__name__})
        return 1

    class ScopedYDL(SystemCurlYDL):
        def __init__(self, *options, **kwargs):
            super().__init__(*options, **kwargs)
            self.TLS_RETRY_BUDGET = policy['effective_total_extra_retries']

    original_write, original_emit = worker.atomic_write_json, worker.emit

    def annotate(record):
        record.update({'retry_override': dict(policy),
                       'transport_trial': 'system_curl_googlevideo_https',
                       'private_manifest_sha256': manifest_hash,
                       'system_curl_requests': sum(t.requests for t in TRANSPORTS),
                       'system_curl_exit_codes': [c for t in TRANSPORTS for c in t.exit_codes][-20:]})
        return record

    def write(path, record):
        if Path(path) == job / 'result.json':
            annotate(record)
        return original_write(path, record)

    worker.RESULT_FIELDS.update(FIELDS)
    worker.parse_args = lambda: args
    worker.atomic_write_json = write
    worker.emit = lambda record: original_emit(annotate(record))
    worker.BoundedConnectionYDL = ScopedYDL
    return worker.main()


if __name__ == '__main__':
    raise SystemExit(main())
