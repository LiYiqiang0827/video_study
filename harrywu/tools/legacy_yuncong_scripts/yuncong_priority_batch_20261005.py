"""Frozen priority-20 text batch: one GPU producer, local state, bounded jobs."""
from __future__ import annotations
import argparse,csv,hashlib,json,os,re,subprocess,sys,time
from datetime import datetime,timezone
from pathlib import Path
sys.stdout.reconfigure(encoding='utf-8')
ROOT=Path(r'D:\YuncongVideoAnalysis\ledger_2026_v0.1')
BATCH=ROOT/'text_priority_batch_20261005'
VOCAB=ROOT/'planning/stock_vocabulary_v0.1_20261004'
DATES=['2026-01-09','2026-03-20','2026-03-31','2026-04-13','2026-06-10','2026-07-29','2026-07-30','2026-07-31','2026-08-04','2026-08-05','2026-08-06','2026-08-07','2026-08-11','2026-08-12','2026-08-13','2026-08-14','2026-08-19','2026-08-20','2026-08-26','2026-09-02']
NOISE=re.compile(r'请不吝点赞\s*订阅\s*转发\s*打赏支持明镜与点点栏目|优优独播剧场[—\-\s]*YoYo Television Series Exclusive|中文字幕志愿者\s*李宗盛')
def now():return datetime.now(timezone.utc).isoformat()
def read(p):return json.loads(p.read_text(encoding='utf-8'))
def save(p,v):
 p.parent.mkdir(parents=True,exist_ok=True);temp=p.with_name(p.name+'.tmp')
 temp.write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8');os.replace(temp,p)
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def stamp(t):
 t=int(t);return f'{t//3600:02}:{t//60%60:02}:{t%60:02}'
def event(kind,**kwargs):
 v={'event':kind,'at_utc':now(),**kwargs}
 with (BATCH/'events.jsonl').open('a',encoding='utf-8') as f:f.write(json.dumps(v,ensure_ascii=False)+'\n')
 print(json.dumps(v,ensure_ascii=False),flush=True)
def prepare():
 if (BATCH/'batch.json').exists():raise RuntimeError('Batch already frozen; use --run to resume')
 with (ROOT/'streams_inventory.csv').open(encoding='utf-8-sig',newline='') as f:inventory=list(csv.DictReader(f))
 jobs=[];BATCH.mkdir(exist_ok=True)
 for date in DATES:
  found=[r for r in inventory if r['title_date']==date and '美股' not in r['title'] and '加密' not in r['title']]
  assert len(found)==1,(date,found)
  r=found[0];sid=r['stream_id'];job_dir=BATCH/(date+'_'+sid);job_dir.mkdir(exist_ok=True)
  source=Path(r['source_path']).resolve(strict=True)
  assert source.parent==Path(r'D:\YuncongVideoAnalysis\2026-01-01_to_2026-10-03').resolve()
  reused=ROOT/'extractions'/sid if sid=='XSFwu5KOo5Y' else None
  completed=ROOT/'text_pilot_20261004_0804' if sid=='-9HY0rxruC0' else None
  asr=completed/'asr' if completed else reused if reused else job_dir/'asr'
  hint=job_dir/'hotwords.txt';hint.write_bytes((VOCAB/'asr_hotwords_short.txt').read_bytes())
  spec={'task_id':'priority_text_'+date+'_'+sid,'stream_id':sid,'title':r['title'],'title_date':date,
   'actual_start_beijing':r['actual_start_beijing'],'source_path':str(source),'source_sha256':r['source_sha256'],
   'duration_seconds':float(r['local_duration_seconds']),'asr_directory':str(asr),'output_directory':str(job_dir),
   'asr_reused':bool(reused or completed),'text_already_complete':bool(completed),
   'completed_output_directory':str(completed) if completed else None,
   'hotwords_file':str(hint),'hotwords_sha256':sha(hint),'model':'large-v3','device':'cuda','compute_type':'float16',
   'language':'zh','chunk_seconds':300,'vad':'conservative','visual_processing':False,'asr_timeout_seconds':3600,
   'created_utc':now()}
  save(job_dir/'task.json',spec)
  state={'asr':'reused' if reused or completed else 'pending','text':'complete' if completed else 'pending',
   'upload':'complete' if completed else 'pending','stream_id':sid,'created_utc':now()}
  if completed:state['cloud_receipt']=str(completed/'谷歌云盘上传记录.json')
  if reused or completed:
   m=read(asr/'manifest.json');speech=read(asr/'speech.json')
   assert m['source_sha256']==spec['source_sha256'] and speech
  save(job_dir/'status.json',state);jobs.append({'stream_id':sid,'date':date,'job_directory':str(job_dir)})
 batch={'scope':'20 previously agreed A-share intraday priority streams only','created_utc':now(),'jobs':jobs,
  'source_hours':sum(read(Path(j['job_directory'])/'task.json')['duration_seconds'] for j in jobs)/3600,
  'new_asr_jobs':18,'fully_completed_reused_jobs':1,'old_asr_reused_jobs':1,
  'requirements':['original_asr_and_source_offsets_preserved','keep_trading_and_stock_questions','remove_chatter',
   'correct_stock_names_per_stream','mark_ambiguity','no_chart_ocr_or_trade_verification','upload_to_existing_drive_parent'],
  'cloud_parent_id':'1hmjFxVJSLKOPbSFTE98XaPQBIrZcDbmG','batch_timeout_seconds':36000,
  'stop_control':str(BATCH/'STOP'),'secondary_workers':False}
 save(BATCH/'batch.json',batch);save(BATCH/'producer_status.json',{'status':'prepared','created_utc':now()})
 print(json.dumps(batch,ensure_ascii=False))
