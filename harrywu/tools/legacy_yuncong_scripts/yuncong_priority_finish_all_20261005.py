"""Final acceptance and a compact user index for the frozen priority-20 batch."""
import csv,hashlib,json,sys
from datetime import datetime,timezone
from pathlib import Path
sys.stdout.reconfigure(encoding='utf-8')
B=Path(r'D:\YuncongVideoAnalysis\ledger_2026_v0.1\text_priority_batch_20261005')
FILES=['研究文本_切分版.txt','学员个股快评.txt','全文_带时间_股票名校正版.txt','全文_带时间.txt','股票名称校正记录.csv','切分索引.csv','试跑结果.txt']
def read(p):return json.loads(p.read_text(encoding='utf-8'))
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def save(p,x):p.write_text(json.dumps(x,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
def write_csv(p,rows,fields):
 with p.open('w',encoding='utf-8-sig',newline='') as f:
  w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)
def main(partial=False):
 batch=read(B/'batch.json');assert len(batch['jobs'])==20
 records=[];checks=[];pending=[];cloud_checks=[];complete_jobs=[];remaining=[]
 for j in batch['jobs']:
  job=Path(j['job_directory']);spec=read(job/'task.json');status=read(job/'status.json')
  ready=status['asr'] in ['complete','reused'] and status['text']=='complete' and status['upload']=='complete'
  if not ready:
   assert partial,j['stream_id'];remaining.append({**j,'title':spec['title'],'source_minutes':round(spec['duration_seconds']/60,2),'status':{k:status[k] for k in ['asr','text','upload']}});continue
  complete_jobs.append(j)
  out=Path(spec['completed_output_directory']) if spec['text_already_complete'] else job
  report=read(out/'timing_report.json');asr=Path(spec['asr_directory']);rows=read(asr/'speech.json');corrected=read(out/'speech_股票名校正版.json')
  assert read(asr/'manifest.json')['source_sha256']==spec['source_sha256']
  assert len(rows)==len(corrected) and len(set(r['id'] for r in rows))==len(rows)
  assert all((r['id'],r['start'],r['end'])==(c['id'],c['start'],c['end']) for r,c in zip(rows,corrected))
  original_hash=report.get('original_asr_sha256')
  if spec['text_already_complete']:
   original_hash=report['stock_name_correction']['original_hashes_unchanged'][str(asr/'speech.json')]
  assert sha(asr/'speech.json')==original_hash
  sections=read(out/'切分索引.json')
  assert abs(sections[0]['start'])<.001 and abs(sections[-1]['end']-spec['duration_seconds'])<.001
  assert all(abs(a['end']-b['start'])<.001 for a,b in zip(sections,sections[1:]))
  assert all(x['start']<x['end'] for x in sections)
  if not spec['text_already_complete']:
   review=read(job/'review.json');assert review['read_coverage_seconds']==[0,spec['duration_seconds']]
   assert report['checks'].get('numbers_outside_approved_stock_name_aliases_unchanged',report['checks']['spoken_numbers_unchanged'])
  else:assert report['stock_name_correction']['spoken_numbers_unchanged']
  receipt=read(out/'谷歌云盘上传记录.json')
  assert len(receipt['files'])==7
  assert receipt.get('verified',False) or receipt.get('verified_file_count')==7
  folder=receipt.get('folder',{}).get('url') or receipt.get('folder_url') or receipt['url']
  file_map={f.get('local_name',f.get('local_file')):f for f in receipt['files']}
  assert set(file_map)==set(FILES)
  for fn in FILES:
   f=file_map[fn];p=out/fn;assert p.exists() and p.stat().st_size>0
   cloud_checks.append({'stream_id':j['stream_id'],'folder_url':folder,'id':f['id'],'title':f.get('title',f.get('drive_name')),'bytes':p.stat().st_size,'local_file':str(p),'sha256':sha(p)})
  with (out/'股票名称校正记录.csv').open(encoding='utf-8-sig',newline='') as f:
   for r in csv.DictReader(f):
    if r['status']=='待确认':pending.append({'date':j['date'],'stream_id':j['stream_id'],'time':r.get('time',r.get('start_time')),'raw_name':r['raw_name'],'annotated_name':r['corrected_name'],'reason':r['basis'],'research_url':file_map['研究文本_切分版.txt']['url']})
  names=report.get('stock_name_replacements',report.get('stock_name_correction',{}).get('applied_name_replacements',0))
  pcount=report.get('pending_name_occurrences',report.get('stock_name_correction',{}).get('pending_name_fragments',0))
  rec={'date':j['date'],'stream_id':j['stream_id'],'title':spec['title'],'actual_start_beijing':spec['actual_start_beijing'],'source_minutes':round(spec['duration_seconds']/60,2),'retained_window_minutes':round(report['retained_source_seconds']/60,2),'stock_name_replacements':names,'pending_name_occurrences':pcount,'asr_new_this_batch':not spec['asr_reused'],'asr_minutes':round(report.get('asr_wall_seconds',0)/60,2),'research_url':file_map['研究文本_切分版.txt']['url'],'folder_url':folder}
  records.append(rec);checks.append({'stream_id':j['stream_id'],'text_and_upload_complete':True,'source_asr_unchanged':True,'ids_and_timestamps_unchanged':True,'full_section_coverage':True,'read_complete':True,'files':7,'noise_exception':bool(report.get('asr_noise_exception'))})
 index_rows=[{'日期':r['date'],'处理状态':'已完成','标题':r['title'],'原视频分钟':r['source_minutes'],'保留源时段分钟':r['retained_window_minutes'],'股票名校正处数':r['stock_name_replacements'],'待确认名称处数':r['pending_name_occurrences'],'研究文本链接':r['research_url'],'全套文件夹链接':r['folder_url']} for r in records]
 index_rows.extend({'日期':r['date'],'处理状态':'暂停待继续','标题':r['title'],'原视频分钟':r['source_minutes'],'保留源时段分钟':'','股票名校正处数':'','待确认名称处数':'','研究文本链接':'','全套文件夹链接':''} for r in remaining)
 index_rows.sort(key=lambda r:r['日期']);write_csv(B/'20场处理索引.csv',index_rows,list(index_rows[0]))
 write_csv(B/'待确认股票名称汇总.csv',pending,['date','stream_id','time','raw_name','annotated_name','reason','research_url'])
 kept=sum(r['retained_source_seconds'] for r in (read((Path(j['job_directory']) if not read(Path(j['job_directory'])/'task.json')['text_already_complete'] else Path(read(Path(j['job_directory'])/'task.json')['completed_output_directory']))/'timing_report.json') for j in complete_jobs))
 total_new=sum(read(Path(j['job_directory'])/'timing_report.json').get('asr_wall_seconds',0) for j in complete_jobs if not read(Path(j['job_directory'])/'task.json')['asr_reused'])
 new_count=sum(not read(Path(j['job_directory'])/'task.json')['asr_reused'] for j in complete_jobs)
 completed_source_hours=sum(read(Path(j['job_directory'])/'task.json')['duration_seconds'] for j in complete_jobs)/3600
 summary={'completed_streams':len(records),'planned_streams':20,'remaining_streams':[r['stream_id'] for r in remaining],'source_hours':completed_source_hours,'planned_source_hours':batch['source_hours'],'retained_source_window_hours':round(kept/3600,4),'new_local_asr_streams':new_count,'new_asr_wall_minutes_sum':round(total_new/60,2),'stock_name_replacements':sum(r['stock_name_replacements'] for r in records),'pending_name_occurrences':len(pending),'visual_processing':False,'secondary_workers':False,'accepted_utc':datetime.now(timezone.utc).isoformat(),'checks':checks}
 save(B/('checkpoint_acceptance.json' if partial else 'final_acceptance.json'),summary);save(B/'cloud_verification_targets.json',cloud_checks)
 lines=['优先盘中直播20场 — 阅读索引与处理说明','',f"目前已完成{len(records)}/20场；已完成原视频合计{completed_source_hours:.2f}小时；研究文本保留源时段合计{kept/3600:.2f}小时。",'保留时长是切分区间的长度，包含区间内静音，不能等同于纯讲话时长。','', '每场先读01_研究文本；02是学员快评；03为名称校正版全文；04为完整原始转写；05列出逐处改名；06列出保留/删除时段；07列出处理结果。','股票名称逐场结合证券词表、连续语境和可用口述代码校正；无法确定的名字标注“名称待确认”，汇总另附CSV。','时间戳是原视频偏移。未核对K线、图上指代、实际成交或现场AI查询的基本面资料。','包含8月4日原先完成的试跑，链接到已有文件夹；本批新增成果按日期存放在本目录下。','只处理本次确认的20场优先盘中直播。','','已完成场次：']
 for r in records:lines.extend([f"{r['date']} | 原视频{r['source_minutes']:.2f}分钟 | 保留源时段{r['retained_window_minutes']:.2f}分钟",r['title'],f"研究文本：{r['research_url']}",f"全套文件：{r['folder_url']}",''])
 if remaining:
  lines.extend(['剩余场次（按用户要求暂停，明天继续）：']+[f"{r['date']} | {r['title']}" for r in remaining]+[''])
 lines.extend([f"本批已完成{new_count}场本地ASR转写累计耗时：{total_new/60:.2f}分钟；复用的两场未重新转写。",'这是已完成转写任务耗时之和，不等同于从启动到最后上传的总时间；语义阅读、名称校正和上传另有耗时。','本地ASR本身未调用大语言模型；文本阅读和校正由当前Codex完成。'])
 (B/'00_从这里开始_20场阅读索引.txt').write_text('\n'.join(lines)+'\n',encoding='utf-8')
 print(json.dumps({k:v for k,v in summary.items() if k!='checks'},ensure_ascii=False))
if __name__=='__main__':main('--partial' in sys.argv)
