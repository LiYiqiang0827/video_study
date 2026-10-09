#!/usr/bin/env python3
"""Extract grounded video viewpoints with local/SSH Qwen or Hermes MiniMax.

The model receives cue IDs and source text, never subtitle timestamps. Strict
schema/citation/coverage checks run before the tool derives all time intervals.
No TradingAgent imports, credential copying, market lookup, or transcript edits.
"""
from __future__ import annotations

import argparse
import copy
from datetime import date
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from urllib.request import Request, urlopen

sys.dont_write_bytecode = True

from batch_storage import ExportError, atomic_json, now, read_json, sha256_file
from tool_paths import external_path

PROMPT = Path(__file__).resolve().parent.parent / "prompts/video_analysis.md"
ID = re.compile(r"[a-z][a-z0-9_]{0,63}")
KINDS = {"stock", "index", "method", "market", "other"}
MODES = {"actual_action", "conditional_plan", "historical_review", "unclear"}
G = {"text", "cue_ids", "quotes"}
TOP = {"overview", "recording_kind", "recording_kind_evidence", "discussed_session_dates", "entities", "gaps"}
ENTITY = {"id", "name", "kind", "aliases", "codes", "name_evidence", "overall_view", "segments"}
SEGMENT = {"id", "cue_ids", "summary", "views", "buy_conditions", "sell_conditions", "actions", "pass_reason", "quotes", "visual_focus", "uncertainties"}


class AnalysisValidationError(ValueError):
    def __init__(self, errors):
        self.errors = errors
        super().__init__("; ".join(errors[:40]))


def _norm(value):
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKC", value).casefold() if c.isalnum())


def extract_json(text):
    clean = text.strip()
    if clean.startswith("```"):
        clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", clean, flags=re.S)
    def pairs(items):
        output = {}
        for key, value in items:
            if key in output:
                raise ValueError(f"Duplicate JSON key: {key}")
            output[key] = value
        return output
    value = json.loads(clean, object_pairs_hook=pairs, parse_constant=lambda x: (_ for _ in ()).throw(ValueError(f"Nonfinite JSON: {x}")))
    if not isinstance(value, dict):
        raise ValueError("Model output must be one JSON object")
    return value


def build_input_packet(transcript, input_path, input_sha256, context=None):
    cues = transcript.get("cues")
    if not isinstance(cues, list) or not cues:
        raise ExportError("transcript_cues_missing")
    ids, previous = set(), -1.0
    for cue in cues:
        if (not isinstance(cue, dict) or type(cue.get("id")) is not int or cue["id"] in ids
                or not isinstance(cue.get("text"), str) or not cue["text"].strip()):
            raise ExportError("transcript_cue_invalid")
        start, end = cue.get("start"), cue.get("end")
        if (type(start) not in (int, float) or type(end) not in (int, float)
                or not math.isfinite(start) or not math.isfinite(end) or not 0 <= start < end or start < previous):
            raise ExportError("transcript_cue_time_invalid", cue_id=cue["id"])
        ids.add(cue["id"])
        previous = end
    if context is not None and not isinstance(context, dict):
        raise ExportError("recording_context_must_be_object")
    return {"schema_version": 1, "source": {"filename": Path(input_path).name, "input_sha256": input_sha256},
            "recording_context": context or {}, "cues": [{"id": c["id"], "text": c["text"]} for c in cues]}


