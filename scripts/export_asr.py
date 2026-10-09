#!/usr/bin/env python3
"""Export one local benchmark run into subtitles and an offline review page.

Usage: python3 export_asr.py RUN --audio-url ../audio/sample.wav
Outputs default to RUN/subtitles. --audio-url is a local relative filename,
resolved by the browser relative to review.html; the page also has a file picker.
All subtitle times remain selected-clip-relative. Source offsets are retained
separately. Runtime metrics (including total_time) are never audio durations.
No models, network requests, text correction, or runner mutations are involved.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
from pathlib import Path
import re
import shutil
import sys
sys.dont_write_bytecode = True
from tool_paths import external_path
from typing import Any, Mapping
from urllib.parse import quote, urlsplit
import wave

try:
    from .subtitle_utils import prepare_subtitles, render_markdown, write_subtitle_outputs
except ImportError:
    from subtitle_utils import prepare_subtitles, render_markdown, write_subtitle_outputs


def _numeric(value: Any, *, positive: bool = False) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(number) or number < 0 or (positive and number == 0):
        return None
    return number


def _problem(code: str, message: str, **detail: Any) -> dict[str, Any]:
    return {"code": code, "message": message, "status": "pending", **detail}


def _json_safe(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return {"_non_finite_number": repr(value)}
    if isinstance(value, Mapping):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    return value


def _json(value: Any, *, indent: int | None = 2) -> str:
    return json.dumps(_json_safe(value), ensure_ascii=False, indent=indent, allow_nan=False)


def _read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _local_audio_url(value: str | None) -> str | None:
    if value is None:
        return None
    if not value or any(c in value for c in "\x00\r\n\\"):
        raise ValueError("audio-url must be a non-empty local relative filename")
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or value.startswith("/"):
        raise ValueError("audio-url must be relative; select absolute local files in the review page")
    # Treat query/hash/percent characters as part of the actual local filename.
    return quote(value, safe="/")


def load_run_results(run_dir: str | Path) -> tuple[Path, dict[str, dict[str, Any]], dict[str, Path]]:
    """Prefer results.json; fill missing backends from their result.json files."""
    root = Path(run_dir).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("run directory must be a local directory")
    results: dict[str, dict[str, Any]] = {}
    sources: dict[str, Path] = {}
    combined = root / "results.json"
    if combined.is_file():
        loaded = _read_json(combined)
        if not isinstance(loaded, dict):
            raise ValueError("results.json must contain a backend mapping")
        loaded = loaded.get("backends", loaded.get("results", loaded))
        if not isinstance(loaded, dict):
            raise ValueError("results.json backends must be a mapping")
        for name, result in loaded.items():
            if name not in {"whisper", "qwen"}:
                continue
            if not isinstance(result, dict):
                raise ValueError(f"Backend result for {name} must be an object")
            results[name] = result
            sources[name] = combined
    for name in ["whisper", "qwen"]:
        path = root / name / "result.json"
        if name not in results and path.is_file():
            loaded = _read_json(path)
            if not isinstance(loaded, dict):
                raise ValueError(f"{name}/result.json must contain an object")
            results[name] = loaded
            sources[name] = path
    if not results:
        raise ValueError("No whisper/qwen results found in results.json or backend result.json files")
    return root, results, sources


def _audio_context(root: Path, backend: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    problems: list[dict[str, Any]] = []
    common_path = root / "audio_metadata.json"
    common = _read_json(common_path) if common_path.is_file() else {}
    if not isinstance(common, dict):
        common = {}
        problems.append(_problem("invalid_audio_metadata", "audio_metadata.json must contain an object."))
    specific = backend.get("audio") or {}
    if not isinstance(specific, dict):
        specific = {}
        problems.append(_problem("invalid_backend_audio", "Backend audio metadata must contain an object."))
    metadata = {**common, **specific}
    candidates: list[dict[str, Any]] = []
    for label, data in [("audio_metadata.json", common), ("backend.audio", specific)]:
        count, rate = _numeric(data.get("sample_count"), positive=True), _numeric(data.get("sample_rate"), positive=True)
        if count is not None and rate is not None:
            candidates.append({"source": f"{label}.sample_count/sample_rate", "seconds": count / rate})
        duration = _numeric(data.get("duration_seconds"), positive=True)
        if duration is not None:
            candidates.append({"source": f"{label}.duration_seconds", "seconds": duration})
    # The copied pilot layout has audio/sample.wav. Read its header only.
    sample_path = root / "audio" / "sample.wav"
    if sample_path.is_file():
        try:
            with wave.open(str(sample_path), "rb") as audio:
                measured = audio.getnframes() / audio.getframerate()
            if measured > 0:
                candidates.insert(0, {"source": "audio/sample.wav header", "seconds": measured})
        except (wave.Error, EOFError, OSError, ZeroDivisionError) as error:
            problems.append(_problem("sample_header_unreadable", "Cannot verify copied sample WAV header.", error=str(error)))
    duration = candidates[0]["seconds"] if candidates else None
    duration_source = candidates[0]["source"] if candidates else "unavailable"
    if duration is not None:
        rate = _numeric(metadata.get("sample_rate"), positive=True)
        tolerance = max(1 / rate if rate else 0, 0.001)
        if any(abs(candidate["seconds"] - duration) > tolerance for candidate in candidates):
            problems.append(_problem("audio_duration_conflict", "Audio duration sources disagree; header/sample count is preferred.", candidates=candidates))
    offset_candidates = [(label, _numeric(data.get("source_offset_seconds"))) for label, data in [("audio_metadata.json", common), ("backend.audio", specific)]]
    offsets = [(label, value) for label, value in offset_candidates if value is not None]
    offset = offsets[-1][1] if offsets else None
    if offsets and any(abs(value - offset) > .001 for _, value in offsets):
        problems.append(_problem("source_offset_conflict", "Backend and shared source offsets disagree.", offsets=dict(offsets)))
    if offset is None:
        problems.append(_problem("source_offset_unknown", "Source offset is unavailable; source-relative seeking is disabled."))
    return {
        "metadata": metadata,
        "duration_seconds": duration,
        "duration_source": duration_source,
        "duration_candidates": candidates,
        "source_offset_seconds": offset,
        "source_end_seconds": offset + duration if offset is not None and duration is not None else None,
        "timeline": "subtitle start/end are selected-clip-relative; source_start/end include the source offset",
    }, problems


def _backend_pending(backend: Mapping[str, Any]) -> list[dict[str, Any]]:
    state = backend.get("state", backend.get("status", "unknown"))
    problems = []
    if state not in {"complete", "passed"}:
        problems.append(_problem("backend_state_pending", "Backend run has not completed without pending issues.", backend_state=state))
    for key in ["warnings", "pending"]:
        values = backend.get(key, [])
        if not isinstance(values, list):
            values = [values] if values else []
        for warning in values:
            if isinstance(warning, dict):
                problems.append({**warning, "code": warning.get("code", f"backend_{key}"), "message": warning.get("message", "Backend reported an unresolved item."), "status": "pending", "origin": "backend"})
            else:
                problems.append(_problem(f"backend_{key}", str(warning), origin="backend"))
    truncation = backend.get("truncation")
    if isinstance(truncation, dict) and (truncation.get("limit_reached") or truncation.get("truncated") is True):
        problems.append(_problem("backend_possible_truncation", "Backend token-limit/truncation status requires review.", truncation=truncation))
    if backend.get("exception"):
        problems.append(_problem("backend_exception", "Backend recorded an exception.", exception=backend["exception"]))
    return problems


def prepare_backend_export(
    root: Path, name: str, backend: Mapping[str, Any], source_path: Path, *, max_cue_chars: int = 36, pause_threshold: float = .6, aggregate_zero_duration: bool = False,
    merge_short_fragments: bool = False, short_fragment_max_lexical: int = 6, short_fragment_max_gap: float = 2.0,
    blocked_raw_spans: list[list[int]] | None = None
) -> dict[str, Any]:
    audio, audio_problems = _audio_context(root, backend)
    raw_text = backend.get("raw_text", "")
    input_problems = []
    if not isinstance(raw_text, str):
        raw_text = ""
        input_problems.append(_problem("invalid_raw_text", "Backend raw_text is not a string."))
    units = backend.get("units", [])
    if not isinstance(units, list):
        units = []
        input_problems.append(_problem("invalid_units", "Backend units are not a list."))
    merge_settings = ({"merge_short_fragments": True, "short_fragment_max_lexical": short_fragment_max_lexical,
                       "short_fragment_max_gap": short_fragment_max_gap, "blocked_raw_spans": blocked_raw_spans or []}
                      if merge_short_fragments else {})
    if merge_short_fragments and not aggregate_zero_duration:
        raise ValueError("Short-fragment merging requires aggregate_zero_duration")
    result = prepare_subtitles(units, raw_text, audio["duration_seconds"], max_cue_chars=max_cue_chars, pause_threshold=pause_threshold, aggregate_zero_duration=aggregate_zero_duration, **merge_settings)
    result["backend"] = name
    result["backend_state"] = backend.get("state", backend.get("status", "unknown"))
    result["backend_metadata"] = {key: value for key, value in backend.items() if key not in {"raw_text", "units", "audio"}}
    result["audio"] = audio
    result["input_result"] = {"path": str(source_path), "sha256": hashlib.sha256(source_path.read_bytes()).hexdigest()}
    pending = _backend_pending(backend) + audio_problems + input_problems
    offset = audio["source_offset_seconds"]
    for segment in result["sentences"] + result["cues"] + result.get("pre_merge_cues", []):
        start, end = _numeric(segment.get("start")), _numeric(segment.get("end"))
        segment["source_start"] = start + offset if start is not None and offset is not None else None
        segment["source_end"] = end + offset if end is not None and offset is not None else None
    if offset is not None:
        for index, unit in enumerate(units):
            if not isinstance(unit, dict):
                continue
            for clip_field, source_field in [("start", "source_start"), ("end", "source_end")]:
                clip_time, source_time = _numeric(unit.get(clip_field)), _numeric(unit.get(source_field))
                if clip_time is not None and source_time is not None and abs(source_time - clip_time - offset) > .001:
                    pending.append(_problem("unit_source_offset_conflict", "Unit source timestamp disagrees with the declared offset; original unit is retained.", unit_index=index, field=source_field))
    result["pending"] = pending + result["pending"]
    result["validation"].update({"passed": not result["pending"], "status": "pending" if result["pending"] else "passed", "pending_count": len(result["pending"]), "backend_state_preserved": result["backend_state"]})
    return result


def _comparison_row(name: str, result: Mapping[str, Any]) -> dict[str, Any]:
    timings = result["backend_metadata"].get("timings") or {}
    asr = _numeric(timings.get("asr_inference_seconds")) if isinstance(timings, dict) else None
    alignment = _numeric(timings.get("alignment_inference_seconds")) if isinstance(timings, dict) else None
    inference = asr + (alignment or 0) if asr is not None else None
    duration = result["audio"]["duration_seconds"]
    row = {
        "backend": name,
        "backend_state": result["backend_state"],
        "subtitle_status": result["validation"]["status"],
        "pending_count": len(result["pending"]),
        "audio_duration_seconds": duration,
        "audio_duration_source": result["audio"]["duration_source"],
        "source_offset_seconds": result["audio"]["source_offset_seconds"],
        "text_chars": len(result["raw_text"]),
        "aligned_units": len(result["units"]),
        "alignment_coverage": result["validation"]["alignment_coverage"],
        "sentences": len(result["sentences"]),
        "cues": len(result["cues"]),
        "exported_cues": result["validation"]["exported_cue_count"],
        "withheld_cues": result["validation"]["withheld_cue_count"],
        "provisional_cues": result["validation"]["provisional_cue_count"],
        "exported_text_coverage": result["validation"]["exported_text_coverage"],
        "asr_inference_seconds": asr,
        "alignment_inference_seconds": alignment,
        "combined_inference_seconds": inference,
        "inference_realtime_factor": inference / duration if inference is not None and duration else None,
        "inference_audio_seconds_per_second": duration / inference if duration and inference else None,
        "backend_timings": timings,
    }
    if result.get("settings", {}).get("merge_short_fragments"):
        row["short_fragment_merge_count"] = result["validation"].get("short_fragment_merge_count", 0)
    return row


def _comparison_markdown(report: Mapping[str, Any]) -> str:
    lines = ["# 本地转写对比", "", f"运行目录：{report['run_directory']}", "", "时间均以样本音频为原点；原始音频偏移另列。", "", "| 模型 | 后端状态 | 字幕校验 | 样本秒数 | 来源偏移秒 | ASR秒 | 对齐秒 | 导出／保留字幕 | 待验收项 |", "|---|---|---|---:|---:|---:|---:|---:|---:|"]
    def number(value: Any) -> str:
        return f"{value:.3f}" if isinstance(value, (int, float)) else "未知"
    for row in report["backends"]:
        lines.append(f"| {row['backend']} | {row['backend_state']} | {row['subtitle_status']} | {number(row['audio_duration_seconds'])} | {number(row['source_offset_seconds'])} | {number(row['asr_inference_seconds'])} | {number(row['alignment_inference_seconds'])} | {row['exported_cues']}／{row['cues']} | {row['pending_count']} |")
    lines.extend(["", report["quality_note"], "", "## 待验收项", ""])
    for name, issues in report["pending"].items():
        if issues:
            for issue in issues:
                lines.append(f"- {name} `{issue['code']}`：{issue.get('message', '')}")
        else:
            lines.append(f"- {name}：自动校验无异常，仍需人工听音复核。")
    return "\n".join(lines) + "\n"


def render_review_html(exports: Mapping[str, Any], report: Mapping[str, Any], audio_url: str | None, timeline: str = "clip") -> str:
    """Self-contained page: safe JSON embedding, textContent, local audio only."""
    data = {"exports": exports, "report": report, "audio_url": audio_url, "audio_timeline": timeline}
    embedded = _json(data, indent=None).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    title = html.escape(Path(report["run_directory"]).name)
    document = _HTML.replace("__TITLE__", title).replace("__DATA__", embedded)
    if any(result.get("settings", {}).get("merge_short_fragments") for result in exports.values()):
        notice = '<p class="notice">短碎片整理版：零时长的短词仅在同一音频段、邻句时间有依据且间隔很近时并入上下句。原始文字和逐字时间保留；疑似提示词回声单独待复核。合并仅改善字幕阅读，不证明文本准确。</p>'
        blocked_texts = []
        for name, result in exports.items():
            for entry in result.get("blocked_raw_spans", []):
                begin, end = entry["raw_span"]
                blocked_texts.append('<li>' + html.escape(name + '：' + result["raw_text"][begin:end]) + '</li>')
        if blocked_texts:
            notice += '<details class="issues"><summary>单独隔离的可疑转写</summary><p>以下内容疑似提示词回声或重复识别，未作为已核实的语音内容接受，也未并入邻句。原始识别结果保留用于复核。</p><ul>' + ''.join(blocked_texts) + '</ul></details>'
        document = document.replace('<section class="player">', notice + '<section class="player">', 1)
    return document


_HTML = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; media-src 'self' file: blob:; connect-src 'none';">
<title>字幕复核 · __TITLE__</title>
<style>
:root{font-family:system-ui,-apple-system,sans-serif;color:#e6edf7;background:#101621;color-scheme:dark}*{box-sizing:border-box}body{margin:0}main{max-width:1160px;margin:auto;padding:28px}h1{font-size:25px;margin:0 0 8px}p{line-height:1.6}.muted{color:#9baac0;font-size:14px}.player{position:sticky;top:0;background:#182231;padding:16px;border:1px solid #334155;border-radius:12px;z-index:1;margin:20px 0}audio{width:100%;margin:12px 0 4px}.controls{display:flex;gap:12px;align-items:center;flex-wrap:wrap}select,input,button{font:inherit}button,select,.pick{border:1px solid #475569;border-radius:7px;padding:9px 12px;background:#233246;color:#edf3fc;cursor:pointer}input[type=file]{max-width:300px;font-size:13px}.tabs{display:flex;gap:10px;flex-wrap:wrap;margin:20px 0}.tabs button[aria-selected=true]{background:#265d89;border-color:#61b8f1}.badge{display:inline-block;border-radius:6px;padding:4px 8px;font-size:13px}.pending{color:#ffce85;background:#4b351d}.passed{color:#a6e7be;background:#17452f}.summary{display:flex;gap:12px;flex-wrap:wrap;margin:12px 0}.stat{padding:12px;border:1px solid #334155;border-radius:8px;min-width:140px}.stat b{display:block;font-size:20px;margin-top:5px}.notice{border-left:4px solid #d39942;background:#2e2519;padding:12px;line-height:1.6}.links{display:flex;gap:14px;margin:18px 0;flex-wrap:wrap}a{color:#89caf8}.cues{display:grid;gap:8px;margin:15px 0}.cue{display:grid;grid-template-columns:180px 1fr;gap:12px;align-items:start;padding:14px;background:#182231;border:1px solid #334155;border-radius:9px}.cue.pending-row{border-color:#99652c}.cue.active{border-color:#73bef0;background:#1e3850}.time{font-size:13px;text-align:left;line-height:1.45;padding:6px 8px}.time:disabled{cursor:default;color:#b6a180;border-color:#665038}.cue-text{font-size:18px;line-height:1.65;white-space:pre-wrap}.origin{font-size:12px;color:#a5b5c8;margin:5px 0}.issues{background:#211c18;border:1px solid #70502c;border-radius:9px;padding:14px;margin:16px 0}li{margin:8px 0;overflow-wrap:anywhere}pre{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.8}details{margin:18px 0}summary{cursor:pointer;font-weight:600}.empty{padding:20px;border:1px dashed #665038;color:#ffce85}footer{border-top:1px solid #334155;margin-top:28px;padding-top:12px;color:#9baac0;font-size:13px}#seek-status{min-height:20px;color:#ffce85}@media(max-width:640px){main{padding:16px}.cue{grid-template-columns:1fr}.player{position:static}.cue-text{font-size:17px}}
</style></head><body><main>
<h1>字幕复核 · __TITLE__</h1><div class="muted">全文与异常均保留。点击有效时间播放对应片段，听音确认文本和边界。</div>
<section class="player"><div class="controls"><label>音频时间轴 <select id="timeline"><option value="clip">样本音频（从 0 秒起）</option><option value="source">原始音频（包含来源偏移）</option></select></label><label class="pick">选择本地音频 <input id="audio-file" type="file" accept="audio/*,video/*"></label></div><audio id="player" controls preload="metadata"></audio><div id="audio-label" class="muted"></div><div id="seek-status" role="status"></div></section>
<nav id="tabs" class="tabs" aria-label="转写模型"></nav><section id="backend-view"></section>
<footer id="quality-note"></footer></main><script type="application/json" id="review-data">__DATA__</script>
<script>
'use strict';
const data=JSON.parse(document.getElementById('review-data').textContent);
const player=document.getElementById('player'),timeline=document.getElementById('timeline'),view=document.getElementById('backend-view'),tabs=document.getElementById('tabs');
const status=document.getElementById('seek-status');let activeName=Object.keys(data.exports)[0],objectUrl=null;
const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
const finite=v=>typeof v==='number'&&Number.isFinite(v);
const fmt=v=>finite(v)?v.toFixed(3):'未知';
const stamp=v=>{if(!finite(v))return'时间待确认';const h=Math.floor(v/3600),m=Math.floor(v%3600/60),s=v%60;return(h?String(h).padStart(2,'0')+':':'')+String(m).padStart(2,'0')+':'+s.toFixed(3).padStart(6,'0');};
function seek(cue){const b=data.exports[activeName],offset=b.audio.source_offset_seconds;status.textContent='';if(!player.currentSrc&&!player.getAttribute('src')){status.textContent='请先选择本地音频。';return;}if(timeline.value==='source'&&!finite(offset)){status.textContent='来源偏移未知，请使用样本音频时间轴。';return;}const target=cue.start+(timeline.value==='source'?offset:0);if(finite(player.duration)&&target>=player.duration){status.textContent='时间超出当前音频，请确认所选文件和时间轴。';return;}try{player.currentTime=target;const promise=player.play();if(promise&&promise.catch)promise.catch(()=>{status.textContent='已定位片段，请点击播放器播放。';});}catch(error){status.textContent='请等待音频读取完成后再点击时间。';}}
function render(){const b=data.exports[activeName],row=data.report.backends.find(r=>r.backend===activeName);view.replaceChildren();for(const button of tabs.children)button.setAttribute('aria-selected',String(button.dataset.backend===activeName));
view.append(el('h2',activeName==='qwen'?'Qwen3-ASR + ForcedAligner':'Whisper'));
view.append(el('span',b.validation.passed?'自动校验通过 · 仍需听音复核':'待验收 · '+b.pending.length+' 项异常','badge '+(b.validation.passed?'passed':'pending')));
view.append(el('p','后端原始状态：'+b.backend_state+' · 样本 '+fmt(b.audio.duration_seconds)+' 秒 · 来源偏移 '+fmt(b.audio.source_offset_seconds)+' 秒','muted'));
const stats=el('div',undefined,'summary');for(const [label,value] of [['正文字符',row.text_chars],['语义句子',row.sentences],['字幕已导出 / 全部',row.exported_cues+' / '+row.cues],['文本对齐覆盖',Math.round(row.alignment_coverage*10000)/100+'%']]){const box=el('div',label,'stat');box.append(el('b',String(value)));stats.append(box);}view.append(stats);
if(!b.validation.passed)view.append(el('div','部分字幕时间或后端结果尚未确认。异常正文仍显示；缺乏可靠时间的字幕未写入 SRT / VTT。','notice'));
const links=el('div',undefined,'links');for(const [label,ext] of [['JSON','json'],['SRT','srt'],['VTT','vtt'],['Markdown','md']]){const link=el('a',(b.settings.aggregate_zero_duration?'聚合待验收 ':'下载 ')+label);link.href='./'+activeName+'/'+(b.download_stem||'transcript')+'.'+ext;link.download='';links.append(link);}if(b.settings.aggregate_zero_duration){for(const ext of ['srt','vtt']){const link=el('a','严格版 '+ext.toUpperCase());link.href='./'+activeName+'/transcript.strict.'+ext;link.download='';links.append(link);}view.append(el('p','聚合区间仅使用首末原始单元边界；内部零时长字没有独立时间。小数位只是显示格式，不代表毫秒精度。','notice'));}view.append(links);
const full=el('details');full.append(el('summary','查看完整原文'));full.append(el('pre',b.raw_text));view.append(full);
const sentences=el('details');sentences.append(el('summary','完整句子 · 点击首末边界区间'));const sentenceList=el('div',undefined,'cues');for(const sentence of b.sentences){const item=el('article',undefined,'cue');const button=el('button',sentence.timing_valid?stamp(sentence.start)+' → '+stamp(sentence.end):'时间待确认','time');button.disabled=!sentence.timing_valid;button.addEventListener('click',()=>seek(sentence));item.append(button,el('div',sentence.text,'cue-text'));sentenceList.append(item);}sentences.append(sentenceList);view.append(sentences);
if(b.pending.length){const issues=el('details',undefined,'issues');issues.append(el('summary','待验收项（'+b.pending.length+'，点击展开）'));const list=el('ul');for(const issue of b.pending){const details=Object.fromEntries(Object.entries(issue).filter(([key])=>!['code','message','status'].includes(key)));list.append(el('li',issue.code+'：'+(issue.message||'')+(Object.keys(details).length?' '+JSON.stringify(details):'')));}issues.append(list);view.append(issues);}
const list=el('div',undefined,'cues');if(!b.cues.length)list.append(el('div','暂无字幕条目；完整正文和后端异常请查看上方。','empty'));
for(const cue of b.cues){const item=el('article',undefined,'cue'+(cue.exportable?'':' pending-row'));item.dataset.cue=cue.id;const times=el('div');const button=el('button',cue.timing_valid?stamp(cue.start)+' → '+stamp(cue.end):'时间待确认','time');button.disabled=!cue.timing_valid;button.addEventListener('click',()=>seek(cue));times.append(button);times.append(el('div','原始音频 '+stamp(cue.source_start)+' → '+stamp(cue.source_end),'origin'));times.append(el('div','字幕 '+cue.id+' · 句 '+cue.sentence_ids.join(', '),'origin'));if(cue.provisional)times.append(el('div','聚合待验收 · '+cue.zero_duration_unit_indices.length+' 个零时长单元','origin'));item.append(times,el('div',cue.text,'cue-text'));list.append(item);}view.append(list);}
for(const [name,b] of Object.entries(data.exports)){const button=el('button',(name==='qwen'?'Qwen + 对齐':'Whisper')+' · '+(b.validation.passed?'校验通过':'待验收 '+b.pending.length));button.dataset.backend=name;button.setAttribute('role','tab');button.addEventListener('click',()=>{activeName=name;render();});tabs.append(button);}
timeline.value=data.audio_timeline;timeline.addEventListener('change',()=>{status.textContent='';updateActive();});
if(data.audio_url){player.src=data.audio_url;document.getElementById('audio-label').textContent='本地相对路径：'+data.audio_url;}else document.getElementById('audio-label').textContent='请选择本地样本音频；若选择原始音频，请切换时间轴。';
document.getElementById('audio-file').addEventListener('change',event=>{const file=event.target.files[0];if(!file)return;if(objectUrl)URL.revokeObjectURL(objectUrl);objectUrl=URL.createObjectURL(file);player.src=objectUrl;document.getElementById('audio-label').textContent='已选择：'+file.name+'（请确认时间轴）';status.textContent='';});
player.addEventListener('error',()=>{status.textContent='音频无法读取。可通过上方文件选择器载入本地音频。';});
function updateActive(){const b=data.exports[activeName],offset=timeline.value==='source'?b.audio.source_offset_seconds:0;const t=finite(offset)?player.currentTime-offset:NaN;for(const item of view.querySelectorAll('[data-cue]')){const cue=b.cues.find(c=>String(c.id)===item.dataset.cue);item.classList.toggle('active',cue.timing_valid&&t>=cue.start&&t<cue.end);}}
player.addEventListener('timeupdate',updateActive);document.getElementById('quality-note').textContent=data.report.quality_note;render();
</script></body></html>'''


def export_run(
    run_dir: str | Path, *, output_dir: str | Path | None = None, audio_url: str | None = None, audio_timeline: str = "clip", max_cue_chars: int = 36, pause_threshold: float = .6, aggregate_zero_duration: bool = False,
    merge_short_fragments: bool = False, short_fragment_max_lexical: int = 6, short_fragment_max_gap: float = 2.0,
    blocked_raw_spans: list[list[int]] | None = None
) -> dict[str, Any]:
    if audio_timeline not in {"clip", "source"}:
        raise ValueError("audio_timeline must be clip or source")
    if merge_short_fragments and not aggregate_zero_duration:
        raise ValueError("Short-fragment merging requires aggregate_zero_duration")
    audio_url = _local_audio_url(audio_url)
    root, backends, sources = load_run_results(run_dir)
    destination = external_path(output_dir if output_dir else root / "subtitles")
    destination.mkdir(parents=True, exist_ok=True)
    exports, output_paths, strict_exports = {}, {}, {}
    for name, backend in backends.items():
        strict_result = prepare_backend_export(root, name, backend, sources[name], max_cue_chars=max_cue_chars, pause_threshold=pause_threshold)
        strict_exports[name] = strict_result
        backend_dir = destination / name
        existing = {key: backend_dir / f"transcript.{ext}" for key, ext in {"json": "json", "srt": "srt", "vtt": "vtt", "markdown": "md"}.items()}
        if aggregate_zero_duration and all(path.is_file() for path in existing.values()):
            paths = {key: str(path) for key, path in existing.items()}
        else:
            paths = write_subtitle_outputs(strict_result, backend_dir)
        strict_paths = {}
        if aggregate_zero_duration:
            for key, original_path in paths.items():
                original = Path(original_path)
                snapshot = original.with_name("transcript.strict" + original.suffix)
                if not snapshot.exists():
                    shutil.copyfile(original, snapshot)
                strict_paths[key] = str(snapshot)
            result = prepare_backend_export(root, name, backend, sources[name], max_cue_chars=max_cue_chars, pause_threshold=pause_threshold, aggregate_zero_duration=True,
                                            merge_short_fragments=merge_short_fragments, short_fragment_max_lexical=short_fragment_max_lexical,
                                            short_fragment_max_gap=short_fragment_max_gap, blocked_raw_spans=blocked_raw_spans)
            result["strict_pending"] = strict_result["pending"]
            result["download_stem"] = "transcript.aggregated.provisional"
            provisional_paths = write_subtitle_outputs(result, backend_dir, "transcript.aggregated.provisional")
        else:
            result, provisional_paths = strict_result, {}
        exports[name] = result
        # Add explicit source/clip metadata before the generic Markdown body.
        audio = result["audio"]
        preamble = f"模型：{name}；后端原始状态：{result['backend_state']}\n\n样本音频时长：{audio['duration_seconds']} 秒（{audio['duration_source']}）；来源偏移：{audio['source_offset_seconds']} 秒。字幕文件时间以样本为原点。\n\n"
        if aggregate_zero_duration:
            merge_note = "短碎片合并：同段邻句须有正时长依据，不新增或估算时间；原始字幕条目及合并记录在 JSON 中保留。疑似回声不自动并入。\n\n" if merge_short_fragments else ""
            Path(provisional_paths["markdown"]).write_text(preamble + "显式聚合模式：仅使用首末原始单元边界；含零时长单元的区间为 provisional，不具备逐字时间精度。严格版文件原样保留。\n\n" + merge_note + render_markdown(result), encoding="utf-8")
            output_paths[name] = {**paths, "strict": strict_paths, "provisional": provisional_paths}
        else:
            Path(paths["markdown"]).write_text(preamble + render_markdown(result), encoding="utf-8")
            output_paths[name] = paths
    report = {
        "schema_version": 1,
        "run_directory": str(root),
        "output_directory": str(destination),
        "state": "passed" if all(r["validation"]["passed"] for r in exports.values()) else "pending_review",
        "export_mode": "aggregate_zero_duration_merged_fragments_provisional" if merge_short_fragments else "aggregate_zero_duration_provisional" if aggregate_zero_duration else "strict",
        "backends": [_comparison_row(name, result) for name, result in exports.items()],
        "strict_backends": [_comparison_row(name, result) for name, result in strict_exports.items()],
        "strict_pending": {name: result["pending"] for name, result in strict_exports.items()},
        "pending": {name: result["pending"] for name, result in exports.items()},
        "quality_note": "没有人工参考文本，运行速度、时间校验和模型一致性均不能证明转写准确率。正文未经自动纠错；所有后端待验收状态保留。计算耗时不作为音频时长。",
    }
    report_json = destination / "comparison_report.json"
    report_md = destination / "comparison_report.md"
    review = destination / "review.html"
    report_json.write_text(_json(report) + "\n", encoding="utf-8")
    report_md.write_text(_comparison_markdown(report), encoding="utf-8")
    review.write_text(render_review_html(exports, report, audio_url, audio_timeline), encoding="utf-8")
    return {"state": report["state"], "output_directory": str(destination), "backends": output_paths, "report_json": str(report_json), "report_markdown": str(report_md), "review_html": str(review)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path, help="One local run directory")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--audio-url", help="Local relative audio filename, relative to review.html")
    parser.add_argument("--audio-timeline", choices=["clip", "source"], default="clip")
    parser.add_argument("--max-cue-chars", type=int, default=36)
    parser.add_argument("--pause-threshold", type=float, default=.6)
    parser.add_argument("--aggregate-zero-duration", action="store_true", help="Also export provisional intervals using native first/last boundaries; retain strict files and all zero-duration warnings")
    parser.add_argument("--merge-short-fragments", action="store_true", help="Combine zero-duration fragments with adjacent supported cues; requires --aggregate-zero-duration")
    parser.add_argument("--short-fragment-max-lexical", type=int, default=6)
    parser.add_argument("--short-fragment-max-gap", type=float, default=2.0)
    parser.add_argument("--blocked-spans-json", type=Path, help="JSON list of raw-text intervals excluded from short-fragment merging")
    parser.add_argument("--strict", action="store_true", help="Return exit 2 after a successful export with pending validation")
    args = parser.parse_args(argv)
    if args.merge_short_fragments and not args.aggregate_zero_duration:
        parser.error("--merge-short-fragments requires --aggregate-zero-duration")
    blocked_path = args.blocked_spans_json
    del args.blocked_spans_json
    strict = args.strict
    del args.strict
    try:
        args.blocked_raw_spans = json.loads(blocked_path.read_text(encoding="utf-8")) if blocked_path else None
        result = export_run(**vars(args))
        print(_json(result))
        return 2 if strict and result["state"] != "passed" else 0
    except (ValueError, TypeError, OSError, json.JSONDecodeError) as error:
        print(f"Export failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
