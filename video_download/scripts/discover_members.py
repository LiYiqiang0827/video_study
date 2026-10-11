#!/usr/bin/env python3
"""Build a date-frozen member-video list from an explicitly authorized channel."""
import argparse
import fcntl
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from download_video import BoundedConnectionYDL, SafeFailure, SafeLogger, dependencies, executable_health, load_cookies

TZ = ZoneInfo('Asia/Shanghai')
AUTH_ERRORS = {'login_or_verification', 'membership_required', 'http_429', 'tls_certificate'}
BLOCK_STATUSES = {'missing_dependency', 'invalid_cookie_export', 'cookie_export_missing_or_symlink',
                  'nonempty_youtube_only_export_required', 'existing_proxy_without_embedded_credentials_required'}


class InventoryYDL(BoundedConnectionYDL):
    # A complete history spans many browse pages. Still <=2 retries per request,
    # with a finite 30-retry budget across the whole directory, not per page.
    TLS_RETRY_BUDGET = 30


def now():
    return datetime.now(TZ).isoformat()


def save(path, value):
    temp = path.with_name(path.name + f'.{os.getpid()}.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush(); os.fsync(stream.fileno())
    os.replace(temp, path)


def read_cache(path, retry=False):
    try:
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (json.JSONDecodeError, UnicodeError, ValueError):
        if not retry:
            raise SafeFailure('discovery_cache_invalid_retry_required') from None
        # Preserve the invalid bytes for diagnosis; never turn them into evidence.
        os.rename(path, path.with_name(path.name + f'.invalid_{time.time_ns()}'))
        return None


def date_value(value):
    if not re.fullmatch(r'[0-9]{8}', value):
        raise argparse.ArgumentTypeError('Expected YYYYMMDD')
    try:
        return datetime.strptime(value, '%Y%m%d').date()
    except ValueError:
        raise argparse.ArgumentTypeError('Invalid calendar date') from None


def selection(record, start, end, channel):
    """Only exact watch metadata can qualify a video; relative dates never do."""
    if record.get('channel_id') != channel:
        return 'wrong_channel'
    if record.get('availability') in {'public', 'unlisted', 'private', 'premium_only'}:
        return 'not_members_only'
    if record.get('availability') != 'subscriber_only':
        return 'not_confirmed_members_only'
    if record.get('live_status') in {'is_live', 'is_upcoming', 'post_live'}:
        return 'live_or_processing'
    stamp = record.get('timestamp')
    exact_date = None
    if isinstance(stamp, (int, float)) and not isinstance(stamp, bool) and math.isfinite(stamp):
        try:
            exact_date = datetime.fromtimestamp(stamp, timezone.utc).astimezone(TZ).date()
        except (ValueError, OverflowError, OSError):
            pass
    if exact_date is not None:
        record['selection_date'] = exact_date.isoformat()
        record['selection_date_source'] = 'upload_timestamp_Asia/Shanghai'
        return 'selected' if start <= exact_date <= end else 'outside_date_window'
    # Date-only values cannot locate a boundary upload in Beijing with certainty.
    value = record.get('upload_date')
    if isinstance(value, str):
        try:
            day = date_value(value)
        except argparse.ArgumentTypeError:
            day = None
        if day is not None:
            record['selection_date'] = day.isoformat()
            record['selection_date_source'] = 'upload_date_timezone_unknown'
            if day in {start - timedelta(days=1), start, end, end + timedelta(days=1)}:
                return 'boundary_date_requires_review'
            return 'selected' if start < day < end else 'outside_date_window'
    return 'upload_date_missing'


def candidate(entry, start, end, exact_all=False, current=None):
    """Broad prefilter for recent windows; unknown dates always get resolved."""
    current = current or datetime.now(TZ).date()
    if exact_all or (current - start).days > 180:
        return True
    stamp = entry.get('approximate_timestamp')
    if not isinstance(stamp, (int, float)) or isinstance(stamp, bool) or not math.isfinite(stamp):
        return True
    try:
        day = datetime.fromtimestamp(stamp, timezone.utc).astimezone(TZ).date()
    except (ValueError, OverflowError, OSError):
        return True
    return start - timedelta(days=62) <= day <= end + timedelta(days=62)


def options(args, logger, executables):
    value = {'quiet': True, 'noprogress': True, 'logger': logger, 'socket_timeout': 20,
             'cachedir': False, 'extractor_retries': 1, 'retries': 1,
             'tls_connection_retries': 2, 'skip_download': True, 'ignore_no_formats_error': True,
             'js_runtimes': {'node': {'path': executables['node']}},
             'extractor_args': {'youtube': {'player_client': ['web_safari'], 'skip': ['hls', 'dash']},
                                'youtubetab': {'approximate_date': ['true']}}}
    if args.proxy:
        from urllib.parse import urlparse
        parsed = urlparse(args.proxy)
        if (parsed.scheme not in {'http', 'https', 'socks5', 'socks5h'} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
            raise SafeFailure('existing_proxy_without_embedded_credentials_required')
        value['proxy'] = args.proxy
    if args.impersonate_chrome:
        from yt_dlp.networking.impersonate import ImpersonateTarget
        value['impersonate'] = ImpersonateTarget.from_str('chrome')
    return value


def extract(url, opts, cookies):
    client = InventoryYDL if opts.get('extract_flat') else BoundedConnectionYDL
    with client(opts) as ydl:
        for cookie in cookies:
            ydl.cookiejar.set_cookie(cookie)
        return ydl.extract_info(url, download=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--channel-id', required=True)
    parser.add_argument('--start-date', type=date_value, required=True)
    parser.add_argument('--end-date', type=date_value, required=True)
    parser.add_argument('--root', type=Path, default=Path.home() / 'YuncongVideoAnalysis')
    parser.add_argument('--cookie-file', type=Path)
    parser.add_argument('--proxy')
    parser.add_argument('--impersonate-chrome', action='store_true')
    parser.add_argument('--max-inventory', type=int, default=5000)
    parser.add_argument('--exact-all', action='store_true', help='Resolve watch metadata for every entry, with no relative-date prefilter.')
    parser.add_argument('--retry-failed', action='store_true')
    parser.add_argument('--interval', type=float, default=1)
    parser.add_argument('--status', action='store_true')
    args = parser.parse_args()
    log = SafeLogger(); progress = {}; folder = None; lock = None
    try:
        if not re.fullmatch(r'UC[A-Za-z0-9_-]{22}', args.channel_id):
            raise SafeFailure('invalid_channel_id')
        if args.start_date > args.end_date or not 1 <= args.max_inventory <= 20000 or not math.isfinite(args.interval) or args.interval < 0:
            raise SafeFailure('invalid_discovery_range_or_limit')
        root = args.root.expanduser().resolve()
        folder = root / 'discovery' / f'{args.start_date:%Y%m%d}_{args.end_date:%Y%m%d}'
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        status_path = folder / 'discovery_status.json'
        if args.status:
            identity_path = folder / 'discovery_identity.json'
            if identity_path.exists() and read_cache(identity_path).get('channel_id') != args.channel_id:
                raise SafeFailure('discovery_identity_mismatch')
            print(status_path.read_text() if status_path.exists() else '{"status":"not_started"}')
            return 0
        lock = (folder / '.discovery.lock').open('a')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SafeFailure('discovery_already_running') from None
        identity = {'channel_id': args.channel_id, 'start_date': args.start_date.isoformat(),
                    'end_date': args.end_date.isoformat(), 'timezone': 'Asia/Shanghai',
                    'exact_all': args.exact_all, 'max_inventory': args.max_inventory}
        identity_path = folder / 'discovery_identity.json'
        if identity_path.exists() and json.loads(identity_path.read_text()) != identity:
            raise SafeFailure('discovery_identity_mismatch')
        save(identity_path, identity)
        progress = {**identity, 'status': 'running', 'updated_at': now(), 'current_video_id': None}
        save(status_path, progress)
        network = {}
        def request(url, extra=None):
            if not network:
                executables, versions = dependencies()
                _, failures = executable_health(executables, ['node'])
                if failures or not versions['yt-dlp-ejs'] or (args.impersonate_chrome and not versions['curl-cffi']):
                    raise SafeFailure('missing_dependency')
                network['cookies'] = load_cookies(args.cookie_file or root / 'local_auth' / 'www.youtube.com_cookies.txt')
                network['options'] = options(args, log, executables)
            return extract(url, {**network['options'], **(extra or {})}, network['cookies'])
        inventory_path = folder / 'member_inventory_v2.json'
        if inventory_path.exists():
            inventory = read_cache(inventory_path, args.retry_failed)
            if inventory and (inventory.get('channel_id') != args.channel_id or not inventory.get('complete')):
                raise SafeFailure('inventory_identity_or_completeness_invalid')
        else:
            inventory = None
        if inventory is None:
            playlist = 'UUMO' + args.channel_id[2:]
            info = request(f'https://www.youtube.com/playlist?list={playlist}',
                           {'extract_flat': True, 'playlistend': args.max_inventory + 1,
                            'lazy_playlist': False, 'ignoreerrors': False})
            if not info or info.get('channel_id') != args.channel_id:
                raise SafeFailure('unexpected_inventory_channel')
            entries = info.get('entries') or []
            if any(not entry or not re.fullmatch(r'[A-Za-z0-9_-]{11}', str(entry.get('id', ''))) for entry in entries):
                raise SafeFailure('incomplete_inventory_entries')
            complete = (len(entries) <= args.max_inventory and
                        (not isinstance(info.get('playlist_count'), int) or len(entries) >= info['playlist_count']))
            inventory = {'channel_id': args.channel_id, 'channel': info.get('channel'), 'playlist_id': playlist,
                         'fetched_at': now(), 'playlist_count': info.get('playlist_count'), 'complete': complete,
                         'entries': [{**{k: e.get(k) for k in ['id', 'title', 'duration', 'availability', 'live_status']},
                                      'approximate_timestamp': e.get('timestamp')} for e in entries]}
            save(inventory_path, inventory)
            if not complete:
                raise SafeFailure('inventory_limit_or_coverage_incomplete')
        candidates = {e['id']: e for e in inventory['entries'] if candidate(e, args.start_date, args.end_date, args.exact_all)}
        metadata = folder / 'metadata'; metadata.mkdir(exist_ok=True, mode=0o700)
        progress.update({'inventory_count': len(inventory['entries']), 'metadata_candidate_count': len(candidates),
                         'coarse_excluded_count': len(inventory['entries']) - len(candidates),
                         'prefilter': 'none' if args.exact_all or (datetime.now(TZ).date() - args.start_date).days > 180 else 'relative_date_plus_minus_62_days'})
        consecutive_errors = 0
        metadata_completed = 0
        for ident, entry in candidates.items():
            cached = metadata / (ident + '.json')
            old = read_cache(cached, args.retry_failed) if cached.exists() else None
            if old and (old.get('id') != ident or old.get('channel_id') != args.channel_id):
                raise SafeFailure('metadata_cache_identity_mismatch')
            if old and old.get('id') == ident and old.get('channel_id') == args.channel_id and old.get('status') == 'metadata_accessible':
                old['selection'] = selection(old, args.start_date, args.end_date, args.channel_id)
                save(cached, old)
                metadata_completed += 1
                continue
            if old and old.get('status') == 'metadata_failed' and (not args.retry_failed or old.get('attempts', 0) >= 3):
                continue
            progress.update({'current_video_id': ident, 'updated_at': now()}); save(status_path, progress)
            item_log = SafeLogger()
            try:
                info = request(f'https://www.youtube.com/watch?v={ident}', {'logger': item_log, 'noplaylist': True})
                if not info or info.get('id') != ident or info.get('channel_id') != args.channel_id:
                    raise SafeFailure('unexpected_video_or_channel')
                record = {k: info.get(k) for k in ['id', 'title', 'channel_id', 'channel', 'duration', 'upload_date', 'timestamp', 'release_timestamp', 'availability', 'live_status']}
                record.update({'status': 'metadata_accessible', 'fetched_at': now(), 'attempts': (old or {}).get('attempts', 0) + 1})
                record['selection'] = selection(record, args.start_date, args.end_date, args.channel_id)
                save(cached, record); consecutive_errors = 0; metadata_completed += 1
            except Exception as exc:
                item_log.final_error_categories.update(item_log.classify(exc))
                record = {'id': ident, 'channel_id': args.channel_id, 'status': 'metadata_failed',
                          'error_type': type(exc).__name__, 'error_status': exc.status if isinstance(exc, SafeFailure) else 'metadata_request_failed',
                          'final_error_categories': sorted(item_log.final_error_categories), 'attempts': (old or {}).get('attempts', 0) + 1, 'updated_at': now()}
                save(cached, record); consecutive_errors += 1
                if (AUTH_ERRORS & item_log.final_error_categories or record['error_status'] in BLOCK_STATUSES
                        or consecutive_errors >= 2):
                    raise SafeFailure('metadata_access_or_transport_blocked') from None
            progress.update({'metadata_completed': metadata_completed, 'updated_at': now()}); save(status_path, progress)
            time.sleep(args.interval)
        records = [json.loads((metadata / (ident + '.json')).read_text()) for ident in candidates if (metadata / (ident + '.json')).exists()]
        selected = [r for r in records if r.get('selection') == 'selected']
        failed = [r['id'] for r in records if r.get('status') != 'metadata_accessible']
        unresolved = [r['id'] for r in records if r.get('selection') in {'boundary_date_requires_review', 'upload_date_missing', 'not_confirmed_members_only'}]
        queue = [{'video_id': r['id'], 'expected_duration_seconds': r.get('duration')} for r in selected]
        save(folder / 'videos.json', queue)
        save(folder / 'selected_metadata.json', selected)
        progress.update({'status': 'ready' if not failed and not unresolved and len(records) == len(candidates) else 'requires_review',
                         'current_video_id': None, 'updated_at': now(), 'selected_count': len(selected),
                         'metadata_completed': len(records) - len(failed), 'failed_ids': failed, 'unresolved_ids': unresolved,
                         'queue_path': str(folder / 'videos.json')})
        save(status_path, progress); print(json.dumps(progress, ensure_ascii=False))
        lock.close(); lock = None
        return 0 if progress['status'] == 'ready' else 2
    except KeyboardInterrupt:
        progress.update({'status': 'paused', 'updated_at': now()}); code = 130
    except Exception as exc:
        log.final_error_categories.update(log.classify(exc))
        progress.update({'status': exc.status if isinstance(exc, SafeFailure) else 'discovery_failed', 'error_type': type(exc).__name__, 'updated_at': now(), 'diagnostic_categories': sorted(log.categories), 'final_error_categories': sorted(log.final_error_categories)}); code = 1
    try:
        if folder and progress and progress.get('channel_id') == args.channel_id:
            save(folder / 'discovery_status.json', progress)
        print(json.dumps(progress, ensure_ascii=False)); return code
    finally:
        if lock:
            lock.close()


if __name__ == '__main__':
    raise SystemExit(main())
