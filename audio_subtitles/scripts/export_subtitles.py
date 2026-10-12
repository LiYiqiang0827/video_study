#!/usr/bin/env python3
"""Export five production subtitle artifacts from closed local ASR runs.

No inference, network, historical-attempt fallback, or source modifications.
Existing output contracts and file hashes must match; otherwise choose a new
external output directory. Prompt exclusions must be explicitly registered.
"""
from __future__ import annotations

import argparse
from collections import Counter
import fcntl
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time

sys.dont_write_bytecode = True

import batch_storage as storage
import export_asr
import practical_subtitles
import subtitle_utils
import tool_paths

PROVENANCE = "AUTO_EXPORT_PROVENANCE.json"
STATUS = "SUBTITLE_AUTO_STATUS.json"
ARTIFACTS = {"transcript.json", "transcript.srt", "transcript.vtt", "transcript.md"}
POLICY = "automatic_practical_subtitles_v1"


def code_hashes():
    return {Path(module.__file__).name: storage.sha256_file(module.__file__)
            for module in (storage, export_asr, practical_subtitles, subtitle_utils, tool_paths)} | {
                Path(__file__).name: storage.sha256_file(__file__)}


def registered_blocks(path, video_id, attempt_name, raw_text, *, batch=False):
    """Read only explicitly scoped exclusions and verify their exact text."""
    if path is None:
        return []
    value = storage.read_json(path)
    if isinstance(value, dict) and "videos" in value:
        value = value["videos"].get(video_id, {}).get(attempt_name, [])
    elif isinstance(value, dict) and "blocks" in value:
        if value.get("video_id") != video_id:
            raise storage.ExportError("blocked_registry_video_mismatch")
        if value.get("attempt") is not None and value["attempt"] != attempt_name:
            raise storage.ExportError("blocked_registry_attempt_mismatch")
        value = value["blocks"]
    elif batch:
        raise storage.ExportError("batch_blocks_require_video_and_attempt_scope")
    if not isinstance(value, list):
        raise storage.ExportError("blocked_registry_invalid")
    normalized = []
    for entry in value:
        if not isinstance(entry, dict):
            raise storage.ExportError("blocked_registry_requires_objects")
        span = entry.get("raw_span")
        reason = entry.get("reason", "").removesuffix("_requires_listening")
        if (not isinstance(span, list) or len(span) != 2
                or any(isinstance(x, bool) or not isinstance(x, int) for x in span)
                or not 0 <= span[0] < span[1] <= len(raw_text)
                or reason not in {"suspected_prompt_echo", "long_repetition"}):
            raise storage.ExportError("blocked_registry_entry_invalid")
        if entry.get("text") != raw_text[span[0]:span[1]]:
            raise storage.ExportError("blocked_registry_text_mismatch", raw_span=span)
        normalized.append({**entry, "reason": reason})
    return normalized


def closed_run(run):
    status_path = run / "STATUS.json"
    if not status_path.is_file():
        raise storage.ExportError("run_closure_missing")
    value = storage.read_json(status_path)
    if value.get("state") != "complete":
        raise storage.ExportError("run_not_complete", source_state=value.get("state"))
    return value


def output_path(value, run):
    raw = Path(value).expanduser().absolute()
    if raw.is_symlink():
        raise storage.ExportError("output_symlink_rejected", path=str(raw))
    output = tool_paths.external_path(raw)
    if output == run or output.is_relative_to(run) or run.is_relative_to(output):
        raise storage.ExportError("output_overlaps_source_run")
    return output


