"""Assemble one primary-reviewed transcript; originals are never mutated."""
from __future__ import annotations
import csv,hashlib,json,re,sys
from datetime import datetime,timezone
from pathlib import Path
sys.stdout.reconfigure(encoding='utf-8')
ROOT=Path(r'D:\YuncongVideoAnalysis\ledger_2026_v0.1')
BATCH=ROOT/'text_priority_batch_20261005';VOCAB=ROOT/'planning/stock_vocabulary_v0.1_20261004'
NOISE=re.compile(r'请不吝点赞\s*(?:订阅\s*)?转发\s*打赏支持明镜与点点栏目|明镜需要您的支持\s*欢迎订阅明镜|优优独播剧场[—\-\s]*YoYo Television Series Exclusive|中文字幕志愿者\s*(?:李宗盛|杨栋梁)|中文字幕由\s*Amara[.]org\s*社群提供|中文字幕[—\-\s]+YK|中文字幕提供')
def read(p):return json.loads(p.read_text(encoding='utf-8'))
def save(p,v):p.write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def stamp(t):
 t=int(t);return f'{t//3600:02}:{t//60%60:02}:{t%60:02}'
def csv_write(p,rows,fields):
 with p.open('w',encoding='utf-8-sig',newline='') as f:
  w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)
def group(rows):
 result=[];current=[]
 for r in rows:
  if current and (r['start']-current[0]['start']>=35 or r['start']-current[-1]['end']>10):
   result.append(f"[{stamp(current[0]['start'])}–{stamp(current[-1]['end'])}] "+''.join(x['text'] for x in current));current=[]
  current.append(r)
 if current:result.append(f"[{stamp(current[0]['start'])}–{stamp(current[-1]['end'])}] "+''.join(x['text'] for x in current))
 return result
