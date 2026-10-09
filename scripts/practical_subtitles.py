"""Automatic display subtitles derived from audited local ASR preparation.

Native units/raw_text are immutable evidence. A short zero-point fragment may
join a neighboring display cue without extending that neighbor's interval.
Such display timing is explicitly approximate, never reported as model timing.
Only known prompt pollution and long repeated non-lexical sounds are omitted.
No text corrections, model inference, network, or third-party dependencies.
"""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path
import re
import unicodedata
from typing import Any, Mapping

_REPETITION = re.compile(r"([嗯哼啊哦呃哎唉哈呵嘿吼])\1{7,}[，。！？!?、；;\s]*")
_STRONG = set("。！？!?；;")
_TAILS = {"了", "了吗", "来", "呢", "吗", "吧", "呀", "的"}
_OMIT_REASONS = {"suspected_prompt_echo", "long_repetition"}


def _number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _lexical(text):
    return "".join(c for c in unicodedata.normalize("NFKC", text).casefold() if unicodedata.category(c)[0] in "LNM")


def _span(value, length):
    if (not isinstance(value, (list, tuple)) or len(value) != 2
            or any(isinstance(x, bool) or not isinstance(x, int) for x in value)
            or not 0 <= value[0] < value[1] <= length):
        raise ValueError("raw spans must be non-empty integer [begin,end] offsets within raw_text")
    return list(value)


def _milliseconds(start, end):
    # Inward file quantization never introduces an overlap or audio overrun.
    return math.ceil(start * 1000), math.floor(end * 1000)


def _safe(cue, duration):
    start, end = _number(cue.get("start")), _number(cue.get("end"))
    if duration is None or start is None or end is None or not 0 <= start < end <= duration:
        return False
    first, last = _milliseconds(start, end)
    return first < last


def _coalesce(spans):
    output = []
    for begin, end in sorted(spans):
        if output and begin <= output[-1][1]:
            output[-1][1] = max(end, output[-1][1])
        else:
            output.append([begin, end])
    return output


def _retain(span, removed):
    start, end = span
    parts, cursor = [], start
    for begin, finish in removed:
        if finish <= cursor or begin >= end:
            continue
        if cursor < begin:
            parts.append([cursor, min(begin, end)])
        cursor = max(cursor, finish)
    if cursor < end:
        parts.append([cursor, end])
    return parts


