"""Offline, resumable two-pass ASR with a source-bound Codex review handoff.

Audio candidates are evidence, never ground truth. A completed audio run waits
for the current agent's text decisions; no synthetic model review is reported.
"""
from __future__ import annotations

import argparse
import contextlib
import difflib
import gc
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid
import wave

from .evidence import load_evidence
from .ingest import atomic_write, digest, enable_cuda_libraries, probe, resource_snapshot, stamp
from .paths import model_cache

VERSION = "2.2.1"
# Recognition inputs/cache schema stay compatible; implementation provenance is
# recorded separately so completed, verified windows survive this boundary fix.
PARAMS_VERSION = "2.2.0"
DEFAULTS = dict(core_seconds=60, context_seconds=5, baseline_seconds=300,
    max_rounds=2, max_target_windows=120, max_seconds=21600,
    temp_budget_bytes=512*2**20, output_budget_bytes=2*2**30,
    min_free_bytes=3*2**30, terms=[], image_review_required=False,
    ram_reserve_gib=4, vram_reserve_gib=1.5)


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def object_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":")).encode()).hexdigest()


def normalized(text):
    return "".join(c for c in unicodedata.normalize("NFKC", text)
        if not c.isspace() and not unicodedata.category(c).startswith("P"))


def safe_child(root, relative):
    root = Path(root).absolute()
    child = root / relative
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ValueError("Unsafe relative artifact path")
    for p in [root, *root.parents, child, *child.parents]:
        if p.exists() and (p.is_symlink() or getattr(p, "is_junction", lambda: False)()):
            raise ValueError("Symlink/junction path is not an owned workspace")
    if not child.resolve().is_relative_to(root.resolve()):
        raise ValueError("Artifact escapes workspace")
    return child


@contextlib.contextmanager
def exclusive_lock():
    # Same file as the legacy ingest lock, so the two entry points serialize.
    path = Path(tempfile.gettempdir()) / "media_platform_ingest.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR)
    acquired = False
    try:
        if sys.platform == "win32":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
        yield
    except OSError as exc:
        if not acquired:
            raise RuntimeError("Another ASR process owns the shared lock") from exc
        raise
    finally:
        os.close(fd)


def local_model(name):
    """Only inspect existing caches. Never invoke a downloader."""
    direct = Path(name)
    paths = [direct] if direct.is_dir() else []
    if name == "medium":
        revision = "08e178d48790749d25932bbc082711ddcfdfbc4f"
        paths += [model_cache()/"faster-whisper-medium"/revision,
            Path.home()/".cache/annx/models/faster-whisper-medium"/revision]
    if not paths or not any(p.is_dir() for p in paths):
        for hub in [Path.home()/".cache/huggingface/hub", model_cache()/"huggingface"]:
            base = hub / ("models--Systran--faster-whisper-" + name) / "snapshots"
            if base.is_dir():
                paths.extend(sorted(base.iterdir()))
    for path in paths:
        if all((path/x).is_file() for x in ("model.bin", "config.json", "tokenizer.json")):
            files = {p.name: digest(p) for p in sorted(path.iterdir()) if p.is_file()}
            return {"id": name, "path": str(path.resolve()), "files": files,
                "family": "Whisper", "revision": path.name if re.fullmatch(r"[0-9a-f]{40}",path.name) else None}
    raise ValueError(f"No complete local cache for {name}; downloads are disabled")


def validate_config(config):
    if set(config)-set(DEFAULTS):
        raise ValueError("Unknown review-config keys: " + str(set(config)-set(DEFAULTS)))
    result = dict(DEFAULTS, **config)
    for key in ("core_seconds", "baseline_seconds", "max_seconds", "temp_budget_bytes",
                "output_budget_bytes", "min_free_bytes", "ram_reserve_gib", "vram_reserve_gib"):
        if not isinstance(result[key], (int,float)) or not math.isfinite(result[key]) or result[key] <= 0:
            raise ValueError("Invalid positive setting: " + key)
    if not 15 <= result["core_seconds"] <= 120 or not 0 <= result["context_seconds"] <= 15:
        raise ValueError("Core/context window outside supported bounds")
    if result["max_rounds"] not in (0,1,2) or not isinstance(result["max_target_windows"],int) or result["max_target_windows"] < 0:
        raise ValueError("Invalid bounded retry limits")
    if not isinstance(result["terms"],list) or any(not isinstance(t,str) or not t for t in result["terms"]):
        raise ValueError("terms must be nonempty strings")
    if len("、".join(result["terms"])) > 1000:
        raise ValueError("Glossary exceeds 1000 characters")
    return result


def workspace(path):
    path = Path(path).absolute()
    if not Path(path.anchor).exists():
        raise ValueError("Work/output volume is unavailable; no implicit fallback")
    safe_child(path, "probe")
    path.mkdir(parents=True, exist_ok=True)
    check = path / (".write-test-"+uuid.uuid4().hex)
    check.write_bytes(b"")
    check.unlink()
    return path.resolve()


