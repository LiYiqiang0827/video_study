"""Portable title/upload-time filenames from yt-dlp's whitelisted metadata."""
import math
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


def safe_title(value, max_bytes=180):
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError('max_bytes_must_be_a_positive_integer')
    title = unicodedata.normalize('NFC', value if isinstance(value, str) else '')
    title = ''.join('_' if unicodedata.category(c).startswith('C') or c in '/\\:*?"<>|' else c for c in title)
    title = re.sub(r'\s+', ' ', title).strip(' .')
    title = title.encode('utf-8')[:max_bytes].decode('utf-8', errors='ignore').rstrip(' .')
    if not title:
        title = '未命名视频'.encode('utf-8')[:max_bytes].decode('utf-8', errors='ignore') or '_'
    if re.fullmatch(r'(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', title, re.I):
        title = ('_' + title).encode('utf-8')[:max_bytes].decode('utf-8', errors='ignore')
    return title


def naming_metadata(info):
    """Use true upload timestamp; never substitute release or download time."""
    record = {key: info.get(key) for key in ['title', 'upload_date', 'timestamp', 'release_timestamp']}
    stamp = info.get('timestamp')
    uploaded = None
    if not isinstance(stamp, bool) and isinstance(stamp, (int, float)) and math.isfinite(stamp) and stamp > 0:
        try:
            uploaded = datetime.fromtimestamp(stamp, timezone.utc)
            local = uploaded.astimezone(ZoneInfo('Asia/Shanghai'))
        except (ValueError, OverflowError, OSError):
            uploaded = None
    if uploaded is not None:
        record.update({'upload_time_utc': uploaded.isoformat(), 'upload_time_local': local.isoformat(),
                       'filename_time_zone': 'Asia/Shanghai', 'upload_time_precision': 'second',
                       'upload_time_source': 'youtube_upload_timestamp', 'filename_time': local.strftime('%Y%m%d_%H%M%S')})
    else:
        value = info.get('upload_date')
        valid_date = None
        if isinstance(value, str) and re.fullmatch(r'[0-9]{8}', value):
            try:
                # Keep the explicitly provided calendar date. Converting a
                # date-only value to a timezone would invent an upload time.
                datetime.strptime(value, '%Y%m%d')
                valid_date = value
            except ValueError:
                pass
        record.update({'filename_time_zone': 'unknown_date_only' if valid_date else None,
                       'upload_time_precision': 'date' if valid_date else 'unknown',
                       'upload_time_source': 'youtube_upload_date' if valid_date else None,
                       'filename_time': valid_date or '上传时间未知'})
    return record


def media_filename(info, suffix='.mp4'):
    if suffix not in {'.mp4', '.mkv', '.webm'}:
        raise ValueError('unsupported_media_extension')
    return safe_title(info.get('title')) + '_' + naming_metadata(info)['filename_time'] + suffix


def rename_media(path, info):
    """Create the final name without ever overwriting another file."""
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise OSError('regular_media_file_required')
    target = path.with_name(media_filename(info, path.suffix))
    if target == path:
        return path
    # Hard link gives an atomic no-replace operation on the same filesystem.
    # Remove the old download filename only once the new directory entry exists.
    import os
    for index in range(1000):
        candidate = target if index == 0 else target.with_stem(target.stem + f'_{index}')
        try:
            os.link(path, candidate)
        except FileExistsError:
            continue
        path.unlink()
        return candidate
    raise OSError('filename_collision_limit')