def validate_result(result, original):
    if result.get("raw_text") != original.get("raw_text") or result.get("units") != original.get("units"):
        raise storage.ExportError("original_asr_modified")
    summary = result.get("auto_summary", {})
    if summary.get("unresolved_cue_count") or not summary.get("raw_mapping_complete"):
        raise storage.ExportError("automatic_display_incomplete", summary=summary)
    duration = export_asr._numeric(result.get("duration"), positive=True)
    prior_end, prior_ms = -1.0, -1
    for cue in result.get("cues", []):
        start, end = export_asr._numeric(cue.get("start")), export_asr._numeric(cue.get("end"))
        first, last = cue.get("start_ms"), cue.get("end_ms")
        # Compare in the same millisecond domain as inward quantization.
        # Dividing an integer back to seconds can add an IEEE-754 ULP and
        # incorrectly reject a boundary produced by floor(end * 1000).
        if (not cue.get("exportable") or not cue.get("text", "").strip() or duration is None
                or start is None or end is None or not 0 <= start < end <= duration
                or start < prior_end or not isinstance(first, int) or not isinstance(last, int)
                or not prior_ms <= first < last or first < start * 1000 or last > end * 1000):
            raise storage.ExportError("display_timing_invalid", cue_id=cue.get("id"))
        prior_end, prior_ms = end, last


def existing_output(output, expected):
    path = output / PROVENANCE
    if not path.is_file():
        raise storage.ExportError("existing_output_without_provenance_preserved")
    prior = storage.read_json(path)
    if any(prior.get(key) != value for key, value in expected.items()):
        raise storage.ExportError("existing_output_contract_changed_preserved", suggestion="Use a new --output-dir/--output-root.")
    measured = storage.inventory(output)
    if set(measured) != ARTIFACTS | {PROVENANCE}:
        raise storage.ExportError("existing_output_inventory_changed_preserved")
    hashes = {name: storage.sha256_file(output / name) for name in ARTIFACTS}
    if hashes != prior.get("artifact_hashes"):
        raise storage.ExportError("existing_output_modified_preserved")
    return prior


def export_closed_run(
    run_dir, output_dir, video_id, *, backend="qwen", blocked_registry=None,
    batch=False, attempt_name=None, source_contract=None, source_guard=None,
):
    if not storage.ID.fullmatch(video_id):
        raise storage.ExportError("invalid_video_id")
    run = tool_paths.external_path(run_dir).resolve(strict=True)
    if not run.is_dir():
        raise storage.ExportError("run_directory_missing")
    if Path(run_dir).is_symlink():
        raise storage.ExportError("source_symlink_rejected")
    output = output_path(output_dir, run)
    closure = closed_run(run)
    _, backends, sources = export_asr.load_run_results(run)
    original = backends.get(backend)
    if not original or original.get("state", original.get("status")) != "complete":
        raise storage.ExportError("backend_not_complete", backend=backend)
    if not isinstance(original.get("raw_text"), str) or not original["raw_text"].strip() or not isinstance(original.get("units"), list):
        raise storage.ExportError("source_transcription_missing")
    blocks = registered_blocks(blocked_registry, video_id, attempt_name or run.name, original["raw_text"], batch=batch)
    utilities = code_hashes()
    expected = {"schema_version": 1, "policy": POLICY, "video_id": video_id, "backend": backend,
                "source_run_directory": str(run), "source_closure": closure,
                "registered_exclusions": blocks, "code_hashes": utilities,
                "source_contract": source_contract or {}}
    output.parent.mkdir(parents=True, exist_ok=True)
    lock_path = output.parent / ("." + output.name + ".export.lock")
    if lock_path.is_symlink():
        raise storage.ExportError("output_symlink_rejected", path=str(lock_path))
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise storage.ExportError("export_already_running") from None
        prior = existing_output(output, expected) if output.exists() else None
        stats = storage.inventory(run)
        input_hashes = storage.hash_inventory(run, stats, prior)
        if prior is not None:
            if input_hashes != prior.get("input_hashes"):
                raise storage.ExportError("existing_output_source_changed_preserved")
            if source_guard is not None:
                source_guard()
            return {"state": "exported_auto", "production_directory": str(output), "skipped_existing": True,
                    "provenance_sha256": storage.sha256_file(output / PROVENANCE), "metrics": prior["auto_summary"]}
        temporary = Path(tempfile.mkdtemp(prefix="." + output.name + ".staging-", dir=output.parent))
        try:
            prepared = export_asr.prepare_backend_export(run, backend, original, sources[backend],
                aggregate_zero_duration=True, merge_short_fragments=True,
                blocked_raw_spans=[record["raw_span"] for record in blocks])
            result = practical_subtitles.generate_practical_subtitles(prepared, blocked_raw_spans=blocks)
            validate_result(result, original)
            practical_subtitles.write_practical_outputs(result, temporary)
            if storage.inventory(run) != stats:
                raise storage.ExportError("source_changed_during_export")
            if closed_run(run) != closure or code_hashes() != utilities:
                raise storage.ExportError("export_contract_changed_during_export")
            if source_guard is not None:
                source_guard()
            hashes = {name: storage.sha256_file(temporary / name) for name in ARTIFACTS}
            provenance = {**expected, "created_at": storage.now(), "state": "exported_auto",
                          "input_hashes": input_hashes, "source_fingerprints": stats, "artifact_hashes": hashes,
                          "original_asr_unchanged": True, "source_files_unchanged": True,
                          "auto_summary": result["auto_summary"]}
            storage.atomic_json(temporary / PROVENANCE, provenance)
            if output.exists():
                raise storage.ExportError("output_appeared_preserved")
            os.rename(temporary, output)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    return {"state": "exported_auto", "production_directory": str(output), "skipped_existing": False,
            "provenance_sha256": storage.sha256_file(output / PROVENANCE), "metrics": result["auto_summary"]}