def disk_check(work, output, config, temp_need=0, output_need=0):
    if temp_need > config["temp_budget_bytes"] or output_need > config["output_budget_bytes"]:
        raise BudgetStop("Configured size budget exceeded")
    same = os.stat(work).st_dev == os.stat(output).st_dev
    required_work = config["min_free_bytes"] + temp_need + (output_need if same else 0)
    if shutil.disk_usage(work).free < required_work:
        raise BudgetStop("Insufficient free space on actual work volume")
    if not same and shutil.disk_usage(output).free < config["min_free_bytes"]+output_need:
        raise BudgetStop("Insufficient free space on actual output volume")


class BudgetStop(RuntimeError):
    pass


def merge_candidates(rows):
    """Drop only exact text at strongly overlapping times from distinct windows.

    No midpoint filter, fuzzy replacement or prefix trimming. Raw rows survive.
    Short repeated responses at distinct moments remain separate.
    """
    kept, events = [], []
    for row in sorted(rows, key=lambda r:(r["start"], r["end"], r["id"])):
        twin = None
        for prior in reversed(kept):
            if prior["end"] < row["start"]-1:
                continue
            overlap = max(0,min(prior["end"],row["end"])-max(prior["start"],row["start"]))
            minimum = min(prior["end"]-prior["start"],row["end"]-row["start"])
            if (prior["window"] != row["window"] and normalized(prior["text"]) == normalized(row["text"])
                    and minimum > 0 and overlap/minimum >= .8 and abs(prior["start"]-row["start"]) <= .65):
                twin = prior
                break
        if twin:
            events.append({"discarded":row["id"],"retained":twin["id"],
                "reason":"Exact normalized text and strongly coincident time; raw candidate retained"})
        else:
            kept.append(row)
    return kept, events


def align(baseline, second):
    """Temporal connected components: multi-to-multi, including missing turns.

    Text equivalence is decided on the entire component, never list positions.
    Touching consecutive intervals don't merge without actual overlap.
    """
    entries = sorted([(r["start"],r["end"],side,r) for side,rows in (("baseline",baseline),("second",second))
                      for r in rows],key=lambda x:(x[0],x[1],x[2],x[3]["id"]))
    groups=[]
    for begin,end,side,row in entries:
        if not groups or begin >= groups[-1]["end"]-.04:
            groups.append({"start":begin,"end":end,"baseline":[],"second":[]})
        group=groups[-1]
        group["end"]=max(end,group["end"])
        group[side].append(row)
    for i,group in enumerate(groups):
        group["id"]=f"g{i:06d}"
        group["baseline_text"]="".join(r["text"] for r in group["baseline"])
        group["second_text"]="".join(r["text"] for r in group["second"])
    return groups


def flag_group(g,terms,index):
    a,b=g["baseline_text"],g["second_text"]
    flags=[]
    if normalized(a)!=normalized(b): flags.append("substantive_conflict")
    if not a or not b: flags.append("possible_missing_turn")
    if any(r.get("review") for side in ("baseline","second") for r in g[side]): flags.append("asr_low_score")
    if re.search(r"\d|[一二三四五六七八九十百]+[年月日人回枚歳]|ない|なかった|ません|じゃなく|「|『",a+b):
        flags.append("number_negation_or_quote")
    if any(term in a+b for term in terms): flags.append("important_entity")
    if any(len(normalized(r["text"]))<=4 for side in ("baseline","second") for r in g[side]): flags.append("short_response")
    if any(a.count(a[i:i+8])>=3 for i in range(0,max(0,len(a)-8),8)): flags.append("possible_repetition")
    if index % 20 == 0 and not flags: flags.append("agreement_sample")
    return flags


def priority(g):
    return (0 if any(x in g["flags"] for x in ("number_negation_or_quote","important_entity","possible_missing_turn"))
            else 1 if "substantive_conflict" in g["flags"] else 2, g["start"])


def windows(start,end,core,context,prefix):
    return [{"id":f"{prefix}-{i:05d}","core_start":start+i*core,
        "core_end":min(end,start+(i+1)*core),"start":max(start,start+i*core-context),
        "end":min(end,start+(i+1)*core+context)} for i in range(math.ceil((end-start)/core))]


def audio_timeline(info,stream):
    """Container-relative audio metadata, distinct from actual decoded coverage.

    MP3 encoder delay may be present in both format and stream start times.
    Duration alone never establishes silence or equivalence between two files.
    """
    def number(value,default=None):
        try:result=float(value)
        except (ValueError,TypeError):return default
        return result if math.isfinite(result) else default
    origin=number(info.get('format',{}).get('start_time'),0)
    start=max(0,number(stream.get('start_time'),origin)-origin)
    duration=number(stream.get('duration'))
    return {'start':start,'end':start+duration if duration is not None else None,
        'duration':duration,'container_origin':origin,'basis':'ffprobe selected audio stream; decoded windows are authoritative'}


def missing_pcm_intervals(coverage):
    merged=[]
    for a,b in sorted(coverage.get('second_pass',{}).get('no_decoded_samples_intervals',[])):
        if merged and a<=merged[-1][1]+.001:merged[-1][1]=max(b,merged[-1][1])
        else:merged.append([a,b])
    return merged