def validate_and_normalize(output, transcript):
    """Strict model JSON -> grounded analysis, with source-derived time fields."""
    errors = []
    source = transcript["cues"]
    cues = {c["id"]: c for c in source}
    order = {c["id"]: i for i, c in enumerate(source)}
    covered, entity_names, segment_ids, entity_ids = set(), {}, set(), set()
    result = copy.deepcopy(output)

    def obj(value, keys, path):
        if not isinstance(value, dict):
            errors.append(f"{path}: expected object")
            return {}
        if set(value) != keys:
            errors.append(f"{path}: missing={sorted(keys-set(value))}, extra={sorted(set(value)-keys)}")
        return value

    def array(value, path):
        if not isinstance(value, list):
            errors.append(f"{path}: expected array")
            return []
        return value

    def text(value, path, nullable=False):
        if nullable and value is None:
            return
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{path}: expected nonempty string")

    def ids(value, path, allowed=None, contiguous=False):
        values = array(value, path)
        if not values or any(type(i) is not int or i not in cues for i in values):
            errors.append(f"{path}: must contain known integer cue IDs")
            return []
        if len(values) != len(set(values)) or values != sorted(values, key=order.get):
            errors.append(f"{path}: IDs must be unique and in source order")
        if allowed is not None and not set(values) <= set(allowed):
            errors.append(f"{path}: references cues outside its segment")
        if contiguous and [order[i] for i in values] != list(range(order[values[0]], order[values[-1]] + 1)):
            errors.append(f"{path}: independent segment must cover a continuous cue range")
        return values

    def quotes(value, path, allowed):
        values = array(value, path)
        if not values:
            errors.append(f"{path}: needs at least one exact source quote")
        for index, quote in enumerate(values):
            p = f"{path}[{index}]"
            quote = obj(quote, {"cue_id", "text"}, p)
            cue_id, excerpt = quote.get("cue_id"), quote.get("text")
            if type(cue_id) is not int or cue_id not in cues or cue_id not in allowed:
                errors.append(f"{p}: quote cue_id is not among its cited cues")
            elif not isinstance(excerpt, str) or not excerpt or excerpt not in cues[cue_id]["text"]:
                errors.append(f"{p}: quote must be an exact substring of cue {cue_id}")
        return values

    def grounded(value, path, extra=frozenset(), allowed=None):
        value = obj(value, G | set(extra), path)
        text(value.get("text"), path + ".text")
        references = ids(value.get("cue_ids"), path + ".cue_ids", allowed)
        quotes(value.get("quotes"), path + ".quotes", references)
        return references

    def uncertainty(value, path, allowed=None):
        references = grounded(value, path, {"raw_text", "canonical_text", "basis"}, allowed)
        value = value if isinstance(value, dict) else {}
        text(value.get("raw_text"), path + ".raw_text")
        text(value.get("canonical_text"), path + ".canonical_text", nullable=True)
        if value.get("basis") not in {"unconfirmed_asr", "spoken_self_correction", "visual_needed"}:
            errors.append(path + ": invalid uncertainty basis")
        if not any(value.get("raw_text", "") in cues[i]["text"] for i in references):
            errors.append(path + ": raw_text must remain an actual source substring")

    def derive(value, references):
        if references:
            value.update({"start_seconds": min(cues[i]["start"] for i in references),
                          "end_seconds": max(cues[i]["end"] for i in references),
                          "original_excerpt": "".join(cues[i]["text"] for i in references)})

    obj(result, TOP, "output")
    overview = obj(result.get("overview"), {"summary", "points", "uncertainties"}, "overview")
    text(overview.get("summary"), "overview.summary")
    for index, point in enumerate(array(overview.get("points"), "overview.points")):
        covered.update(grounded(point, f"overview.points[{index}]"))
    for index, item in enumerate(array(overview.get("uncertainties"), "overview.uncertainties")):
        uncertainty(item, f"overview.uncertainties[{index}]")
    if result.get("recording_kind") not in {"intraday_live", "postmarket_review", "mixed", "unknown"}:
        errors.append("recording_kind: invalid enum")
    evidence = array(result.get("recording_kind_evidence"), "recording_kind_evidence")
    if not evidence:
        errors.append("recording_kind_evidence: must explain classification or uncertainty")
    for index, item in enumerate(evidence):
        grounded(item, f"recording_kind_evidence[{index}]")
    for index, item in enumerate(array(result.get("discussed_session_dates"), "discussed_session_dates")):
        p = f"discussed_session_dates[{index}]"
        item = obj(item, {"raw_expression", "date", "basis", "cue_ids", "quotes"}, p)
        references = ids(item.get("cue_ids"), p + ".cue_ids")
        quotes(item.get("quotes"), p + ".quotes", references)
        text(item.get("raw_expression"), p + ".raw_expression")
        text(item.get("basis"), p + ".basis")
        if not any(item.get("raw_expression", "") in cues[i]["text"] for i in references):
            errors.append(p + ": raw date expression must be a source substring")
        if item.get("date") is not None:
            try:
                parsed = date.fromisoformat(item["date"])
                source_dates = set()
                for i in references:
                    for y, m, d in re.findall(r"(20\d{2})[年/\-](\d{1,2})[月/\-](\d{1,2})日?", cues[i]["text"]):
                        source_dates.add(date(int(y), int(m), int(d)))
                if parsed not in source_dates:
                    errors.append(p + ": no explicit source year/month/day; relative date must remain null")
            except (ValueError, TypeError):
                errors.append(p + ": invalid ISO date")

    entities = array(result.get("entities"), "entities")
    first_positions = {}
    timeline = []
    for index, entity in enumerate(entities):
        p = f"entities[{index}]"
        entity = obj(entity, ENTITY, p)
        entity_id, name = entity.get("id"), entity.get("name")
        if not isinstance(entity_id, str) or not ID.fullmatch(entity_id) or entity_id in entity_ids:
            errors.append(p + ": entity id must be a unique safe identifier")
        entity_ids.add(entity_id) if isinstance(entity_id, str) else None
        if not isinstance(entity_id, str):
            entity_id = f"invalid_entity_{index}"
        text(name, p + ".name")
        normalized_name = _norm(name) if isinstance(name, str) else ""
        if normalized_name in entity_names:
            errors.append(p + ": same-name entities must be merged")
        entity_names[normalized_name] = entity_id
        if entity.get("kind") not in KINDS:
            errors.append(p + ": invalid entity kind")
        name_evidence = array(entity.get("name_evidence"), p + ".name_evidence")
        name_ids = set()
        for j, item in enumerate(name_evidence):
            name_ids.update(grounded(item, f"{p}.name_evidence[{j}]"))
        if not name_ids or not any(normalized_name and normalized_name in _norm(cues[i]["text"]) for i in name_ids):
            errors.append(p + ": name must appear in its cited source evidence; do not silently invent/correct names")
        grounded(entity.get("overall_view"), p + ".overall_view")
        for j, alias in enumerate(array(entity.get("aliases"), p + ".aliases")):
            q = f"{p}.aliases[{j}]"
            alias = obj(alias, {"raw_name", "canonical_name", "basis", "cue_ids", "quotes"}, q)
            references = ids(alias.get("cue_ids"), q + ".cue_ids")
            quotes(alias.get("quotes"), q + ".quotes", references)
            text(alias.get("raw_name"), q + ".raw_name")
            text(alias.get("basis"), q + ".basis")
            if alias.get("canonical_name") != name or not any(alias.get("raw_name", "") in cues[i]["text"] for i in references):
                errors.append(q + ": alias must preserve raw name and cite its canonical relationship")
        for j, code in enumerate(array(entity.get("codes"), p + ".codes")):
            q = f"{p}.codes[{j}]"
            code = obj(code, {"code", "market", "basis", "cue_ids", "quotes"}, q)
            references = ids(code.get("cue_ids"), q + ".cue_ids")
            values = quotes(code.get("quotes"), q + ".quotes", references)
            if (not isinstance(code.get("code"), str) or not re.fullmatch(r"\d{6}", code["code"])
                    or code.get("basis") != "spoken_code"
                    or not any(code.get("code", "?") in quote.get("text", "") for quote in values if isinstance(quote, dict))):
                errors.append(q + ": unconfirmed stock codes are forbidden; explicit quoted six-digit code required")
            text(code.get("market"), q + ".market")
        entity_covered = set()
        segments = array(entity.get("segments"), p + ".segments")
        if not segments:
            errors.append(p + ": every discussed entity needs source segments, including pass entities")
        for j, segment in enumerate(segments):
            q = f"{p}.segments[{j}]"
            segment = obj(segment, SEGMENT, q)
            segment_id = segment.get("id")
            if not isinstance(segment_id, str) or not ID.fullmatch(segment_id) or segment_id in segment_ids:
                errors.append(q + ": segment id must be globally unique")
            segment_ids.add(segment_id) if isinstance(segment_id, str) else None
            references = ids(segment.get("cue_ids"), q + ".cue_ids", contiguous=True)
            covered.update(references)
            entity_covered.update(references)
            text(segment.get("summary"), q + ".summary")
            quotes(segment.get("quotes"), q + ".quotes", references)
            for field in ("views", "buy_conditions", "sell_conditions"):
                for k, item in enumerate(array(segment.get(field), q + "." + field)):
                    grounded(item, f"{q}.{field}[{k}]", allowed=references)
            for k, action in enumerate(array(segment.get("actions"), q + ".actions")):
                a = f"{q}.actions[{k}]"
                grounded(action, a, {"classification"}, references)
                if not isinstance(action, dict):
                    continue
                if action.get("classification") not in MODES:
                    errors.append(a + ": action classification must distinguish execution/condition/history/unclear")
                if action.get("classification") == "actual_action" and any(word in action.get("text", "") for word in ("假设", "举个例子", "如果", "将来")):
                    errors.append(a + ": explicitly hypothetical summary cannot be an actual action")
            if segment.get("pass_reason") is not None:
                grounded(segment["pass_reason"], q + ".pass_reason", allowed=references)
            visual_ids = set()
            for k, focus in enumerate(array(segment.get("visual_focus"), q + ".visual_focus")):
                visual_ids.update(grounded(focus, f"{q}.visual_focus[{k}]", {"focus_kind"}, references))
                if not isinstance(focus, dict):
                    continue
                if focus.get("focus_kind") not in {"price_level", "trendline", "volume", "valuation", "position", "other"}:
                    errors.append(q + ": invalid visual focus kind")
            for k, item in enumerate(array(segment.get("uncertainties"), q + ".uncertainties")):
                uncertainty(item, f"{q}.uncertainties[{k}]", references)
            derive(segment, references)
            segment["visual_cue_ids"] = sorted(visual_ids, key=order.get)
            if references:
                event_types = sorted({a.get("classification") for a in segment.get("actions", []) if isinstance(a, dict) and a.get("classification") in MODES}
                                     | ({"conditional_plan"} if segment.get("buy_conditions") or segment.get("sell_conditions") else set())
                                     | ({"pass"} if segment.get("pass_reason") else set()) | ({"view"} if segment.get("views") else set()))
                timeline.append({"entity_id": entity_id, "segment_id": segment_id, "cue_ids": references,
                                 "start_seconds": segment["start_seconds"], "end_seconds": segment["end_seconds"],
                                 "summary": segment["summary"], "event_types": event_types})
        if normalized_name:
            missing_mentions = [c["id"] for c in source if normalized_name in _norm(c["text"]) and c["id"] not in entity_covered]
            if missing_mentions:
                errors.append(f"{p}: entity repeated mentions not covered by its segments: {missing_mentions}")
        first_positions[entity_id] = min((order[i] for i in name_ids | entity_covered), default=len(source))
        if isinstance(segments, list) and all(isinstance(s, dict) for s in segments):
            segments.sort(key=lambda s: min((order[i] for i in s.get("cue_ids", []) if i in order), default=len(source)))
    # Exact aliases must not silently identify two different stocks/entities.
    alias_owners = dict(entity_names)
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        for alias in entity.get("aliases", []) if isinstance(entity.get("aliases"), list) else []:
            if not isinstance(alias, dict):
                continue
            raw_name = _norm(alias.get("raw_name", ""))
            owner = alias_owners.get(raw_name)
            if owner is not None and owner != entity.get("id"):
                errors.append("ambiguous entity alias reused across different entities: " + alias.get("raw_name", ""))
            alias_owners[raw_name] = entity.get("id")
    for index, gap in enumerate(array(result.get("gaps"), "gaps")):
        p = f"gaps[{index}]"
        references = grounded(gap, p, {"reason"})
        covered.update(references)
        if not isinstance(gap, dict):
            continue
        if gap.get("reason") not in {"greeting", "closing", "non_financial", "unclear", "visual_dependency"}:
            errors.append(p + ": invalid gap reason")
        if gap.get("reason") == "non_financial" and any(word in "".join(cues[i]["text"] for i in references) for word in ("买入", "加仓", "止损", "目标位", "排除", "不选")):
            errors.append(p + ": financial actions/targets/pass cannot be hidden as non-financial gaps")
        derive(gap, references)
    missing = set(cues) - covered
    if missing:
        errors.append(f"source cue coverage incomplete: {sorted(missing, key=order.get)}")
    if entity_ids & segment_ids:
        errors.append("entity and segment identifiers must be globally unique")
    if errors:
        raise AnalysisValidationError(errors)
    entities.sort(key=lambda e: first_positions[e["id"]])
    timeline.sort(key=lambda t: (order[t["cue_ids"][0]], first_positions[t["entity_id"]], t["segment_id"]))
    result.update({"schema_version": 1, "timeline": timeline,
                   "validation": {"status": "passed", "schema_checked": True, "quotes_are_source_substrings": True,
                                  "all_cues_accounted_for": True, "entity_names_and_aliases_unique": True,
                                  "time_basis": "derived_from_source_cues", "model_generated_timestamps": False,
                                  "cue_count": len(cues), "covered_cue_count": len(covered),
                                  "semantic_note": "机器校验出处、覆盖与结构；观点和收益均为讲者表述，未使用外部行情核实。"}})
    return result