def child(job_dir):
 os.environ.update(HF_HUB_OFFLINE='1',PYTHONIOENCODING='utf-8')
 import faster_whisper
 from huggingface_hub import snapshot_download
 import media_platform.ingest as ingest
 spec=read(job_dir/'task.json');out=Path(spec['asr_directory']);hint=Path(spec['hotwords_file'])
 assert sha(hint)==spec['hotwords_sha256']
 hints=hint.read_text(encoding='utf-8').strip();runtime=read(ROOT/'runtime_manifest.json')
 cache=snapshot_download('Systran/faster-whisper-large-v3',revision=runtime['snapshot'],local_files_only=True,
  allow_patterns=['config.json','model.bin','tokenizer.json','preprocessor_config.json','vocabulary.*'])
 identity={'id':'Systran/faster-whisper-large-v3','revision':runtime['snapshot'],'cache':cache,'hotwords_sha256':spec['hotwords_sha256']}
 ingest.model_identity=lambda name:identity if name=='large-v3' else (_ for _ in ()).throw(ValueError(name))
 original=faster_whisper.WhisperModel
 progress={'status':'loading','pid':os.getpid(),'started_utc':now(),'completed_calls':0,'processed_media_seconds':0}
 save(job_dir/'progress.json',progress)
 class HintedWhisper(original):
  def __init__(self,*a,**k):
   assert k.get('device')=='cuda' and k.get('compute_type')=='float16'
   super().__init__(*a,**k);progress.update(status='transcribing',updated_utc=now());save(job_dir/'progress.json',progress)
  def transcribe(self,*a,**k):
   if (BATCH/'STOP').exists() or (job_dir/'STOP').exists():raise RuntimeError('User stop control requested')
   k['hotwords']=hints;began=time.monotonic();segments,info=super().transcribe(*a,**k)
   def timed():
    count=0
    for s in segments:count+=1;yield s
    progress['completed_calls']+=1
    progress.update(status='transcribing',updated_utc=now(),processed_media_seconds=min(progress['completed_calls']*300,spec['duration_seconds']),
     last_call_seconds=round(time.monotonic()-began,2),last_call_segments=count)
    save(job_dir/'progress.json',progress)
   return timed(),info
 faster_whisper.WhisperModel=HintedWhisper
 sys.argv=['priority_text_asr',spec['source_path'],'--output',str(out),'--model','large-v3','--device','cuda',
  '--compute-type','float16','--language','zh','--chunk-seconds','300','--vad','conservative','--no-video','--no-ocr']
 if out.exists():sys.argv.append('--resume')
 ingest.main();progress.update(status='asr_complete',updated_utc=now());save(job_dir/'progress.json',progress)
def stop_tree(pid):
 import psutil
 try:
  process=psutil.Process(pid);children=process.children(recursive=True);targets=children+[process]
  for p in targets:p.terminate()
  _,alive=psutil.wait_procs(targets,timeout=10)
  for p in alive:p.kill()
 except psutil.NoSuchProcess:pass