def target_windows(groups,activity_gaps,start,end,core):
    intervals=[(max(start,g["start"]-7),min(end,g["end"]+7)) for g in groups if g["flags"]]
    intervals += [(max(start,a-5),min(end,b+5)) for a,b in activity_gaps]
    union=[]
    for a,b in sorted(intervals):
        if union and a<=union[-1][1]: union[-1][1]=max(b,union[-1][1])
        else: union.append([a,b])
    result=[]
    for a,b in union: result.extend(windows(a,b,core,5,"target"))
    return result


class Engine:
    def __init__(self,args,manifest,output,work):
        self.args,self.m,self.output,self.work=args,manifest,output,work
        self.model=None
        self.loaded=None
        self.began=time.monotonic()

    def save(self):
        self.m["updated_unix"]=time.time()
        atomic_write(self.output/"refine_manifest.json",self.m)

    def checkpoint(self,name,value):
        path=safe_child(self.output,name)
        atomic_write(path,value)
        self.m["artifacts"][name]=digest(path)
        self.save()

    def limits(self,seconds=0):
        if (self.output/"STOP").exists(): raise BudgetStop("Cooperative stop requested")
        if time.monotonic()-self.began >= self.m["config"]["max_seconds"]:
            raise BudgetStop("Per-invocation time budget reached")
        used=sum(p.stat().st_size for p in self.output.rglob("*") if p.is_file())
        disk_check(self.work,self.output,self.m["config"],int(seconds*32000+4096),used)

    def cleanup(self):
        errors=[]
        for name in list(self.m.get("temporary_files",[])):
            try:
                p=safe_child(self.work,name)
                if p.exists(): p.unlink()
                self.m["temporary_files"].remove(name)
            except OSError as exc: errors.append(str(exc))
        self.m["cleanup"]={"status":"failed" if errors else "completed","errors":errors}
        self.save()
        if errors: raise RuntimeError("Temporary cleanup failed; next media is blocked")

    def recognize(self,win,identity,vad,prompt=None):
        filename="candidates/"+win["id"]+".json"
        if filename in self.m["artifacts"]:
            cached=read(self.output/filename)
            if (cached["window"]!=win or cached["model_identity_hash"]!=object_hash(identity)
                    or cached["vad"]!=vad or cached["initial_prompt"]!=prompt):
                raise ValueError("Completed candidate window/model/recognition settings changed")
            return cached
        self.limits(win["end"]-win["start"])
        key=object_hash(identity)
        wav=safe_child(self.work,win["id"]+".wav")
        self.m.setdefault("temporary_files",[]).append(wav.name)
        self.save()
        started=time.monotonic()
        try:
            env=dict(os.environ,TEMP=str(self.work),TMP=str(self.work),TMPDIR=str(self.work))
            decode_start=max(win['start'],min(win['end'],self.m.get('audio_timeline',{}).get('start',0)))
            subprocess.run(["ffmpeg","-nostdin","-y","-loglevel","error","-ss",str(decode_start),
                "-i",self.m["source"],"-map",f"0:{self.m['audio_stream']}","-t",str(win["end"]-decode_start),
                "-vn","-ac","1","-ar","16000","-c:a","pcm_s16le",str(wav)],
                check=True,capture_output=True,timeout=180,env=env)
            size=wav.stat().st_size
            self.m["temporary_peak_bytes"]=max(self.m.get("temporary_peak_bytes",0),size)
            if size > self.m["config"]["temp_budget_bytes"]: raise BudgetStop("Actual PCM exceeds budget")
            import numpy as np
            with wave.open(str(wav),"rb") as stream:
                frame_count=stream.getnframes()
                data=np.frombuffer(stream.readframes(stream.getnframes()),dtype=np.int16).astype(np.float32)
            decoded_end=min(win['end'],decode_start+frame_count/16000)
            no_samples=[]
            if decode_start>win['start']:no_samples.append([win['start'],decode_start])
            if decoded_end<win['end']-.001:no_samples.append([decoded_end,win['end']])
            activity=[]
            for offset in range(0,len(data),8000):
                piece=data[offset:offset+8000]
                if len(piece):
                    db=float(20*np.log10(max(float(np.sqrt(np.mean(piece**2)))/32768,1e-10)))
                    if db > -38:
                        activity.append([round(decode_start+offset/16000,3),round(min(decoded_end,decode_start+(offset+len(piece))/16000),3)])
            del data
            segments=[]
            language=self.args.language
            if frame_count:
                if self.loaded!=key:
                    self.model=None
                    gc.collect()
                    snap=resource_snapshot()
                    config=self.m['config']
                    if snap['ram_free_gib'] < config['ram_reserve_gib']:
                        raise BudgetStop('RAM reserve unavailable')
                    if self.args.device=='cuda' and snap.get('vram_free_gib',0) < config['vram_reserve_gib']+3.5:
                        raise BudgetStop('VRAM reserve unavailable before model load')
                    enable_cuda_libraries()
                    from faster_whisper import WhisperModel
                    self.model=WhisperModel(identity['path'],device=self.args.device,
                        compute_type=self.args.compute_type,cpu_threads=4,num_workers=1,local_files_only=True)
                    self.loaded=key
                segments,info=self.model.transcribe(str(wav),language=self.args.language,
                    beam_size=5,condition_on_previous_text=False,vad_filter=vad,
                    vad_parameters={'min_silence_duration_ms':700,'speech_pad_ms':500} if vad else None,
                    initial_prompt=prompt,word_timestamps=False)
                language=info.language
            rows=[]
            for index,s in enumerate(segments):
                a=max(decode_start,decode_start+s.start)
                b=min(decoded_end,decode_start+s.end)
                if b>a and s.text.strip():
                    rows.append({"id":f"{win['id']}-s{index:04d}","window":win["id"],
                        "source_sha256":self.m["source_sha256"],"start":round(a,3),"end":round(b,3),
                        "text":s.text.strip(),"avg_logprob":s.avg_logprob,"no_speech_prob":s.no_speech_prob,
                        "review":s.avg_logprob<-.8 or s.no_speech_prob>.5})
            record={"window":win,"segments":rows,"activity":activity,"input_modality":"audio",
                "audio_sha256":digest(wav),"model_identity_hash":key,"vad":vad,"initial_prompt":prompt,
                "beam_size":5,"condition_on_previous_text":False,"family":"Whisper",
                "device":self.args.device,"compute_type":self.args.compute_type,
                "elapsed_seconds":round(time.monotonic()-started,3),"pcm_bytes":size,
                "language":language,'implementation_version':VERSION,
                'decoded_frames':frame_count,'decoded_interval':[decode_start,decoded_end] if frame_count else None,
                'no_decoded_samples_intervals':no_samples,
                'recognition_status':'completed' if frame_count else 'skipped_no_decodable_audio'}
            self.checkpoint(filename,record)
            print(json.dumps({"window":win["id"],"seconds":record["elapsed_seconds"],"segments":len(rows)},ensure_ascii=False),flush=True)
            return record
        finally:
            self.cleanup()

    def pass_asr(self,specs,identity,vad):
        records=[self.recognize(w,identity,vad) for w in specs]
        merged,events=merge_candidates([r for block in records for r in block["segments"]])
        return merged,events,records


