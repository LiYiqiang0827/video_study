"""Finite task runner; all recognition remains in the installed media_platform."""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def now():
    return datetime.now(timezone.utc).isoformat()

def save(state):
    target = ROOT / 'pilot_status.json'
    temp = target.with_suffix('.tmp')
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, target)

def main():
    config = json.loads((ROOT / 'config_v0.1.json').read_text(encoding='utf-8'))
    state = dict(status='running', started=now(), runner_pid=os.getpid(),
                 config='config_v0.1.json', completed=[], current=None, child_pid=None)
    save(state)
    environment = {**os.environ, 'PYTHONIOENCODING':'utf-8', 'HF_HUB_OFFLINE':'1'}
    for pilot in config['pilots']:
        video_id = pilot['stream_id']
        if (ROOT / 'STOP_PILOT').exists():
            state.update(status='stopped_before_next_stream', ended=now())
            save(state)
            return
        source = Path(pilot['source_path']).resolve(strict=True)
        if source.parent != Path(config['source_root']).resolve():
            raise ValueError('Source outside frozen scope')
        output = ROOT / 'extractions' / video_id
        args = [sys.executable, '-m', 'media_platform', 'ingest', str(source), '--output', str(output),
                '--model', config['model'], '--device', config['device'], '--compute-type', config['compute_type'],
                '--language', config['language'], '--chunk-seconds', str(config['chunk_seconds']),
                '--vad', config['vad'], '--no-video']
        if output.exists():
            args.append('--resume')
        state.update(current=video_id, stream_started=now())
        with (ROOT / 'logs' / f'{video_id}.asr.log').open('a', encoding='utf-8') as log:
            log.write(f'\nSTART {now()}\n')
            log.flush()
            child = subprocess.Popen(args, stdout=log, stderr=log, env=environment)
            state['child_pid'] = child.pid
            save(state)
            try:
                code = child.wait(timeout=7200)
            except subprocess.TimeoutExpired:
                child.terminate()
                child.wait(timeout=30)
                raise RuntimeError(f'Two-hour per-stream timeout: {video_id}')
        if code:
            raise RuntimeError(f'ASR failed for {video_id}, return code {code}; see own log')
        verify = subprocess.run([sys.executable, '-m', 'media_platform', 'verify', str(output)],
                                capture_output=True, text=True, encoding='utf-8', timeout=300, env=environment)
        (ROOT / 'logs' / f'{video_id}.verify.json').write_text(verify.stdout + verify.stderr, encoding='utf-8')
        if verify.returncode:
            raise RuntimeError(f'Verification failed: {video_id}')
        manifest = json.loads((output / 'manifest.json').read_text(encoding='utf-8'))
        segments = json.loads((output / 'speech.json').read_text(encoding='utf-8'))
        if not segments or not all(c['device']=='cuda' and c['compute_type']=='float16' for c in manifest['chunks']):
            raise RuntimeError(f'Empty ASR or unexpected device/precision: {video_id}')
        header = (f'# {video_id} 全片自动转写\n\n源文件：{source.name}\n\n'
                  'large-v3 / CUDA / float16；300秒分块，分块边界原文保留。\n\n'
                  '本文件保留平台ASR原文；说话人类别、证券相关校正及视频核对尚待完成。\n\n')
        (ROOT / 'transcripts' / f'{video_id}.md').write_text(header + (output / 'speech.md').read_text(encoding='utf-8'), encoding='utf-8')
        state['completed'].append(dict(stream_id=video_id, segments=len(segments), chunks=len(manifest['chunks']),
                                      duration_seconds=manifest['duration'], completed=now()))
        state.update(child_pid=None, current=None)
        save(state)
    state.update(status='asr_complete_pending_video_review', ended=now())
    save(state)

if __name__ == '__main__':
    try:
        main()
    except BaseException as exc:
        state_path = ROOT / 'pilot_status.json'
        state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}
        state.update(status='failed', ended=now(), error=f'{type(exc).__name__}: {exc}')
        save(state)
        raise