def run():
 batch=read(BATCH/'batch.json');started=time.monotonic()
 previous=read(BATCH/'producer_status.json')
 if previous.get('status')=='running':
  import psutil
  if psutil.pid_exists(previous.get('producer_pid',-1)):raise RuntimeError('Producer already running')
 state={'status':'running','producer_pid':os.getpid(),'started_utc':now(),'current':None,'completed_new':[],'failures':[]}
 save(BATCH/'producer_status.json',state)
 env={**os.environ,'HF_HUB_OFFLINE':'1','PYTHONIOENCODING':'utf-8'}
 for j in batch['jobs']:
  job=Path(j['job_directory']);spec=read(job/'task.json');status=read(job/'status.json')
  if status['asr'] in ('reused','complete'):continue
  if (BATCH/'STOP').exists() or time.monotonic()-started>batch['batch_timeout_seconds']:
   state.update(status='stopped',ended_utc=now());save(BATCH/'producer_status.json',state);return 1
  state.update(current=j['stream_id']);save(BATCH/'producer_status.json',state)
  status.update(asr='running',asr_started_utc=now());save(job/'status.json',status)
  event('asr_start',stream_id=j['stream_id'],date=j['date'])
  began=time.monotonic()
  with (job/'asr.log').open('a',encoding='utf-8') as log:
   p=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--child',str(job)],stdout=log,stderr=log,env=env,
    creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
   state['child_pid']=p.pid;save(BATCH/'producer_status.json',state)
   while p.poll() is None:
    if (BATCH/'STOP').exists() or (job/'STOP').exists() or time.monotonic()-began>spec['asr_timeout_seconds']:
     stop_tree(p.pid);p.wait(timeout=20);break
    time.sleep(2)
  seconds=round(time.monotonic()-began,3)
  if p.returncode:
   status.update(asr='failed',asr_finished_utc=now(),asr_wall_seconds=seconds,return_code=p.returncode)
   save(job/'status.json',status);state['failures'].append(j['stream_id']);event('asr_failed',stream_id=j['stream_id'],seconds=seconds);continue
  out=Path(spec['asr_directory'])
  verified=subprocess.run([sys.executable,'-m','media_platform','verify',str(out)],capture_output=True,text=True,encoding='utf-8',timeout=300,env=env)
  (job/'verification.json').write_text(verified.stdout+verified.stderr,encoding='utf-8')
  m=read(out/'manifest.json');speech=read(out/'speech.json')
  assert verified.returncode==0 and m['source_sha256']==spec['source_sha256'] and speech
  assert all(c['device']=='cuda' and c['compute_type']=='float16' for c in m['chunks'])
  report={'stream_id':j['stream_id'],'duration_seconds':spec['duration_seconds'],'asr_wall_seconds':seconds,
   'media_speed_multiple':round(spec['duration_seconds']/seconds,3),'segments':len(speech),'chunks':len(m['chunks']),
   'source_hash_matches':True,'visual_processing':False,'asr_large_model_token_usage':0,'text_stage':'pending','finished_utc':now()}
  save(job/'timing_report.json',report)
  status.update(asr='complete',asr_finished_utc=now(),asr_wall_seconds=seconds);save(job/'status.json',status)
  state['completed_new'].append(j['stream_id']);state.update(child_pid=None);save(BATCH/'producer_status.json',state)
  event('asr_complete',stream_id=j['stream_id'],date=j['date'],seconds=seconds,segments=len(speech))
 state.update(status='asr_queue_complete' if not state['failures'] else 'asr_queue_complete_with_failures',current=None,child_pid=None,ended_utc=now())
 save(BATCH/'producer_status.json',state);event(state['status'],completed=len(state['completed_new']),failures=state['failures'])
 return int(bool(state['failures']))
def resolve_job(sid):
 b=read(BATCH/'batch.json');return Path(next(j['job_directory'] for j in b['jobs'] if j['stream_id']==sid))
def compact_read(sid,start,end):
 spec=read(resolve_job(sid)/'task.json');out=Path(spec['asr_directory'])
 if (out/'speech.json').exists():rows=read(out/'speech.json')
 else:
  rows=[]
  for p in sorted((out/'chunks').glob('[0-9][0-9][0-9][0-9][0-9].json')):
   rows.extend(read(p)['segments'])
 group=[];count=0
 for r in rows:
  if not start<=r['start']<end:continue
  text=NOISE.sub('',r['text']).strip()
  if not text:continue
  if group and (r['start']-group[0]['start']>=40 or r['start']-group[-1]['end']>15):
   print(f"[{stamp(group[0]['start'])}–{stamp(group[-1]['end'])}] "+''.join(x['text'] for x in group));group=[]
  group.append({**r,'text':text});count+=1
 if group:print(f"[{stamp(group[0]['start'])}–{stamp(group[-1]['end'])}] "+''.join(x['text'] for x in group))
 print(json.dumps({'stream_id':sid,'start':start,'end':end,'nonempty_segments':count},ensure_ascii=False))
def status_read():
 b=read(BATCH/'batch.json');state=read(BATCH/'producer_status.json');records=[]
 for j in b['jobs']:
  s=read(Path(j['job_directory'])/'status.json');records.append({'date':j['date'],'stream_id':j['stream_id'],**{k:s[k] for k in ['asr','text','upload']}})
 if state.get('current'):
  p=resolve_job(state['current'])/'progress.json'
  if p.exists():state['progress']=read(p)
 print(json.dumps({'producer':state,'jobs':records},ensure_ascii=False))
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--prepare',action='store_true');p.add_argument('--run',action='store_true');p.add_argument('--child',type=Path)
 p.add_argument('--status',action='store_true');p.add_argument('--read',nargs=3);a=p.parse_args()
 if a.prepare:prepare()
 elif a.child:child(a.child)
 elif a.run:raise SystemExit(run())
 elif a.status:status_read()
 elif a.read:compact_read(a.read[0],float(a.read[1]),float(a.read[2]))