def source_and_artifacts(output,manifest):
    if digest(Path(manifest["source"])) != manifest["source_sha256"]:
        raise ValueError("Actual source hash changed")
    for name,sha in manifest["artifacts"].items():
        path=safe_child(output,name)
        if not path.is_file() or digest(path)!=sha:
            raise ValueError("Permanent evidence hash mismatch: "+name)
    extraction=manifest.get("extraction")
    if extraction:
        if digest(Path(extraction)/"manifest.json")!=manifest["extraction_manifest_sha256"]:
            raise ValueError("Baseline extraction manifest changed")
        bundle=load_evidence(Path(extraction))
        if bundle["media_asset"]["source_sha256"]!=manifest["source_sha256"]:
            raise ValueError("Baseline source mismatch")


def build_packet(engine,groups,retries,dedupe):
    m=engine.m
    candidates={r["id"]:r for group in groups for side in ("baseline","second") for r in group[side]}
    for r in retries: candidates[r["id"]]=r
    items=[]
    for g in groups:
        refs=[r["id"] for r in retries if r["start"] < g["end"] and r["end"] > g["start"]]
        items.append({k:g[k] for k in ("id","start","end","baseline_text","second_text","flags")}|{
            "baseline_refs":[r["id"] for r in g["baseline"]],
            "second_refs":[r["id"] for r in g["second"]],"retry_refs":refs})
    packet={"run_id":m["run_id"],"source_sha256":m["source_sha256"],"interval":m["interval"],
        "actual_modalities":["text"],"audio_provider":"Whisper ASR only; agent has NOT received audio",
        "terms":m["config"]["terms"],"groups":items,"candidates":candidates,"deduplication":dedupe,
        'audio_coverage':m.get('coverage',{}).get('second_pass',{}),
        "instructions":"Current Codex reads candidates and returns one decision per group. Retain material conflicts as unresolved. Do not claim hearing audio. Refs and exact selected wording are validated. No user JSON entry is required."}
    engine.checkpoint("agent_packet.json",packet)
    return packet


def include_new_turns(groups,retries,terms):
    # Speech found ONLY during an activity-gap recheck must enter the review
    # queue too. Never silently throw it away because both initial passes lack it.
    novel=[r for r in retries if not any(r["start"]<g["end"] and r["end"]>g["start"] for g in groups)]
    novel,_=merge_candidates(novel)
    for g in align([],novel):
        g["flags"]=["possible_missing_turn","supplementary_asr_only"]
        groups.append(g)
    groups.sort(key=lambda g:(g["start"],g["end"]))
    for i,g in enumerate(groups):g["id"]=f"g{i:06d}"
    return groups


