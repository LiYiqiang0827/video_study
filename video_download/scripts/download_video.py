#!/usr/bin/env python3
"""Download one explicitly selected YouTube member video; never log credentials."""
import argparse
import contextlib
import fcntl
import hashlib
import importlib.metadata
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import warnings
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from video_naming import media_filename, naming_metadata, rename_media

try:
    import yt_dlp
    from yt_dlp.cookies import YoutubeDLCookieJar
    from yt_dlp.networking.exceptions import RequestError, SSLError
except ImportError:
    print('{"status":"missing_python_dependencies"}')
    raise SystemExit(1)

TOLERANCE = 2.0
MAX_TLS_RETRIES = 6
MEDIA_SUFFIXES = {'.mp4', '.webm', '.mkv'}
RESULT_FIELDS = {
    'mode', 'stage_directory', 'tool_versions', 'executable_versions', 'status', 'tool_failures',
    'expected_duration_seconds', 'listed_duration_seconds', 'file', 'video_id', 'watch_url',
    'title', 'selected_format', 'player_client', 'tls_retry_count', 'tls_retry_budget_exhausted', 'upload_date', 'timestamp',
    'release_timestamp', 'upload_time_utc', 'upload_time_local', 'filename_time_zone',
    'upload_time_precision', 'upload_time_source', 'filename_time', 'planned_filename',
    'canonical_filename', 'original_download_filename', 'bytes', 'sha256',
    'local_duration_seconds', 'streams', 'duration_checks', 'decode_check', 'quality_frames',
    'visual_review', 'audio_human_listening', 'diagnostic_categories', 'final_error_categories',
    'error_type', 'job_directory', 'job_identity', 'resume_mode', 'attempt_started_at',
    'attempt_number', 'download_attempted', 'recorded_sha256', 'actual_sha256',
    'media_revalidation_blocked', 'download_target_template', 'network_failures', 'network_failure_count',
    'native_media_network', 'native_media_network_backend_used',
}


class SafeFailure(Exception):
    def __init__(self, status):
        self.status = status


def emit(record):
    print(json.dumps(record, ensure_ascii=False), flush=True)