def generate_practical_subtitles(
    prepared: Mapping[str, Any], chunk_bounds=None, *, blocked_raw_spans=None,
    max_fragment_lexical: int = 6, estimated_duration: float = 1.2,
) -> dict[str, Any]:
    """Return an independent ``state=exported_auto`` display result.

    Input is the v3 prepared dictionary, including original units and raw_text.
    ``blocked_raw_spans`` objects accept raw_span, reason, and optional exact
    text. Only suspected_prompt_echo/long_repetition (or their
    *_requires_listening aliases) permit omission. Bare/unknown blocks remain
    visible and are reported. Chunk bounds default to backend_metadata.chunks;
    entries use index/start_seconds/end_seconds (start/end aliases accepted).

    Every output cue carries raw_spans, source_cue_ids and original_cue_ids.
    ``source_cues/source_pending/source_validation`` retain prior evidence.
    ``auto_actions`` records omissions/grouping/estimates; ``auto_warnings`` and
    ``unresolved_cues`` expose text that cannot safely receive display timing.
    start/end retain a supported neighbor's native bounds during grouping;
    start_ms/end_ms are inward file quantization, not millisecond ASR accuracy.
    """
    if not isinstance(prepared.get("raw_text"), str):
        raise TypeError("prepared.raw_text must be a string")
    if isinstance(max_fragment_lexical, bool) or not isinstance(max_fragment_lexical, int) or max_fragment_lexical <= 0:
        raise ValueError("max_fragment_lexical must be a positive integer")
    if _number(estimated_duration) is None or not .8 <= estimated_duration <= 2:
        raise ValueError("estimated_duration must be between 0.8 and 2 seconds")
    result = copy.deepcopy(dict(prepared))
    raw, units = result["raw_text"], result.get("units", [])
    duration = _number(result.get("duration")) or _number(result.get("audio", {}).get("duration_seconds"))
    if duration is not None and duration <= 0:
        duration = None
    source = result.pop("cues", [])
    result["source_cues"] = source
    result["source_pending"] = result.pop("pending", [])
    result["source_validation"] = result.pop("validation", {})
    result["source_sentences"] = copy.deepcopy(result.get("sentences", []))
    actions, warnings = [], []
    removed_records = []

    def warn(code, **details):
        warnings.append({"code": code, "status": "warning", **details})

    blocks = list(blocked_raw_spans if blocked_raw_spans is not None else result.get("blocked_raw_spans", []))
    for block in blocks:
        record = dict(block) if isinstance(block, Mapping) else {"raw_span": block}
        span = _span(record.get("raw_span"), len(raw))
        reason = record.get("reason", "unknown_blocked_span").removesuffix("_requires_listening")
        if reason not in _OMIT_REASONS:
            warn("blocked_span_not_omitted", raw_span=span, reason=reason)
            continue
        if "text" in record and record["text"] != raw[span[0]:span[1]]:
            warn("blocked_span_text_mismatch", raw_span=span, reason=reason)
            continue
        removed_records.append({**record, "raw_span": span, "reason": reason, "text": raw[span[0]:span[1]]})
    for match in _REPETITION.finditer(raw):
        removed_records.append({"raw_span": [match.start(), match.end()], "reason": "long_repetition",
                                "text": match.group(), "detection": "eight_or_more_identical_nonlexical_sounds"})
    removed = _coalesce([r["raw_span"] for r in removed_records])
    cues, last_span_end, prior_native_end = [], 0, None
    source_ids = set()
    bad_units = set()
    for issue in result.get("timing_validation", {}).get("issues", []):
        if issue.get("code") not in {"zero_duration", "duplicate_boundary"}:
            bad_units.update(issue[key] for key in ("unit_index", "previous_unit_index") if key in issue)

    def append_cue(original):
        span = _span(original["raw_span"], len(raw))
        parts = _retain(span, removed)
        if parts and parts != [span] and not _lexical("".join(raw[a:b] for a, b in parts)):
            removed_records.extend({"raw_span": part, "reason": "filtered_residual_punctuation", "text": raw[part[0]:part[1]], "detection": "residual_punctuation"} for part in parts)
            removed.extend(parts)
            parts = []
        if not parts:
            actions.append({"action": "omit_display", "source_cue_ids": [original["id"]], "raw_span": span,
                            "original_cue_ids": original.get("original_cue_ids", [original["id"]])})
            return
        cue = copy.deepcopy(original)
        cue.update({"raw_spans": parts, "text": "".join(raw[a:b] for a, b in parts),
                    "source_cue_ids": [original["id"]],
                    "original_cue_ids": copy.deepcopy(original.get("original_cue_ids", [original["id"]])),
                    "native_start": original.get("start"), "native_end": original.get("end"),
                    "approximate": False})
        cue["display_timing_valid"] = bool(original.get("exportable") and original.get("timing_valid") and _safe(cue, duration))
        if cue["display_timing_valid"] and parts != [span]:
            cue.update({"approximate": True, "timing_basis": "filtered_native_interval"})
            actions.append({"action": "filter_native_cue", "source_cue_ids": cue["source_cue_ids"], "raw_spans": parts})
        cues.append(cue)

    for original in source:
        span = _span(original.get("raw_span"), len(raw))
        if span[0] < last_span_end or original.get("id") in source_ids:
            raise ValueError("source cues must have ordered non-overlapping raw spans and unique ids")
        if original.get("text") != raw[span[0]:span[1]]:
            raise ValueError("source cue text must equal its original raw span")
        if last_span_end < span[0]:
            append_cue({"id": f"unmapped_{last_span_end}", "raw_span": [last_span_end, span[0]], "text": raw[last_span_end:span[0]], "unit_indices": [], "alignment_missing": True})
        source_ids.add(original.get("id"))
        append_cue(original)
        last_span_end = span[1]
    if last_span_end < len(raw):
        append_cue({"id": f"unmapped_{last_span_end}", "raw_span": [last_span_end, len(raw)], "text": raw[last_span_end:], "unit_indices": [], "alignment_missing": True})
    removed = _coalesce(removed)
    # Native conflicting cues are retained, but cannot be relabeled as verified.
    for cue in cues:
        if cue["display_timing_valid"]:
            if prior_native_end is not None and cue["start"] < prior_native_end:
                cue["display_timing_valid"] = False
                cue["native_display_conflict"] = True
                warn("native_display_overlap", source_cue_ids=cue["source_cue_ids"])
            else:
                prior_native_end = cue["end"]

    def zero_fragment(cue):
        indices = cue.get("unit_indices", [])
        return (not cue["display_timing_valid"] and not cue.get("native_display_conflict")
                and 0 < len(_lexical(cue["text"])) <= max_fragment_lexical
                and not cue.get("alignment_missing") and not cue.get("partial_unit_indices")
                and bool(indices) and not set(indices) & bad_units
                and all(isinstance(i, int) and 0 <= i < len(units)
                        and _number(units[i].get("start")) is not None
                        and units[i].get("start") == units[i].get("end") for i in indices))

    def blocked_between(left, right):
        return any(a < right["raw_span"][0] and left["raw_span"][1] < b for a, b in removed)

    while True:
        changed = False
        for index, fragment in enumerate(cues):
            if not zero_fragment(fragment):
                continue
            candidates = [i for i in (index + 1, index - 1) if 0 <= i < len(cues)]
            if index > 0:
                previous = cues[index - 1]
                same_sentence = set(fragment.get("sentence_ids", [])) & set(previous.get("sentence_ids", []))
                tail = _lexical(fragment["text"]) in _TAILS and previous["text"].rstrip()[-1:] not in _STRONG
                if same_sentence or tail:
                    candidates.sort(key=lambda i: i != index - 1)
            for neighbor_index in candidates:
                support = cues[neighbor_index]
                if not support["display_timing_valid"]:
                    continue
                left_index, right_index = sorted((index, neighbor_index))
                left, right = cues[left_index], cues[right_index]
                if blocked_between(left, right):
                    continue
                merged = copy.deepcopy(support)
                for field in ("raw_spans", "source_cue_ids", "original_cue_ids", "unit_indices", "sentence_ids"):
                    merged[field] = list(dict.fromkeys(left.get(field, []) + right.get(field, []))) if field != "raw_spans" else left[field] + right[field]
                merged.update({"text": left["text"] + right["text"],
                               "raw_span": [left["raw_span"][0], right["raw_span"][1]],
                               "approximate": True, "timing_basis": "approximate_neighbor_grouping",
                               "strict_timing_valid": False, "display_timing_valid": True})
                actions.append({"action": "approximate_neighbor_grouping", "fragment_source_cue_ids": fragment["source_cue_ids"],
                                "support_source_cue_ids": support["source_cue_ids"], "source_cue_ids": merged["source_cue_ids"],
                                "original_cue_ids": merged["original_cue_ids"], "raw_spans": merged["raw_spans"],
                                "direction": "previous" if neighbor_index < index else "next",
                                "start": support["start"], "end": support["end"],
                                "note": "Display uses the supported neighbor interval; fragment timing is approximate and pauses are not extended."})
                cues[left_index:right_index + 1] = [merged]
                changed = True
                break
            if changed:
                break
        if not changed:
            break

    chunks = chunk_bounds if chunk_bounds is not None else result.get("backend_metadata", {}).get("chunks", [])
    if isinstance(chunks, Mapping):
        chunks = [{"index": key, **value} for key, value in chunks.items()]
    bounds = {}
    for entry in chunks or []:
        start, end = _number(entry.get("start_seconds", entry.get("start"))), _number(entry.get("end_seconds", entry.get("end")))
        if duration is not None and start is not None and end is not None and 0 <= start < end <= duration:
            bounds[entry.get("index", entry.get("chunk_index"))] = (start, end)
    for index, cue in enumerate(cues):
        if cue["display_timing_valid"] or not zero_fragment(cue):
            continue
        chunk_ids = {units[i].get("chunk_index") for i in cue["unit_indices"]}
        bound = bounds.get(next(iter(chunk_ids))) if len(chunk_ids) == 1 else None
        if bound is None:
            continue
        previous = next((c for c in reversed(cues[:index]) if c["display_timing_valid"]), None)
        following = next((c for c in cues[index + 1:] if c["display_timing_valid"]), None)
        begin = max(bound[0], previous["end"] if previous else bound[0])
        finish = min(bound[1], following["start"] if following else bound[1])
        if finish - begin < .8:
            continue
        length = min(estimated_duration, finish - begin)
        point = _number(cue.get("native_start"))
        start = min(max(point if point is not None else begin, begin), finish - length)
        cue.update({"start": start, "end": start + length, "display_timing_valid": True,
                    "approximate": True, "timing_basis": "approximate_chunk_gap", "strict_timing_valid": False})
        actions.append({"action": "approximate_chunk_gap", "source_cue_ids": cue["source_cue_ids"],
                        "original_cue_ids": cue["original_cue_ids"], "raw_spans": cue["raw_spans"],
                        "chunk_index": next(iter(chunk_ids)), "start": start, "end": start + length,
                        "available_gap": [begin, finish], "note": "Estimated display duration inside available chunk bounds; not model timing."})

    offset = _number(result.get("audio", {}).get("source_offset_seconds"))
    unresolved = []
    for new_id, cue in enumerate(cues, 1):
        cue["id"] = new_id
        cue["exportable"] = cue["timing_valid"] = bool(cue["display_timing_valid"] and _safe(cue, duration))
        if cue["exportable"]:
            cue["start_ms"], cue["end_ms"] = _milliseconds(cue["start"], cue["end"])
            cue["source_start"] = cue["start"] + offset if offset is not None else None
            cue["source_end"] = cue["end"] + offset if offset is not None else None
        else:
            unresolved.append(copy.deepcopy(cue))
            warn("unresolved_display_timing", source_cue_ids=cue["source_cue_ids"], raw_spans=cue["raw_spans"], text=cue["text"],
                 note="Text remains in the complete display transcript and audit; no safe subtitle interval was invented.")
    for sentence in result.get("sentences", []):
        sentence["source_cue_ids"] = sentence.get("cue_ids", [])
        sentence["cue_ids"] = [c["id"] for c in cues if any(a < sentence["raw_span"][1] and sentence["raw_span"][0] < b for a, b in c["raw_spans"])]
    display = "".join(c["text"] for c in cues)
    classified = _coalesce(removed + [part for c in cues for part in c["raw_spans"]])
    mapping_complete = classified == ([[0, len(raw)]] if raw else [])
    if not mapping_complete:
        warn("raw_span_mapping_gap")
    summary = {"source_cue_count": len(source), "cue_count": len(cues),
               "exported_cue_count": sum(c["exportable"] for c in cues), "unresolved_cue_count": len(unresolved),
               "omitted_text_chars": sum(b - a for a, b in removed), "display_text_chars": len(display),
               "approximate_neighbor_grouping_count": sum(a["action"] == "approximate_neighbor_grouping" for a in actions),
               "estimated_cue_count": sum(a["action"] == "approximate_chunk_gap" for a in actions),
               "warning_count": len(warnings), "raw_mapping_complete": mapping_complete}
    result.update({"schema_version": 1, "state": "exported_auto", "cues": cues, "display_text": display,
                   "auto_actions": actions, "auto_warnings": warnings, "removed_raw_spans": removed_records,
                   "removed_raw_span_union": removed, "unresolved_cues": unresolved, "auto_summary": summary,
                   "quality_note": "自动成品字幕。保留识别原文；少量显示时间近似并已标注。毫秒格式不表示模型精度，原始时间证据保存在 JSON 审计中。",
                   "validation": {"status": "exported_auto", "display_timing_passed": not unresolved,
                                  "source_timing_verified": False, "raw_mapping_complete": mapping_complete}})
    result["settings"] = {**result.get("settings", {}), "practical_display": True,
                          "max_fragment_lexical": max_fragment_lexical, "estimated_duration": estimated_duration}
    return result