def conservative_decisions(packet):
    decisions=[]
    for g in packet["groups"]:
        same=bool(g["baseline_text"]) and normalized(g["baseline_text"])==normalized(g["second_text"])
        refs=g["baseline_refs"] or g["second_refs"]
        decisions.append({"id":g["id"],"candidate_refs":refs,"selected_refs":refs,
            "refined_text":g["baseline_text"] or g["second_text"],
            "decision":"unchanged" if same else "unresolved",
            "reason":"Conservative draft pending agent review; model agreement is not proof",
            "remaining_uncertainty":[] if same else ["ASR alternatives require text adjudication; keep all candidates"]})
    return {"run_id":packet["run_id"],"source_sha256":packet["source_sha256"],
        "packet_sha256":object_hash(packet),"actual_modalities":[],"reviewer":"deterministic_draft",
        "decisions":decisions}


def validate_decisions(packet,response):
    if response.get("run_id")!=packet["run_id"] or response.get("source_sha256")!=packet["source_sha256"] or response.get("packet_sha256")!=object_hash(packet):
        raise ValueError("Decisions are not bound to this source/run/packet")
    if response.get("actual_modalities") != ["text"]:
        raise ValueError("Text adapter cannot claim other modalities or omit text input")
    groups=packet["groups"]
    decisions=response.get("decisions",[])
    if [r.get("id") for r in decisions]!=[g["id"] for g in groups]:
        raise ValueError("Decision IDs incomplete, repeated or out of order")
    for g,d in zip(groups,decisions):
        allowed=set(g["baseline_refs"]+g["second_refs"]+g["retry_refs"])
        refs=d.get("candidate_refs",[])
        chosen=d.get("selected_refs",[])
        abstain=d.get('abstain',False)
        if not refs or (not chosen and not abstain) or len(chosen)!=len(set(chosen)) or not set(refs)<=allowed or not set(chosen)<=set(refs):
            raise ValueError("Invalid candidate reference")
        if abstain and (chosen or d.get('refined_text','').strip() or d.get('decision')!='unresolved'
                or 'supplementary_asr_only' not in g.get('flags',[])):
            raise ValueError('Abstention is only allowed for explicitly unconfirmed supplementary-only candidates')
        if d.get("decision") not in ("unchanged","auto_corrected","context_inferred","unresolved"):
            raise ValueError("Invalid content decision")
        expected="".join(packet["candidates"][r]["text"] for r in chosen)
        if normalized(d.get("refined_text","")) != normalized(expected):
            raise ValueError("Unbacked wording; first create a separately audited inference proposal")
        if not isinstance(d.get("reason"),str) or not d["reason"].strip() or not isinstance(d.get("remaining_uncertainty"),list):
            raise ValueError("Missing reasoning/uncertainty")
        if d["decision"] in ("unresolved","context_inferred") and not d["remaining_uncertainty"]:
            raise ValueError("Unresolved/inferred decision must retain uncertainty")
        selected=[packet["candidates"][r] for r in chosen]
        if selected!=sorted(selected,key=lambda r:(r["start"],r["end"],r["id"])):
            raise ValueError("Selected candidates are out of temporal order")
        if any(r["source_sha256"]!=packet["source_sha256"] or r["start"]<g["start"]-2 or r["end"]>g["end"]+2 for r in selected):
            raise ValueError("Selected candidate extends outside group/source")


def write_results(engine,packet,response,reviewed):
    m=engine.m
    rows=[]
    for g,d in zip(packet["groups"],response["decisions"]):
        rows.append({"id":g["id"],"source_sha256":m["source_sha256"],"start":g["start"],"end":g["end"],
            "original_text":g["baseline_text"],"refined_text":d["refined_text"],
            "candidate_refs":d["candidate_refs"],"evidence_refs":d["selected_refs"],"decision":d["decision"],
            "reason":d["reason"],"review_status":"completed" if reviewed else "not_run",
            "counterevidence":d.get("counterevidence","See complete source candidates"),
            "remaining_uncertainty":d["remaining_uncertainty"],"speaker_id":None,
            "utterance_status":"unconfirmed_supplementary_candidate" if d.get('abstain') else "candidate_selected"})
    status="自动精校稿" if reviewed else "音频复核候选稿（文字裁决尚未完成）"
    header=f"# {status}\n\n源：{Path(m['source']).name}\n\n全文音频复核：{m['coverage']['audio_review']}；文字复核：{'completed' if reviewed else 'not_run'}。\n同家族 ASR 一致不代表逐字正确；未计算 CER/WER。说话人身份未知。\n\n"
    empty=missing_pcm_intervals(m['coverage'])
    if empty:header+='解码未得到音频样本的区间：'+'；'.join(f'{stamp(a)}–{stamp(b)}' for a,b in empty)+'。这些区间不送入 ASR，不等同于有音轨的静音，也不代表已复核画面。\n\n'
    engine.checkpoint("refined.json",{"schema_version":"1.0","run_id":m["run_id"],"segments":rows})
    engine.checkpoint("refined.md",header+"\n\n".join(f"[{stamp(r['start'])}–{stamp(r['end'])}] {r['refined_text'] or '［仅有未确认补查候选；不列为已确认对白］'}"+
        (" ［未决］" if r["decision"]=="unresolved" else "") for r in rows)+"\n")
    changed=[r for r in rows if normalized(r["original_text"])!=normalized(r["refined_text"])]
    engine.checkpoint("changes.md","# 修订记录\n\n"+("\n\n".join(
        f"## {r['id']} {stamp(r['start'])}\n\n原文：{r['original_text']}\n\n候选修订：{r['refined_text']}\n\n决定：{r['decision']}；{r['reason']}\n\n证据：{', '.join(r['evidence_refs'])}\n\n未决：{'；'.join(r['remaining_uncertainty'])}" for r in changed) or "没有自动采用实质改动。")+"\n")
    uncertain=[r for r in rows if r["remaining_uncertainty"]]
    engine.checkpoint("unresolved.md","# 未决与能力限制\n\n"+
        ("文字裁决尚未运行。\n\n" if not reviewed else "")+
        f"图片复核：{m['coverage']['image_review']}；连续视频复核：{m['coverage']['video_review']}；音频能量仅为活动候选，不能证明漏话。\n\n"+
        "\n\n".join(f"- {r['id']} {stamp(r['start'])}：{'；'.join(r['remaining_uncertainty'])}" for r in uncertain)+
        f"\n\n未决段数：{len(uncertain)}。详见 agent_packet.json 的全部反证候选。\n")
    m["coverage"]["text_review"]="completed" if reviewed else "not_run"
    m["counts"]={"segments":len(rows),"flagged":sum(bool(g["flags"]) for g in packet["groups"]),
        "unresolved":len(uncertain),"changed":len(changed)}
    image_ready=not m["config"]["image_review_required"] or m["coverage"]["image_review"]=="completed"
    m["status"]=("completed" if m["coverage"]["audio_review"]=="completed" and image_ready else "partial") if reviewed else "awaiting_agent_review"
    engine.save()


