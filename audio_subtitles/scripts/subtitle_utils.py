"""Conservative subtitle preparation for locally aligned ASR output.

Public contract:
    prepare_subtitles(units, raw_text, duration, **settings) -> result dict
    validate_units(units, duration, max_unit_span=30.0) -> validation dict
    write_subtitle_outputs(result, output_dir, stem="transcript") -> path dict

``units`` are ordered mappings with text/start/end, in global audio seconds.
Original values and extra fields are preserved. Exact normalized text matches,
not character-count allocation, connect raw punctuation to timed units. No time
is interpolated, sorted, clamped, or repaired. Semantic sentences and display
cues are distinct, with explicit many-to-many references. Unaligned text remains
in JSON and Markdown. SRT/VTT contain only cues whose timing is supportable.
Every unresolved issue is exposed in ``pending`` and prevents ``passed=True``.
This module has no network or third-party dependencies.
"""

from __future__ import annotations

from collections import defaultdict
from bisect import bisect_left
from difflib import SequenceMatcher
import copy
import json
import math
from pathlib import Path
import re
import unicodedata
from typing import Any, Iterable, Mapping


_STRONG = set("。！？!?；;\n")
_SOFT = set("，、,:：")
_CLOSERS = set("”’\"'）)]】》」』")
_LATIN_NUMBER = re.compile(r"[A-Za-z0-9]+(?:[.,:/_\-][A-Za-z0-9]+)*(?:%)?")
_ORAL_FRAGMENTS = {"嗯", "嗯嗯", "啊", "哦", "呃", "好", "好的", "好吧", "ok", "okay"}


def _issue(code: str, message: str, **details: Any) -> dict[str, Any]:
    return {"code": code, "message": message, "status": "pending", **details}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        converted = float(value)
    except (OverflowError, ValueError):
        return None
    return converted if math.isfinite(converted) else None


def validate_units(
    units: Iterable[Mapping[str, Any]],
    duration: float | None,
    *,
    max_unit_span: float = 30.0,
    overlap_tolerance: float = 0.0,
) -> dict[str, Any]:
    """Inspect input times without changing them; unknown duration is pending.

    Numeric strings are intentionally not coerced. Overlap and non-monotonic
    starts/ends are reported separately. Equal adjacent boundaries are reported
    even when the associated text differs.
    """
    if _number(max_unit_span) is None or _number(overlap_tolerance) is None or max_unit_span <= 0 or overlap_tolerance < 0:
        raise ValueError("max_unit_span must be positive; tolerance non-negative")
    items = list(units)
    problems: list[dict[str, Any]] = []
    checked_duration = _number(duration)
    if checked_duration is None or checked_duration <= 0:
        checked_duration = None
        problems.append(_issue("duration_unchecked", "Audio duration is unavailable or invalid."))
    previous: tuple[int, float, float] | None = None
    seen_boundaries: dict[tuple[float, float], int] = {}
    for index, unit in enumerate(items):
        if not isinstance(unit, Mapping):
            problems.append(_issue("invalid_unit", "Alignment unit is not a mapping.", unit_index=index))
            previous = None
            continue
        text = unit.get("text")
        if not isinstance(text, str) or not text.strip():
            problems.append(_issue("empty_text", "Alignment unit has no usable text.", unit_index=index))
        start, end = _number(unit.get("start")), _number(unit.get("end"))
        if start is None or end is None:
            problems.append(_issue("invalid_time", "Start/end must be finite numeric seconds.", unit_index=index))
            previous = None
            continue
        if start < 0 or end < 0:
            problems.append(_issue("negative_time", "Unit has a negative timestamp.", unit_index=index))
        if start == end:
            problems.append(_issue("zero_duration", "Unit has zero duration; its text is retained.", unit_index=index))
        elif start > end:
            problems.append(_issue("reversed_time", "Unit start exceeds its end.", unit_index=index))
        if checked_duration is not None and (start > checked_duration or end > checked_duration):
            problems.append(_issue("exceeds_duration", "Unit extends beyond audio duration.", unit_index=index))
        if end - start > max_unit_span:
            problems.append(_issue("excessive_span", "Unit duration exceeds the configured span limit.", unit_index=index, span=end - start))
        if previous is not None:
            prev_index, prev_start, prev_end = previous
            pair = {"unit_index": index, "previous_unit_index": prev_index}
            if start < prev_start or end < prev_end:
                problems.append(_issue("non_monotonic", "Unit timestamps move backwards in source order.", **pair))
            if start < prev_end - overlap_tolerance:
                problems.append(_issue("overlapping_units", "Adjacent unit intervals overlap.", **pair))
        if (start, end) in seen_boundaries:
            problems.append(_issue("duplicate_boundary", "Units have identical time boundaries.", unit_index=index, previous_unit_index=seen_boundaries[(start, end)]))
        else:
            seen_boundaries[(start, end)] = index
        previous = (index, start, end)
    if not items:
        problems.append(_issue("no_units", "No alignment units were supplied."))
    return {
        "passed": not problems,
        "issues": problems,
        "unit_count": len(items),
        "duration_checked": checked_duration is not None,
        "duration": checked_duration,
    }


