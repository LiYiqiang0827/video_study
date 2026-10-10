import json,subprocess,sys,os
from pathlib import Path
ROOT=Path(r'C:\Project Rocket\output\feiyun_double_breakout')
env={**os.environ,'PYTHONPATH':r'C:\Users\HarryWu_IOKI\Documents\推し活','PYTHONIOENCODING':'utf-8'}
for extraction in sorted((ROOT/'platform_large_v3_v1').iterdir()):
    if not extraction.is_dir():continue
    m=json.loads((extraction/'manifest.json').read_text(encoding='utf-8'))
    assert m['steps']['evidence']=='complete'
    dirs=[ROOT/'review_frames_v1'/Path(m['source']).stem]
    dirs+=list((ROOT/'targeted_frames').glob('*'))
    for vr in dirs:
        fm=json.loads((vr/'frame_manifest.json').read_text(encoding='utf-8'))
        if fm['params']['source_sha256']!=m['source_sha256']:continue
        cmd=[sys.executable,'-u','-m','media_platform.frame_review',m['source'],'--output',str(vr),'--resume','--extraction',str(extraction)]
        if vr.parent.name=='targeted_frames':cmd+=['--times',','.join(map(str,fm['params']['times']))]
        subprocess.run(cmd,check=True,env=env,timeout=180)
