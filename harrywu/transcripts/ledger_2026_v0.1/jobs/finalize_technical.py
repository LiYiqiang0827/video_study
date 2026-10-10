"""Wait once for finite ASR/frame jobs, then bind existing evidence viewers."""
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
import psutil
from media_platform.evidence import load_evidence, sha256_file
from media_platform.frame_review import make_html

ROOT=Path(__file__).resolve().parents[1]

def save(state):
    path=ROOT/'technical_status.json'
    temp=path.with_suffix('.tmp')
    temp.write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding='utf-8')
    os.replace(temp,path)

def await_job(status_name, allowed_final):
    state=json.loads((ROOT/status_name).read_text(encoding='utf-8'))
    if state['status'] not in allowed_final:
        try:
            process=psutil.Process(state['runner_pid'])
            if str(ROOT/'jobs').casefold() not in ' '.join(process.cmdline()).casefold():
                raise RuntimeError('PID identity no longer belongs to this job')
            process.wait(timeout=14400)
        except psutil.NoSuchProcess:
            pass
    state=json.loads((ROOT/status_name).read_text(encoding='utf-8'))
    if state['status'] not in allowed_final:
        raise RuntimeError(f"Required task did not complete: {status_name}, {state['status']}")

def main():
    state=dict(status='waiting_for_finite_jobs',runner_pid=os.getpid(),started=datetime.now(timezone.utc).isoformat())
    save(state)
    await_job('pilot_status.json',{'asr_complete_pending_video_review'})
    await_job('frames_status.json',{'ten_second_frames_complete_scene_and_semantic_review_pending'})
    await_job('scene_status.json',{'complete'})
    config=json.loads((ROOT/'config_v0.1.json').read_text(encoding='utf-8'))
    with (ROOT/'streams_inventory.csv').open(encoding='utf-8-sig',newline='') as f:
        inventory={r['stream_id']:r for r in csv.DictReader(f)}
    candidates=[]
    stats=[]
    terms=re.compile(r'买|卖|加仓|减仓|持仓|仓位|止损|止盈|趋势线|颈线|箱体|沽压|目标|双突破|2[Bb]')
    for pilot in config['pilots']:
        video_id=pilot['stream_id']
        extraction=ROOT/'extractions'/video_id
        bundle=load_evidence(extraction)
        segments=bundle['speech_segments']
        manifest=json.loads((extraction/'manifest.json').read_text(encoding='utf-8'))
        frame_root=ROOT/'frames'/video_id
        frame_manifest=json.loads((frame_root/'frame_manifest.json').read_text(encoding='utf-8'))
        if frame_manifest['params']['source_sha256']!=bundle['media_asset']['source_sha256']:
            raise RuntimeError('Frame/speech source mismatch')
        merged=list(frame_manifest['frames'])
        scene_root=frame_root/'scene_changes'
        if scene_root.exists():
            scene_manifest=json.loads((scene_root/'frame_manifest.json').read_text(encoding='utf-8'))
            if scene_manifest['params']['source_sha256']!=bundle['media_asset']['source_sha256']:
                raise RuntimeError('Scene/speech source mismatch')
            merged.extend([{**r,'frame':'scene_changes/'+r['frame']} for r in scene_manifest['frames']])
            make_html(scene_root,scene_manifest,segments)
        merged.sort(key=lambda r:r['time'])
        for row in merged:
            if sha256_file(frame_root/row['frame'])!=row['sha256']:
                raise RuntimeError('Frame hash mismatch before binding')
        review_manifest={**frame_manifest,'frames':merged,'speech_extraction':str(extraction),'combined_view_only':True}
        (frame_root/'review_manifest.json').write_text(json.dumps(review_manifest,ensure_ascii=False,indent=2),encoding='utf-8')
        make_html(frame_root,review_manifest,segments)
        raw=(extraction/'speech.md').read_text(encoding='utf-8')
        header=(f"# {inventory[video_id]['title']}\n\n视频ID：{video_id}\n\n"
                f"实际直播开始（北京时间）：{inventory[video_id]['actual_start_beijing']}\n\n"
                '全片自动转写，large-v3 / CUDA / float16，300秒分块。\n\n'
                '识别原文及分块边界留存；说话人类别、证券相关校正及完整内容核对待完成。\n\n')
        (ROOT/'transcripts'/f'{video_id}.md').write_text(header+raw,encoding='utf-8')
        for segment in segments:
            if terms.search(segment['text']):
                candidates.append(dict(stream_id=video_id,segment_id=segment['id'],start=segment['start'],end=segment['end'],
                    context_start=max(0,segment['start']-30),context_end=min(manifest['duration'],segment['end']+30),
                    asr_text=segment['text'],status='keyword_candidate_only_not_an_event'))
        stats.append(dict(stream_id=video_id,segments=len(segments),chunks=len(manifest['chunks']),
                          seconds=manifest['duration'],frames=len(merged),asr_review_flags=manifest['audio']['review_count']))
    with (ROOT/'speech_review_candidates.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(candidates[0]))
        writer.writeheader()
        writer.writerows(candidates)
    report=ROOT/'qa_report.md'
    addition='\n\n## 技术作业完成记录\n\n'+ '\n'.join(f"- {x['stream_id']}：{x['seconds']:.3f}秒，全片{x['chunks']}个分块、{x['segments']}段转写、{x['frames']}张定时/变化帧；ASR待核标记{x['asr_review_flags']}段。" for x in stats)
    addition+='\n\n三个场次的源哈希、全片分块数量与参数、平台证据校验和画面哈希均通过。frames/各场/index.html已经绑定附近原始语音，可搜索并打开原图。speech_review_candidates.csv只是关键词定位索引，不是事件台账，不能据此计算漏记率。\n'
    text=report.read_text(encoding='utf-8')
    text=text.replace('状态：转写与证据准备中；尚未完成三场事件提取或人工验收。不得据此启动批量或行情评估。',
                      '状态：三场全片技术转写、抽帧和语音画面绑定完成；完整事件提取和人工验收尚未完成。不得据此启动批量或行情评估。')
    text=text.replace('正在按全片每10秒保存原帧（包含闲聊，作为图表时段要求的保守超集），不自动删去相似画面。还需画面变化补帧、语音涉及操作/证券/持仓/画线的前后30秒定点核对，以及证券身份清单匹配。',
                      '已按全片每10秒保存原帧（包含闲聊，作为图表时段要求的保守超集），并补充画面变化帧；不自动删去相似画面。还需全部证券/操作/持仓/画线片段的前后30秒核对及证券身份清单匹配。')
    text=text.replace('三场完整ASR验证、说话人标记、A/B/F代码识别率、漏记率、事件时间和分类争议均待完成。',
                      '三场完整ASR的技术验证已通过；说话人标记、A/B/F代码识别率、漏记率、事件时间和分类争议的完整内容核对仍待完成。')
    report.write_text(text+addition,encoding='utf-8')
    state.update(status='technical_complete_semantic_pilot_pending',stats=stats,candidate_segments=len(candidates),
                 ended=datetime.now(timezone.utc).isoformat())
    save(state)
    # A technical checkpoint is separate from the final human-approved ledger freeze.
    files=[ROOT/'config_v0.1.json',ROOT/'rules_v0.1.md',ROOT/'streams_inventory.csv',ROOT/'source_SHA256SUMS',
           ROOT/'qa_report.md',ROOT/'speech_review_candidates.csv',ROOT/'technical_status.json']
    files += list((ROOT/'transcripts').glob('*.md'))
    (ROOT/'TECHNICAL_SHA256SUMS').write_text('\n'.join(f"{sha256_file(p)}  {p.relative_to(ROOT).as_posix()}" for p in files)+'\n',encoding='utf-8')

if __name__=='__main__':
    try:
        main()
    except BaseException as exc:
        path=ROOT/'technical_status.json'
        state=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        state.update(status='failed',error=f'{type(exc).__name__}: {exc}')
        save(state)
        raise
