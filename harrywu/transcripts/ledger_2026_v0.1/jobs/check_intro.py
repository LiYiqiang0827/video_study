"""A single bounded no-VAD comparison for the first pilot's initial ASR gap."""
import json
import os
import subprocess
import sys
from pathlib import Path
import psutil

ROOT=Path(__file__).resolve().parents[1]
state=json.loads((ROOT/'pilot_status.json').read_text(encoding='utf-8'))
try:
    process=psutil.Process(state['runner_pid'])
    if 'run_pilot.py' not in ' '.join(process.cmdline()):
        raise RuntimeError('Unexpected process identity')
    process.wait(timeout=14400)
except psutil.NoSuchProcess:
    pass
state=json.loads((ROOT/'pilot_status.json').read_text(encoding='utf-8'))
if state['status']!='asr_complete_pending_video_review':
    raise RuntimeError('ASR pilot did not complete; no comparison started')
config=json.loads((ROOT/'config_v0.1.json').read_text(encoding='utf-8'))
pilot=config['pilots'][0]
video_id=pilot['stream_id']
output=ROOT/'speech_checks'/f'{video_id}_intro_no_vad'
command=[sys.executable,'-m','media_platform','ingest',pilot['source_path'],'--start','0','--end','90',
         '--model','large-v3','--device','cuda','--compute-type','float16','--language','zh',
         '--chunk-seconds','300','--vad','off','--no-video','--output',str(output)]
if output.exists():
    command.append('--resume')
with (ROOT/'logs'/'intro_comparison.log').open('a',encoding='utf-8') as log:
    subprocess.run(command,stdout=log,stderr=log,check=True,timeout=600,
                   env={**os.environ,'PYTHONIOENCODING':'utf-8','HF_HUB_OFFLINE':'1'})
rows=json.loads((output/'speech.json').read_text(encoding='utf-8'))
result=dict(stream_id=video_id,reason='Baseline first recognized segment begins near 69.84s, initial audio contains non-silence',
            variant='Same model/precision, VAD off, only 0-90s',
            earlier_candidates=[s for s in rows if s['start']<69.84],
            status='requires_audio_review_if_new_text',baseline_preserved=True)
(ROOT/'intro_comparison.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