def request_json(route, payload, *, transport, base_url, ssh_host, timeout, api_key_env=None):
    url = base_url.rstrip("/") + route
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    if transport == "ssh":
        if not re.fullmatch(r"[A-Za-z0-9_.@-]+", ssh_host) or ssh_host.startswith("-"):
            raise ExportError("ssh_host_invalid")
        command = ["curl", "-sS", "--fail-with-body", "--max-time", str(timeout), "-H", "Content-Type:application/json"]
        if data is not None:
            command.extend(["--data-binary", "@-"])
        command.append(url)
        run = subprocess.run(["ssh", "-T", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=20", "-o", "ServerAliveCountMax=30", ssh_host, shlex.join(command)],
                             input=data, capture_output=True, timeout=timeout + 45)
        if run.returncode:
            raise ExportError("qwen_transport_failed", returncode=run.returncode)
        return json.loads(run.stdout.decode("utf-8"))
    headers = {"Content-Type": "application/json", "Connection": "close"}
    if api_key_env:
        key = os.environ.get(api_key_env)
        if not key:
            raise ExportError("api_key_environment_unset", environment_name=api_key_env)
        headers["Authorization"] = "Bearer " + key
    with urlopen(Request(url, data=data, headers=headers), timeout=timeout) as response:
        return json.load(response)


def _response_text(response):
    content = response["choices"][0]["message"]["content"]
    if not isinstance(content, str):
        raise ExportError("model_text_missing")
    return content


def analyze_transcript(input_file, output_dir, *, context_file=None, provider="qwen-vllm", transport="ssh",
                       base_url="http://127.0.0.1:8002/v1", ssh_host="llm-server", model=None,
                       timeout=600, max_tokens=24000, repair_attempts=2, raw_response=None, api_key_env=None):
    source_path = external_path(input_file).resolve(strict=True)
    output = external_path(output_dir)
    input_hash = sha256_file(source_path)
    transcript = read_json(source_path)
    context = read_json(external_path(context_file)) if context_file else None
    packet = build_input_packet(transcript, source_path, input_hash, context)
    prompt_body = PROMPT.read_text(encoding="utf-8")
    prompt = prompt_body + "\n\n输入证据包：\n" + json.dumps(packet, ensure_ascii=False, indent=2)
    contract = {"schema_version": 1, "input_path": str(source_path), "input_sha256": input_hash,
                "input_packet_sha256": hashlib.sha256(json.dumps(packet, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
                "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(), "script_sha256": sha256_file(__file__),
                "provider": "offline_response" if raw_response else provider, "requested_model": model,
                "transport": transport, "base_url": base_url, "ssh_host": ssh_host if transport == "ssh" else None,
                "max_tokens": max_tokens, "repair_attempts": repair_attempts}
    if raw_response:
        response_path = external_path(raw_response).resolve(strict=True)
        contract.update({"offline_response_path": str(response_path), "offline_response_sha256": sha256_file(response_path)})
    if output.exists():
        provenance_path = output / "ANALYSIS_PROVENANCE.json"
        prior = read_json(provenance_path) if provenance_path.is_file() else {}
        if any(prior.get(k) != v for k, v in contract.items()):
            raise ExportError("existing_analysis_contract_changed_preserved", suggestion="Use a new --output-dir.")
        for name, digest in prior.get("artifact_hashes", {}).items():
            path = output / name
            if not path.is_file() or sha256_file(path) != digest:
                raise ExportError("existing_analysis_modified_preserved")
        return {"state": "analyzed", "skipped_existing": True, "analysis_path": str(output / "content_analysis.json")}
    output.mkdir(parents=True, exist_ok=False)
    (output / "prompt.txt").write_text(prompt, encoding="utf-8")
    atomic_json(output / "input_packet.json", packet)
    actual_model, model_meta = model, {}
    invoke = dict(transport=transport, base_url=base_url, ssh_host=ssh_host, timeout=timeout, api_key_env=api_key_env)
    if not raw_response and provider == "qwen-vllm":
        model_meta = request_json("/models", None, **{**invoke, "timeout": 20})
        atomic_json(output / "model_discovery.json", model_meta)
        actual_model = model or model_meta["data"][0]["id"]
        try:
            version = request_json("/version", None, **{**invoke, "timeout": 20})
        except Exception:
            version = {"state": "unavailable"}
        atomic_json(output / "server_version.json", version)
    history, current_prompt, budget = [], prompt, max_tokens
    for attempt in range(1 if raw_response else repair_attempts + 1):
        prefix = f"attempt_{attempt + 1:03}"
        (output / (prefix + ".prompt.txt")).write_text(current_prompt, encoding="utf-8")
        if raw_response:
            response_content = external_path(raw_response).read_text(encoding="utf-8")
            try:
                response_json = json.loads(response_content)
                raw = _response_text(response_json) if isinstance(response_json, dict) and "choices" in response_json else response_content
            except json.JSONDecodeError:
                response_json, raw = {"content": response_content}, response_content
        elif provider == "qwen-vllm":
            payload = {"model": actual_model, "messages": [{"role": "user", "content": current_prompt}],
                       "temperature": 0, "max_tokens": budget, "chat_template_kwargs": {"enable_thinking": False}}
            atomic_json(output / (prefix + ".request.json"), payload)
            response_json = request_json("/chat/completions", payload, **invoke)
            raw = _response_text(response_json)
        else:
            actual_model = model or "MiniMax-M3"
            hermes = os.environ.get("HERMES_BIN", str(Path.home() / ".local/bin/hermes"))
            completed = subprocess.run([hermes, "chat", "--query-file", str(output / (prefix + ".prompt.txt")), "--oneshot", "-Q", "--provider", "minimax-cn", "-m", actual_model,
                                        "--ignore-rules", "--max-turns", "1", "--source", "tool"], text=True, capture_output=True, timeout=timeout)
            if completed.returncode:
                raise ExportError("hermes_transport_failed", returncode=completed.returncode)
            raw = completed.stdout.strip()
            response_json = {"model": actual_model, "content": raw, "transport": "hermes_oneshot"}
        atomic_json(output / (prefix + ".response.json"), response_json)
        (output / (prefix + ".raw.txt")).write_text(raw, encoding="utf-8")
        record = {"attempt": attempt + 1, "model": response_json.get("model", actual_model), "usage": response_json.get("usage"),
                  "finish_reason": response_json.get("choices", [{}])[0].get("finish_reason"),
                  "raw_response_sha256": hashlib.sha256(raw.encode()).hexdigest(), "prompt_sha256": hashlib.sha256(current_prompt.encode()).hexdigest(), "max_tokens": budget}
        try:
            if record["finish_reason"] == "length":
                raise ValueError("Response was truncated (finish_reason=length); return complete compact JSON")
            normalized = validate_and_normalize(extract_json(raw), transcript)
            record["validation"] = "passed"
            history.append(record)
            break
        except (AnalysisValidationError, ValueError, KeyError, TypeError, AttributeError) as error:
            record.update({"validation": "failed", "errors": getattr(error, "errors", [str(error)])})
            history.append(record)
            atomic_json(output / "validation_attempts.json", history)
            if raw_response or attempt >= repair_attempts:
                raise ExportError("model_output_validation_failed", errors=record["errors"][:40]) from error
            if record["finish_reason"] == "length":
                budget = max(budget, min(65536, int(budget * 1.5)))
            current_prompt = prompt + "\n\n上次未通过机器校验。只修正结构/出处/漏项，不添加来源外事实。禁止生成时间字段。\n错误：\n" + json.dumps(record["errors"][:40], ensure_ascii=False) + "\n上次输出：\n" + raw
    if sha256_file(source_path) != input_hash:
        raise ExportError("source_changed_during_analysis")
    normalized["source"] = {**packet["source"], "input_path": str(source_path), "recording_context": context or {}}
    normalized["input_sha256"] = input_hash
    normalized["model_run"] = {"provider": contract["provider"], "model": history[-1]["model"], "attempts": len(history), "usage": history[-1]["usage"]}
    normalized["model_provenance"] = {**normalized["model_run"], "input_sha256": input_hash,
                                       "input_packet_sha256": contract["input_packet_sha256"], "base_prompt_sha256": contract["prompt_sha256"],
                                       "prompt_sha256": history[-1]["prompt_sha256"],
                                       "raw_response_sha256": history[-1]["raw_response_sha256"],
                                       "server_version": version if not raw_response and provider == "qwen-vllm" else None}
    atomic_json(output / "content_analysis.json", normalized)
    atomic_json(output / "validation_attempts.json", history)
    provenance = {**contract, "state": "analyzed", "created_at": now(), "actual_model": history[-1]["model"], "attempts": history,
                  "source_unchanged": True, "artifact_hashes": {p.name: sha256_file(p) for p in output.iterdir() if p.is_file()}}
    atomic_json(output / "ANALYSIS_PROVENANCE.json", provenance)
    return {"state": "analyzed", "skipped_existing": False, "analysis_path": str(output / "content_analysis.json"),
            "entities": len(normalized["entities"]), "segments": len(normalized["timeline"]), "model": history[-1]["model"], "attempts": len(history)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--context-json", type=Path)
    parser.add_argument("--provider", choices=("qwen-vllm", "minimax-hermes"), default="qwen-vllm")
    parser.add_argument("--transport", choices=("http", "ssh"), default=os.environ.get("THEME_VLLM_TRANSPORT", "ssh"))
    parser.add_argument("--base-url", default=os.environ.get("THEME_VLLM_REMOTE_BASE_URL", "http://127.0.0.1:8002/v1"))
    parser.add_argument("--ssh-host", default=os.environ.get("THEME_VLLM_SSH_HOST", "llm-server"))
    parser.add_argument("--model", default=os.environ.get("THEME_VLLM_MODEL"))
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--max-tokens", type=int, default=24000)
    parser.add_argument("--repair-attempts", type=int, choices=(0, 1, 2), default=2)
    parser.add_argument("--raw-response", type=Path, help="Offline schema/citation/coverage verification; makes no model call")
    parser.add_argument("--api-key-env", help="HTTP only: read an existing environment variable; never copy its value to artifacts")
    args = parser.parse_args(argv)
    if args.timeout < 1 or args.max_tokens < 1:
        parser.error("timeout/max-tokens must be positive")
    try:
        value = analyze_transcript(args.input, args.output_dir, context_file=args.context_json, provider=args.provider,
                transport=args.transport, base_url=args.base_url, ssh_host=args.ssh_host, model=args.model,
                timeout=args.timeout, max_tokens=args.max_tokens, repair_attempts=args.repair_attempts,
                raw_response=args.raw_response, api_key_env=args.api_key_env)
        print(json.dumps(value, ensure_ascii=False), flush=True)
        return 0
    except Exception as error:
        value = {"state": "failed", "code": error.code if isinstance(error, ExportError) else type(error).__name__,
                 "details": error.details if isinstance(error, ExportError) else {"message": str(error)}}
        output = external_path(args.output_dir)
        if output.is_dir() and not (output / "content_analysis.json").exists():
            atomic_json(output / "ERROR.json", value)
        print(json.dumps(value, ensure_ascii=False), file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
