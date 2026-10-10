import csv
import html
import json
import math
import re
from datetime import datetime,timedelta,timezone
from pathlib import Path
import psutil
from media_platform.evidence import sha256_file,load_evidence

ROOT=Path(__file__).resolve().parents[1]

def wait_owned(pid,script):
    try:
        process=psutil.Process(pid)
        if script not in ' '.join(process.cmdline()) or str(ROOT/'jobs').casefold() not in ' '.join(process.cmdline()).casefold():
            raise RuntimeError('Dependency PID identity mismatch')
        process.wait(timeout=14400)
    except psutil.NoSuchProcess:
        pass

wait_owned(3752,'finalize_technical.py')
wait_owned(12548,'header_ocr.py')
technical=json.loads((ROOT/'technical_status.json').read_text(encoding='utf-8'))
ocr_state=json.loads((ROOT/'header_ocr_status.json').read_text(encoding='utf-8'))
if technical['status']!='technical_complete_semantic_pilot_pending' or ocr_state['status']!='complete':
    raise RuntimeError('Required technical evidence is not complete')
config=json.loads((ROOT/'config_v0.1.json').read_text(encoding='utf-8'))
with (ROOT/'streams_inventory.csv').open(encoding='utf-8-sig',newline='') as f:
    inventory={r['stream_id']:r for r in csv.DictReader(f)}
checks=[]
for pilot in config['pilots']:
    video_id=pilot['stream_id']
    extraction=ROOT/'extractions'/video_id
    manifest=json.loads((extraction/'manifest.json').read_text(encoding='utf-8'))
    source=Path(pilot['source_path'])
    source_hash=sha256_file(source)
    if source_hash!=manifest['source_sha256'] or source_hash!=inventory[video_id]['source_sha256']:
        raise RuntimeError('Current source hash differs from frozen inventory')
    params=manifest['params']
    required=dict(model='large-v3',device='cuda',compute_type='float16',language='zh',chunk_seconds=300,start=0.0)
    if any(params[k]!=v for k,v in required.items()):
        raise RuntimeError('ASR parameters differ')
    expected=math.ceil(manifest['duration']/300)
    if sorted(c['index'] for c in manifest['chunks'])!=list(range(expected)):
        raise RuntimeError('Chunk coverage differs')
    for c in manifest['chunks']:
        path=extraction/'chunks'/f"{c['index']:05d}.json"
        if sha256_file(path)!=c['sha256'] or c['device']!='cuda' or c['compute_type']!='float16':
            raise RuntimeError('Chunk hash or precision mismatch')
        chunk=json.loads(path.read_text(encoding='utf-8'))
        if chunk['begin']!=c['index']*300 or chunk['finish']!=min((c['index']+1)*300,manifest['duration']):
            raise RuntimeError('Chunk boundary differs')
    load_evidence(extraction)
    base=ROOT/'frames'/video_id
    frames=json.loads((base/'review_manifest.json').read_text(encoding='utf-8'))['frames']
    title_data=json.loads((ROOT/'ocr'/f'{video_id}.title_recognition.json').read_text(encoding='utf-8'))
    titles={str(Path(r['frame']).resolve()):r for r in title_data['frames']}
    if len(title_data['frames'])!=len(frames):
        raise RuntimeError('OCR and review frame coverage differ')
    started=datetime.fromisoformat(inventory[video_id]['actual_start_beijing'])
    content=(base/'index.html').read_text(encoding='utf-8')
    number=0
    def attach(match):
        global number
        frame=frames[number]
        number+=1
        data=titles[str((base/frame['frame']).resolve())]
        if data['frame_sha256']!=frame['sha256'] or abs(data['time']-frame['time'])>.001:
            raise RuntimeError('OCR/frame evidence differs')
        text=' / '.join(t['text'] for t in data['ocr'])
        wall=(started+timedelta(seconds=frame['time'])).strftime('%Y-%m-%d %H:%M:%S')
        extra=f'<p>候选北京时间（开播＋偏移，屏幕时钟待核）：{wall}</p>'
        extra+=f'<p>主标题区域OCR候选（需先核对画面布局）：{html.escape(text) if text else "未识别到文字"}</p>'
        return match.group(0)+extra
    content=re.sub(r'<article><h2>.*?</h2>',attach,content)
    if number!=len(frames):
        raise RuntimeError('Viewer frame count differs')
    (base/'index.html').write_text(content,encoding='utf-8')
    checks.append(dict(stream_id=video_id,source_hash_verified=True,chunks=expected,
                       frame_ocr_associations=len(frames),viewer_sha256=sha256_file(base/'index.html')))
report=dict(status='passed',checked=datetime.now(timezone.utc).isoformat(),checks=checks,
            acceptance_scope='Technical source/chunk/frame binding only; event extraction and human accuracy acceptance remain pending')
(ROOT/'technical_acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
qa=ROOT/'qa_report.md'
qa.write_text(qa.read_text(encoding='utf-8')+'\n\n## 时间轴与OCR绑定复核\n\n三场原视频当前哈希与初始清单一致，全部原始分块的哈希、边界及CUDA/float16参数复核通过。每张定时/变化帧已绑定主标题区域OCR与候选北京时间，画面、OCR和原始语音仍分别保留。固定区域在其他软件布局中可能读到无关文字，未自动接受为证券代码。价格、持仓表与完整事件核对仍待完成。\n',encoding='utf-8')
files=[ROOT/'config_v0.1.json',ROOT/'rules_v0.1.md',ROOT/'streams_inventory.csv',ROOT/'source_SHA256SUMS',
       ROOT/'qa_report.md',ROOT/'speech_review_candidates.csv',ROOT/'technical_status.json',ROOT/'technical_acceptance.json',ROOT/'runtime_manifest.json']
files+=list((ROOT/'transcripts').glob('*.md'))
files+=[ROOT/'frames'/p['stream_id']/'index.html' for p in config['pilots']]
(ROOT/'TECHNICAL_SHA256SUMS').write_text('\n'.join(f"{sha256_file(p)}  {p.relative_to(ROOT).as_posix()}" for p in files)+'\n',encoding='utf-8')
print(json.dumps(report,ensure_ascii=False))
