import csv,json,sys
from pathlib import Path
sys.stdout.reconfigure(encoding='utf-8')
root=Path(r'D:\YuncongVideoAnalysis\ledger_2026_v0.1\text_pilot_20261004_0804')
vocab=Path(r'D:\YuncongVideoAnalysis\ledger_2026_v0.1\planning\stock_vocabulary_v0.1_20261004')
rows=json.loads((root/'asr/speech.json').read_text(encoding='utf-8'))
need=['光讯','天赋','天福','拥有','用有','合建','许记','许致','四元','光酷','中大利德','丰范','底龙股份','新一盛','诺布克特','华盛','山东新密','文艺招新','新云乐高','新疆时代','素签联盟','苏千联盟','苏千联盛','郑华']
for key in need:
 print('\n'+key)
 for i,r in enumerate(rows):
  if key in r['text']:
   print(json.dumps({'id':r['id'],'start':r['start'],'text':r['text'],'context':''.join(x['text'] for x in rows[max(0,i-1):i+2])},ensure_ascii=False))
with (vocab/'a_share_names.csv').open(encoding='utf-8-sig',newline='') as f:
 names=list(csv.DictReader(f))
for key in ['中大力德','风范股份','鼎龙股份','罗博特科','光库科技','宿迁联盛','利通','天孚通信','立讯精密']:
 print('\nDB '+key+': '+json.dumps([{'name':r['name'],'ts_code':r['ts_code']} for r in names if key in r['name']],ensure_ascii=False))