make_practical_subtitles = generate_practical_subtitles


def _stamp(milliseconds, vtt=False):
    hours, remainder = divmod(milliseconds, 3600000)
    minutes, remainder = divmod(remainder, 60000)
    seconds, remainder = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02}{'.' if vtt else ','}{remainder:03}"


def render_srt(result):
    return "\n\n".join(f"{index}\n{_stamp(c['start_ms'])} --> {_stamp(c['end_ms'])}\n{c['text'].strip()}"
                        for index, c in enumerate((c for c in result["cues"] if c["exportable"]), 1)) + ("\n" if any(c["exportable"] for c in result["cues"]) else "")


def render_vtt(result):
    blocks = [f"{_stamp(c['start_ms'], True)} --> {_stamp(c['end_ms'], True)}\n{c['text'].strip()}" for c in result["cues"] if c["exportable"]]
    return "WEBVTT\n\n" + "\n\n".join(blocks) + ("\n" if blocks else "")


def render_markdown(result):
    lines = ["# 自动字幕", "", result["quality_note"], "", "## 显示正文", "", result["display_text"], "", "## 字幕", ""]
    for cue in result["cues"]:
        timing = f"{cue['start']:.3f}–{cue['end']:.3f} 秒" if cue["exportable"] else "未能安全估计显示时间"
        if cue.get("approximate"):
            timing += "（显示近似）"
        lines.append(f"- {cue['id']} [{timing}]：{cue['text']}")
    if result["auto_warnings"]:
        lines.extend(["", "## 自动处理记录", ""])
        lines.extend(f"- {w['code']}：{json.dumps(w, ensure_ascii=False)}" for w in result["auto_warnings"])
    lines.extend(["", "完整识别原文、移除记录、原始对齐与显示映射保存在同名 JSON 文件中。", ""])
    return "\n".join(lines)


def write_practical_outputs(result, output_dir, stem="transcript"):
    """Write JSON/SRT/VTT/Markdown; caller selects a new derivative directory."""
    if not stem or Path(stem).name != stem or stem in {".", ".."}:
        raise ValueError("stem must be a plain filename")
    from tool_paths import external_path
    output = external_path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    paths = {}
    def json_safe(value):
        if isinstance(value, float) and not math.isfinite(value):
            return {"_non_finite_number": "nan" if math.isnan(value) else "inf" if value > 0 else "-inf"}
        if isinstance(value, Mapping):
            return {key: json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [json_safe(item) for item in value]
        return value
    for kind, suffix, content in [("json", ".json", json.dumps(json_safe(result), ensure_ascii=False, indent=2, allow_nan=False) + "\n"),
                                  ("srt", ".srt", render_srt(result)), ("vtt", ".vtt", render_vtt(result)),
                                  ("markdown", ".md", render_markdown(result))]:
        path = output / (stem + suffix)
        path.write_text(content, encoding="utf-8")
        paths[kind] = str(path)
    return paths
