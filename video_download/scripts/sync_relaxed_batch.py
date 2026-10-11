#!/usr/bin/env python3
"""Read-only Mini synchronization of the frozen, verified 34-video batch."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path('/Users/nickzhang/YuncongVideoAnalysis')
BATCH = ROOT / 'batches/yuncong_202608_202609'
STATUS = BATCH / 'local_sync_status.json'
MANIFEST_HASH = '57c9c5484efece61d621e7304e8c9953196d37486006e5f52c200a30764fe849'
REMOTE_COMMAND = '/Users/nickzhang/YuncongVideoAnalysis/download_tools/venv/bin/python -'
HOST = 'macmini'
TERMINAL = {'completed', 'completed_with_issues', 'paused', 'blocked', 'terminated'}
ID_PATTERN = re.compile(r'[A-Za-z0-9_-]{11}')
SHA_PATTERN = re.compile(r'[0-9a-f]{64}')
REMOTE_SOURCE = r'''
import hashlib, json, os, pathlib, subprocess, sys
root=pathlib.Path('/Users/nickzhang/YuncongVideoAnalysis')
directory=root/'batches/yuncong_202608_202609'
sys.path.insert(0,'/Users/nickzhang/.codex/skills/yuncong-member-video-download/scripts')
import download_batch as batch
try:
    manifest,state,_=batch.load_batch(directory)
    if manifest['manifest_sha256']!='57c9c5484efece61d621e7304e8c9953196d37486006e5f52c200a30764fe849' or len(manifest['items'])!=34:
        raise ValueError('scope')
    def artifact(path,job,kind):
        path=pathlib.Path(path)
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(job.resolve()):
            raise ValueError('artifact')
        with path.open('rb') as f: digest=hashlib.file_digest(f,'sha256').hexdigest()
        return {'kind':kind,'file':str(path),'bytes':path.stat().st_size,'sha256':digest}
    completed=[]
    rejected=[]
    for item in manifest['items']:
        ident=item['video_id'];saved=state['items'][ident]
        if saved['status']!='complete': continue
        job=directory/'items'/ident;receipt=saved.get('receipt')
        if job.is_symlink():
            rejected.append({'video_id':ident,'label':'remote_receipt_invalid'});continue
        if not batch.file_receipt_valid(receipt,job,item['expected_duration_seconds']):
            rejected.append({'video_id':ident,'label':'remote_receipt_invalid'});continue
        try:
            result=batch.load_json(job/'result.json')
            if (result.get('status')!='technical_checks_passed' or result.get('video_id')!=ident
                or result.get('file')!=receipt['file'] or result.get('sha256')!=receipt['sha256']
                or result.get('bytes')!=receipt['bytes']): raise ValueError('result')
            media={'kind':'media','file':receipt['file'],'bytes':receipt['bytes'],'sha256':receipt['sha256']}
            files=[media,artifact(job/'result.json',job,'result')]
            frames=[]
            for filename in (result.get('quality_frames') or [])[:2]:
                try:
                    path=pathlib.Path(filename)
                    if path.suffix.lower() not in {'.jpg','.jpeg','.png'}: continue
                    frames.append(artifact(path,job,'frame'))
                except (OSError,ValueError,TypeError): pass
            completed.append({'video_id':ident,'artifacts':files,'frames':frames})
        except (OSError,ValueError,TypeError,KeyError,batch.BatchError):
            rejected.append({'video_id':ident,'label':'remote_result_invalid'})
    runner_alive=None
    try:
        policy=batch.load_json(directory/'relaxed_runtime_policy.json');pid=policy.get('pid')
        if isinstance(pid,int) and not isinstance(pid,bool) and pid>0:
            check=subprocess.run(['/bin/ps','-p',str(pid),'-o','command='],capture_output=True,timeout=5,check=False)
            runner_alive=check.returncode==0 and b'run_batch_relaxed.py' in check.stdout
    except (OSError,ValueError,TypeError,batch.BatchError,subprocess.TimeoutExpired): pass
    batch_state=state['batch_state']
    if batch_state=='running' and runner_alive is False: batch_state='terminated'
    print(json.dumps({'status':'snapshot_ready','manifest_sha256':manifest['manifest_sha256'],
                     'batch_state':batch_state,'runner_alive':runner_alive,
                     'completed_count':sum(x['status']=='complete' for x in state['items'].values()),
                     'items':completed,'rejected':rejected},ensure_ascii=False))
except Exception:
    print(json.dumps({'status':'remote_snapshot_failed'}))
    sys.exit(1)
'''


class SyncError(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def safe_directory(path):
    path = Path(path).absolute()
    if not path.is_relative_to(ROOT.absolute()):
        raise SyncError('local_path_scope_invalid')
    for parent in [path, *path.parents]:
        if parent.is_symlink():
            raise SyncError('local_symlink_rejected')
        if parent == ROOT.absolute():
            break
    path.mkdir(parents=True, exist_ok=True, mode=0o700)


def atomic_json(path, record):
    safe_directory(path.parent)
    if path.is_symlink():
        raise SyncError('local_symlink_rejected')
    fd, temporary = tempfile.mkstemp(prefix='.local-sync-status-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def remaining(deadline, maximum):
    value = deadline - time.monotonic()
    if value <= 0:
        raise SyncError('runtime_limit')
    return max(.01, min(value, maximum))


def read_snapshot(deadline):
    try:
        result = subprocess.run(
            ['/usr/bin/ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', HOST, REMOTE_COMMAND],
            input=REMOTE_SOURCE.encode(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=remaining(deadline, 300), check=False)
        if result.returncode or len(result.stdout) > 1048576:
            raise SyncError('remote_snapshot_failed')
        data = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError):
        raise SyncError('remote_snapshot_failed') from None
    if (not isinstance(data, dict) or data.get('status') != 'snapshot_ready'
            or data.get('manifest_sha256') != MANIFEST_HASH
            or not isinstance(data.get('items'), list) or not isinstance(data.get('rejected'), list)):
        raise SyncError('remote_snapshot_invalid')
    return data


def validate_artifact(value, ident, kind):
    if (not isinstance(value, dict) or value.get('kind') != kind
            or not isinstance(value.get('bytes'), int) or isinstance(value['bytes'], bool)
            or value['bytes'] <= 0 or not isinstance(value.get('sha256'), str)
            or SHA_PATTERN.fullmatch(value['sha256']) is None or not isinstance(value.get('file'), str)
            or any(ord(c) < 32 for c in value['file'])):
        raise SyncError('artifact_receipt_invalid')
    path = Path(value['file'])
    job = BATCH / 'items' / ident
    if (not path.is_absolute() or '..' in path.parts or not path.is_relative_to(job)
            or (kind == 'media' and (path.parent != job or path.suffix.lower() not in {'.mp4', '.mkv', '.webm'}))
            or (kind == 'result' and path != job / 'result.json')
            or (kind == 'frame' and path.suffix.lower() not in {'.jpg', '.jpeg', '.png'})):
        raise SyncError('artifact_path_scope_invalid')
    return path


def existing_matches(path, artifact):
    if path.is_symlink():
        raise SyncError('local_symlink_rejected')
    if not path.exists():
        return False
    if (not path.is_file() or path.stat().st_size != artifact['bytes']
            or digest(path) != artifact['sha256']):
        raise SyncError('local_hash_mismatch')
    return True


def copy_artifact(artifact, ident, kind, deadline, record, on_progress=None):
    target = validate_artifact(artifact, ident, kind)
    safe_directory(target.parent)
    if existing_matches(target, artifact):
        return 'skipped'
    temporary = target.parent / f'.local-sync-{ident}-{kind}-{os.getpid()}.part'
    if temporary.is_symlink():
        raise SyncError('local_symlink_rejected')
    record['partial_file'] = str(temporary)
    for attempt in range(1, 4):
        record['artifact_attempts_this_cycle'] = attempt
        if on_progress is not None:
            on_progress()
        try:
            transfer = subprocess.run(
                ['/usr/bin/scp', '-q', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                 f'{HOST}:{target}', str(temporary)], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=remaining(deadline, 600), check=False)
            if transfer.returncode:
                raise SyncError('transfer_failed')
            temporary.chmod(0o600)
            if (temporary.stat().st_size != artifact['bytes'] or digest(temporary) != artifact['sha256']):
                raise SyncError('transferred_hash_mismatch')
            try:
                os.link(temporary, target)
            except FileExistsError:
                existing_matches(target, artifact)
            temporary.unlink()
            record.pop('partial_file', None)
            return 'copied'
        except (OSError, subprocess.TimeoutExpired):
            record['last_error'] = 'transfer_failed'
        except SyncError as exc:
            record['last_error'] = str(exc)
            if str(exc) in {'runtime_limit', 'local_hash_mismatch', 'local_symlink_rejected'}:
                raise
        if attempt < 3:
            time.sleep(min(2 * attempt, remaining(deadline, 2 * attempt)))
    raise SyncError(record.get('last_error', 'transfer_failed'))


def sync_snapshot(data, state, deadline, include_frames=False):
    state['remote_batch_state'] = data['batch_state']
    state['remote_completed'] = data['completed_count']
    state['last_snapshot_at'] = now()
    state['cycle_copied_artifacts'] = state['cycle_skipped_artifacts'] = 0
    for entry in data['rejected']:
        ident = entry.get('video_id') if isinstance(entry, dict) else None
        if isinstance(ident, str) and ID_PATTERN.fullmatch(ident):
            state['items'][ident] = {'status': 'remote_validation_failed', 'updated_at': now()}
    for entry in data['items']:
        ident = entry.get('video_id') if isinstance(entry, dict) else None
        if not isinstance(ident, str) or ID_PATTERN.fullmatch(ident) is None:
            raise SyncError('snapshot_item_invalid')
        artifacts = entry.get('artifacts')
        if (not isinstance(artifacts, list) or len(artifacts) != 2
                or artifacts[0].get('kind') != 'media' or artifacts[1].get('kind') != 'result'):
            raise SyncError('snapshot_item_invalid')
        record = state['items'].setdefault(ident, {})
        record.update({'status': 'syncing', 'updated_at': now(), 'last_error': None})
        try:
            selected = [(artifacts[0], 'media'), (artifacts[1], 'result')]
            if include_frames:
                selected.extend((a, 'frame') for a in entry.get('frames', [])[:2])
            for artifact, kind in selected:
                action = copy_artifact(artifact, ident, kind, deadline, record,
                                       lambda: atomic_json(STATUS, state))
                state[f'cycle_{action}_artifacts'] += 1
            record.update({'status': 'synced', 'file': artifacts[0]['file'],
                           'bytes': artifacts[0]['bytes'], 'sha256': artifacts[0]['sha256'],
                           'frames_requested': include_frames, 'updated_at': now(), 'last_error': None})
        except SyncError as exc:
            record.update({'status': str(exc), 'last_error': str(exc), 'updated_at': now()})
            if str(exc) == 'runtime_limit':
                atomic_json(STATUS, state)
                raise
        state['synced_count'] = sum(item.get('status') == 'synced' for item in state['items'].values())
        atomic_json(STATUS, state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--frames', action='store_true', help='Also copy the two verified picture files.')
    parser.add_argument('--once', action='store_true', help='Sync one snapshot only.')
    args = parser.parse_args()
    os.umask(0o077)
    safe_directory(BATCH)
    lock_path = ROOT / '.relaxed_batch_sync.lock'
    if lock_path.is_symlink():
        raise SyncError('local_symlink_rejected')
    with lock_path.open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({'status': 'another_sync_is_running'}))
            return 2
        try:
            state = json.loads(STATUS.read_text()) if STATUS.is_file() and not STATUS.is_symlink() else {}
        except (OSError, ValueError):
            state = {}
        if not isinstance(state, dict) or state.get('manifest_sha256') != MANIFEST_HASH:
            state = {'schema_version': 1, 'manifest_sha256': MANIFEST_HASH, 'items': {}}
        if not isinstance(state.get('items'), dict):
            state['items'] = {}
        state.update({'status': 'running', 'pid': os.getpid(), 'run_started_at': now(),
                      'interval_seconds': 60, 'max_runtime_seconds': 21600})
        deadline = time.monotonic() + 21600
        atomic_json(STATUS, state)
        while True:
            try:
                snapshot = read_snapshot(deadline)
                sync_snapshot(snapshot, state, deadline, args.frames)
                if snapshot['batch_state'] in TERMINAL or args.once:
                    state['status'] = 'finished' if snapshot['batch_state'] == 'completed' else 'stopped'
                    break
            except SyncError as exc:
                state['last_error'] = str(exc)
                if str(exc) == 'runtime_limit':
                    state['status'] = 'runtime_limit'
                    break
                if args.once:
                    state['status'] = 'snapshot_failed'
                    break
            state['updated_at'] = now()
            atomic_json(STATUS, state)
            time.sleep(min(60, max(0, deadline - time.monotonic())))
        state['run_finished_at'] = now()
        state['synced_count'] = sum(item.get('status') == 'synced' for item in state['items'].values())
        state['issues_count'] = sum(item.get('status') != 'synced' for item in state['items'].values())
        atomic_json(STATUS, state)
        print(json.dumps({'status': state['status'], 'synced': state['synced_count'],
                          'issues': state['issues_count'], 'remote_batch_state': state.get('remote_batch_state')}))
        return 0 if state['issues_count'] == 0 and state['status'] in {'finished', 'stopped'} else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print(json.dumps({'status': 'sync_interrupted'}))
        raise SystemExit(130)
    except Exception:
        print(json.dumps({'status': 'sync_local_operation_failed'}))
        raise SystemExit(1)
