"""Read-only local completion checks when an upstream receipt is absent."""
from __future__ import annotations
from datetime import datetime
from pathlib import Path
import sqlite3
import time
from zoneinfo import ZoneInfo

QUIET_SECONDS = 180
MAX_WAIT_SECONDS = 360
INDEX_CODES = ('000001.SH', '399001.SZ', '399006.SZ')

def ro(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro&immutable=1', uri=True)

def residual_files(root):
    """Only unfinished artifacts associated with the three inputs, not backups."""
    result = []
    for name in ('basic', 'kpl', 'index'):
        for path in Path(root).glob(f'db_cn_{name}*'):
            suffix = path.name[len(f'db_cn_{name}'):].lower()
            if any(token in suffix for token in ('.incomplete', '.partial', '.tmp', '.temp', '.download', '.writing', '.lock')):
                result.append(path.name)
    return sorted(result)

def expected_date(root, now):
    today = now.strftime('%Y%m%d')
    with ro(Path(root) / 'db_cn_basic.db') as c:
        sessions = {}
        for exchange in ('SSE', 'SZSE'):
            row = c.execute('SELECT is_open FROM tbl_cn_tradecal WHERE exchange=? AND cal_date=?', (exchange, today)).fetchone()
            if row is None:
                raise RuntimeError(f'calendar_horizon_insufficient: {exchange} lacks {today}; await unified calendar')
            # A trading day in progress is not yet an expected completed day.
            eligible = today if row[0] and now.hour >= 15 else '00000000'
            cutoff = '<=' if eligible == today else '<'
            last = c.execute(f'SELECT MAX(cal_date) FROM tbl_cn_tradecal WHERE exchange=? AND is_open=1 AND cal_date{cutoff}?', (exchange, today)).fetchone()[0]
            if not last:
                raise RuntimeError(f'calendar_incomplete: no completed {exchange} session')
            sessions[exchange] = (int(row[0]), last)
    if sessions['SSE'] != sessions['SZSE']:
        raise RuntimeError('calendar_conflict: SSE/SZSE disagree')
    return sessions['SSE'][1]

def latest_dates(root):
    result = {}
    with ro(Path(root) / 'db_cn_basic.db') as c:
        for table in ('tbl_cn_day', 'tbl_cn_stk_limit'):
            result[table] = c.execute(f'SELECT MAX(trade_date) FROM {table}').fetchone()[0]
    with ro(Path(root) / 'db_cn_kpl.db') as c:
        result['tbl_cn_kpl_list'] = c.execute('SELECT MAX(trade_date) FROM tbl_cn_kpl_list').fetchone()[0]
    with ro(Path(root) / 'db_cn_index.db') as c:
        for code in INDEX_CODES:
            result[code] = c.execute('SELECT MAX(trade_date) FROM tbl_cn_index_daily WHERE ts_code=?', (code,)).fetchone()[0]
    return result

def check_without_receipt(root, state_reader, *, now=None, wall_clock=time.time,
                          monotonic=time.monotonic, sleep=time.sleep):
    """Mtime quiet-age fast path; changing sources reset a bounded observation.

    Immutable readers never checkpoint, repair or create database sidecars.
    Empty WAL/SHM are normal SQLite artifacts; nonempty WAL/journal are rejected
    by state_reader. Neither absent labels nor derived quality flags block here.
    """
    started = monotonic()
    while True:
        leftovers = residual_files(root)
        if leftovers:
            raise RuntimeError('upstream_incomplete_residual: ' + ', '.join(leftovers))
        before = state_reader(root)
        remaining = max(0., QUIET_SECONDS - (wall_clock() - max(v['mtime_ns'] for v in before.values()) / 1e9))
        if remaining:
            if monotonic() - started >= MAX_WAIT_SECONDS:
                raise RuntimeError('upstream_not_quiet: files did not stabilize within 360 seconds')
            sleep(min(15., remaining, MAX_WAIT_SECONDS - (monotonic() - started)))
            continue
        stamp = now or datetime.now(ZoneInfo('Asia/Shanghai'))
        expected = expected_date(root, stamp)
        latest = latest_dates(root)
        mismatches = {key: value for key, value in latest.items() if value != expected}
        if mismatches:
            raise RuntimeError(f'upstream_incomplete: expected {expected}; cutoff mismatch {mismatches}')
        if before != state_reader(root) or residual_files(root):
            if monotonic() - started >= MAX_WAIT_SECONDS:
                raise RuntimeError('upstream_changed_during_preflight')
            continue
        return {'status': 'local_checks_passed_without_receipt', 'expected_trade_date': expected,
                'checked_at': stamp.isoformat(), 'latest_dates': latest, 'source_state': before,
                'quiet_seconds_required': QUIET_SECONDS,
                'quiet_age_seconds': wall_clock() - max(v['mtime_ns'] for v in before.values()) / 1e9,
                'unfinished_residuals': [], 'receipt_attached': False}