def _content(text: str) -> tuple[str, list[int]]:
    """Return normalized lexical characters plus their actual source offsets."""
    characters: list[str] = []
    positions: list[int] = []
    for offset, character in enumerate(text):
        normalized = unicodedata.normalize("NFKC", character).casefold()
        for value in normalized:
            if unicodedata.category(value)[0] in "LNM":
                characters.append(value)
                positions.append(offset)
    return "".join(characters), positions


def _ranges(offsets: Iterable[int]) -> list[list[int]]:
    values = sorted(set(offsets))
    groups: list[list[int]] = []
    for value in values:
        if groups and value == groups[-1][1]:
            groups[-1][1] = value + 1
        else:
            groups.append([value, value + 1])
    return groups


def _alignment(units: list[dict[str, Any]], raw_text: str) -> dict[str, Any]:
    raw_content, raw_positions = _content(raw_text)
    source_characters: list[str] = []
    source_units: list[int] = []
    unit_content_counts: dict[int, int] = {}
    for index, unit in enumerate(units):
        content, _ = _content(unit.get("text", "") if isinstance(unit.get("text"), str) else "")
        unit_content_counts[index] = len(content)
        source_characters.extend(content)
        source_units.extend([index] * len(content))
    source = "".join(source_characters)
    # A forced aligner normally returns exactly the supplied lexical content.
    # Its actual ordered characters can map directly to actual raw positions;
    # this is the same single matching block that SequenceMatcher would return,
    # without its quadratic cost on repetitive hour-long Chinese transcripts.
    # Different content keeps the conservative exact-match fallback unchanged.
    if source == raw_content:
        matching_blocks = [(0, 0, len(source))]
    else:
        # autojunk=False remains essential for Chinese characters / digits.
        matcher = SequenceMatcher(None, source, raw_content, autojunk=False)
        matching_blocks = [(block.a, block.b, block.size) for block in matcher.get_matching_blocks()]
    raw_to_unit: dict[int, int] = {}
    matched_source: set[int] = set()
    matched_raw: set[int] = set()
    unit_matched_counts: dict[int, int] = defaultdict(int)
    for source_begin, raw_begin, block_size in matching_blocks:
        for delta in range(block_size):
            source_offset, raw_offset = source_begin + delta, raw_begin + delta
            unit_index = source_units[source_offset]
            raw_to_unit[raw_positions[raw_offset]] = unit_index
            matched_source.add(source_offset)
            matched_raw.add(raw_offset)
            unit_matched_counts[unit_index] += 1
    unmatched_positions = {raw_positions[i] for i in range(len(raw_content)) if i not in matched_raw}
    problems: list[dict[str, Any]] = []
    for start, end in _ranges(unmatched_positions):
        problems.append(_issue("missing_alignment", "Raw text has lexical content without an exact alignment match.", raw_span=[start, end], text=raw_text[start:end]))
    unmatched_units = sorted({source_units[i] for i in range(len(source)) if i not in matched_source})
    for index in unmatched_units:
        problems.append(_issue("unit_text_unmatched", "Some alignment text is absent or different in raw text.", unit_index=index, text=units[index].get("text")))
    if not raw_text.strip():
        problems.append(_issue("empty_raw_text", "Raw transcript is empty."))
    elif not raw_content:
        problems.append(_issue("missing_alignment", "Raw transcript contains no lexical anchors for timing.", raw_span=[0, len(raw_text)], text=raw_text))
    unit_spans: dict[int, list[int]] = {}
    for position, index in raw_to_unit.items():
        if index not in unit_spans:
            unit_spans[index] = [position, position + 1]
        else:
            unit_spans[index][0] = min(unit_spans[index][0], position)
            unit_spans[index][1] = max(unit_spans[index][1], position + 1)
    return {
        "raw_to_unit": raw_to_unit,
        "raw_positions": sorted(set(raw_positions)),
        "unmatched_positions": unmatched_positions,
        "unit_spans": unit_spans,
        "unit_content_counts": unit_content_counts,
        "unit_matched_counts": unit_matched_counts,
        "issues": problems,
        "coverage": len(matched_raw) / len(raw_content) if raw_content else 0.0,
    }


