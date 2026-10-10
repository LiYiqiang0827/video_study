"""Verify source identity, complete chunk coverage, evidence hashes and all review frames."""
from pathlib import Path
import json, sys
from datetime import datetime, timezone
from PIL import Image
sys.path.insert(0,r'C:\Users\HarryWu_IOKI\Documents\推し活')
from media_platform.evidence import load_evidence,sha256_file

ROOT=Path(r'C:\Project Rocket\output\feiyun_double_breakout')
SOURCE=Path(r'C:\Users\HarryWu_IOKI\Videos\飞云双突破\For GPT')
def read(p): return json.loads(p.read_text(encoding='utf-8'))
rows=[]
for d in sorted((ROOT/'platform_large_v3_v1').iterdir()):
    if not d.is_dir(): continue
    m=read(d/'manifest.json')
    assert m['steps']['audio']==m['steps']['evidence']=='complete',d
    assert m['params']['language']=='zh' and m['params']['model']=='large-v3'
    source=Path(m['source'])
    assert source.parent.resolve()==SOURCE.resolve()
    assert source.stat().st_size==m['source_bytes']
    assert sha256_file(source)==m['source_sha256'],source
    bundle=load_evidence(d)
    assert bundle['speech_segments']
    last=0
    for c in m['chunks']:
        cp=d/'chunks'/f"{c['index']:05d}.json"
        assert sha256_file(cp)==c['sha256']
        chunk=read(cp)
        assert abs(chunk['begin']-last)<.05, (d,last,chunk['begin'])
        last=chunk['finish']
        assert c['device']=='cuda'
    assert abs(last-m['duration'])<.05
    segments=bundle['speech_segments']
    assert all(a['start']<=b['start'] for a,b in zip(segments,segments[1:]))
    row={'video':source.name,'duration_seconds':m['duration'],'source_sha256':m['source_sha256'],
        'source_unchanged':True,'audio_coverage_complete':True,'evidence_hashes_passed':True,
        'chunks':len(m['chunks']),'segments':len(segments),'gpu_chunks':len(m['chunks']),
        'model_review_flags':sum(s['review_required'] for s in segments),
        'frame_sets':[]}
    for vr in [ROOT/'review_frames_v1'/source.stem,*list((ROOT/'targeted_frames').glob('*'))]:
        if not vr.is_dir():continue
        fm=read(vr/'frame_manifest.json')
        if fm['params']['source_sha256']!=m['source_sha256']:continue
        assert fm['status']=='complete'
        assert len(fm['frames'])==len(fm['params']['times'])
        for f in fm['frames']:
            assert f['requested_time']-.1<=f['time']<=f['requested_time']+1
            path=vr/f['frame']
            assert sha256_file(path)==f['sha256']
            with Image.open(path) as im: assert im.size==(1920,1080),path
        row['frame_sets'].append({'directory':vr.relative_to(ROOT).as_posix(),'frames':len(fm['frames']),
            'sha256_and_timestamps_passed':True})
    assert row['frame_sets']
    rows.append(row)
assert len(rows)==5
assert {r['video'] for r in rows}=={p.name for p in SOURCE.glob('*.mkv')}
result={'passed':True,'checked_at':datetime.now(timezone.utc).isoformat(),'videos':rows,
    'total_duration_seconds':sum(r['duration_seconds'] for r in rows),
    'total_frames':sum(f['frames'] for r in rows for f in r['frame_sets']),
    'limits':['Full audio processing is not proof of verbatim accuracy.',
        'Visual review uses sampled and targeted original frames, not every source frame.',
        'Interpretation is source-based synthesis, not validation of profitability or asserted probabilities.']}
(ROOT/'acceptance.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({k:v for k,v in result.items() if k!='videos'},ensure_ascii=False))
