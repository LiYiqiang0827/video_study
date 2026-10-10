"""Bounded scene timestamp selection with FFmpeg and existing platform frame CLI."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timezone
from media_platform import frame_review

ROOT = Path(__file__).resolve().parents[1]

def save(state):
    path = ROOT / 'scene_status.json'
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding='utf-8')
    os.replace(tmp,path)

def main():
    config=json.loads((ROOT/'config_v0.1.json').read_text(encoding='utf-8'))
    state=dict(status='running',runner_pid=os.getpid(),completed=[],current=None,
               selection='all decoded frames, 320px scene metric, threshold 0.04',
               started=datetime.now(timezone.utc).isoformat())
    save(state)
    for pilot in config['pilots']:
        video_id=pilot['stream_id']
        if (ROOT/'STOP_SCENES').exists():
            state['status']='stopped_before_next_stream'
            save(state)
            return
        state.update(current=video_id,phase='scene_scan')
        save(state)
        result=subprocess.run(['ffmpeg','-nostdin','-hide_banner','-loglevel','info',
            '-threads','2','-i',pilot['source_path'],'-map','0:v:0','-an',
            '-filter_threads','1','-vf',"scale=320:-2,select='gt(scene,0.04)',showinfo",
            '-frames:v','6001','-fps_mode','vfr','-f','null','-'],
            capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=600)
        (ROOT/'logs'/f'{video_id}.scene_scan.log').write_text(result.stderr,encoding='utf-8')
        if result.returncode:
            raise RuntimeError(f'Scene scan failed: {video_id}')
        points=sorted(set(float(t) for t in re.findall(r'showinfo[^\n]*?pts_time:([-\d.]+)',result.stderr)))
        if len(points)>=6000:
            raise RuntimeError(f'Scene limit reached; coverage not complete: {video_id}')
        points=[t for t in points if 0<=t<float(pilot['duration_seconds'])]
        (ROOT/'metadata'/f'{video_id}.scene_points.json').write_text(json.dumps(dict(
            stream_id=video_id,threshold=0.04,metric_size=320,points=points,
            limitation='Scene threshold detects large visual changes; speech-driven detail review remains required'),indent=2),encoding='utf-8')
        if points:
            output=ROOT/'frames'/video_id/'scene_changes'
            state.update(phase='original_frame_extraction',scene_count=len(points))
            save(state)
            sys.argv=['media_platform frames',pilot['source_path'],'--times',','.join(str(t) for t in points),
                      '--max-frames','6000','--output',str(output)]
            if output.exists():
                sys.argv.append('--resume')
            frame_review.main()
        state['completed'].append(dict(stream_id=video_id,scene_frames=len(points)))
        save(state)
    state.update(status='complete',current=None,ended=datetime.now(timezone.utc).isoformat())
    save(state)

if __name__=='__main__':
    try:
        main()
    except BaseException as exc:
        path=ROOT/'scene_status.json'
        state=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        state.update(status='failed',error=f'{type(exc).__name__}: {exc}')
        save(state)
        raise