def export_cycle(job_root, output_root, *, backend="qwen", blocked_registry=None):
    root = tool_paths.external_path(job_root).resolve(strict=True)
    output_root = tool_paths.external_path(output_root)
    if output_root == root:
        raise storage.ExportError("batch_output_must_be_an_independent_directory")
    state_path = root / "state.json" if (root / "state.json").is_file() else root / "REMOTE_STATE.json"
    snapshot = storage.read_json(state_path)
    if not isinstance(snapshot.get("items"), dict):
        raise storage.ExportError("batch_state_items_missing")
    mirror_path = root / "local_sync_status.json"
    mirrored = state_path.name == "REMOTE_STATE.json"
    if mirrored and not mirror_path.is_file():
        raise storage.ExportError("mirror_verification_missing")
    mirror = storage.read_json(mirror_path) if mirrored else None
    items = {}
    for ident, item in snapshot["items"].items():
        try:
            selected = storage.select_current_run(root, ident, item, mirror)
            if selected is None:
                items[ident] = {"state": "waiting", "source_state": item.get("state"),
                                "issue": "current_attempt_not_complete_or_synced"}
                continue
            attempt = selected["attempt_directory"]
            destination = output_root / ident / selected["attempt_name"]
            prior_path = destination / PROVENANCE
            prior = storage.read_json(prior_path) if prior_path.is_file() else {}
            prior_source = prior.get("source_contract", {})
            cache = {"input_hashes": prior_source.get("attempt_input_hashes", {}),
                     "source_fingerprints": prior_source.get("attempt_source_fingerprints", {})}
            stats = storage.verify_declared(attempt, selected["declared_hashes"], cache)
            contract = {"source_attempt_directory": str(attempt), "source_state": selected["source_state"],
                        "attempt_finished_at": selected["attempt_finished_at"],
                        "attempt_input_hashes": selected["declared_hashes"], "attempt_source_fingerprints": stats}

            def guard(ident=ident, selected=selected, stats=stats):
                current = storage.read_json(state_path).get("items", {}).get(ident, {})
                current_mirror = storage.read_json(mirror_path) if mirrored else None
                updated = storage.select_current_run(root, ident, current, current_mirror)
                if updated is None or any(updated[k] != selected[k] for k in ("attempt_directory", "attempt_finished_at", "source_state", "declared_hashes")):
                    raise storage.ExportError("current_attempt_changed")
                if any(storage.fingerprint(storage.local_path(attempt, name)) != stat for name, stat in stats.items()):
                    raise storage.ExportError("attempt_source_changed_during_export")

            value = export_closed_run(selected["run_directory"], destination, ident, backend=backend,
                    blocked_registry=blocked_registry, batch=True, attempt_name=selected["attempt_name"],
                    source_contract=contract, source_guard=guard)
            value["source_attempt_directory"] = str(attempt)
            if destination.resolve().is_relative_to(root):
                value["derivative_relative"] = destination.resolve().relative_to(root).as_posix()
            items[ident] = value
        except (storage.ExportError, OSError, ValueError, TypeError, KeyError) as error:
            items[ident] = error_record(error)
    counts = Counter(total=len(items), exported_auto=0, waiting=0, issues=0)
    for value in items.values():
        counts[value["state"]] += 1
    complete = bool(items) and counts["exported_auto"] == counts["total"]
    status = {"schema_version": 1, "updated_at": storage.now(), "state": "exported_auto" if complete else "issues" if counts["issues"] else "waiting",
              "source_state_path": str(state_path), "source_batch_state": snapshot.get("state"),
              "output_root": str(output_root), "counts": dict(counts), "items": items,
              "all_current_exported": complete}
    storage.atomic_json(output_root / STATUS, status)
    return status


