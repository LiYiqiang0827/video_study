"""Accept a completed ASR only when its packaging failure is fixed closing-noise."""
import json,sys,hashlib,csv,shutil,subprocess
from pathlib import Path
from datetime import datetime,timezone
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0,str(Path(__file__).parent))
import yuncong_priority_batch_20261005 as batch
from media_platform.evidence import build_evidence
def main(sid):
 job=batch.resolve_job(sid);spec=batch.read(job/'task.json');state=batch.read(job/'status.json')
 out=Path(spec['asr_directory']);m=batch.read(out/'manifest.json');rows=batch.read(out/'speech.json')
 assert m['steps']['audio']=='complete' and m['source_sha256']==spec['source_sha256']
 assert batch.sha(Path(spec['source_path']))==spec['source_sha256']
 for name,h in m['outputs'].items():assert batch.sha(out/name)==h
 assert all(c['device']=='cuda' and c['compute_type']=='float16' for c in m['chunks'])
 chunks=sorted((out/'chunks').glob('[0-9][0-9][0-9][0-9][0-9].json'))
 assert len(chunks)==int((spec['duration_seconds']+299.999)//300)
 bad=[r for r in rows if not m['start']-.1<=r['start']<r['end']<=m['end']+.1]
 assert bad and all(batch.NOISE.fullmatch(r['text'].strip()) and r['start']>=m['end']-45 for r in bad),bad
 clean=[r for r in rows if r not in bad];v=job/'asr_validation';v.mkdir(exist_ok=True)
 batch.save(v/'speech.json',clean);shutil.copyfile(out/'speech.md',v/'speech.md')
 vm={**m,'outputs':{**m['outputs'],'speech.json':batch.sha(v/'speech.json')}}
 vm['steps']={**m['steps'],'evidence':'complete'}
 batch.save(v/'manifest.json',vm);bundle=build_evidence(v,vm);batch.save(v/'evidence.json',bundle)
 proof={'reason':'Only fixed closing ASR advertisement extended beyond media; original retained, excluded from validation and research.','removed_from_validation':bad,'original_asr_sha256':batch.sha(out/'speech.json'),'validated_segments':len(clean),'checked_utc':batch.now()}
 batch.save(job/'asr_noise_exception.json',proof)
 report={'stream_id':sid,'duration_seconds':spec['duration_seconds'],'asr_wall_seconds':state['asr_wall_seconds'],'segments':len(rows),'chunks':len(chunks),'source_hash_matches':True,'visual_processing':False,'asr_large_model_token_usage':0,'text_stage':'pending','asr_noise_exception':str(job/'asr_noise_exception.json')}
 batch.save(job/'timing_report.json',report)
 state.update(asr='complete',asr_acceptance='source and outputs verified; derived evidence validation excludes only closing fixed ASR noise',asr_noise_exception=str(job/'asr_noise_exception.json'))
 batch.save(job/'status.json',state)
 batch.event('asr_accepted_with_closing_noise_exception',stream_id=sid,excluded=len(bad))
 print(json.dumps({'stream_id':sid,'accepted_segments':len(clean),'closing_noise_segments':len(bad)},ensure_ascii=False))
if __name__=='__main__':main(sys.argv[1])

