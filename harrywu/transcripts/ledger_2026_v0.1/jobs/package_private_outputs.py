import csv
import hashlib
import json
from pathlib import Path
import zipfile

ROOT=Path(__file__).resolve().parents[1]
TARGET=ROOT/'cloud_sync'
TARGET.mkdir(exist_ok=True)
LIMIT=32*1024*1024

def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(4*1024*1024),b''):
            h.update(block)
    return h.hexdigest()

checkpoint=ROOT/'TECHNICAL_SHA256SUMS'
references=[line.split('  ',1)[1] for line in checkpoint.read_text(encoding='utf-8').splitlines() if '  ' in line]
references+=['先看这里.md','语音画面核对样例.md']
references=sorted(set(references))
if any(not (ROOT/reference).resolve().is_relative_to(ROOT.resolve()) for reference in references):
    raise ValueError('Checkpoint path outside project')
checkpoint.write_text('\n'.join(f"{sha(ROOT/reference)}  {reference}" for reference in references)+'\n',encoding='utf-8')

files=sorted(p for p in ROOT.rglob('*') if p.is_file() and 'cloud_sync' not in p.relative_to(ROOT).parts)
if any(p.is_symlink() or not p.resolve().is_relative_to(ROOT.resolve()) for p in files):
    raise ValueError('Package input outside owned project')
groups=[]
group=[]
size=0
records=[]
for p in files:
    length=p.stat().st_size
    if length>LIMIT:
        raise ValueError('Single file exceeds package bound')
    if group and size+length>LIMIT-131072:
        groups.append(group)
        group=[]
        size=0
    group.append(p)
    size+=length
    records.append(dict(path=p.relative_to(ROOT).as_posix(),bytes=length,sha256=sha(p)))
if group:
    groups.append(group)
parts=[]
for i,group in enumerate(groups,1):
    path=TARGET/f'pilot_evidence_{i:03d}_of_{len(groups):03d}.zip'
    with zipfile.ZipFile(path,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=1) as archive:
        for p in group:
            archive.write(p,p.relative_to(ROOT).as_posix())
    with zipfile.ZipFile(path) as archive:
        if archive.testzip() is not None or len(archive.namelist())!=len(group):
            raise ValueError('Archive integrity failed')
    parts.append(dict(path=str(path),name=path.name,bytes=path.stat().st_size,sha256=sha(path),files=len(group)))
manifest=dict(scope='Three pilot derived artifacts only; source MP4 inputs stay local',
              project=str(ROOT),files=records,parts=parts,total_files=len(records),total_bytes=sum(x['bytes'] for x in parts))
(TARGET/'backup_manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
(TARGET/'BACKUP_SHA256SUMS').write_text('\n'.join(f"{p['sha256']}  {p['name']}" for p in parts)+'\n',encoding='utf-8')
print(json.dumps(dict(parts=len(parts),files=len(records),mb=round(manifest['total_bytes']/1048576,1)),ensure_ascii=False))