def run_refine(args):
    config=validate_config(read(args.review_config) if args.review_config else {})
    source=args.media.resolve(strict=True)
    info=probe(source)
    streams=[s for s in info["streams"] if s["codec_type"]=="audio"]
    if not streams: raise ValueError("Source has no audio")
    duration=float(info["format"]["duration"])
    start=args.start
    end=args.end if args.end is not None else duration
    if not 0<=start<end<=duration+.05: raise ValueError("Invalid media interval")
    work_root=workspace(args.work_dir)
    output=workspace(args.output)
    source_hash=digest(source)
    models={"baseline":local_model(args.baseline_model),"second":local_model(args.model)}
    params={"source":str(source),"source_sha256":source_hash,"interval":[start,end],"config":config,
        "models":models,"language":args.language,"device":args.device,"compute_type":args.compute_type,
        "extraction":str(args.extraction.resolve()) if args.extraction else None,"version":PARAMS_VERSION}
    estimate={"media_seconds":end-start,"second_windows":math.ceil((end-start)/config["core_seconds"]),
        "temp_peak_estimate_bytes":int((config["baseline_seconds"]+10)*32000+4096),
        "permanent_estimate_bytes":int((end-start)/3600*20*2**20+2**20),
        "work_free_bytes":shutil.disk_usage(work_root).free,"output_free_bytes":shutil.disk_usage(output).free,
        "model_loaded":False,"network_download":False,"uncertainty":"Text/candidate density, retry count and frame retention vary"}
    disk_check(work_root,output,config,estimate["temp_peak_estimate_bytes"],estimate["permanent_estimate_bytes"])
    if args.dry_run:
        print(json.dumps(estimate,ensure_ascii=False,indent=2))
        return
    with exclusive_lock():
        manifest_path=output/"refine_manifest.json"
        if manifest_path.exists():
            if not args.resume: raise ValueError("Output exists; use --resume or a new output")
            m=read(manifest_path)
            if m["params_sha256"]!=object_hash(params): raise ValueError("Source/config/model/parameters changed")
            source_and_artifacts(output,m)
            if Path(m["work_root"])!=work_root: raise ValueError("Work root changed")
            if m["status"]=="completed":
                print(json.dumps({"status":"completed","resume":"verified_without_repeating"}))
                return
        else:
            if any(output.iterdir()): raise ValueError("New output directory must be empty")
            m=dict(params,run_id=uuid.uuid4().hex,params_sha256=object_hash(params),
                audio_stream=streams[0]["index"],duration=duration,streams=info["streams"],
                status="running",artifacts={},created_unix=time.time(),work_root=str(work_root),
                coverage={"audio_review":"not_run","text_review":"not_run","image_review":"not_run","video_review":"not_run"},
                estimate=estimate,temporary_files=[],temporary_peak_bytes=0)
            if args.extraction:
                bundle=load_evidence(args.extraction)
                asset=bundle["media_asset"]
                if asset["source_sha256"]!=source_hash or asset["selected_interval"][0]>start or asset["selected_interval"][1]<end:
                    raise ValueError("Existing extraction is from another source or lacks requested coverage")
                if bundle["run_manifest"]["steps"].get("audio")!="complete": raise ValueError("Baseline audio not complete")
                m["extraction_manifest_sha256"]=digest(args.extraction/"manifest.json")
        m['audio_timeline']=audio_timeline(info,streams[0])
        implementation={'version':VERSION,'source_sha256':digest(Path(__file__))}
        if implementation not in m.setdefault('implementation_history',[]):m['implementation_history'].append(implementation)
        task_work=safe_child(work_root,m["run_id"])
        task_work.mkdir(exist_ok=True)
        import psutil
        owner=psutil.Process()
        m["owner"]={"pid":owner.pid,"created":owner.create_time()}
        engine=Engine(args,m,output,task_work)
        engine.save()
        engine.cleanup()
        try:
            if args.extraction:
                bundle=load_evidence(args.extraction)
                baseline=[dict(id="base-"+r["id"],window="imported_baseline",source_sha256=source_hash,
                    start=r["start"],end=r["end"],text=r["text"],review=r["review_required"])
                    for r in bundle["speech_segments"] if r["start"]<end and r["end"]>start]
                first_dedupe=[]
            else:
                baseline,first_dedupe,_=engine.pass_asr(windows(start,end,config["baseline_seconds"],2,"base"),models["baseline"],True)
            engine.checkpoint("baseline.json",baseline)
            second,second_dedupe,records=engine.pass_asr(windows(start,end,config["core_seconds"],config["context_seconds"],"second"),models["second"],True)
            engine.checkpoint("second.json",second)
            m["coverage"]["second_pass"]={"status":"completed","interval":[start,end],"windows":len(records),
                "input_intervals":[[r["window"]["start"],r["window"]["end"]] for r in records],
                'decoded_intervals':[r['decoded_interval'] for r in records if r.get('decoded_interval')],
                'no_decoded_samples_intervals':[v for r in records for v in r.get('no_decoded_samples_intervals',[])],
                'legacy_windows_without_decoded_frame_count':[r['window']['id'] for r in records if 'decoded_frames' not in r]}
            groups=align(baseline,second)
            for i,g in enumerate(groups): g["flags"]=flag_group(g,config["terms"],i)
            activity_gaps=[]
            for record in records:
                for a,b in record["activity"]:
                    if record["window"]["core_start"]<=a<record["window"]["core_end"] and not any(r["start"]<b and r["end"]>a for r in second):
                        if activity_gaps and a<=activity_gaps[-1][1]+.1: activity_gaps[-1][1]=b
                        else: activity_gaps.append([a,b])
            engine.checkpoint("activity_gaps.json",{"threshold_dbfs":-38,"intervals":activity_gaps,"interpretation":"Possible voice/music/noise; not proof of missing speech"})
            targets=target_windows(groups,activity_gaps,start,end,config["core_seconds"])
            total=len(targets)
            targets=targets[:config["max_target_windows"]]
            retries=[]
            for i,w in enumerate(targets):
                prior=None
                for attempt in range(config["max_rounds"]):
                    prompt="、".join(config["terms"]) if attempt and config["terms"] else None
                    if attempt and not prompt: break  # Don't relabel identical inference as independent evidence.
                    window=dict(w,id=f"retry-{i:05d}-{attempt}")
                    record=engine.recognize(window,models["second"],False,prompt)
                    retries.extend(record["segments"])
                    text=normalized("".join(r["text"] for r in record["segments"]))
                    if text==prior: break
                    prior=text
            engine.checkpoint("retries.json",retries)
            m["coverage"]["targeted_review"]={"planned_windows":total,"processed_windows":len(targets),"max_rounds":config["max_rounds"],
                "status":"completed" if len(targets)==total else "partial","same_model_family":True}
            m["coverage"]["audio_review"]="completed" if len(targets)==total else "partial"
            groups=include_new_turns(groups,retries,config["terms"])
            packet=build_packet(engine,groups,retries,first_dedupe+second_dedupe)
            response=conservative_decisions(packet)
            engine.checkpoint("agent_decisions.template.json",response)
            write_results(engine,packet,response,False)
        except (BudgetStop,KeyboardInterrupt) as exc:
            m["status"]="partial"
            m["last_error"]=str(exc) or "KeyboardInterrupt"
            engine.checkpoint("unresolved.md","# 部分完成\n\n"+m["last_error"]+"\n已有逐块候选已保存，可用同参数 --resume 续跑。\n")
        except Exception as exc:
            m["status"]="failed"
            m["last_error"]=str(exc)[:2000]
            engine.save()
            raise
        finally:
            engine.model=None
            gc.collect()
            engine.cleanup()
            source_and_artifacts(output,m)
            m["elapsed_seconds"]=m.get("elapsed_seconds",0)+round(time.monotonic()-engine.began,3)
            m["permanent_bytes"]=sum(p.stat().st_size for p in output.rglob("*") if p.is_file())
            m["disk_end"]={"work_free":shutil.disk_usage(task_work).free,"output_free":shutil.disk_usage(output).free}
            engine.save()
        print(json.dumps({"status":m["status"],"output":str(output),"counts":m.get("counts")},ensure_ascii=False))


