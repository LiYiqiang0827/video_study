#!/usr/bin/env python3
"""Render speaker-view analysis as an offline HTML page and plain text.

No model calls, network requests or changes to source videos/subtitles. Runtime
outputs live outside this tool checkout; existing different output is preserved.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import html
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import quote, urlsplit

sys.dont_write_bytecode = True
from tool_paths import external_path

UNKNOWN_BJ = "未知"
RECORDING_KINDS = {"intraday_live": "盘中直播", "postmarket_review": "盘后复盘", "mixed": "混合内容", "unknown": "未明确"}
ENTITY_KINDS = {"stock": "股票", "index": "指数", "method": "分析方法", "market": "市场与题材", "other": "其他", "unknown": "未明确"}
ACTION_KINDS = {"actual_action": "已操作", "conditional_plan": "计划",
                "historical_review": "交易回顾", "unclear": "未明确",
                "historical": "历史叙述", "historical_trade": "交易回顾", "plan": "计划",
                "current_plan": "当前计划", "conditional": "条件计划", "conditional_future": "未来计划",
                "pass": "不参与", "educational": "示例说明"}


def _kind_text(value, labels):
    value = _string(value)
    if value in labels:
        return labels[value]
    # Natural-language labels can pass through; unfamiliar model enums cannot.
    return value if any("\u4e00" <= char <= "\u9fff" for char in value) else "未明确"


def _session_dates(value):
    result = []
    for item in value if isinstance(value, list) else [value]:
        if isinstance(item, str):
            result.append("未明确" if item in {"", "unknown", "unclear"} else item)
        elif isinstance(item, dict):
            date = _string(item.get("date")) or "未明确"
            if date in {"unknown", "unclear"}:
                date = "未明确"
            result.append(date)
    return " / ".join(dict.fromkeys(result)) or "未明确"


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value) if math.isfinite(value) and value >= 0 else None
    except OverflowError:
        return None


def _array(value):
    return value if isinstance(value, list) else []


def _string(value):
    return value if isinstance(value, str) else ""


def _lines(value):
    """Keep structured model fields readable without interpreting their facts."""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [line for item in value for line in _lines(item)]
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            result = [value["text"]]
            if value.get("classification"):
                result[0] += "（" + _kind_text(value["classification"], ACTION_KINDS) + "）"
            return result
        if any(key in value for key in ["summary", "points", "uncertainties"]):
            return _lines(value.get("summary")) + _lines(value.get("points"))
        labels = {"start": "开始", "end": "结束", "start_time": "开始时间", "end_time": "结束时间",
                  "estimated_start": "估计开始", "estimated_end": "估计结束", "timezone": "时区",
                  "timezone_assumption": "时区假设", "assumption": "假设", "earliest": "最早", "latest": "最晚",
                  "uncertainty_seconds": "时间不确定性（秒）", "display_text": "说明", "reason": "理由"}
        return [labels.get(str(key), str(key)) + "：" + "；".join(_lines(item)) for key, item in value.items() if item is not None]
    return [str(value)]


def _escape(value):
    return html.escape(str(value), quote=True)


def _clock(value):
    seconds = _number(value)
    if seconds is None:
        return "未知"
    whole = int(seconds)
    return f"{whole // 3600:02}:{whole // 60 % 60:02}:{whole % 60:02}"


def _start(segment):
    return _number(segment.get("start_seconds", segment.get("start")))


def _end(segment):
    return _number(segment.get("end_seconds", segment.get("end")))


def _video_range(segment):
    if _start(segment) is not None and _end(segment) is not None:
        return _clock(_start(segment)) + " — " + _clock(_end(segment))
    return _string(segment.get("video_range_text")) or "未知"


def _display_time(value):
    """Hide clock-audit annotations without changing the underlying mapping."""
    text = _string(value)
    text = re.sub(r"[（(][^）)]*(?:时钟映射|误差区间|不确定性|北京时间假设)[^）)]*[）)]", "", text)
    text = re.sub(r"^约[，,\s]*", "", text)
    return text.strip() or UNKNOWN_BJ


def _recording_kind(analysis):
    value = _metadata(analysis).get("recording_kind_display")
    if not value:
        return _kind_text(analysis.get("recording_kind"), RECORDING_KINDS)
    return re.sub(r"[（(][^）)]*(?:桌面时钟|时区|北京时间)[^）)]*[）)]", "", str(value)).strip()


def _beijing_range(segment):
    return _display_time(segment.get("beijing_range_text"))


def _metadata(analysis):
    value = analysis.get("metadata", {})
    return value if isinstance(value, dict) else {}


def _entities(analysis):
    """Merge repeated entity names, retaining every independently timed segment."""
    grouped = {}
    for index, item in enumerate(_array(analysis.get("entities"))):
        if not isinstance(item, dict):
            continue
        name = _string(item.get("name")) or "未命名实体"
        kind = _string(item.get("kind")) or "未知类型"
        key = (name.strip().casefold(), kind.casefold())
        group = grouped.setdefault(key, {"name": name, "kind": kind, "aliases": [], "views": [], "segments": [], "order": index})
        group["aliases"].append(str(item.get("id", index)))
        for view in _lines(item.get("overall_view")):
            if view not in group["views"]:
                group["views"].append(view)
        for segment in _array(item.get("segments")):
            if isinstance(segment, dict):
                group["segments"].append(dict(segment))
    groups = list(grouped.values())
    for group in groups:
        group["segments"].sort(key=lambda s: (_start(s) is None, _start(s) or 0))
    groups.sort(key=lambda g: (min((_start(s) for s in g["segments"] if _start(s) is not None), default=float("inf")), g["order"]))
    for i, group in enumerate(groups, 1):
        group["anchor"] = f"entity-{i:03}"
        for j, segment in enumerate(group["segments"], 1):
            segment["anchor"] = f"segment-{i:03}-{j:03}"
    return groups


def _timeline(analysis, groups):
    provided = {}
    for row in _array(analysis.get("timeline")):
        if isinstance(row, dict):
            provided[(str(row.get("entity_id")), str(row.get("segment_id")))] = row
    rows = []
    for group in groups:
        for segment in group["segments"]:
            row = dict(segment)
            for alias in group["aliases"]:
                matching = provided.get((alias, str(segment.get("id"))))
                if matching:
                    for key in ["summary", "video_range_text", "beijing_range_text"]:
                        if matching.get(key):
                            row[key] = matching[key]
                    break
            row["name"], row["entity_anchor"] = group["name"], group["anchor"]
            rows.append(row)
    return sorted(rows, key=lambda s: (_start(s) is None, _start(s) or 0))


def _source_url(metadata):
    name = metadata.get("source_filename", metadata.get("sourcefilename", metadata.get("video_filename")))
    if not isinstance(name, str) or not name or any(c in name for c in "\\/\x00\r\n") or name in {".", ".."}:
        return None
    return "../" + quote(name, safe="")


def _asset_url(value, output_dir):
    """Only local assets inside the video folder; never accept model URLs."""
    if not isinstance(value, str) or not value or any(c in value for c in "\x00\r\n\\"):
        return None
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        return None
    root = Path(output_dir).resolve()
    candidate = Path(value)
    candidate = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    if not candidate.is_relative_to(root.parent):
        return None
    return quote(os.path.relpath(candidate, root), safe="/")


def _list_html(value, empty="未明确说明"):
    lines = _lines(value)
    return "<ul>" + "".join("<li>" + _escape(line).replace("\n", "<br>") + "</li>" for line in lines) + "</ul>" if lines else '<p class="muted">' + empty + "</p>"


def _vtt_text(path, output_dir):
    """Read only approved local tracks; no network or fetch in the browser."""
    if _asset_url(path, output_dir) is None:
        return ""
    file = Path(path)
    file = file if file.is_absolute() else Path(output_dir) / file
    if not file.is_file() or file.stat().st_size > 2 * 1024 * 1024:
        return ""
    try:
        return file.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return ""


def _vtt_cues(text):
    import re
    pattern = re.compile(r"(?:(\d+):)?(\d{2}):(\d{2})\.(\d{3})")
    def seconds(stamp):
        match = pattern.fullmatch(stamp)
        if not match:
            return None
        h, m, s, ms = match.groups()
        return int(h or 0) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000
    cues = []
    blocks = text.replace("\r\n", "\n").split("\n\n")
    for block in blocks:
        lines = block.splitlines()
        timing = next((i for i, line in enumerate(lines) if " --> " in line), None)
        if timing is None:
            continue
        begin, finish = lines[timing].split(" --> ", 1)
        start, end = seconds(begin.strip()), seconds(finish.split()[0])
        if start is not None and end is not None and start < end:
            cues.append({"start": start, "end": end, "text": "\n".join(lines[timing + 1:])})
    return cues


def _embedded_json(value):
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    encoded = encoded.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return _escape(encoded)


def _pass_reasons(group):
    reasons = []
    for segment in group["segments"]:
        for reason in _lines(segment.get("pass_reason")):
            if reason not in reasons:
                reasons.append(reason)
    return reasons


def _segment_quotes(segment):
    """Centralize grounded quotations once rather than repeating each statement."""
    result, seen = [], set()
    def collect(value):
        if isinstance(value, list):
            for item in value:
                collect(item)
        elif isinstance(value, dict):
            if isinstance(value.get("text"), str) and "cue_id" in value:
                key = (str(value["cue_id"]), value["text"])
                if key not in seen:
                    result.append(value)
                    seen.add(key)
            else:
                collect(value.get("quotes"))
    collect(segment.get("quotes"))
    for field in ["summary", "views", "buy_conditions", "sell_conditions", "actions", "pass_reason", "uncertainties"]:
        collect(segment.get(field))
    return result


def _summary_quotes(segment):
    quotes = _segment_quotes(segment)
    priority = []
    for field in ["actions", "buy_conditions", "sell_conditions", "pass_reason", "views", "summary"]:
        for statement in _array(segment.get(field)):
            if isinstance(statement, dict):
                priority.extend(map(str, _array(statement.get("cue_ids"))))
    ranks = {cue: priority.index(cue) for cue in set(priority)}
    quotes.sort(key=lambda item: ranks.get(str(item.get("cue_id")), len(priority)))
    return quotes[:2]


def _media_html(segment, output_dir):
    result = []
    frames = segment.get("frames", [])
    if isinstance(frames, list) and frames:
        result.append('<div class="frames">')
        for frame in frames:
            if not isinstance(frame, dict):
                continue
            src = _asset_url(frame.get("path"), output_dir)
            caption = _string(frame.get("caption")) or "代表画面"
            time = (_clock(frame.get("video_seconds")) if _number(frame.get("video_seconds")) is not None
                    else _string(frame.get("video_time_text")) or "未知")
            bj = _display_time(frame.get("beijing_time_text"))
            result.append("<figure>")
            if src:
                result.append(f'<button class="image-button" data-preview="{_escape(src)}" data-caption="{_escape(caption)}" aria-label="放大画面"><img src="{_escape(src)}" alt="{_escape(caption)}" loading="lazy"></button>')
            else:
                result.append('<p class="muted">画面路径不可用或超出本地视频目录</p>')
            result.append(f'<figcaption>{_escape(caption)}<br>视频：{_escape(time)} · 北京时间：{_escape(bj)}</figcaption></figure>')
        result.append("</div>")
    clip = segment.get("clip")
    if isinstance(clip, dict):
        src = _asset_url(clip.get("path"), output_dir)
        track = _asset_url(clip.get("subtitle_path"), output_dir)
        vtt = _string(clip.get("vtt_text")) or (_vtt_text(clip.get("subtitle_path"), output_dir) if track else "")
        if src:
            result.append('<div class="clip"><h4>本段视频切片</h4>')
            result.append(f'<video class="clip-player" controls playsinline preload="metadata" src="{_escape(src)}">')
            if track or vtt:
                result.append('<track default kind="subtitles" srclang="zh" label="中文字幕"' + (f' src="{_escape(track)}"' if track else "") + '>')
            result.append("浏览器不支持视频播放。</video>")
            result.append(f'<p class="muted">原视频范围：{_escape(_video_range(clip))}；北京时间：{_escape(_beijing_range(segment))}</p>')
            result.append(f'<div class="clip-captions" aria-live="polite" data-vtt-cues="{_embedded_json(_vtt_cues(vtt))}" data-vtt-text="{_embedded_json(vtt)}"></div>')
            if track:
                result.append(f'<p class="small"><a href="{_escape(track)}">打开切片字幕</a></p>')
            result.append("</div>")
        else:
            result.append('<p class="muted">视频切片路径不可用或超出本地视频目录</p>')
    return "".join(result)


def _clock_frames_html(analysis, output_dir):
    frames = _array(analysis.get("clock_frames"))
    if not frames:
        return ""
    out = ['<details class="clock-panel"><summary>时间对齐依据 · ' + str(len(frames)) + ' 张桌面时钟画面</summary><p class="small">桌面时钟与录制时间、时区假设分别保留。遮挡画面不作为时间锚；上传时间不用于反推录制时刻。</p><div class="frames">']
    for frame in frames:
        if not isinstance(frame, dict):
            continue
        src = _asset_url(frame.get("path"), output_dir)
        clock = _string(frame.get("desktop_time_text")) or "未知"
        video_time = _string(frame.get("video_time_text")) or _clock(frame.get("video_seconds"))
        reason = "；".join(_lines(frame.get("reason"))) or "未提供理由"
        usable = "可用时间锚" if frame.get("usable") is True else "未用于对齐 / 时钟不明确"
        caption = "桌面时钟：" + clock
        out.append("<figure>")
        if src:
            out.append(f'<button class="image-button" data-preview="{_escape(src)}" data-caption="{_escape(caption)}" aria-label="放大时钟依据"><img src="{_escape(src)}" alt="{_escape(caption)}" loading="lazy"></button>')
        else:
            out.append('<p class="muted">时钟画面路径未知或不可用</p>')
        out.append(f'<figcaption>视频时间：{_escape(video_time)}<br>桌面显示：{_escape(clock)}<br>{usable} · {_escape(reason)}</figcaption></figure>')
    return "".join(out) + "</div></details>"


def render_summary(analysis):
    """A reading summary; exhaustive excerpts and media remain in HTML/JSON."""
    metadata, groups = _metadata(analysis), _entities(analysis)
    text = [_string(metadata.get("title")) or "视频观点整理", "",
            "录制类型：" + _recording_kind(analysis),
            "录制北京时间：" + _display_time(metadata.get("recording_time")),
            "上传北京时间：" + (_string(metadata.get("upload_time_beijing")) or "未知"),
            "讨论日期：" + _session_dates(analysis.get("discussed_session_dates"))]
    context = analysis.get("visual_context", {})
    if context.get("latest_visible_trade_date"):
        text.append("画面最近交易日：" + str(context['latest_visible_trade_date']))
    text += ["", "整体观点："] + _lines(analysis.get("overview", {}).get("summary"))
    for point in _array(analysis.get("overview", {}).get("points")):
        text.append("- " + (_string(point.get("text")) if isinstance(point, dict) else str(point)))
    text += ["", "讨论时间线："]
    for row in _timeline(analysis, groups):
        text.append(f'{row["name"]} | 视频 {_video_range(row)} | 北京时间 {_beijing_range(row)}')
    fields = [("buy_conditions", "买入/再参与条件"), ("sell_conditions", "退出/目标条件"),
              ("actions", "历史操作或假设计划"), ("pass_reason", "未优先选择原因")]
    for group in groups:
        text += ["", "=" * 32, group["name"], "总体看法："] + group['views']
        for index, segment in enumerate(group['segments'], 1):
            text += ["", f'讨论 {index} | 视频 {_video_range(segment)}',
                     "北京时间：" + _beijing_range(segment),
                     "摘要：" + "；".join(_lines(segment.get('summary')))]
            for key, label in fields:
                statements = _lines(segment.get(key))
                if statements:
                    text.append(label + "：")
                    text.extend("- " + statement for statement in statements)
    return "\n".join(map(str, text)) + "\n"


def render_html(analysis, output_dir):
    metadata, groups = _metadata(analysis), _entities(analysis)
    timeline = _timeline(analysis, groups)
    source = _source_url(metadata)
    title = _string(metadata.get("title")) or "视频观点整理"
    duration = _number(metadata.get("duration_seconds", metadata.get("duration"))) or None
    scale = duration or max((_end(row) or 0 for row in timeline), default=1) or 1
    result = ["<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">",
              "<title>" + _escape(title) + " · 讲者观点整理</title><style>" + _CSS + "</style></head><body>",
              '<header><div class="eyebrow">云聪视频解析 · 本地离线</div><h1>' + _escape(title) + "</h1>",
              '<div class="metadata"><span>时长：' + _escape(_clock(duration)) + "</span><span>录制类型：" + _escape(_recording_kind(analysis)) + "</span><span>讨论日期：" + _escape(_session_dates(analysis.get("discussed_session_dates"))) + "</span></div>",
              '<p class="small">录制北京时间：' + _escape(_display_time(metadata.get("recording_time"))) + '</p><p><a href="summary.txt">打开纯文本摘要</a></p></header>',
              '<div class="layout"><aside><h2>实体 / 股票目录</h2><input id="entity-search" type="search" placeholder="检索名称" aria-label="检索实体目录"><nav>']
    context = analysis.get("visual_context", {})
    if context.get("latest_visible_trade_date"):
        result[-2] = (result[-2].removesuffix('</header>') + '<p class="small">画面中最近可见交易日：' +
                      _escape(context['latest_visible_trade_date']) + '</p></header>')
    if metadata.get('upload_time_beijing'):
        result[-2] = (result[-2].removesuffix('</header>') + '<p class="small">上传北京时间：' +
                      _escape(metadata['upload_time_beijing']) + '</p></header>')
    for group in groups:
        result.append(f'<div class="index-item" data-name="{_escape(group["name"].casefold())}"><a href="#{group["anchor"]}">{_escape(group["name"])}</a><span class="tag">{_escape(_kind_text(group["kind"], ENTITY_KINDS))}</span>')
        reasons = _pass_reasons(group)
        if reasons:
            result.append('<div class="index-reasons"><strong>讲者跳过 / 暂不参与：</strong>' + _list_html(reasons) + "</div>")
        result.append("</div>")
    result += ['</nav></aside><main><section class="panel"><h2>原视频</h2>']
    if source:
        result.append(f'<video id="source-video" controls playsinline preload="metadata" src="{_escape(source)}">浏览器不支持视频播放。</video><p class="small"><a href="{_escape(source)}">单独打开源视频</a> · 片段的“从这里播放”会定位此播放器。</p>')
    else:
        result.append('<p class="muted">源视频文件名未知；未创建猜测链接。</p>')
    result += ['</section><section class="panel"><h2>总览 · 云聪观点</h2>', _list_html(analysis.get("overview")),
               '</section><section class="panel"><h2>视频时间线</h2>']
    result.append(f'<div class="axis"><span>00:00:00</span><span>{_escape(_clock(scale))}</span></div>')
    for row in timeline:
        begin, end = _start(row), _end(row)
        valid = begin is not None and end is not None and begin < end
        left = min(100, begin / scale * 100) if valid else 0
        width = max(.8, min(100 - left, (end - begin) / scale * 100)) if valid else .8
        result.append(f'<article class="timeline-row"><a href="#{row["anchor"]}">{_escape(row["name"])}</a><div class="time-pair">视频：{_escape(_video_range(row))}<br>北京时间：{_escape(_beijing_range(row))}</div><div class="timeline-track"><a class="timeline-bar" href="#{row["anchor"]}" style="left:{left:.4f}%;width:{width:.4f}%" aria-label="查看片段"></a></div><p>{_escape("；".join(_lines(row.get("summary"))))}</p></article>')
    result.append("</section>")
    labels = [("views", "讲者观点"), ("buy_conditions", "讲者买入条件"), ("sell_conditions", "讲者卖出条件"),
              ("actions", "讲者动作与计划"), ("pass_reason", "讲者跳过原因")]
    for group in groups:
        result.append(f'<section class="entity panel" id="{group["anchor"]}"><div class="section-heading"><h2>{_escape(group["name"])}</h2><span class="tag">{_escape(_kind_text(group["kind"], ENTITY_KINDS))}</span><a href="#entity-search">回到目录</a></div><h3>综合表述 · 讲者观点</h3>{_list_html(group["views"])}')
        for index, segment in enumerate(group["segments"], 1):
            result.append(f'<article class="segment" id="{segment["anchor"]}"><h3>独立片段 {index}</h3><div class="time-pair">视频时间：{_escape(_video_range(segment))}<br>北京时间：{_escape(_beijing_range(segment))}</div>')
            begin = _start(segment)
            if source and begin is not None:
                href = source + "#t=" + str(begin)
                result.append(f'<p><a class="seek-button" href="{_escape(href)}" data-seek="{begin}">从 {_escape(_clock(begin))} 播放原视频</a></p>')
            result.append('<h4>讲者思路摘要</h4>' + _list_html(segment.get("summary")) + '<div class="detail-grid">')
            for key, label in labels:
                if not _lines(segment.get(key)):
                    continue
                result.append("<div><h4>" + label + "</h4>" + _list_html(segment.get(key)) + "</div>")
            result.append('</div><details class="quotes"><summary>展开来源引述 · 原字幕</summary>')
            quotes = _segment_quotes(segment)
            for item in quotes if isinstance(quotes, list) else []:
                if isinstance(item, dict):
                    result.append(f'<blockquote><p>{_escape(_string(item.get("text"))).replace(chr(10), "<br>")}</p></blockquote>')
            if not quotes:
                result.append('<p class="muted">未提供来源引述；不能据此补写讲者原话。</p>')
            result.append("</details>" + _media_html(segment, output_dir) + "</article>")
        result.append("</section>")
    result.append('</main></div><dialog id="image-preview"><button id="preview-close">关闭</button><img alt="放大画面"><p></p></dialog><script>' + _JS + "</script></body></html>\n")
    return "".join(result)


_CSS = """
:root{color-scheme:light;--ink:#253346;--muted:#627084;--blue:#245c88;--line:#d9e2e8}*{box-sizing:border-box}body{margin:0;background:#f1f5f7;color:var(--ink);font:16px/1.65 -apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif}header{max-width:1500px;margin:auto;padding:34px 30px 20px}h1{font-size:clamp(24px,3.5vw,36px);line-height:1.4;margin:8px 0 16px}h2{font-size:23px;margin:0 0 16px}h3{font-size:19px}h4{font-size:16px;margin:14px 0 4px}p{margin:8px 0}a{color:var(--blue);text-decoration:none}a:hover{text-decoration:underline}ul{padding-left:24px;margin:7px 0}li{margin:5px 0}.eyebrow{color:var(--blue);font-size:14px;letter-spacing:.08em}.notice{padding:14px 18px;background:#fff3dc;border-left:4px solid #d6a655;border-radius:5px}.metadata{display:flex;flex-wrap:wrap;gap:10px 25px}.small,.muted{font-size:14px;color:var(--muted)}.layout{max-width:1500px;margin:0 auto;display:grid;grid-template-columns:300px minmax(0,1fr);gap:24px;padding:12px 30px 50px}aside{position:sticky;top:16px;align-self:start;background:white;border-radius:12px;padding:20px;max-height:calc(100vh - 32px);overflow:auto}input{width:100%;font:inherit;border:1px solid var(--line);border-radius:6px;padding:8px;margin-bottom:12px}.index-item{border-bottom:1px solid var(--line);padding:14px 0}.index-item>a{font-weight:700}.index-reasons{font-size:13px;margin-top:8px;color:#765b36}.tag{display:inline-block;margin-left:10px;font-size:12px;color:#567083;background:#edf3f7;padding:2px 8px;border-radius:20px}.panel{background:white;border:1px solid #e4eaee;border-radius:12px;padding:25px;margin-bottom:22px;scroll-margin-top:20px}video{display:block;max-width:100%;width:100%;max-height:600px;background:#12202c;border-radius:8px}.axis{display:flex;justify-content:space-between;font-size:13px;color:var(--muted);margin-top:20px}.timeline-row{border-top:1px solid var(--line);padding:15px 0}.timeline-row>a{font-weight:700;font-size:17px}.time-pair{font-size:14px;background:#f2f6f8;padding:9px 13px;border-radius:7px;margin:9px 0}.timeline-track{height:14px;background:#e8edf1;position:relative;border-radius:7px;overflow:hidden;margin:12px 0}.timeline-bar{height:100%;position:absolute;background:#4686ad;border-radius:7px}.section-heading{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.section-heading h2{margin:0}.section-heading>a{margin-left:auto;font-size:13px}.segment{border-top:2px solid var(--line);margin-top:24px;padding-top:18px;scroll-margin-top:20px}.detail-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 25px}.seek-button{display:inline-block;background:var(--blue);color:#fff;border-radius:6px;padding:8px 15px}.quotes{background:#f7f9fa;border:1px solid var(--line);border-radius:8px;padding:12px;margin:18px 0}.quotes summary{font-weight:600;cursor:pointer}blockquote{margin:15px 5px;padding-left:15px;border-left:3px solid #bed1df}.frames{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}figure{margin:0}.image-button{border:0;padding:0;display:block;width:100%;background:#152434;border-radius:7px;overflow:hidden;cursor:zoom-in}.image-button img{display:block;width:100%;aspect-ratio:16/9;object-fit:contain}figcaption{font-size:13px;padding:8px 0}.clip{margin:20px 0}.clip-captions{white-space:pre-line;font-size:17px;text-align:center;min-height:0;color:#183047;font-weight:600}.clip-captions:not(:empty){padding:9px;background:#edf4f8;margin-top:8px;border-radius:6px}dialog{width:min(1300px,96vw);max-height:95vh;border:0;border-radius:12px;padding:18px;background:#142331;color:white}dialog::backdrop{background:#09121bd9}dialog img{max-width:100%;max-height:78vh;display:block;margin:12px auto}dialog button{padding:8px 18px;cursor:pointer}@media(max-width:850px){header{padding:24px 18px}.layout{display:block;padding:10px 18px}aside{position:static;max-height:none;margin-bottom:20px}.panel{padding:18px}.detail-grid,.frames{grid-template-columns:1fr}.section-heading>a{margin-left:0}}@media print{aside,video,.clip,.seek-button,dialog{display:none}.layout{display:block;padding:0}header{padding:0}.panel{border:0;break-inside:avoid}.notice{background:none}body{background:white}.detail-grid{display:block}}
"""

_JS = """
document.querySelectorAll('[data-seek]').forEach(a=>a.addEventListener('click',event=>{const player=document.getElementById('source-video');const seconds=Number(a.dataset.seek);if(!player||!Number.isFinite(seconds)||seconds<0)return;event.preventDefault();const seek=()=>{player.currentTime=seconds;const promise=player.play();if(promise&&promise.catch)promise.catch(()=>{});};if(player.readyState>=1)seek();else{player.addEventListener('loadedmetadata',seek,{once:true});player.load();}player.scrollIntoView({behavior:'smooth',block:'center'});}));
const search=document.getElementById('entity-search');if(search)search.addEventListener('input',()=>{const q=search.value.toLocaleLowerCase();document.querySelectorAll('.index-item').forEach(item=>item.hidden=!item.dataset.name.includes(q));});
const dialog=document.getElementById('image-preview');document.querySelectorAll('[data-preview]').forEach(button=>button.addEventListener('click',()=>{dialog.querySelector('img').src=button.dataset.preview;dialog.querySelector('img').alt=button.dataset.caption;dialog.querySelector('p').textContent=button.dataset.caption;if(dialog.showModal)dialog.showModal();else dialog.setAttribute('open','');}));document.getElementById('preview-close').addEventListener('click',()=>{if(dialog.close)dialog.close();else dialog.removeAttribute('open');});
// file:// may block an external VTT track in some browsers. The renderer also
// embeds validated local VTT cue text for an offline below-video fallback.
document.querySelectorAll('.clip-player').forEach(video=>{const box=video.parentElement.querySelector('.clip-captions');let cues=[],vtt='';try{cues=JSON.parse(box.dataset.vttCues||'[]');vtt=JSON.parse(box.dataset.vttText||'""');}catch(error){}const update=()=>{box.textContent=cues.filter(c=>c.start<=video.currentTime&&video.currentTime<c.end).map(c=>c.text).join('\\n');};video.addEventListener('timeupdate',update);video.addEventListener('seeked',update);const track=video.querySelector('track');if(track){track.addEventListener('load',()=>{track.track.mode='showing';});if(vtt){const url=URL.createObjectURL(new Blob([vtt],{type:'text/vtt;charset=utf-8'}));track.src=url;window.addEventListener('beforeunload',()=>URL.revokeObjectURL(url),{once:true});}}});
"""


def render_analysis(analysis, output_dir, analysis_path=None):
    if not isinstance(analysis, dict):
        raise ValueError("analysis must be a JSON object")
    raw = Path(output_dir).expanduser().absolute()
    if raw.is_symlink():
        raise ValueError("Output directory must not be a symlink")
    directory = external_path(raw)
    payloads = {"index.html": render_html(analysis, directory), "summary.txt": render_summary(analysis)}
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / ".render.lock"
    if lock_path.is_symlink():
        raise ValueError("Render lock must not be a symlink")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Preflight both files so a conflict never leaves a mixed rendered pair.
        for name, content in payloads.items():
            destination = directory / name
            if destination.is_symlink():
                raise ValueError("Rendered output symlink rejected")
            if destination.exists() and destination.read_text(encoding="utf-8") != content:
                raise ValueError(f"Existing modified/different output preserved: {destination}; use a new output directory")
        written = []
        for name, content in payloads.items():
            destination = directory / name
            if destination.exists():
                continue
            fd, temporary = tempfile.mkstemp(prefix=".render-", dir=directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, destination)
            finally:
                Path(temporary).unlink(missing_ok=True)
            written.append(name)
    return {"state": "rendered", "output_directory": str(directory), "html": str(directory / "index.html"),
            "summary": str(directory / "summary.txt"), "written": written,
            "sha256": {name: hashlib.sha256(content.encode("utf-8")).hexdigest() for name, content in payloads.items()},
            "speaker_view_only": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis", type=Path, required=True, help="Local analysis.json")
    parser.add_argument("--output-dir", type=Path, required=True, help="External local video analysis directory")
    args = parser.parse_args(argv)
    try:
        with args.analysis.open(encoding="utf-8") as handle:
            value = json.load(handle)
        result = render_analysis(value, args.output_dir, args.analysis)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return 0
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(json.dumps({"state": "failed", "message": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