def _sentence_boundaries(
    text: str, units: list[dict[str, Any]], alignment: dict[str, Any], pause_threshold: float
) -> list[int]:
    boundaries = {0, len(text)}
    for offset, character in enumerate(text):
        terminal = character in _STRONG
        if character == ".":
            before = text[offset - 1] if offset else ""
            after = text[offset + 1] if offset + 1 < len(text) else ""
            # Decimal points and internal dotted names are not sentence stops.
            terminal = not (before.isdigit() and after.isdigit()) and (not after or after.isspace() or after in _CLOSERS)
        if terminal:
            end = offset + 1
            while end < len(text) and (text[end] in _STRONG or text[end] in _CLOSERS):
                end += 1
            boundaries.add(end)
    spans = alignment["unit_spans"]
    ordered = sorted(spans, key=lambda index: spans[index][0])
    atomic_positions = {offset for match in _LATIN_NUMBER.finditer(text) for offset in range(match.start() + 1, match.end())}
    for previous, current in zip(ordered, ordered[1:]):
        start = _number(units[current].get("start"))
        end = _number(units[previous].get("end"))
        if start is not None and end is not None and start - end >= pause_threshold and spans[current][0] not in atomic_positions:
            boundaries.add(spans[current][0])
    # Whitespace stays in the preceding span; avoid standalone blank sentences.
    nonblank = [0]
    for boundary in sorted(boundaries)[1:]:
        if text[nonblank[-1]:boundary].strip():
            nonblank.append(boundary)
        elif boundary == len(text):
            nonblank[-1] = boundary
    if nonblank[-1] != len(text):
        nonblank[-1] = len(text)
    return nonblank


def _safe_boundaries(text: str, alignment: dict[str, Any]) -> set[int]:
    unsafe: set[int] = set()
    for first, last in alignment["unit_spans"].values():
        unsafe.update(range(first + 1, last))
    for match in _LATIN_NUMBER.finditer(text):
        unsafe.update(range(match.start() + 1, match.end()))
    # Punctuation belongs with the text before it, and opening quotation marks
    # with the text after them. These restrictions never invent lexical anchors.
    for position in range(1, len(text)):
        if text[position] in _SOFT | _STRONG | _CLOSERS:
            unsafe.add(position)
        if text[position - 1] in set("“‘（([【《「『"):
            unsafe.add(position)
    return set(range(len(text) + 1)) - unsafe


def _cue_spans(text: str, sentence_boundaries: list[int], safe: set[int], limit: int) -> list[tuple[int, int]]:
    base = sorted({0, len(text)} | (set(sentence_boundaries) & safe))
    spans: list[tuple[int, int]] = []
    for begin, finish in zip(base, base[1:]):
        position = begin
        while position < finish:
            if len(text[position:finish].strip()) <= limit:
                end = finish
            else:
                candidates = [x for x in safe if position < x <= min(position + limit, finish) and text[position:x].strip()]
                preferred = [x for x in candidates if text[x - 1] in _SOFT]
                if preferred:
                    end = max(preferred)
                elif candidates:
                    end = max(candidates)
                else:
                    # A coarse timed phrase may exceed the display limit. Keep
                    # it whole rather than allocating fabricated word times.
                    end = min(x for x in safe if position < x <= finish)
            spans.append((position, end))
            position = end
    return spans


def _segment(
    begin: int,
    end: int,
    text: str,
    units: list[dict[str, Any]],
    alignment: dict[str, Any],
    bad_units: set[int],
    aggregate_zero_duration: bool = False,
    strict_bad_units: set[int] | None = None,
) -> dict[str, Any]:
    mapped = alignment["raw_to_unit"]
    # raw_positions is already ordered and includes every mapped raw position.
    # Slice its true offsets instead of scanning/sorting the whole transcript
    # for every sentence and display cue. Unmatched positions remain unanchored.
    positions = alignment["raw_positions"]
    first, last = bisect_left(positions, begin), bisect_left(positions, end)
    indices = list(dict.fromkeys(mapped[p] for p in positions[first:last] if p in mapped))
    missing = [p for p in alignment["unmatched_positions"] if begin <= p < end]
    partial = [index for index in indices if not (begin <= alignment["unit_spans"][index][0] and alignment["unit_spans"][index][1] <= end)]
    complete_units = all(alignment["unit_matched_counts"].get(i, 0) == alignment["unit_content_counts"].get(i, 0) for i in indices)
    start = units[indices[0]].get("start") if indices and not partial else None
    finish = units[indices[-1]].get("end") if indices and not partial else None
    numeric_start, numeric_end = _number(start), _number(finish)
    zero_units = [i for i in indices if _number(units[i].get("start")) is not None and _number(units[i].get("start")) == _number(units[i].get("end"))]
    positive_support = any(_number(units[i].get("start")) is not None and _number(units[i].get("end")) is not None and units[i]["end"] > units[i]["start"] for i in indices)
    valid = bool(indices) and not missing and not partial and complete_units and not (set(indices) & bad_units)
    valid = valid and numeric_start is not None and numeric_end is not None and 0 <= numeric_start < numeric_end
    if aggregate_zero_duration and zero_units:
        valid = valid and positive_support
    resolution_valid = valid and round(numeric_start * 1000) < round(numeric_end * 1000)
    return {
        "text": text[begin:end],
        "raw_span": [begin, end],
        "unit_indices": indices,
        "start": start,
        "end": finish,
        "timing_valid": bool(valid),
        "exportable": bool(resolution_valid),
        "alignment_missing": bool(missing) or not indices,
        "partial_unit_indices": partial,
        "zero_duration_unit_indices": zero_units,
        "timing_basis": "aggregate_native_unit_boundaries" if aggregate_zero_duration and zero_units and valid else "native_unit_boundaries",
        "strict_timing_valid": bool(valid and not (set(indices) & (strict_bad_units if strict_bad_units is not None else bad_units))),
        "provisional": bool(aggregate_zero_duration and zero_units and valid),
    }


