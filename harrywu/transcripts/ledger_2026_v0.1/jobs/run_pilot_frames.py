import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def save(state):
    path = ROOT / 'frames_status.json'
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, path)

def main():
    config = json.loads((ROOT / 'config_v0.1.json').read_text(encoding='utf-8'))
    state = dict(status='running', runner_pid=os.getpid(), completed=[], current=None, child_pid=None,
                 interval_seconds=10, started=datetime.now(timezone.utc).isoformat())
    save(state)
    for pilot in config['pilots']:
        video_id = pilot['stream_id']
        if (ROOT / 'STOP_FRAMES').exists():
            state['status']='stopped_before_next_stream'
            save(state)
            return
        output = ROOT / 'frames' / video_id
        command = [sys.executable, '-m', 'media_platform', 'frames', pilot['source_path'],
                   '--interval', '10', '--max-frames', '3000', '--output', str(output)]
        if output.exists():
            command.append('--resume')
        state.update(current=video_id)
        with (ROOT / 'logs' / f'{video_id}.frames.log').open('a', encoding='utf-8') as log:
            child = subprocess.Popen(command, stdout=log, stderr=log, env={**os.environ,'PYTHONIOENCODING':'utf-8'})
            state['child_pid']=child.pid
            save(state)
            try:
                code = child.wait(timeout=3600)
            except subprocess.TimeoutExpired:
                child.terminate()
                child.wait(timeout=30)
                raise RuntimeError(f'One-hour frame timeout: {video_id}')
        if code:
            raise RuntimeError(f'Frame extraction failed: {video_id}')
        manifest = json.loads((output / 'frame_manifest.json').read_text(encoding='utf-8'))
        if manifest['status']!='complete' or any(abs(f['time']-f['requested_time'])>1 for f in manifest['frames']):
            raise RuntimeError(f'Frame coverage or measured PTS failed: {video_id}')
        state['completed'].append(dict(stream_id=video_id, frames=len(manifest['frames'])))
        state.update(current=None,child_pid=None)
        save(state)
    state.update(status='ten_second_frames_complete_scene_and_semantic_review_pending',
                 ended=datetime.now(timezone.utc).isoformat())
    save(state)

if __name__=='__main__':
    try:
        main()
    except BaseException as exc:
        path = ROOT / 'frames_status.json'
        state=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        state.update(status='failed',error=f'{type(exc).__name__}: {exc}')
        save(state)
        raise
