"""Apply reviewed, context-scoped name corrections to the one completed pilot."""
from __future__ import annotations
import csv, hashlib, importlib.util, json, re, sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
sys.stdout.reconfigure(encoding='utf-8')
ROOT=Path(r'D:\YuncongVideoAnalysis\ledger_2026_v0.1\text_pilot_20261004_0804')
VOCAB=Path(r'D:\YuncongVideoAnalysis\ledger_2026_v0.1\planning\stock_vocabulary_v0.1_20261004')

def read_json(path):
 return json.loads(path.read_text(encoding='utf-8'))
def save_json(path,data):
 path.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
def sha(path):
 return hashlib.sha256(path.read_bytes()).hexdigest()
def write_csv(path,rows,fields):
 with path.open('w',encoding='utf-8-sig',newline='') as f:
  w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)

def main():
 original_paths=[ROOT/'asr/speech.json',ROOT/'全文_带时间.txt',ROOT/'reviewed_sections.json',ROOT/'reviewed_name_candidates.json']
 original_hashes={str(p):sha(p) for p in original_paths}
 spec=read_json(ROOT/'task.json')
 rows=read_json(ROOT/'asr/speech.json')
 original_sections=read_json(ROOT/'切分索引.json')
 with (VOCAB/'a_share_names.csv').open(encoding='utf-8-sig',newline='') as f:
  stock_rows=list(csv.DictReader(f))
 by_name={r['name']:r for r in stock_rows}
 with (ROOT/'股票名称校正候选.csv').open(encoding='utf-8-sig',newline='') as f:
  old_candidates=list(csv.DictReader(f))
 rules=[]
 for c in old_candidates:
  display='京东方' if c['raw_name']=='金东方' else c['candidate']
  # The candidate row identifies every source segment reviewed in the pilot.
  basis='同音错字与本场股票讨论上下文一致；标准名称匹配本地证券清单'
  if c['candidate'] in {'太辰光','普洛药业','恺英网络'}:
   basis='同场口述证券代码支持名称；对应段落上下文连续'
  if c['raw_name']=='金东方':
   basis='本场同时出现京东方，持仓问答上下文一致；只校公司称呼，未确定A/B股'
  rules.append({'raw':c['raw_name'],'replacement':display,'canonical':c['candidate'],
   'segment_ids':c['source_refs'].split('|'),'basis':basis,'security_class_confirmed':display!='京东方'})
 # Additional names found during this pass; replacement is limited to reviewed segment IDs.
 extras=[
  ('光讯','光迅科技',['s00081','s00306','s00431','s01596']),
  ('天赋','天孚通信',['s00359']),('天福','天孚通信',['s00341']),
  ('拥有软件','用友网络',['s00173']),
  ('拥有','用友网络',['s00706','s00709','s00713','s00935','s00936','s00937']),
  ('用有','用友',['s01339']),
  ('合建','中国核建',['s00166','s00167','s00175','s00176']),
  ('许记','许继电气',['s00984']),('许致','许继电气',['s00996']),
  ('四元','思源电气',['s00984','s00996']),
  ('光酷','光库科技',['s01256']),('中大利德','中大力德',['s00555']),
  ('丰范','风范股份',['s00549','s00550']),
  ('底龙股份','鼎龙股份',['s01446']),('新一盛','新易盛',['s01581']),
  ('华盛','华胜天成',['s01653']),('郑华','振华科技',['s01546']),
 ]
 for raw,display,ids in extras:
  canonical='用友网络' if display=='用友' else display
  rules.append({'raw':raw,'replacement':display,'canonical':canonical,'segment_ids':ids,
   'basis':'本场连续个股语境确认的简称或同音错字；名称匹配本地证券清单','security_class_confirmed':True})
 # Ambiguous or fused stock-name fragments remain explicit and local.
 pending=[
  {'raw':'素签联盟','segment_ids':['s00123'],'candidates':['宿迁联盛'],'reason':'同音候选，文本上下文不足以确认'},
  {'raw':'苏千联盟','segment_ids':['s00130'],'candidates':['宿迁联盛'],'reason':'同音候选，文本上下文不足以确认'},
  {'raw':'苏千联盛','segment_ids':['s00132'],'candidates':['宿迁联盛'],'reason':'同音候选，文本上下文不足以确认'},
  {'raw':'力通','segment_ids':['s00342'],'candidates':['利通电子','利通科技'],'reason':'简称可对应两只证券'},
  {'raw':'新疆时代','segment_ids':['s00721'],'candidates':[],'reason':'识别不清，无法确认是否为股票名称'},
  {'raw':'新云乐高,阳电,太阳,莲花','segment_ids':['s00727'],'candidates':[],'reason':'连续股票名或简称识别不清，保留片段'},
  {'raw':'诺布克特','segment_ids':['s00848'],'candidates':['罗博特科'],'reason':'名称音序不清，保留候选'},
  {'raw':'山东新密','segment_ids':['s01579'],'candidates':[],'reason':'名称识别不清，不能确定对应股票'},
  {'raw':'文艺招新','segment_ids':['s01595'],'candidates':[],'reason':'连续名称识别不清，不能确定拆分'},
  {'raw':'职业园','segment_ids':['s01597'],'candidates':[],'reason':'连续名称识别不清，不能确定拆分'},
 ]
 raw_by_id={r['id']:r for r in rows}
 by_segment={}
 for rule in rules:
  assert rule['canonical'] in by_name,rule
  for segment_id in rule['segment_ids']:
   assert rule['raw'] in raw_by_id[segment_id]['text'],(segment_id,rule)
   by_segment.setdefault(segment_id,{})[rule['raw']]=rule
 for p in pending:
  assert all(c in by_name for c in p['candidates'])
  for segment_id in p['segment_ids']:
   assert p['raw'] in raw_by_id[segment_id]['text'],(segment_id,p)
   assert p['raw'] not in by_segment.get(segment_id,{})
   by_segment.setdefault(segment_id,{})[p['raw']]={**p,'pending':True}
 # All edits use original-text matches in one pass, so replacements cannot cascade.
 changes=[];corrected=[]
 for r in rows:
  local=by_segment.get(r['id'],{})
  pattern=re.compile('|'.join(re.escape(s) for s in sorted(local,key=len,reverse=True))) if local else None
  def replace(m):
   rule=local[m.group(0)]
   if rule.get('pending'):
    candidates='；疑似'+ '/'.join(rule['candidates']) if rule['candidates'] else ''
    text=m.group(0)+'〔名称待确认'+candidates+'〕'
    status='待确认'
    codes='|'.join(by_name[c]['ts_code'] for c in rule['candidates'])
   else:
    text=rule['replacement'];status='已按文本语境校正'
    codes='|'.join(r['ts_code'] for r in stock_rows if r['name'].startswith('京东方')) if text=='京东方' else by_name[rule['canonical']]['ts_code']
   changes.append({'segment_id':r['id'],'start':r['start'],'end':r['end'],
    'raw_name':m.group(0),'corrected_name':text,'ts_code_candidates':codes,
    'status':status,'basis':rule.get('basis',rule.get('reason',''))})
   return text
  text=pattern.sub(replace,r['text']) if pattern else r['text']
  item={**r,'text':text}
  if text!=r['text']:item['raw_text']=r['text']
  corrected.append(item)
 # Same segment identities, dates, times, numbers, and all non-name source text.
 assert len(corrected)==len(rows)
 assert all(a['id']==b['id'] and a['start']==b['start'] and a['end']==b['end'] for a,b in zip(rows,corrected))
 assert all(re.findall(r'\d+(?:\.\d+)?',a['text'])==re.findall(r'\d+(?:\.\d+)?',b['text']) for a,b in zip(rows,corrected))
 module_spec=importlib.util.spec_from_file_location('pilot_layout',r'C:\Project Rocket\tmp\finalize_yuncong_text_pilot_20261004.py')
 layout=importlib.util.module_from_spec(module_spec);module_spec.loader.exec_module(layout)
 stamp=layout.stamp
 sections=json.loads(json.dumps(original_sections,ensure_ascii=False))
 note_updates={
  'p02':'能确认的股票同音错字已按本场语境校正；少数歧义名称在正文标待确认。止损价位等画面指代暂不补猜。',
  'p03':'中国核建、用友等股票名称已校正；京东方暂只写公司称呼。包含学员提问，不把提问本身记为本人买入。',
  'p06':'口述代码000739支持普洛药业名称；图上突破形态留待后续K线核对。',
  'p09':'口述代码300570支持太辰光名称；仅保留口述，不核委托、成交或K线画面。',
  'p16':'东方电气、中钨高新等股票同音错字已按语境校正；与后续本人回扫选股分开。',
  'p18':'振华科技名称已按文本上下文校正，未核图；保留本人评价。',
 }
 for s in sections:
  if s['section_id'] in note_updates:s['notes']=note_updates[s['section_id']]
 selected=[f"{spec['title']} — 按内容切分的研究文本（股票名称校正版）\n"
  '时间戳可回到原视频。本人操作、候选计划、学员问答按语境标注；未核实实际成交。\n'
  '股票名字的明显同音错字已结合本场语境、本地证券清单及可用口述代码校正；不清楚的名字标“名称待确认”。\n'
  '京东方只写公司称呼，暂不确定A/B股类别。原始转写完整保留，改动另有记录。\n'
  '与画面有关的锚点、指代股票或价位留待以后补看K线，不在这里补猜。\n']
 quick=['学员个股快评（股票名称校正版；含混合段，不计为本人买入）\n股票名称按本场语境校正；歧义名字标待确认。\n']
 noise=re.compile(r'请不吝点赞\s*订阅\s*转发\s*打赏支持明镜与点点栏目|优优独播剧场[—\-\s]*YoYo Television Series Exclusive|中文字幕志愿者\s*李宗盛')
 kept_ids=set()
 for s in sections:
  if not s['keep']:continue
  clean=[]
  for r in corrected:
   if s['start']<=(r['start']+r['end'])/2<s['end']:
    text=noise.sub('',r['text']).strip()
    if text:clean.append({**r,'text':text});kept_ids.add(r['id'])
  header=f"\n{s['section_id']} [{stamp(s['start'])}–{stamp(s['end'])}] {s['type']}：{s['label']}\n{s['notes']}\n"
  selected.append(header);selected.extend(layout.groups(clean))
  if s['type']=='学员个股快评':quick.append(header);quick.extend(layout.groups(clean))
 # Preserve the pre-correction research outputs without touching the ASR original.
 for name in ['研究文本_切分版.txt','学员个股快评.txt','切分索引.csv','切分索引.json']:
  backup=ROOT/'名称校正前'/name
  backup.parent.mkdir(exist_ok=True)
  if not backup.exists():backup.write_bytes((ROOT/name).read_bytes())
 (ROOT/'研究文本_切分版.txt').write_text('\n'.join(selected)+'\n',encoding='utf-8')
 (ROOT/'学员个股快评.txt').write_text('\n'.join(quick)+'\n',encoding='utf-8')
 full=[f"{spec['title']}\n场次：{spec['stream_id']}\n北京时间开播：{spec['actual_start_beijing']}\n完整时长：{stamp(spec['duration_seconds'])}\n时间戳均为源视频偏移。\n股票名称校正版；原始全文_带时间.txt保留。仅修改股票名称与增加名称待确认标记。\n"]
 full.extend(f"{r['id']} [{stamp(r['start'])}–{stamp(r['end'])}] {r['text']}" for r in corrected)
 (ROOT/'全文_带时间_股票名校正版.txt').write_text('\n'.join(full)+'\n',encoding='utf-8')
 save_json(ROOT/'speech_股票名校正版.json',corrected)
 save_json(ROOT/'切分索引.json',sections)
 flat=[{k:s[k] for k in ['section_id','start','end','keep','type','label','notes']} | {'start_time':stamp(s['start']),'end_time':stamp(s['end'])} for s in sections]
 write_csv(ROOT/'切分索引.csv',flat,['section_id','start','end','start_time','end_time','keep','type','label','notes'])
 for c in changes:c['start_time']=stamp(c['start']);c['end_time']=stamp(c['end'])
 write_csv(ROOT/'股票名称校正记录.csv',changes,['segment_id','start','end','start_time','end_time','raw_name','corrected_name','ts_code_candidates','status','basis'])
 save_json(ROOT/'已审核名称修改规则.json',{'source_stream_id':spec['stream_id'],'corrections':rules,'pending':pending})
 # Shared correction memory is a lookup overlay, never a global string-replacement rule.
 overlay_path=VOCAB/'转写名称校正经验.json'
 overlay=read_json(overlay_path) if overlay_path.exists() else {'automatic_replacement':False,'requires_security_context':True,'entries':[]}
 overlay['entries']=[e for e in overlay['entries'] if e.get('source_stream_id')!=spec['stream_id']]
 for rule in rules:
  if rule['replacement']=='京东方':
   codes=[r['ts_code'] for r in stock_rows if r['name'].startswith('京东方')]
  else:codes=[by_name[rule['canonical']]['ts_code']]
  overlay['entries'].append({'raw':rule['raw'],'display_name':rule['replacement'],'ts_code_candidates':codes,
   'source_stream_id':spec['stream_id'],'source_segment_ids':rule['segment_ids'],
   'status':'本场文本语境已校正；其他场次需结合上下文','basis':rule['basis'],'automatic_replacement':False})
 save_json(overlay_path,overlay)
 now=datetime.now(timezone.utc).isoformat()
 for p in original_paths:assert sha(p)==original_hashes[str(p)],p
 correct_changes=[c for c in changes if c['status']!='待确认']
 summary={'completed_utc':now,'source_stream_id':spec['stream_id'],'scope':'仅8月4日试跑的一期',
  'reviewed_correction_patterns':len(rules),'applied_name_replacements':len(correct_changes),
  'corrected_segments':len({c['segment_id'] for c in correct_changes}),
  'corrected_canonical_names':len({r['canonical'] for r in rules}),
  'pending_name_fragments':sum(c['status']=='待确认' for c in changes),
  'original_hashes_unchanged':original_hashes,
  'source_ids_and_timestamps_unchanged':True,'spoken_numbers_unchanged':True,
  'kept_nonempty_segments':len(kept_ids),'visual_processing':False,'next_video_started':False}
 save_json(ROOT/'名称校正结果.json',summary)
 report=read_json(ROOT/'timing_report.json');report['stock_name_correction']=summary
 save_json(ROOT/'timing_report.json',report)
 status=read_json(ROOT/'status.json');status['name_correction']='complete_with_pending_fragments';status['name_correction_completed_utc']=now
 save_json(ROOT/'status.json',status)
 result=ROOT/'试跑结果.txt'
 text=result.read_text(encoding='utf-8')
 text=text.replace('股票错字已提供数据库候选；图上股票归属和不清楚的同音名称仍待以后核实。',
  f"股票名称已按本场语境校正：{len(correct_changes)}处名称替换，{summary['pending_name_fragments']}处名称片段标待确认。原始转写保留。\n研究文本、学员快评及新增全文_带时间_股票名校正版.txt已使用校正名字；名称修改有逐处记录。\n以上21.64分钟为先前转写与切分的试跑计时，不包含本次后续名称校正。")
 result.write_text(text,encoding='utf-8')
 usage=VOCAB/'使用说明.txt'
 text=usage.read_text(encoding='utf-8')
 text=text.replace('云聪直播股票名称词表 v0.1（准备完成，尚未启动视频转写）','云聪直播股票名称词表 v0.1（已用于8月4日单期试跑）')
 text=text.replace('5. 本地词表只准备完毕：现有 media-platform 转写入口尚未传入 initial_prompt/hotwords。\n   启动转写前需要接入短提示；此次没有修改共用平台、旧配置或已完成的转写。',
  '5. 8月4日单期试跑已通过任务本地适配接入短提示；共用平台和旧配置没有修改。')
 if '转写名称校正经验.json' not in text:
  text+='\n转写名称校正经验.json：逐场累积已审核的识别错字和简称，供后续文本按语境匹配。\n其中保留本场来源和原始段落编号，不作为跨场自动替换规则；京东方暂保留A/B股候选。\n'
 usage.write_text(text,encoding='utf-8')
 manifest=read_json(VOCAB/'manifest.json')
 manifest.update(state='used_in_single_text_pilot',video_transcription_started=True)
 manifest['usage']={'single_stream_id':spec['stream_id'],'task_local_hotwords_connected':True,'shared_asr_configuration_changed':False,
  'name_correction_overlay_entries':len(overlay['entries']),'updated_utc':now}
 for name in ['使用说明.txt','转写名称校正经验.json']:
  p=VOCAB/name;manifest['files'][name]={'bytes':p.stat().st_size,'sha256':sha(p)}
 save_json(VOCAB/'manifest.json',manifest)
 print(json.dumps(summary,ensure_ascii=False))

if __name__=='__main__':main()