def error_record(error):
    return {"state": "issues", "issues": [{"code": error.code if isinstance(error, storage.ExportError) else type(error).__name__,
                                            **(error.details if isinstance(error, storage.ExportError) else {"message": str(error)})}]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--run-dir", type=Path)
    source.add_argument("--job-root", type=Path)
    parser.add_argument("--output-dir", "--output-root", dest="output_dir", type=Path)
    parser.add_argument("--video-id")
    parser.add_argument("--backend", choices=("qwen", "whisper"), default="qwen")
    parser.add_argument("--blocked-spans", type=Path, help="Explicit raw-span registry JSON; every block includes exact text and reason")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--watch", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=60)
    args = parser.parse_args(argv)
    if args.run_dir and (not args.output_dir or not args.video_id or args.watch):
        parser.error("single-run mode requires --output-dir and --video-id and does not support --watch")
    if args.job_root and args.video_id:
        parser.error("--video-id is for single-run mode")
    if not 1 <= args.poll_seconds <= 3600:
        parser.error("--poll-seconds must be between 1 and 3600")
    import json
    try:
        if args.run_dir:
            value = export_closed_run(args.run_dir, args.output_dir, args.video_id, backend=args.backend, blocked_registry=args.blocked_spans)
            print(json.dumps(value, ensure_ascii=False), flush=True)
            return 0
        output_root = tool_paths.external_path(args.output_dir or args.job_root / "subtitle_exports")
        if output_root == tool_paths.external_path(args.job_root):
            raise storage.ExportError("batch_output_must_be_an_independent_directory")
        output_root.mkdir(parents=True, exist_ok=True)
        batch_lock = output_root / ".export-batch.lock"
        if batch_lock.is_symlink():
            raise storage.ExportError("output_symlink_rejected", path=str(batch_lock))
        with batch_lock.open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise storage.ExportError("batch_export_already_running") from None
            while True:
                value = export_cycle(args.job_root, output_root, backend=args.backend, blocked_registry=args.blocked_spans)
                print(json.dumps({"state": value["state"], "counts": value["counts"], "status_path": str(output_root / STATUS)}, ensure_ascii=False), flush=True)
                if not args.watch or value["all_current_exported"] or value["counts"]["issues"]:
                    return 1 if value["counts"]["issues"] else 0
                time.sleep(args.poll_seconds)
    except (storage.ExportError, OSError, ValueError, TypeError, KeyError) as error:
        print(json.dumps(error_record(error), ensure_ascii=False), file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