def finalize(args):
    output=args.output.resolve(strict=True)
    with exclusive_lock():
        m=read(output/"refine_manifest.json")
        source_and_artifacts(output,m)
        packet=read(output/m.get("text_review_packet","agent_packet.json"))
        response=read(args.decisions)
        validate_decisions(packet,response)
        engine=Engine(args,m,output,Path(m["work_root"])/m["run_id"])
        engine.checkpoint("agent_decisions.json",response)
        write_results(engine,packet,response,True)
        source_and_artifacts(output,m)
        print(json.dumps({"status":m["status"],"counts":m["counts"]},ensure_ascii=False))


def attach_visual(args):
    output=args.output.resolve(strict=True)
    m=read(output/"refine_manifest.json")
    source_and_artifacts(output,m)
    frames=read(args.frames_dir/"frame_manifest.json")
    observations=read(args.image_observations)
    if frames["params"]["source_sha256"]!=m["source_sha256"] or observations.get("source_sha256")!=m["source_sha256"]:
        raise ValueError("Images/observations belong to another source")
    if observations.get("actual_modalities")!=["image"] or not observations.get("input_transport"):
        raise ValueError("No image-input transport receipt")
    lookup={r["frame"]:r for r in frames["frames"]}
    rows=observations.get("observations",[])
    if not rows or len({r["frame_id"] for r in rows})!=len(rows):
        raise ValueError("Image observation IDs empty or duplicated")
    for row in rows:
        frame=lookup.get(row["frame_id"])
        if not frame or row["frame_sha256"]!=frame["sha256"] or abs(row["time"]-frame["time"])>.001:
            raise ValueError("Image observation frame/time reference invalid")
        if not m["interval"][0]<=row["time"]<=m["interval"][1]:
            raise ValueError("Observed frame outside selected audio interval")
        source=safe_child(args.frames_dir,frame["frame"])
        if digest(source)!=frame["sha256"]:raise ValueError("Observed image bytes changed")
    engine=Engine(args,m,output,Path(m["work_root"])/m["run_id"])
    used=sum(p.stat().st_size for p in output.rglob('*') if p.is_file())
    added=sum(safe_child(args.frames_dir,row['frame_id']).stat().st_size for row in rows)
    disk_check(engine.work,output,m['config'],0,used+added)
    for row in rows:
        name="visual_evidence/"+row["frame_id"]
        dest=safe_child(output,name);dest.parent.mkdir(exist_ok=True)
        shutil.copyfile(safe_child(args.frames_dir,row["frame_id"]),dest)
        m["artifacts"][name]=digest(dest)
    engine.checkpoint("image_observations.json",observations)
    m["coverage"]["image_review"]="completed"
    m["coverage"]["video_review"]="not_run"
    if m["coverage"]["audio_review"]==m["coverage"]["text_review"]=="completed":m["status"]="completed"
    unresolved=output/'unresolved.md'
    if unresolved.exists():
        body=unresolved.read_text(encoding='utf-8')
        body=body.replace('图片/连续视频复核：not_run；','图片复核：completed；连续视频复核：not_run；')
        body=re.sub(r'图片复核：(?:not_run|partial|completed)；连续视频复核：(?:not_run|partial|completed)；',
            '图片复核：completed；连续视频复核：not_run；',body)
        engine.checkpoint('unresolved.md',body)
    engine.save()
    print(json.dumps({"image_review":"completed","actual_images":len(rows),"video_review":"not_run"}))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("media",type=Path,nargs="?")
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--work-dir",type=Path,default=Path("D:/MediaPlatformWork"))
    parser.add_argument("--extraction",type=Path)
    parser.add_argument("--review-config",type=Path)
    parser.add_argument("--language",default="ja")
    parser.add_argument("--mode",choices=["full"],default="full")
    parser.add_argument("--model",default="medium")
    parser.add_argument("--baseline-model",default="medium")
    parser.add_argument("--device",choices=["cpu","cuda"],default="cuda")
    parser.add_argument("--compute-type",choices=["float16","int8_float16","int8","float32"],default="float16")
    parser.add_argument("--start",type=float,default=0)
    parser.add_argument("--end",type=float)
    parser.add_argument("--dry-run",action="store_true")
    parser.add_argument("--resume",action="store_true")
    parser.add_argument("--decisions",type=Path,help="Validated current-agent text response; finalizes without loading ASR")
    parser.add_argument("--verify",action="store_true")
    parser.add_argument("--image-observations",type=Path,help="Current Codex observations with actual image-input receipt")
    parser.add_argument("--frames-dir",type=Path,help="Source-bound frame_review output for the observations")
    args=parser.parse_args()
    if args.verify:
        m=read(args.output/"refine_manifest.json")
        source_and_artifacts(args.output,m)
        print(json.dumps({"valid":True,"status":m["status"],"coverage":m["coverage"]},ensure_ascii=False))
    elif args.image_observations:
        if not args.frames_dir:parser.error("--frames-dir is required with --image-observations")
        attach_visual(args)
    elif args.decisions: finalize(args)
    elif args.media: run_refine(args)
    else: parser.error("Provide media, --decisions or --verify")


if __name__=="__main__":
    main()
