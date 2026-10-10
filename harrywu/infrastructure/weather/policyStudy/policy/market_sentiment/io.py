"""Small deterministic IO helpers; no credentials or raw writes."""
import hashlib
import json
import os
from pathlib import Path
import pandas as pd

def sha256(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(8*1024*1024),b''):h.update(block)
    return h.hexdigest()

def read_csv(path):
    return pd.read_csv(path, float_precision='round_trip', dtype={'trade_date':str,'date':str})

def write_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    text=json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n'
    tmp=path.with_suffix(path.suffix+'.tmp');tmp.write_text(text,encoding='utf-8');os.replace(tmp,path)

def write_csv(path,frame):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp');frame.to_csv(tmp,index=False,encoding='utf-8');os.replace(tmp,path)