def _adjacent_zero_duplicate(issue: Mapping[str, Any], units: list[dict[str, Any]]) -> bool:
    """Recognize a contiguous run of identical zero points, not overlap.

    The validator references the first occurrence of an identical boundary;
    later occurrences may have intervening units. Require every intervening
    unit to be the same zero point, so non-adjacent repetitions stay blocking.
    The enclosing segment must still have independently positive native bounds.
    """
    if issue.get("code") != "duplicate_boundary":
        return False
    previous, current = issue.get("previous_unit_index"), issue.get("unit_index")
    if not isinstance(previous, int) or not isinstance(current, int) or not 0 <= previous < current < len(units):
        return False
    point = _number(units[previous].get("start"))
    return point is not None and point >= 0 and all(
        _number(units[index].get("start")) == point == _number(units[index].get("end"))
        for index in range(previous, current + 1)
    )


def _blocked_spans(spans: Iterable[Any] | None, text_length: int) -> list[dict[str, Any]]:
    normalized = []
    for item in spans or []:
        detail = dict(item) if isinstance(item, Mapping) else {}
        span = detail.get("raw_span") if detail else item
        if not isinstance(span, (list, tuple)) or len(span) != 2 or any(isinstance(x, bool) or not isinstance(x, int) for x in span):
            raise ValueError("blocked_raw_spans must contain [start,end] offsets or objects with raw_span")
        begin, end = span
        if not 0 <= begin < end <= text_length:
            raise ValueError("blocked_raw_spans must be non-empty and within raw_text")
        normalized.append({**detail, "raw_span": [begin, end]})
    return normalized