def atomic_write_json(path, record):
    """Commit complete private JSON on the same filesystem, including crash recovery state."""
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix=f'.{path.name}.', suffix='.tmp', delete=False) as f:
            temporary = Path(f.name)
            json.dump(record, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
        temporary = None
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def frozen_job_identity(args, ident):
    if args.proxy:
        proxy = urlparse(args.proxy)
        if (proxy.scheme not in {'http', 'https', 'socks5', 'socks5h'} or not proxy.hostname
                or proxy.username is not None or proxy.password is not None):
            raise SafeFailure('existing_proxy_without_embedded_credentials_required')
    return {'schema_version': 1, 'video_id': ident,
            'watch_url': f'https://www.youtube.com/watch?v={ident}', 'height': args.height,
            'player_client': args.player_client, 'format_mode': args.format_mode,
            'expected_duration_seconds': args.expected_duration,
            'listed_duration_seconds': args.listed_duration,
            'impersonate_chrome': args.impersonate_chrome,
            'tls_connection_retries': args.tls_connection_retries,
            'proxy_fingerprint': hashlib.sha256(args.proxy.encode()).hexdigest() if args.proxy else None}


def open_job(root, requested, identity):
    """Lock before inspecting state; never replace another job's identity or result."""
    original = requested.expanduser().absolute()
    job = original.resolve()
    auth = (root / 'local_auth').resolve()
    if (job == root or not job.is_relative_to(root) or job == auth
            or job.is_relative_to(auth) or auth.is_relative_to(job)):
        raise SafeFailure('job_directory_invalid')
    component = original
    while True:
        if component.is_symlink():
            raise SafeFailure('job_directory_invalid')
        if component.resolve() == root:
            break
        if component == component.parent:
            raise SafeFailure('job_directory_invalid')
        component = component.parent
    job.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = None
    try:
        descriptor = os.open(job / '.job.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        lock = os.fdopen(descriptor, 'a+b')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SafeFailure('job_locked') from None
        identity_file, result_file = job / 'job.json', job / 'result.json'
        if identity_file.is_symlink() or result_file.is_symlink():
            raise SafeFailure('job_state_invalid')
        if identity_file.exists():
            saved_identity = json.loads(identity_file.read_text())
            if saved_identity != identity:
                raise SafeFailure('job_identity_mismatch')
        else:
            if any(p.name != '.job.lock' and not (p.name.startswith('.job.json.') and p.suffix == '.tmp')
                   for p in job.iterdir()):
                raise SafeFailure('job_identity_missing')
            atomic_write_json(identity_file, identity)
        previous = json.loads(result_file.read_text()) if result_file.exists() else {}
        if not isinstance(previous, dict) or previous.get('job_identity', identity) != identity:
            raise SafeFailure('job_state_invalid')
        previous = {key: value for key, value in previous.items() if key in RESULT_FIELDS}
        return job, lock, previous
    except BaseException as exc:
        if lock is not None:
            lock.close()
        if isinstance(exc, (ValueError, OSError)):
            raise SafeFailure('job_state_invalid') from None
        raise


def recorded_job_media(stage, previous, ident):
    saved = previous.get('file')
    if saved is not None:
        if not isinstance(saved, str):
            raise SafeFailure('job_state_invalid')
        candidate = Path(saved)
        if (not candidate.is_absolute() or candidate.parent.resolve() != stage
                or candidate.suffix not in MEDIA_SUFFIXES or candidate.is_symlink()):
            raise SafeFailure('job_state_invalid')
        if candidate.is_file():
            return candidate.resolve()
        raise SafeFailure('job_media_missing')
    media = [p for p in stage.iterdir() if p.suffix in MEDIA_SUFFIXES]
    if any(p.is_symlink() or not p.is_file() for p in media):
        raise SafeFailure('job_state_invalid')
    originals = [p for p in media if p.stem == ident]
    if len(originals) == 1:
        return originals[0]
    intermediates = re.compile(re.escape(ident) + r'(?:\.f[A-Za-z0-9_-]+)?\.temp$|'
                               + re.escape(ident) + r'\.f[A-Za-z0-9_-]+$')
    if any(not intermediates.fullmatch(p.stem) for p in media):
        raise SafeFailure('job_state_invalid')
    return None


def usable_job_metadata(record, ident):
    return (record.get('video_id') == ident
            and record.get('watch_url') == f'https://www.youtube.com/watch?v={ident}'
            and 'title' in record and (record['title'] is None or isinstance(record['title'], str))
            and isinstance(record.get('planned_filename'), str)
            and (record.get('listed_duration_seconds') is None
                 or number(record['listed_duration_seconds']) is not None))


def record_file_identity(path, record):
    record['file'] = str(path)
    if path.is_symlink() or not path.is_file():
        raise SafeFailure('media_file_missing_or_symlink')
    record['bytes'] = path.stat().st_size
    with path.open('rb') as f:
        record['sha256'] = hashlib.file_digest(f, 'sha256').hexdigest()


def finalize_job_media(path, info, record, stage):
    """Persist the canonical hard link before unlinking the resumable ID filename."""
    target = path.with_name(media_filename(info, path.suffix))
    if target == path:
        record.update({'file': str(path), 'canonical_filename': path.name})
        atomic_write_json(stage / 'result.json', record)
        return path
    for index in range(1000):
        candidate = target if index == 0 else target.with_stem(target.stem + f'_{index}')
        if candidate.is_symlink():
            continue
        try:
            if not candidate.exists() or not os.path.samefile(path, candidate):
                os.link(path, candidate)
        except FileExistsError:
            continue
        record.update({'file': str(candidate), 'canonical_filename': candidate.name,
                       'original_download_filename': path.name})
        atomic_write_json(stage / 'result.json', record)
        path.unlink()
        return candidate
    raise SafeFailure('filename_collision_limit')


def validation_frames(stage, enabled, stable_job):
    if not enabled:
        return None
    if not stable_job:
        return stage
    parent = stage / 'frames'
    if parent.is_symlink():
        raise SafeFailure('job_state_invalid')
    frames = parent / datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    frames.mkdir(parents=True, exist_ok=False, mode=0o700)
    return frames


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def positive_duration(value):
    result = number(value)
    if result is None or result <= 0:
        raise argparse.ArgumentTypeError('Expected a positive finite duration in seconds')
    return result


def video_id(url):
    parsed = urlparse(url)
    if parsed.scheme != 'https':
        raise SafeFailure('https_youtube_url_required')
    if parsed.hostname == 'youtu.be':
        value = parsed.path.strip('/')
    elif parsed.hostname in {'youtube.com', 'www.youtube.com', 'm.youtube.com'}:
        value = (parse_qs(parsed.query).get('v', [''])[0] if parsed.path == '/watch'
                 else parsed.path.split('/')[2] if parsed.path.startswith('/live/') else '')
    else:
        raise SafeFailure('single_youtube_video_required')
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', value):
        raise SafeFailure('single_youtube_video_required')
    return value


class SafeLogger:
    """Retain only fixed labels, including for debug and parser diagnostics."""
    def __init__(self):
        self.categories = set()
        self.final_error_categories = set()

    def classify(self, message):
        value = str(message).lower()
        patterns = {
            'network_timeout': ['timed out', 'timeout'],
            'network_connection': ['connection refused', 'connection reset', 'network is unreachable'],
            'tls_certificate': ['certificate verify', 'certificate_verify'],
            'tls_connection': ['[ssl:', 'ssl connect', 'unexpected eof', 'ssl_error_syscall'],
            'login_or_verification': ['sign in', 'log in', 'not a bot', 'captcha'],
            'membership_required': ['members-only', 'members only', 'join this channel'],
            'unavailable_or_private': ['private video', 'video unavailable', 'video is not available'],
            'http_403': ['http error 403', '403 forbidden'],
            'http_429': ['http error 429', 'too many requests'],
            'po_token': ['po token', 'po_token'],
            'javascript_challenge': ['javascript', 'challenge solving', 'ejs'],
            'format_unavailable': ['requested format is not available', 'no video formats'],
            'fragment_skipped': ['skipping fragment', 'skipped fragment'],
        }
        found = {label for label, terms in patterns.items() if any(term in value for term in terms)}
        self.categories.update(found)
        return found

    def debug(self, message):
        self.classify(message)

    def warning(self, message):
        self.classify(message)

    def error(self, message):
        self.final_error_categories.update(self.classify(message))


class BoundedConnectionYDL(yt_dlp.YoutubeDL):
    TLS_RETRY_BUDGET = MAX_TLS_RETRIES
    def __init__(self, *args, **kwargs):
        self.tls_retry_count = 0
        self.network_failures = []
        self.network_failure_count = 0
        self.native_media_network_backend_used = None
        super().__init__(*args, **kwargs)

    def build_request_director(self, handlers, preferences=None):
        director = super().build_request_director(handlers, preferences)
        if not self.params.get('native_media_network'):
            return director
        original_send = director.send

        def send(request):
            parsed = urlparse(request.url)
            host = (parsed.hostname or '').lower()
            if parsed.scheme.lower() != 'https' or not (
                    host == 'googlevideo.com' or host.endswith('.googlevideo.com')):
                return original_send(request)
            native = director.handlers.get('Urllib')
            if native is None:
                raise RequestError('Native media network handler unavailable')
            routed = request.copy()
            routed.extensions.pop('impersonate', None)
            # Keep the shared jar, proxy, headers, signed URL and TLS settings.
            # Explicit dispatch must not fall back to Chrome on unsupported requests.
            native.validate(routed)
            self.native_media_network_backend_used = 'urllib'
            return native.send(routed)

        director.send = send
        return director

    def _record_open_failure(self, request, exc):
        allowed_classes = {'RequestError', 'NoSupportingHandlers', 'UnsupportedRequest', 'TransportError',
                           'SSLError', 'CertificateVerifyError', 'HTTPError', 'IncompleteRead', 'ProxyError'}
        cause_classes = allowed_classes | {'CurlError', 'RequestsError', 'RequestException', 'ConnectionError',
                                          'SSLEOFError', 'SSLSyscallError', 'Timeout', 'OSError', 'URLError'}
        cause = getattr(exc, 'cause', None) or exc.__cause__
        code = getattr(cause, 'code', None)
        handler = getattr(getattr(exc, 'handler', None), 'RH_KEY', None)
        handlers = {'CurlCFFI': 'curl_cffi', 'Urllib': 'urllib', 'Requests': 'requests', 'Websockets': 'websockets'}
        category = 'other'
        try:
            url = request if isinstance(request, str) else getattr(request, 'url', '')
            host = (urlparse(url).hostname or '').lower()
            if host == 'youtube.com' or host.endswith('.youtube.com') or host == 'youtu.be':
                category = 'youtube'
            elif host == 'googlevideo.com' or host.endswith('.googlevideo.com'):
                category = 'googlevideo'
        except (ValueError, TypeError, AttributeError):
            pass
        self.network_failure_count += 1
        self.network_failures.append({'phase': 'open',
                                      'exception_class': type(exc).__name__ if type(exc).__name__ in allowed_classes else 'Other',
                                      'cause_class': type(cause).__name__ if type(cause).__name__ in cause_classes else 'Other',
                                      'cause_code': int(code) if isinstance(code, int) and not isinstance(code, bool) else None,
                                      'request_category': category, 'handler': handlers.get(handler, 'other'),
                                      'tls_retry_count': self.tls_retry_count})
        del self.network_failures[:-20]

    def urlopen(self, request):
        limit = self.params.get('tls_connection_retries', 0)
        for attempt in range(limit + 1):
            try:
                return super().urlopen(request)
            except RequestError as exc:
                self._record_open_failure(request, exc)
                value = str(exc).lower().replace('_', ' ')
                transient = any(t in value for t in ['unexpected eof', 'ssl error syscall', 'curl: (35)'])
                # CertificateVerifyError is a subclass: never retry it here.
                if (type(exc) is not SSLError or not transient or attempt >= limit
                        or self.tls_retry_count >= self.TLS_RETRY_BUDGET):
                    raise
                self.tls_retry_count += 1
                time.sleep(0.5 * (attempt + 1))


def load_cookies(path):
    path = path.expanduser()
    if path.is_symlink() or not path.is_file():
        raise SafeFailure('cookie_export_missing_or_symlink')
    path.chmod(0o600)
    try:
        # CookieJar may emit a raw malformed cookie line before raising.
        with warnings.catch_warnings(), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            warnings.simplefilter('ignore')
            jar = YoutubeDLCookieJar(str(path))
            jar.load()
            cookies = list(jar)
    except Exception:
        raise SafeFailure('invalid_cookie_export') from None
    if not cookies or any(not (c.domain.lstrip('.') == 'youtube.com'
                              or c.domain.lstrip('.').endswith('.youtube.com')) for c in cookies):
        raise SafeFailure('nonempty_youtube_only_export_required')
    return cookies


def dependencies():
    executables = {name: shutil.which(name) for name in ['node', 'ffmpeg', 'ffprobe']}
    versions = {}
    for name in ['yt-dlp', 'yt-dlp-ejs', 'curl-cffi']:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return executables, versions


def executable_health(executables, required):
    versions, failures = {}, {}
    for name in required:
        path = executables.get(name)
        if not path:
            failures[name] = 'missing'
            continue
        try:
            command = [path, '--version' if name == 'node' else '-version']
            checked = subprocess.run(command, capture_output=True, timeout=10, check=False)
            value = checked.stdout.decode('utf-8', errors='replace').strip()
            pattern = r'^v(\d+(?:\.\d+){1,3})' if name == 'node' else rf'^{name} version (?:[Nn]-)?(\d+(?:\.\d+){{0,3}})'
            match = re.match(pattern, value)
            if checked.returncode != 0 or not match:
                failures[name] = 'not_runnable_or_unrecognized_version'
                continue
            versions[name] = match.group(1)
            if name == 'node' and int(match.group(1).split('.')[0]) < 22:
                failures[name] = 'version_below_22'
        except (OSError, subprocess.SubprocessError):
            failures[name] = 'version_check_failed'
    if sys.version_info < (3, 11):
        failures['python'] = 'version_below_3_11'
    return versions, failures


def completed_video_only(info, *, incomplete=False):
    if info.get('is_live') or info.get('live_status') in {'is_live', 'is_upcoming', 'post_live'}:
        return 'live_or_scheduled_video_not_supported'
    return None


def build_options(args, executables, stage, logger):
    h = args.height
    selector = (f'b[protocol^=m3u8][height<={h}][vcodec^=avc1]/b[protocol^=m3u8][height<={h}]'
                if args.format_mode == 'hls' else
                f'bv[height<={h}][vcodec^=avc1]+ba[ext=m4a]/b[height<={h}][vcodec^=avc1]/bv[height<={h}]+ba/b[height<={h}]')
    opts = {
        'quiet': True, 'noprogress': True, 'logger': logger, 'noplaylist': True,
        'socket_timeout': 20, 'cachedir': False, 'extractor_retries': 1,
        'retries': 2, 'fragment_retries': 2, 'concurrent_fragment_downloads': 1,
        'skip_unavailable_fragments': False, 'continuedl': True, 'overwrites': False,
        'match_filter': completed_video_only,
        'extractor_args': {'youtube': {'player_client': [args.player_client]}},
        'tls_connection_retries': args.tls_connection_retries,
        'native_media_network': bool(getattr(args, 'native_media_network', False)),
        'js_runtimes': {'node': {'path': executables['node']}},
        'ffmpeg_location': str(Path(executables['ffmpeg']).parent),
        'format': selector, 'outtmpl': str(stage / '%(id)s.%(ext)s'), 'merge_output_format': 'mp4',
    }
    if args.proxy:
        u = urlparse(args.proxy)
        if (u.scheme not in {'http', 'https', 'socks5', 'socks5h'} or not u.hostname
                or u.username is not None or u.password is not None):
            raise SafeFailure('existing_proxy_without_embedded_credentials_required')
        opts['proxy'] = args.proxy
    if args.impersonate_chrome:
        from yt_dlp.networking.impersonate import ImpersonateTarget
        opts['impersonate'] = ImpersonateTarget.from_str('chrome')
    return opts


def run_media_command(command, timeout, failure_status):
    result = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
    if result.returncode != 0 or result.stderr:
        raise SafeFailure(failure_status)
    return result.stdout


def inspect_media(path, executables, listed, expected, decode_mode, frames_dir=None, record=None):
    record = {} if record is None else record
    listed, expected = number(listed), number(expected)
    record.update({'file': str(path), 'listed_duration_seconds': listed,
                   'expected_duration_seconds': expected, 'bytes': None, 'sha256': None,
                   'decode_check': {'mode': decode_mode, 'status': 'not_started'},
                   'quality_frames': [], 'visual_review': 'not_performed_by_script',
                   'audio_human_listening': 'not_performed_by_script'})
    if path.is_symlink() or not path.is_file():
        raise SafeFailure('media_file_missing_or_symlink')
    record['bytes'] = path.stat().st_size
    with path.open('rb') as f:
        record['sha256'] = hashlib.file_digest(f, 'sha256').hexdigest()
    if record['bytes'] == 0:
        raise SafeFailure('media_file_missing_or_symlink')
    probe = run_media_command([executables['ffprobe'], '-v', 'error', '-show_streams', '-show_format',
                               '-of', 'json', str(path)], 60, 'ffprobe_failed')
    data = json.loads(probe)
    duration = number(data.get('format', {}).get('duration'))
    record['local_duration_seconds'] = duration
    if duration is None or duration <= 0:
        raise SafeFailure('invalid_media_duration')
    streams = [s for s in data.get('streams', []) if s.get('codec_type') in {'audio', 'video'}]
    if {s['codec_type'] for s in streams} != {'audio', 'video'}:
        raise SafeFailure('audio_or_video_track_missing')
    stream_records = []
    for s in streams:
        start, length = number(s.get('start_time')), number(s.get('duration'))
        end = start + length if start is not None and length is not None else None
        ok = (start is not None and abs(start) <= TOLERANCE and length is not None and length > 0
              and abs(length - duration) <= TOLERANCE and end is not None and abs(end - duration) <= TOLERANCE)
        stream_records.append({**{k: s[k] for k in ['codec_type', 'codec_name', 'width', 'height'] if k in s},
                               'start_time_seconds': start, 'duration_seconds': length,
                               'end_time_seconds': end, 'timing_matches_container': ok})
    listed_ok = abs(duration - listed) <= TOLERANCE if listed is not None else None
    expected_ok = abs(duration - expected) <= TOLERANCE if expected is not None else None
    timing_ok = all(s['timing_matches_container'] for s in stream_records)
    record.update({'streams': stream_records,
                   'duration_checks': {'tolerance_seconds': TOLERANCE, 'listed_matches_actual': listed_ok,
                                       'expected_matches_actual': expected_ok, 'streams_match_container': timing_ok}})
    intervals = [(0, None)] if decode_mode == 'full' else [(duration / 2, 8), (max(0, duration - 20), 8)]
    record['decode_check']['status'] = 'in_progress'
    try:
        for start, length in intervals:
            cmd = [executables['ffmpeg'], '-nostdin', '-hide_banner', '-v', 'error', '-xerror', '-ss', str(start), '-i', str(path)]
            if length is not None:
                cmd.extend(['-t', str(length)])
            cmd.extend(['-map', '0:v', '-map', '0:a', '-f', 'null', '/dev/null'])
            run_media_command(cmd, max(180, duration * 2) if length is None else 90, 'decode_failed')
    except KeyboardInterrupt:
        record['decode_check']['status'] = 'interrupted'
        raise
    except Exception:
        record['decode_check']['status'] = 'failed'
        raise
    record['decode_check']['status'] = 'passed'
    if frames_dir is not None:
        for label, start in [('middle', duration / 2), ('tail', max(0, duration - 20))]:
            target = frames_dir / f'quality_{label}.jpg'
            run_media_command([executables['ffmpeg'], '-nostdin', '-hide_banner', '-v', 'error', '-xerror',
                               '-ss', str(start), '-i', str(path), '-frames:v', '1', '-q:v', '2', str(target)],
                              60, 'frame_extraction_failed')
            record['quality_frames'].append(str(target))
    status = ('duration_requires_review' if listed_ok is False or expected_ok is False else
              'stream_timing_requires_review' if not timing_ok else
              'duration_reference_missing' if listed_ok is None and expected_ok is None else
              'sample_checks_passed_full_decode_pending' if decode_mode == 'sample' else
              'technical_checks_passed')
    record['status'] = status
    return record


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, default=Path.home() / 'YuncongVideoAnalysis')
    p.add_argument('--job-dir', type=Path, help='Fixed private directory for this resumable download job.')
    p.add_argument('--url', help='Exactly one explicitly selected video URL.')
    p.add_argument('--cookie-file', type=Path)
    p.add_argument('--height', type=int, choices=[720, 1080], default=720)
    p.add_argument('--expected-duration', type=positive_duration, help='Frozen reference duration, in seconds.')
    p.add_argument('--listed-duration', type=positive_duration, help='Optional metadata reference for local verification.')
    p.add_argument('--proxy', help='Existing proxy for this process only; never stored in results.')
    p.add_argument('--impersonate-chrome', action='store_true')
    p.add_argument('--native-media-network', action='store_true',
                   help='Use urllib only for HTTPS googlevideo media requests.')
    p.add_argument('--player-client', choices=['web_safari', 'web_creator'], default='web_safari')
    p.add_argument('--format-mode', choices=['hls', 'auto'], default='hls')
    p.add_argument('--tls-connection-retries', type=int, choices=[0, 1, 2], default=0)
    p.add_argument('--decode', choices=['full', 'sample'], default='full')
    p.add_argument('--frames', action='store_true', help='Save two private frames for visual review.')
    modes = p.add_mutually_exclusive_group()
    modes.add_argument('--check', action='store_true', help='No credentials or network.')
    modes.add_argument('--probe-only', action='store_true', help='Metadata only; does not prove media access.')
    modes.add_argument('--verify-file', type=Path, help='Verify a local file without credentials or network.')
    return p.parse_args()


def main():
    args = parse_args()
    result, stage, lock, logger = {
        'native_media_network': bool(getattr(args, 'native_media_network', False)),
        'native_media_network_backend_used': None,
    }, None, None, SafeLogger()
    try:
        if args.job_dir and (args.check or args.probe_only or args.verify_file):
            raise SafeFailure('job_directory_download_only')
        executables, versions = dependencies()
        root = args.root.expanduser().resolve()
        previous, existing_media = {}, None
        if args.job_dir:
            if not args.url:
                raise SafeFailure('explicit_video_url_required')
            ident = video_id(args.url)
            identity = frozen_job_identity(args, ident)
            stage, lock, previous = open_job(root, args.job_dir, identity)
            result = dict(previous)
            for key in ['error_type', 'recorded_sha256', 'actual_sha256', 'tool_failures']:
                result.pop(key, None)
            result.update({'mode': 'download', 'stage_directory': str(stage), 'job_directory': str(stage),
                           'job_identity': identity, 'video_id': ident, 'watch_url': identity['watch_url'],
                           'tool_versions': versions, 'status': 'started',
                           'expected_duration_seconds': args.expected_duration,
                           'attempt_started_at': datetime.now().isoformat(),
                           'attempt_number': int(previous.get('attempt_number', 0)) + 1,
                           'download_attempted': False, 'resume_mode': 'download_or_resume',
                           'download_target_template': str(stage / '%(id)s.%(ext)s'),
                           'quality_frames': [], 'decode_check': {'mode': args.decode, 'status': 'not_started'},
                           'tls_retry_count': 0, 'tls_retry_budget_exhausted': False,
                           'network_failures': [], 'network_failure_count': 0,
                           'native_media_network': bool(getattr(args, 'native_media_network', False)),
                           'native_media_network_backend_used': None,
                           'visual_review': 'not_performed_by_script',
                           'audio_human_listening': 'not_performed_by_script'})
            existing_media = recorded_job_media(stage, previous, ident)
            if existing_media is not None:
                result['resume_mode'] = 'local_revalidation'
                result['decode_check']['mode'] = 'full'
        required = (['ffmpeg', 'ffprobe'] if args.verify_file or existing_media is not None
                    else ['node', 'ffmpeg', 'ffprobe'])
        executable_versions, tool_failures = executable_health(executables, required)
        if args.check:
            ready = not tool_failures and versions['yt-dlp-ejs'] is not None
            if args.impersonate_chrome:
                ready = ready and versions['curl-cffi'] is not None
            emit({'status': 'dependencies_ready' if ready else 'missing_dependency',
                  'packages': versions, 'executables': executables,
                  'executable_versions': executable_versions, 'tool_failures': tool_failures})
            return 0 if ready else 1
        if tool_failures:
            result['tool_failures'] = tool_failures
            raise SafeFailure('missing_dependency')
        if not args.job_dir:
            if args.verify_file:
                if args.url:
                    raise SafeFailure('choose_download_or_local_verification')
                ident, mode = 'local', 'validation'
            else:
                if not args.url:
                    raise SafeFailure('explicit_video_url_required')
                ident, mode = video_id(args.url), 'downloads'
            stage = root / mode / datetime.now().strftime('%Y%m%d_%H%M%S_%f') / ident
            stage.mkdir(parents=True, exist_ok=False)
            result = {'mode': 'local_verification' if args.verify_file else 'probe' if args.probe_only else 'download',
                      'stage_directory': str(stage), 'tool_versions': versions, 'status': 'started',
                      'expected_duration_seconds': args.expected_duration,
                      'listed_duration_seconds': args.listed_duration if args.verify_file else None,
                      'native_media_network': bool(getattr(args, 'native_media_network', False)),
                      'native_media_network_backend_used': None}
            if args.verify_file:
                result['file'] = str(args.verify_file.expanduser().absolute())
            else:
                result.update({'video_id': ident, 'watch_url': f'https://www.youtube.com/watch?v={ident}'})
        result['executable_versions'] = executable_versions
        atomic_write_json(stage / 'result.json', result)
        if existing_media is not None:
            if not usable_job_metadata(previous, ident):
                raise SafeFailure('job_metadata_missing')
            if (previous.get('media_revalidation_blocked') == 'fragment_skipped'
                    or 'fragment_skipped' in previous.get('diagnostic_categories', [])):
                result['media_revalidation_blocked'] = 'fragment_skipped'
                raise SafeFailure('fragment_skipped_not_acceptable')
            baseline = previous.get('sha256')
            if baseline is not None and (not isinstance(baseline, str) or not re.fullmatch(r'[a-f0-9]{64}', baseline)):
                raise SafeFailure('job_state_invalid')
            observed = {}
            record_file_identity(existing_media, observed)
            result.update({'file': str(existing_media), 'bytes': observed['bytes']})
            if baseline is not None and baseline != observed['sha256']:
                result.update({'sha256': baseline, 'recorded_sha256': baseline,
                               'actual_sha256': observed['sha256']})
                raise SafeFailure('hash_mismatch')
            result['sha256'] = observed['sha256']
            atomic_write_json(stage / 'result.json', result)
            if existing_media.stem == ident:
                existing_media = finalize_job_media(existing_media, previous, result, stage)
            result['status'] = 'validating'
            atomic_write_json(stage / 'result.json', result)
            result.update(inspect_media(existing_media, executables, previous.get('listed_duration_seconds'),
                                        args.expected_duration, 'full',
                                        validation_frames(stage, args.frames, True), record=result))
        elif args.verify_file:
            result.update(inspect_media(args.verify_file.expanduser().absolute(), executables,
                                        args.listed_duration, args.expected_duration, args.decode,
                                        stage if args.frames else None, record=result))
        else:
            cookie_path = args.cookie_file or root / 'local_auth' / 'www.youtube.com_cookies.txt'
            cookies = load_cookies(cookie_path)
            options = build_options(args, executables, stage, logger)
            if args.job_dir:
                result.update({'download_attempted': True, 'status': 'downloading'})
                atomic_write_json(stage / 'result.json', result)
            ydl = BoundedConnectionYDL(options)
            try:
                with ydl:
                    for c in cookies:
                        ydl.cookiejar.set_cookie(c)
                    info = ydl.extract_info(f'https://www.youtube.com/watch?v={ident}', download=not args.probe_only)
            finally:
                result.update({'tls_retry_count': ydl.tls_retry_count,
                               'tls_retry_budget_exhausted': ydl.tls_retry_count >= getattr(ydl, 'TLS_RETRY_BUDGET', MAX_TLS_RETRIES),
                               'network_failures': getattr(ydl, 'network_failures', []),
                               'network_failure_count': getattr(ydl, 'network_failure_count', 0),
                               'native_media_network_backend_used': getattr(ydl, 'native_media_network_backend_used', None)})
            if not info or info.get('id') != ident:
                raise SafeFailure('unexpected_video_id')
            if completed_video_only(info):
                raise SafeFailure('live_or_scheduled_video_not_supported')
            result.update({'video_id': ident, 'title': info.get('title'), 'watch_url': f'https://www.youtube.com/watch?v={ident}',
                           'listed_duration_seconds': info.get('duration'), 'expected_duration_seconds': args.expected_duration,
                           'selected_format': {k: info.get(k) for k in ['format_id', 'protocol', 'height']},
                           'player_client': args.player_client})
            result.update(naming_metadata(info))
            result['planned_filename'] = media_filename(info)
            if args.probe_only:
                result['status'] = 'metadata_accessible_media_not_downloaded'
            else:
                media = [f for f in stage.iterdir() if f.stem == ident and f.suffix in MEDIA_SUFFIXES]
                if len(media) != 1:
                    raise SafeFailure('merged_media_not_found')
                result['file'] = str(media[0])
                result['status'] = 'media_downloaded'
                if 'fragment_skipped' in logger.categories:
                    result['media_revalidation_blocked'] = 'fragment_skipped'
                atomic_write_json(stage / 'result.json', result)
                if 'fragment_skipped' in logger.categories:
                    raise SafeFailure('fragment_skipped_not_acceptable')
                record_file_identity(media[0], result)
                atomic_write_json(stage / 'result.json', result)
                if args.job_dir:
                    renamed = finalize_job_media(media[0], info, result, stage)
                else:
                    renamed = rename_media(media[0], info)
                    result.update({'file': str(renamed), 'canonical_filename': renamed.name,
                                   'original_download_filename': media[0].name})
                result['status'] = 'validating'
                atomic_write_json(stage / 'result.json', result)
                result.update(inspect_media(renamed, executables, info.get('duration'), args.expected_duration,
                                            args.decode, validation_frames(stage, args.frames, bool(args.job_dir)),
                                            record=result))
        exit_code = 0 if result['status'] in {'technical_checks_passed', 'metadata_accessible_media_not_downloaded'} else 2
    except KeyboardInterrupt:
        result.update({'status': 'interrupted', 'error_type': 'KeyboardInterrupt'})
        exit_code = 130
    except Exception as exc:
        logger.final_error_categories.update(logger.classify(exc))
        result.update({'status': exc.status if isinstance(exc, SafeFailure) else 'operation_failed',
                       'error_type': type(exc).__name__})
        exit_code = 2 if result['status'] in {'hash_mismatch', 'job_metadata_missing', 'job_media_missing'} else 1
    result.update({'diagnostic_categories': sorted(logger.categories),
                   'final_error_categories': sorted(logger.final_error_categories)})
    try:
        if stage:
            atomic_write_json(stage / 'result.json', result)
        emit(result)
        return exit_code
    finally:
        if lock is not None:
            lock.close()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        emit({'status': 'interrupted'})
        raise SystemExit(130)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        emit({'status': 'local_operation_failed', 'error_type': type(exc).__name__})
        raise SystemExit(1)