def main(sid,preview=False):
 job=Path(next(j['job_directory'] for j in read(BATCH/'batch.json')['jobs'] if j['stream_id']==sid))
 spec=read(job/'task.json');status=read(job/'status.json');review=read(job/'review.json');source=Path(spec['asr_directory'])/'speech.json'
 assert status['asr'] in ['complete','reused'] and not spec['text_already_complete']
 original_hash=sha(source);rows=read(source)
 with (VOCAB/'a_share_names.csv').open(encoding='utf-8-sig',newline='') as f:stocks=list(csv.DictReader(f))
 names={r['name']:r for r in stocks};codes={r['ts_code']:r for r in stocks}
 with (VOCAB/'us_common_names.csv').open(encoding='utf-8-sig',newline='') as f:us=list(csv.DictReader(f))
 for r in us:
  target={'name':r['name_zh'],'ts_code':r['code']};codes[r['code']]=target
  if sum(u['name_zh']==r['name_zh'] for u in us)==1:names[r['name_zh']]=target
 rules=[]
 for entry in review.get('names',[]):
  target=names.get(entry['canonical']) if 'canonical' in entry else codes[entry['code']]
  assert target,entry
  for raw in entry['raw']:
   rules.append({'raw':raw,'display':entry.get('display',target['name']),'canonical':target['name'],'code':target['ts_code'],
    'start':entry.get('start',0),'end':entry.get('end',spec['duration_seconds']),
    'numeric_name_exception':entry.get('numeric_name_exception',False),
    'basis':entry.get('basis','本场同音错字与连续股票语境确认；标准名匹配本地证券词表')})
 for term in review.get('terms',[]):
  for raw in term['raw']:
   rules.append({'raw':raw,'display':term['display'],'canonical':term['display'],'code':'',
    'start':term.get('start',0),'end':term.get('end',spec['duration_seconds']),
    'basis':term.get('basis','本场上下文明确的题材或交易术语同音识别校正')})
 pending=review.get('pending',[])
 for p in pending:
  for c in p.get('candidates',[]):assert c in names or c in codes,c
 if preview:
  for rule in rules:
   hits=[r for r in rows if rule['raw'] in r['text'] and rule['start']<=r['start']<rule['end']]
   if hits:print(json.dumps({'raw':rule['raw'],'target':rule['display'],'hits':len(hits),'examples':[{'id':r['id'],'at':stamp(r['start']),'text':r['text']} for r in hits[:2]]},ensure_ascii=False))
  return
 assert review['read_coverage_seconds']==[0,spec['duration_seconds']], 'Primary review is not complete'
 assert all(s['type']!='待完成审核' for s in review['sections'])
 changes=[];corrected=[];numeric_name_edits={}
 for r in rows:
  local={rule['raw']:rule for rule in rules if rule['start']<=r['start']<rule['end'] and rule['raw'] in r['text']}
  for p in pending:
   if p.get('start',0)<=r['start']<p.get('end',spec['duration_seconds']) and p['raw'] in r['text']:
    assert p['raw'] not in local,p
    local[p['raw']]={**p,'pending':True}
  def replacement(m):
   v=local[m.group()]
   if v.get('pending'):
    candidate='；疑似'+'/'.join(v['candidates']) if v.get('candidates') else ''
    value=m.group()+'〔名称待确认'+candidate+'〕';code='';kind='待确认';basis=v.get('reason','文本信息不足，暂不补猜')
   else:
    value=v['display'];code=v['code'];kind='已按文本语境校正' if code else '术语校正';basis=v['basis']
    if v.get('numeric_name_exception') and re.findall(r'\d+',m.group())!=re.findall(r'\d+',value):
     assert code and any(ch.isdigit() for ch in m.group())
     numeric_name_edits.setdefault(r['id'],[]).append({'raw':m.group(),'display':value})
   changes.append({'segment_id':r['id'],'start':r['start'],'end':r['end'],'time':stamp(r['start']),
    'raw_name':m.group(),'corrected_name':value,'ts_code_candidate':code,'status':kind,'basis':basis})
   return value
  pattern=re.compile('|'.join(re.escape(s) for s in sorted(local,key=len,reverse=True))) if local else None
  text=pattern.sub(replacement,r['text']) if pattern else r['text'];new={**r,'text':text}
  if text!=r['text']:new['raw_text']=r['text']
  corrected.append(new)
 assert len(rows)==len(corrected)
 assert all((a['id'],a['start'],a['end'])==(b['id'],b['start'],b['end']) for a,b in zip(rows,corrected))
 for a,b in zip(rows,corrected):
  before=a['text'];after=b['text']
  for exception in numeric_name_edits.get(a['id'],[]):
   before=before.replace(exception['raw'],'<stock-name>');after=after.replace(exception['display'],'<stock-name>')
  assert re.findall(r'\d+(?:\.\d+)?',before)==re.findall(r'\d+(?:\.\d+)?',after),(a['id'],before,after)
 save(job/'股票名数字识别例外.json',numeric_name_edits)
 sections=[];left=0.0
 for i,s in enumerate(review['sections'],1):
  right=spec['duration_seconds'] if s['end']=='END' else float(s['end'])
  assert left<right<=spec['duration_seconds']
  selected=[r for r in rows if left<=(r['start']+r['end'])/2<right]
  sections.append({'section_id':f'p{i:02}','start':left,'end':right,'start_time':stamp(left),'end_time':stamp(right),
   'keep':s['keep'],'type':s['type'],'label':s['label'],'notes':s.get('notes',''),
   'source_segment_ids':[r['id'] for r in selected]});left=right
 assert abs(left-spec['duration_seconds'])<.001
 caveat='股票名字按本场语境、本地证券词表及可用口述代码校正；不清楚的名字标“名称待确认”。本人操作、计划、学员快评按语境区分；未核实实际成交。图上指代和具体K线以后再看，不在这里补猜。'
 research=[f"{spec['title']} — 按内容切分的研究文本（股票名称校正版）\n{caveat}\n时间戳为原视频偏移，原始转写完整保留。\n"]
 quick=[f"{spec['title']} — 学员个股快评（股票名称校正版；含混合段）\n{caveat}\n"]
 noise_rows=[];kept_ids=set()
 for s in sections:
  if not s['keep']:continue
  clean=[]
  for r in corrected:
   if s['start']<=(r['start']+r['end'])/2<s['end']:
    text=NOISE.sub('',r['text']).strip()
    if text!=r['text']:noise_rows.append(r['id'])
    if text:clean.append({**r,'text':text});kept_ids.add(r['id'])
  header=f"\n{s['section_id']} [{s['start_time']}–{s['end_time']}] {s['type']}：{s['label']}\n{s['notes']}\n"
  research.append(header);research.extend(group(clean))
  if s['type']=='学员个股快评':quick.append(header);quick.extend(group(clean))
 def full(data,corrected_flag):
  header=f"{spec['title']}\n场次：{sid}\n北京时间开播：{spec['actual_start_beijing']}\n完整时长：{stamp(spec['duration_seconds'])}\n时间戳为原视频偏移。\n"+('股票名称校正版；原始稿另存。\n' if corrected_flag else '原始ASR：保留原识别内容和错字。\n')
  return header+'\n'.join(f"{r['id']} [{stamp(r['start'])}–{stamp(r['end'])}] {r['text']}" for r in data)+'\n'
 (job/'研究文本_切分版.txt').write_text('\n'.join(research)+'\n',encoding='utf-8')
 (job/'学员个股快评.txt').write_text('\n'.join(quick)+'\n',encoding='utf-8')
 (job/'全文_带时间.txt').write_text(full(rows,False),encoding='utf-8')
 (job/'全文_带时间_股票名校正版.txt').write_text(full(corrected,True),encoding='utf-8')
 save(job/'speech_股票名校正版.json',corrected);save(job/'切分索引.json',sections)
 csv_write(job/'切分索引.csv',[{k:s[k] for k in ['section_id','start','end','start_time','end_time','keep','type','label','notes']} for s in sections],
  ['section_id','start','end','start_time','end_time','keep','type','label','notes'])
 csv_write(job/'股票名称校正记录.csv',changes,['segment_id','start','end','time','raw_name','corrected_name','ts_code_candidate','status','basis'])
 save(job/'无效识别候选.json',{'source_segment_ids':noise_rows,'reason':'研究文本去除固定重复广告识别；原始全文保留'})
 assert sha(source)==original_hash
 accepted=[c for c in changes if c['status']=='已按文本语境校正'];pending_count=sum(c['status']=='待确认' for c in changes)
 term_count=sum(c['status']=='术语校正' for c in changes)
 retained=sum(s['end']-s['start'] for s in sections if s['keep'])
 report=read(job/'timing_report.json') if (job/'timing_report.json').exists() else {'asr_reused':True}
 report.update(stream_id=sid,original_segments=len(rows),retained_nonempty_segments=len(kept_ids),sections=len(sections),
  retained_source_seconds=round(retained,3),stock_name_replacements=len(accepted),term_replacements=term_count,pending_name_occurrences=pending_count,
  semantic_reviewer='current_codex_primary',text_stage='complete',text_completed_utc=datetime.now(timezone.utc).isoformat(),
  checks={'source_asr_hash_unchanged':True,'source_timestamps_and_ids_unchanged':True,'spoken_numbers_unchanged':not bool(numeric_name_edits),
   'numbers_outside_approved_stock_name_aliases_unchanged':True,'approved_numeric_name_segments':len(numeric_name_edits),
   'section_coverage_complete':True,'visual_processing':False},original_asr_sha256=original_hash)
 save(job/'timing_report.json',report)
 timing='复用已有转写，本批未重新转写' if spec['asr_reused'] else f"本地转写耗时：{report['asr_wall_seconds']/60:.2f}分钟"
 (job/'试跑结果.txt').write_text(f"{spec['title_date']} 盘中直播文本处理结果\n\n完整视频：{spec['duration_seconds']/60:.2f}分钟\n{timing}\n"
  f"研究文本保留源时段：{retained/60:.2f}分钟\n股票名称校正：{len(accepted)}处；待确认名称：{pending_count}处\n明确术语校正：{term_count}处\n"
  '文件：研究文本、学员个股快评、原始全文与名称校正版全文、切分索引、逐处名称修改记录。\n'
  '仅处理文本与时间，不核图、K线、委托或成交。原始转写保留。\n'+
  ('本场末尾有固定广告识别污染且时间超出视频；原始稿保留，研究版剔除，其他语音时间已通过校验。\n' if report.get('asr_noise_exception') else ''),encoding='utf-8')
 status=read(job/'status.json');status.update(text='complete',text_completed_utc=report['text_completed_utc'],stock_name_replacements=len(accepted),pending_names=pending_count)
 save(job/'status.json',status)
 overlay_path=VOCAB/'转写名称校正经验.json';overlay=read(overlay_path)
 overlay['entries']=[e for e in overlay['entries'] if e.get('source_stream_id')!=sid]
 for rule in rules:
  if not rule['code']:continue
  hits=[c for c in accepted if c['raw_name']==rule['raw'] and c['corrected_name']==rule['display']]
  if hits:overlay['entries'].append({'raw':rule['raw'],'display_name':rule['display'],'ts_code_candidates':[rule['code']],
   'source_stream_id':sid,'source_segment_ids':list(dict.fromkeys(c['segment_id'] for c in hits)),
   'status':'本场已按语境校正；其他场次须结合语境','basis':rule['basis'],'automatic_replacement':False})
 save(overlay_path,overlay)
 manifest=read(VOCAB/'manifest.json');manifest['files']['转写名称校正经验.json']={'bytes':overlay_path.stat().st_size,'sha256':sha(overlay_path)}
 manifest['usage']['name_correction_overlay_entries']=len(overlay['entries']);save(VOCAB/'manifest.json',manifest)
 print(json.dumps({k:report[k] for k in ['stream_id','sections','retained_source_seconds','stock_name_replacements','pending_name_occurrences','text_stage']},ensure_ascii=False))
if __name__=='__main__':main(sys.argv[1],'--preview' in sys.argv)