def _merge_short_cues(
    cues: list[dict[str, Any]], units: list[dict[str, Any]], raw_text: str,
    alignment: dict[str, Any], timing: dict[str, Any], strict_bad_units: set[int],
    blocked: list[dict[str, Any]], max_lexical: int, max_gap: float,
    oral_max_duration: float, oral_max_gap: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Attach uncertain short zero-point text to a supported adjacent interval.

    This changes display grouping only. A merged interval derives its start/end
    from its first/last native units; no independent fragment time is asserted.
    """
    remaining = copy.deepcopy(cues)
    for cue in remaining:
        cue["original_cue_ids"] = [cue["id"]]
    blocking = [i for i in timing["issues"] if i["code"] != "zero_duration" and not _adjacent_zero_duplicate(i, units)]
    bad = {issue[key] for issue in blocking for key in ["unit_index", "previous_unit_index"] if key in issue}
    events: list[dict[str, Any]] = []

    def blocked_indices(span: list[int]) -> list[int]:
        return [i for i, entry in enumerate(blocked) if entry["raw_span"][0] < span[1] and span[0] < entry["raw_span"][1]]

    def chunk(cue: Mapping[str, Any]) -> Any:
        values = [units[i].get("chunk_index") for i in cue["unit_indices"]]
        return values[0] if values and values[0] is not None and all(value == values[0] for value in values) else None

    def eligibility(cue: Mapping[str, Any]) -> str | None:
        if cue["timing_valid"]:
            start, end = _number(cue["start"]), _number(cue["end"])
            if (cue.get("short_fragment_merged") or not cue["exportable"] or not cue["strict_timing_valid"]
                    or _content(cue["text"])[0] not in _ORAL_FRAGMENTS
                    or start is None or end is None or not 0 < end - start <= oral_max_duration):
                return "timing_already_valid"
        if blocked_indices(cue["raw_span"]):
            return "blocked_raw_span"
        lexical = len(_content(cue["text"])[0])
        if not 0 < lexical <= max_lexical:
            return "lexical_length_exceeded_or_empty"
        indices = cue["unit_indices"]
        if not timing["duration_checked"] or not indices or cue["alignment_missing"] or cue["partial_unit_indices"]:
            return "missing_alignment_or_duration"
        if set(indices) & bad:
            return "non_zero_timing_problem"
        if not cue["timing_valid"] and not all(_number(units[i].get("start")) is not None and _number(units[i].get("start")) == _number(units[i].get("end")) for i in indices):
            return "not_only_zero_duration_units"
        if _content(cue["text"])[0] != _content("".join(units[i].get("text", "") for i in indices))[0]:
            return "incomplete_lexical_alignment"
        if chunk(cue) is None:
            return "chunk_unknown_or_mixed"
        return None

    def proposal(index: int, neighbor_index: int) -> tuple[dict[str, Any] | None, str]:
        left_index, right_index = sorted([index, neighbor_index])
        fragment, neighbor = remaining[index], remaining[neighbor_index]
        left, right = remaining[left_index], remaining[right_index]
        if not neighbor["exportable"] or not neighbor["timing_valid"]:
            return None, "neighbor_timing_invalid"
        if chunk(fragment) != chunk(neighbor):
            return None, "chunk_boundary"
        if left["raw_span"][1] != right["raw_span"][0]:
            return None, "text_not_adjacent"
        span = [left["raw_span"][0], right["raw_span"][1]]
        if blocked_indices(span):
            return None, "blocked_raw_span"
        left_end, right_start = _number(left["end"]), _number(right["start"])
        if left_end is None or right_start is None:
            return None, "missing_native_boundary"
        gap = right_start - left_end
        fragment_kind = "oral_interjection" if fragment["timing_valid"] else "zero_duration"
        if not 0 <= gap <= (oral_max_gap if fragment_kind == "oral_interjection" else max_gap):
            return None, "gap_exceeds_limit_or_overlap"
        merged = _segment(*span, raw_text, units, alignment, bad, True, strict_bad_units)
        if not merged["exportable"]:
            return None, "combined_interval_invalid"
        # Do not introduce an overlap with either remaining outside neighbor.
        for outside, field, boundary in [(left_index - 1, "end", merged["start"]), (right_index + 1, "start", merged["end"])]:
            if 0 <= outside < len(remaining):
                value = _number(remaining[outside].get(field))
                if value is not None and ((field == "end" and value > boundary) or (field == "start" and value < boundary)):
                    return None, "outside_overlap"
        originals = left["original_cue_ids"] + right["original_cue_ids"]
        merged.update({"id": left["id"], "original_cue_ids": originals,
                       "sentence_ids": list(dict.fromkeys(left["sentence_ids"] + right["sentence_ids"])),
                       "short_fragment_merged": True, "provisional": True,
                       "timing_basis": "aggregate_native_unit_boundaries"})
        events.append({"original_fragment_cue_ids": fragment["original_cue_ids"],
                       "original_neighbor_cue_ids": neighbor["original_cue_ids"],
                       "original_cue_ids": originals, "direction": "previous" if neighbor_index < index else "next",
                       "fragment_kind": fragment_kind,
                       "gap_seconds": gap, "raw_span": span, "start": merged["start"], "end": merged["end"],
                       "timing_basis": "aggregate_native_unit_boundaries"})
        return merged, "merged"

    while True:
        changed = False
        for index, fragment in enumerate(remaining):
            if eligibility(fragment) is not None:
                continue
            candidates = [i for i in [index + 1, index - 1] if 0 <= i < len(remaining)]
            # A tail split off its semantic sentence naturally belongs left.
            if not fragment["timing_valid"] and index > 0 and set(fragment["sentence_ids"]) & set(remaining[index - 1]["sentence_ids"]):
                candidates.sort(key=lambda i: i != index - 1)
            for neighbor_index in candidates:
                merged, _ = proposal(index, neighbor_index)
                if merged is not None:
                    left, right = sorted([index, neighbor_index])
                    remaining[left:right + 1] = [merged]
                    changed = True
                    break
            if changed:
                break
        if not changed:
            break
    rejected = []
    for index, cue in enumerate(remaining):
        if cue["timing_valid"]:
            continue
        reason = eligibility(cue)
        neighbor_reasons = []
        if reason is None:
            for neighbor in [index + 1, index - 1]:
                if 0 <= neighbor < len(remaining):
                    _, why = proposal(index, neighbor)
                    neighbor_reasons.append({"direction": "next" if neighbor > index else "previous", "reason": why})
        rejected.append({"original_cue_ids": cue["original_cue_ids"], "raw_span": cue["raw_span"],
                         "reason": reason or "no_eligible_neighbor", "neighbor_reasons": neighbor_reasons})
    for new_id, cue in enumerate(remaining, 1):
        cue["id"] = new_id
    for event in events:
        event["cue_id"] = next(cue["id"] for cue in remaining if set(event["original_cue_ids"]) <= set(cue["original_cue_ids"]))
    return remaining, events, rejected


def prepare_subtitles(
    units: Iterable[Mapping[str, Any]],
    raw_text: str,
    duration: float | None = None,
    *,
    max_cue_chars: int = 36,
    pause_threshold: float = 0.6,
    max_unit_span: float = 30.0,
    aggregate_zero_duration: bool = False,
    merge_short_fragments: bool = False,
    short_fragment_max_lexical: int = 6,
    short_fragment_max_gap: float = 2.0,
    oral_fragment_max_duration: float = 1.0,
    oral_fragment_max_gap: float = 0.6,
    blocked_raw_spans: Iterable[Any] | None = None,
) -> dict[str, Any]:
    """Preserve transcript text, segment it, and expose all unresolved issues.

    Cues reference ``sentence_ids``; sentences reference ``cue_ids``. A single
    coarse unit may span multiple semantic sentences, in which case one cue can
    refer to both sentences while their individual times remain null/pending.
    ``validation.passed`` covers alignment, timing and display checks together.
    ``timing_validation`` separately contains source-unit validation only.
    Opt-in ``aggregate_zero_duration`` permits positive aggregate intervals
    containing zero-duration native units when all other checks pass and at
    least one unit has positive duration. Native timestamps remain unchanged.
    Such intervals are provisional; zero-duration issues remain pending.
    Opt-in ``merge_short_fragments`` can regroup fully aligned short zero-point
    cues with an adjacent supported cue in the same known ``chunk_index``.
    The default limit is six normalized lexical characters and a two-second
    gap. A tail within the same semantic sentence prefers the previous cue;
    other zero fragments prefer the next. Exact oral interjections from the
    fixed ``oral_fragment_texts`` settings list can also merge when their
    strictly valid native duration is at most one second and gap at most 0.6
    seconds, always preferring next. Other valid short phrases stay unchanged.
    ``blocked_raw_spans`` accepts [begin,end] offsets or mappings with raw_span
    and review metadata; no grouping may touch those spans. Native units and
    sentence times remain unchanged. ``pre_merge_cues`` and ``pre_merge_pending``
    preserve the incoming grouping; new cues carry ``original_cue_ids`` and
    ``short_fragment_merges`` records both kinds, direction and native bounds.
    Existing pending cue ids are explicitly scoped to ``pre_merge_cues``.
    """
    if not isinstance(raw_text, str):
        raise TypeError("raw_text must be a string")
    if isinstance(max_cue_chars, bool) or not isinstance(max_cue_chars, int) or max_cue_chars <= 0:
        raise ValueError("max_cue_chars must be a positive integer")
    if pause_threshold <= 0 or not math.isfinite(pause_threshold):
        raise ValueError("pause_threshold must be positive and finite")
    if not isinstance(merge_short_fragments, bool):
        raise TypeError("merge_short_fragments must be a boolean")
    if isinstance(short_fragment_max_lexical, bool) or not isinstance(short_fragment_max_lexical, int) or short_fragment_max_lexical <= 0:
        raise ValueError("short_fragment_max_lexical must be a positive integer")
    for name, value in [("short_fragment_max_gap", short_fragment_max_gap),
                        ("oral_fragment_max_duration", oral_fragment_max_duration),
                        ("oral_fragment_max_gap", oral_fragment_max_gap)]:
        if _number(value) is None or value < 0:
            raise ValueError(f"{name} must be finite and nonnegative")
    blocked = _blocked_spans(blocked_raw_spans, len(raw_text))
    original = list(units)
    copied = [dict(unit) if isinstance(unit, Mapping) else {"text": "", "start": None, "end": None, "invalid_source": str(unit)} for unit in original]
    timing = validate_units(original, duration, max_unit_span=max_unit_span)
    alignment = _alignment(copied, raw_text)
    pending = list(timing["issues"]) + alignment["issues"]
    if not isinstance(aggregate_zero_duration, bool):
        raise TypeError("aggregate_zero_duration must be a boolean")
    blocking_issues = [issue for issue in timing["issues"] if not (
        aggregate_zero_duration and (issue["code"] == "zero_duration" or _adjacent_zero_duplicate(issue, copied))
    )]
    strict_bad_units = {issue[key] for issue in timing["issues"] for key in ["unit_index", "previous_unit_index"] if key in issue}
    bad_units = {issue["unit_index"] for issue in blocking_issues if "unit_index" in issue}
    # Both sides of a conflicting boundary are withheld from timestamp exports.
    bad_units.update(issue["previous_unit_index"] for issue in blocking_issues if "previous_unit_index" in issue)
    boundaries = _sentence_boundaries(raw_text, copied, alignment, pause_threshold)
    sentences = []
    for index, (begin, end) in enumerate(zip(boundaries, boundaries[1:]), 1):
        sentence = _segment(begin, end, raw_text, copied, alignment, bad_units, aggregate_zero_duration, strict_bad_units)
        sentence.update({"id": index, "cue_ids": []})
        sentences.append(sentence)
        if sentence["partial_unit_indices"]:
            pending.append(_issue("sentence_boundary_inside_unit", "Sentence has no independent aligned boundaries; original coarse unit is retained.", sentence_id=index, unit_indices=sentence["partial_unit_indices"]))
    cues = []
    spans = _cue_spans(raw_text, boundaries, _safe_boundaries(raw_text, alignment), max_cue_chars)
    for index, (begin, end) in enumerate(spans, 1):
        cue = _segment(begin, end, raw_text, copied, alignment, bad_units, aggregate_zero_duration, strict_bad_units)
        sentence_ids = [s["id"] for s in sentences if s["raw_span"][0] < end and begin < s["raw_span"][1]]
        cue.update({"id": index, "sentence_ids": sentence_ids})
        cues.append(cue)
        for sentence in sentences:
            if sentence["id"] in sentence_ids:
                sentence["cue_ids"].append(index)
        if len(cue["text"].strip()) > max_cue_chars:
            pending.append(_issue("cue_too_long", "Cue exceeds display limit because no safe aligned split is available.", cue_id=index, display_chars=len(cue["text"].strip())))
        if cue["timing_valid"] and not cue["exportable"]:
            pending.append(_issue("timestamp_resolution_collision", "Cue cannot retain positive duration at millisecond resolution.", cue_id=index))
        if not cue["timing_valid"]:
            pending.append(_issue("cue_timing_pending", "Cue text is retained but excluded from timestamp exports pending alignment/timing review.", cue_id=index, raw_span=cue["raw_span"]))
    if raw_text.strip() and not cues:
        pending.append(_issue("no_cues", "Non-empty transcript produced no display cues."))
    if any(cue["provisional"] for cue in cues):
        pending.append(_issue("provisional_zero_duration_aggregation", "Provisional cues use only native first/last unit boundaries; internal zero-duration units have no individual timing precision.", cue_ids=[cue["id"] for cue in cues if cue["provisional"]]))
    pre_merge_cues, pre_merge_pending, merge_events, merge_rejected = [], [], [], []
    if merge_short_fragments:
        pre_merge_cues, pre_merge_pending = copy.deepcopy(cues), copy.deepcopy(pending)
        cues, merge_events, merge_rejected = _merge_short_cues(cues, copied, raw_text, alignment, timing, strict_bad_units, blocked, short_fragment_max_lexical, short_fragment_max_gap, oral_fragment_max_duration, oral_fragment_max_gap)
        for issue in pending:
            if "cue_id" in issue or "cue_ids" in issue:
                issue["cue_id_scope"] = "pre_merge_cues"
        for sentence in sentences:
            sentence["cue_ids"] = [cue["id"] for cue in cues if sentence["id"] in cue["sentence_ids"]]
        for event in merge_events:
            message = ("Short zero-duration text was attached to an adjacent native interval; its independent timing remains unknown."
                       if event["fragment_kind"] == "zero_duration" else
                       "A short oral interjection was grouped with an adjacent cue using unchanged native bounds; grouping remains pending review.")
            pending.append(_issue("short_fragment_merge_provisional", message, **event))
    result = {
        "schema_version": 1,
        "raw_text": raw_text,
        "duration": duration,
        "settings": {"max_cue_chars": max_cue_chars, "pause_threshold": pause_threshold, "max_unit_span": max_unit_span, "aggregate_zero_duration": aggregate_zero_duration},
        "units": copied,
        "sentences": sentences,
        "cues": cues,
        "pending": pending,
        "timing_validation": timing,
        "validation": {
            "passed": not pending,
            "status": "passed" if not pending else "pending",
            "pending_count": len(pending),
            "alignment_coverage": alignment["coverage"],
            "duration_checked": timing["duration_checked"],
            "sentence_count": len(sentences),
            "cue_count": len(cues),
            "exported_cue_count": sum(c["exportable"] for c in cues),
            "withheld_cue_count": sum(not c["exportable"] for c in cues),
            "provisional_cue_count": sum(c["provisional"] and c["exportable"] for c in cues),
            "exported_text_chars": sum(len(c["text"]) for c in cues if c["exportable"]),
            "exported_text_coverage": sum(len(c["text"]) for c in cues if c["exportable"]) / len(raw_text) if raw_text else 0.0,
        },
    }
    if merge_short_fragments:
        result.update({"pre_merge_cues": pre_merge_cues, "pre_merge_pending": pre_merge_pending,
                       "short_fragment_merges": merge_events, "short_fragment_merge_rejected": merge_rejected,
                       "blocked_raw_spans": blocked})
        result["settings"].update({"merge_short_fragments": True, "short_fragment_max_lexical": short_fragment_max_lexical,
                                   "short_fragment_max_gap": short_fragment_max_gap,
                                   "oral_fragment_max_duration": oral_fragment_max_duration,
                                   "oral_fragment_max_gap": oral_fragment_max_gap,
                                   "oral_fragment_texts": sorted(_ORAL_FRAGMENTS)})
        result["validation"]["short_fragment_merge_count"] = len(merge_events)
    return result


def _timestamp(seconds: float, *, vtt: bool = False) -> str:
    milliseconds = round(seconds * 1000)
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, fraction = divmod(remainder, 1000)
    separator = "." if vtt else ","
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}{separator}{fraction:03d}"


def render_srt(result: Mapping[str, Any]) -> str:
    """Render only supportable cues; full text and omissions stay in JSON/MD."""
    blocks = []
    for index, cue in enumerate((c for c in result["cues"] if c["exportable"]), 1):
        blocks.append(f"{index}\n{_timestamp(cue['start'])} --> {_timestamp(cue['end'])}\n{cue['text'].strip()}")
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def render_vtt(result: Mapping[str, Any]) -> str:
    blocks = ["WEBVTT"]
    for cue in result["cues"]:
        if cue["exportable"]:
            blocks.append(f"{_timestamp(cue['start'], vtt=True)} --> {_timestamp(cue['end'], vtt=True)}\n{cue['text'].strip()}")
    return "\n\n".join(blocks) + "\n\n"


def render_markdown(result: Mapping[str, Any]) -> str:
    status = "通过" if result["validation"]["passed"] else "待验收"
    lines = [f"# 转写字幕（{status}）", "", "## 完整正文", "", result["raw_text"], "", "## 语义句子", ""]
    for sentence in result["sentences"]:
        when = f"{sentence['start']:.3f}–{sentence['end']:.3f} 秒" if sentence["timing_valid"] else "时间待确认"
        if sentence.get("provisional"):
            when += "；首末原始边界聚合，待验收"
        refs = ", ".join(str(value) for value in sentence["cue_ids"])
        lines.append(f"- 句 {sentence['id']} [{when}]（字幕 {refs}）：{sentence['text'].strip()}")
    lines.extend(["", "## 显示字幕", ""])
    for cue in result["cues"]:
        when = f"{cue['start']:.3f}–{cue['end']:.3f} 秒" if cue["exportable"] else "时间待确认，未导出到 SRT/VTT"
        if cue.get("provisional"):
            if cue["zero_duration_unit_indices"]:
                when += f"；首末原始边界聚合，含 {len(cue['zero_duration_unit_indices'])} 个零时长单元，待验收"
            else:
                when += "；口头语并入邻句，使用原始首末边界，待验收"
        lines.append(f"- 字幕 {cue['id']} [{when}]：{cue['text'].strip()}")
    lines.extend(["", "## 待验收项", ""])
    if result["pending"]:
        for issue in result["pending"]:
            detail = {key: value for key, value in issue.items() if key not in {"code", "message", "status"}}
            suffix = f" {json.dumps(detail, ensure_ascii=False)}" if detail else ""
            lines.append(f"- `{issue['code']}`：{issue['message']}{suffix}")
    else:
        lines.append("无。")
    return "\n".join(lines) + "\n"


def write_subtitle_outputs(
    result: Mapping[str, Any], output_dir: str | Path, stem: str = "transcript"
) -> dict[str, str]:
    """Write UTF-8 JSON/SRT/VTT/Markdown and return their absolute paths.

    Invalid non-finite source numbers are tagged explicitly in JSON as
    {"_non_finite_number": "nan"/"inf"/"-inf"}, never silently repaired to
    numeric timestamps. The in-memory source units are left untouched.
    """
    if not stem or Path(stem).name != stem or stem in {".", ".."}:
        raise ValueError("stem must be a simple non-empty file name")
    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    payloads = {
        "json": json.dumps(_json_safe(result), ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        "srt": render_srt(result),
        "vtt": render_vtt(result),
        "markdown": render_markdown(result),
    }
    extensions = {"json": "json", "srt": "srt", "vtt": "vtt", "markdown": "md"}
    paths = {}
    for name, payload in payloads.items():
        path = destination / f"{stem}.{extensions[name]}"
        path.write_text(payload, encoding="utf-8")
        paths[name] = str(path)
    return paths


def _json_safe(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return {"_non_finite_number": repr(value)}
    if isinstance(value, Mapping):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


# Friendly aliases for runners that use process/export terminology.
process_transcript = prepare_subtitles
write_outputs = write_subtitle_outputs
