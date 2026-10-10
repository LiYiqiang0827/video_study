"""Three-mode daily PNG benchmark for F-001; never labels a run cold-disk."""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
from time import perf_counter
import uuid

from .contracts import ChartRequest, RenderOptions
from .data import get_last_timings
from .io import write_json
from .cli import run_chart, run_multiperiod_chart


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = Path(r"C:\Project Rocket\outputs\chart_tool_development_20261009")
TIMEOUT_SECONDS = 120
HOT_MEDIAN_LIMIT_SECONDS = 3.0


def _request(code: str, cutoff: str) -> ChartRequest:
    return ChartRequest(
        ts_code=code,
        cutoff=cutoff,
        cutoff_source={
            "kind": "study_end",
            "confidence": "high",
            "evidence_refs": ["F-001 daily PNG performance observation"],
            "candidates": [],
            "calibration_segment": None,
        },
        stage="study",
        window="10y",
    )


def _multi_request(code: str, cutoff: str) -> ChartRequest:
    return ChartRequest(
        ts_code=code,
        cutoff=cutoff,
        cutoff_source={
            "kind": "manual",
            "confidence": "medium",
            "evidence_refs": ["F-002 scoped real multi-period performance observation"],
            "candidates": [],
            "calibration_segment": None,
        },
        stage="restore",
        window="10y",
        periods=("daily", "15m", "1m", "intraday"),
    )


def _outputs(png: Path, report: dict) -> list[dict]:
    paths = [png, png.with_suffix(".view.json"), png.with_suffix(".manifest.json"), png.with_suffix(".run.json")]
    return [{"path": str(path.resolve()), "exists": path.is_file(),
             "bytes": path.stat().st_size if path.is_file() else None}
            for path in paths]


def _sidecars_complete(entries: list[dict]) -> bool:
    return len(entries) == 4 and all(item["exists"] and item["bytes"] is not None and item["bytes"] > 0 for item in entries)


def _multi_outputs(png: Path, report: dict | None = None) -> list[dict]:
    paths = [png, *(png.with_suffix(f".{period}.view.json")
                     for period in ("daily", "15m", "1m", "intraday")),
             png.with_suffix(".manifest.json"), png.with_suffix(".run.json")]
    return [{"path": str(path.resolve()), "exists": path.is_file(),
             "bytes": path.stat().st_size if path.is_file() else None}
            for path in paths]


def _multi_outputs_complete(entries: list[dict]) -> bool:
    return len(entries) == 7 and all(item["exists"] and item["bytes"] is not None and item["bytes"] > 0 for item in entries)


def _write_run_sidecar(png: Path, report: dict) -> None:
    write_json(png.with_suffix(".run.json"), report)


def _in_process_run(request: ChartRequest, *, png: Path, cache_dir: Path,
                    snapshot_dir: Path, refresh: bool = False) -> tuple[dict, dict, float, list[dict]]:
    started = perf_counter()
    report = run_chart(request, output_path=png, cache_dir=cache_dir,
                       snapshot_dir=snapshot_dir, refresh=refresh,
                       options=RenderOptions())
    raw_timings = deepcopy(get_last_timings())
    _write_run_sidecar(png, report)
    complete_wall = perf_counter() - started
    outputs = _outputs(png, report)
    return report, raw_timings, complete_wall, outputs


def _historical_observations() -> dict:
    actual_path = OUTPUT_ROOT / "integration" / "daily_actual_source.run.json"
    actual = json.loads(actual_path.read_text(encoding="utf-8-sig"))
    return {
        "root_actual_source_observation": {
            "source": str(actual_path),
            "status": actual.get("status"),
            "rows": actual.get("rows"),
            "actual_start": actual.get("actual_start"),
            "actual_end": actual.get("actual_end"),
            "reading_seconds": actual.get("timings", {}).get("reading_seconds"),
            "conversion_seconds": actual.get("timings", {}).get("conversion_seconds"),
            "drawing_seconds": actual.get("timings", {}).get("drawing_seconds"),
            "encoding_seconds": actual.get("timings", {}).get("encoding_seconds"),
            "pipeline_total_seconds": actual.get("timings", {}).get("pipeline_total_seconds"),
            "process_to_png_and_sidecars_seconds": actual.get("process_to_png_and_sidecars_seconds"),
            "scope_note": "Separately supplied original-source integration observation; not a cold-disk test and not directly comparable to this C snapshot benchmark."
        },
        "prior_source_probe_observations": {
            "source": str(OUTPUT_ROOT / "source" / "latest_spec.md"),
            "first_observation_seconds": 129.344,
            "later_observation_seconds": 0.203,
            "scope_note": "Earlier three-stock daily/factor source-read probe with different scope; system cache was not cleared; do not calculate a speedup against this PNG benchmark."
        }
    }


def run_benchmark(snapshot_dir: Path, output_dir: Path, code: str, cutoff: str,
                  *, multi_code: str = "002025.SZ",
                  multi_cutoff: str = "2026-09-18T10:07:30+08:00",
                  minute_snapshot_dir: Path | None = None,
                  minute_profile: Path | None = None) -> dict:
    snapshot_dir = snapshot_dir.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    run_dir = output_dir / ("benchmark_run_" + uuid.uuid4().hex)
    run_dir.mkdir()
    cache_dir = run_dir / "C_source_cache"
    request = _request(code, cutoff)
    options = RenderOptions()
    (run_dir / "C_source_cache").mkdir(parents=True, exist_ok=True)

    # Mode 1: reading and normalizing the frozen C source into a fresh raw cache,
    # then rendering and writing the complete PNG/view/manifest/run sidecars.
    source_png = run_dir / "C_source_cache_build.png"
    started = perf_counter()
    source_report = run_chart(request, output_path=source_png, cache_dir=cache_dir,
                              snapshot_dir=snapshot_dir, refresh=False, options=options)
    source_cache_timings = deepcopy(get_last_timings())
    _write_run_sidecar(source_png, source_report)
    source_complete_wall = perf_counter() - started
    source_outputs = _outputs(source_png, source_report)
    source_mode = {
        "label": "C_source_cache_build",
        "source": "frozen C snapshot parquet",
        "cache_dir": str(cache_dir),
        "cache_timings": source_cache_timings,
        "chart_report": source_report,
        "complete_png_and_sidecars_wall_seconds": source_complete_wall,
        "outputs": source_outputs,
        "outputs_complete": _sidecars_complete(source_outputs),
        "cache_build_confirmed": (not source_cache_timings.get("cache_hit", True)
                                   and source_cache_timings.get("source_read_seconds", 0) > 0
                                   and source_cache_timings.get("cache_write_seconds", 0) > 0),
    }

    # Mode 2: the same cache is read in a new Python process. Capture both the
    # real process wall time and the CLI's read/convert/draw/encode split.
    process_dir = run_dir / "existing_cache_new_process"
    process_dir.mkdir()
    process_png = process_dir / f"{code.replace('.', '_')}_daily.png"
    command = [str(Path(sys.executable).resolve()), "-m", "chart_tool", "chart", code,
               "--cutoff", cutoff, "--stage", "study", "--window", "10y",
               "--snapshot-dir", str(snapshot_dir), "--cache-dir", str(cache_dir),
               "--out", str(process_png)]
    access_log = cache_dir / "access.jsonl"
    prior_access_lines = len(access_log.read_text(encoding="utf-8").splitlines()) if access_log.is_file() else 0
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUTF8"] = "1"
    process_started = perf_counter()
    process_result = None
    process_error = None
    try:
        process_result = subprocess.run(command, cwd=PROJECT_ROOT, env=env,
                                        capture_output=True, text=True,
                                        timeout=TIMEOUT_SECONDS, check=False)
    except subprocess.TimeoutExpired as exc:
        process_error = {"type": "TimeoutExpired", "message": f"new-process CLI exceeded {TIMEOUT_SECONDS}s",
                         "stdout": exc.stdout, "stderr": exc.stderr}
    except OSError as exc:
        process_error = {"type": type(exc).__name__, "message": str(exc)}
    process_wall = perf_counter() - process_started
    access_events = []
    if access_log.is_file():
        all_access_lines = access_log.read_text(encoding="utf-8").splitlines()
        for line in all_access_lines[prior_access_lines:]:
            try:
                access_events.append(json.loads(line))
            except json.JSONDecodeError:
                access_events.append({"malformed_log_line": line})
    cli_run_path = process_png.with_suffix(".run.json")
    cli_report = None
    if cli_run_path.is_file():
        try:
            cli_report = json.loads(cli_run_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            process_error = {"type": type(exc).__name__, "message": f"invalid CLI run sidecar: {exc}"}
    process_outputs = _outputs(process_png, cli_report or {})
    process_mode = {
        "label": "existing_cache_new_python_process",
        "command": command,
        "cwd": str(PROJECT_ROOT),
        "timeout_seconds": TIMEOUT_SECONDS,
        "exit_code": process_result.returncode if process_result is not None else None,
        "stdout": process_result.stdout if process_result is not None else "",
        "stderr": process_result.stderr if process_result is not None else (process_error or {}).get("stderr", ""),
        "error": process_error,
        "process_wall_seconds": process_wall,
        "cli_run_report": cli_report,
        "cache_access_events": access_events,
        "cache_hit_confirmed": any(event.get("action") == "cache_hit" for event in access_events),
        "outputs": process_outputs,
        "outputs_complete": _sidecars_complete(process_outputs),
    }

    # Mode 3: warm the interpreter/cache once, then report three individual
    # requests through complete PNG + view/manifest/run sidecar writes.
    repeat_dir = run_dir / "same_process_repeats"
    repeat_dir.mkdir()
    warmup_png = repeat_dir / "warmup.png"
    warmup_report, warmup_cache, warmup_wall, warmup_outputs = _in_process_run(
        request, png=warmup_png, cache_dir=cache_dir, snapshot_dir=snapshot_dir)
    repeats = []
    for index in range(1, 4):
        png = repeat_dir / f"repeat_{index:02d}.png"
        report, cache_timings, wall, outputs = _in_process_run(
            request, png=png, cache_dir=cache_dir, snapshot_dir=snapshot_dir)
        repeats.append({
            "index": index,
            "chart_report": report,
            "cache_timings": cache_timings,
            "complete_png_and_sidecars_wall_seconds": wall,
            "outputs": outputs,
            "outputs_complete": _sidecars_complete(outputs),
        })
    repeat_walls = [item["complete_png_and_sidecars_wall_seconds"] for item in repeats]
    median_wall = statistics.median(repeat_walls)
    repeats_mode = {
        "label": "same_process_three_repeats_after_warmup",
        "warmup": {"chart_report": warmup_report, "cache_timings": warmup_cache,
                   "complete_png_and_sidecars_wall_seconds": warmup_wall,
                   "outputs": warmup_outputs, "outputs_complete": _sidecars_complete(warmup_outputs)},
        "repeats": repeats,
        "median_complete_png_and_sidecars_wall_seconds": median_wall,
        "hot_three_second_criterion": {
            "limit_seconds": HOT_MEDIAN_LIMIT_SECONDS,
            "statistic": "median of three same-process full PNG + view + manifest + run-sidecar wall times",
            "passed": median_wall <= HOT_MEDIAN_LIMIT_SECONDS,
        },
        "all_repeat_outputs_complete": all(item["outputs_complete"] for item in repeats),
        "all_repeats_used_existing_cache": all(item["cache_timings"].get("cache_hit") is True
                                                and item["cache_timings"].get("source_read_seconds") == 0
                                                for item in repeats),
    }

    # The live multi-period performance scope is intentionally limited to the
    # one real profile accepted for 002025.SZ / 2026-09-18. Measure its actual
    # CLI process separately from three complete same-process PNG bundles.
    minute_snapshot_dir = (minute_snapshot_dir or snapshot_dir).expanduser().resolve()
    minute_profile = (minute_profile or snapshot_dir / "minute_profile_20260918.json").expanduser().resolve()
    multi_request = _multi_request(multi_code, multi_cutoff)
    multi_cache = run_dir / "multi_C_source_cache"
    multi_process_dir = run_dir / "multi_existing_scope_new_process"
    multi_process_dir.mkdir()
    multi_process_png = multi_process_dir / "combined.png"
    multi_command = [str(Path(sys.executable).resolve()), "-m", "chart_tool", "chart-multi", multi_code,
                     "--cutoff", multi_cutoff, "--stage", "restore",
                     "--cutoff-evidence", "SYNTHETIC_ENGINEERING_ONLY: F-002 scoped benchmark cutoff",
                     "--window", "10y", "--snapshot-dir", str(snapshot_dir),
                     "--cache-dir", str(multi_cache), "--minute-snapshot-dir", str(minute_snapshot_dir),
                     "--minute-profile", str(minute_profile), "--out", str(multi_process_png)]
    multi_env = os.environ.copy()
    multi_env["PYTHONDONTWRITEBYTECODE"] = "1"
    multi_env["PYTHONUTF8"] = "1"
    multi_started = perf_counter()
    multi_result = None
    multi_error = None
    try:
        multi_result = subprocess.run(multi_command, cwd=PROJECT_ROOT, env=multi_env,
                                      capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
                                      check=False)
    except subprocess.TimeoutExpired as exc:
        multi_error = {"type": "TimeoutExpired", "message": f"multi CLI exceeded {TIMEOUT_SECONDS}s",
                       "stdout": exc.stdout, "stderr": exc.stderr}
    except OSError as exc:
        multi_error = {"type": type(exc).__name__, "message": str(exc)}
    multi_process_wall = perf_counter() - multi_started
    multi_cli_report = None
    multi_run_path = multi_process_png.with_suffix(".run.json")
    if multi_run_path.is_file():
        try:
            multi_cli_report = json.loads(multi_run_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            multi_error = {"type": type(exc).__name__, "message": f"invalid multi CLI receipt: {exc}"}
    multi_process_outputs = _multi_outputs(multi_process_png, multi_cli_report)
    multi_process_mode = {
        "label": "scoped_multi_existing_cache_new_python_process",
        "command": multi_command, "cwd": str(PROJECT_ROOT),
        "timeout_seconds": TIMEOUT_SECONDS,
        "exit_code": multi_result.returncode if multi_result is not None else None,
        "stdout": multi_result.stdout if multi_result is not None else "",
        "stderr": multi_result.stderr if multi_result is not None else (multi_error or {}).get("stderr", ""),
        "error": multi_error, "process_wall_seconds": multi_process_wall,
        "cli_run_report": multi_cli_report,
        "outputs": multi_process_outputs,
        "outputs_complete": _multi_outputs_complete(multi_process_outputs),
    }

    def in_process_multi(png: Path) -> tuple[dict, float, list[dict]]:
        started = perf_counter()
        report = run_multiperiod_chart(
            multi_request, output_path=png, cache_dir=multi_cache,
            snapshot_dir=snapshot_dir, minute_snapshot_dir=minute_snapshot_dir,
            profile_path=minute_profile, refresh=False,
            options=RenderOptions(height_px=2400), display_period="all",
            process_started=started,
        )
        wall = perf_counter() - started
        outputs = _multi_outputs(png, report)
        return report, wall, outputs

    multi_repeat_dir = run_dir / "multi_same_process_repeats"
    multi_repeat_dir.mkdir()
    multi_warmup_report, multi_warmup_wall, multi_warmup_outputs = in_process_multi(multi_repeat_dir / "warmup.png")
    multi_repeats = []
    for index in range(1, 4):
        report, wall, outputs = in_process_multi(multi_repeat_dir / f"repeat_{index:02d}.png")
        multi_repeats.append({"index": index, "chart_report": report,
                              "complete_png_all_views_manifest_and_run_wall_seconds": wall,
                              "outputs": outputs,
                              "outputs_complete": _multi_outputs_complete(outputs)})
    multi_repeat_walls = [item["complete_png_all_views_manifest_and_run_wall_seconds"]
                          for item in multi_repeats]
    multi_median = statistics.median(multi_repeat_walls)
    multi_repeats_mode = {
        "label": "scoped_multi_same_process_three_repeats_after_warmup",
        "matched_scope": {"code": multi_code, "requested_cutoff": multi_cutoff,
                          "periods": ["daily", "15m", "1m", "intraday"],
                          "real_minute_profile": str(minute_profile),
                          "daily_rows": 2240, "15m_rows": 3, "1m_rows": 37, "intraday_rows": 37},
        "warmup": {"chart_report": multi_warmup_report,
                   "complete_png_all_views_manifest_and_run_wall_seconds": multi_warmup_wall,
                   "outputs": multi_warmup_outputs,
                   "outputs_complete": _multi_outputs_complete(multi_warmup_outputs)},
        "repeats": multi_repeats,
        "median_complete_multi_bundle_wall_seconds": multi_median,
        "three_second_criterion_for_this_matched_scope": {
            "limit_seconds": HOT_MEDIAN_LIMIT_SECONDS,
            "statistic": "median of three same-process full combined PNG + four view JSON + manifest + run receipt wall times",
            "passed": multi_median <= HOT_MEDIAN_LIMIT_SECONDS,
        },
        "all_repeat_outputs_complete": all(item["outputs_complete"] for item in multi_repeats),
    }
    multi_mode = {"new_python_process": multi_process_mode,
                  "same_process_three_repeats_after_warmup": multi_repeats_mode}

    all_modes_ok = (source_mode["outputs_complete"] and source_mode["cache_build_confirmed"]
                    and process_mode["exit_code"] == 0 and process_mode["outputs_complete"]
                    and process_mode["cli_run_report"] is not None and process_mode["cache_hit_confirmed"]
                    and repeats_mode["all_repeat_outputs_complete"]
                    and repeats_mode["all_repeats_used_existing_cache"]
                    and multi_process_mode["exit_code"] == 0 and multi_process_mode["outputs_complete"]
                    and multi_cli_report is not None
                    and multi_repeats_mode["all_repeat_outputs_complete"])
    result = {
        "task_id": "F-002",
        "code": code,
        "cutoff": cutoff,
        "window": "10y",
        "contract_version": "chart.v1",
        "snapshot_dir": str(snapshot_dir),
        "output_dir": str(output_dir),
        "run_dir": str(run_dir),
        "status": "passed" if all_modes_ok else "failed",
        "cache_temperature_note": "No OS page-cache flush was performed. C_source_cache_build means a fresh application cache imported from the C snapshot; it is not a cold-disk measurement.",
        "modes": {"daily": {"C_source_cache_build": source_mode,
                             "existing_cache_new_python_process": process_mode,
                             "same_process_three_repeats_after_warmup": repeats_mode},
                  "multi_period": multi_mode},
        "historical_observations": _historical_observations(),
        "limitations": [
            "The 3-second hot criterion is computed from complete same-process PNG and sidecar wall times; drawing_seconds or encoding_seconds alone do not satisfy it.",
            "The new-process run reports real subprocess wall time separately from its CLI pipeline split.",
            "The multi-period timing applies only to the supplied 002025.SZ / 2026-09-18 profile and the measured four-period scope; it does not certify other stock-days.",
            "No HTML timing or full-market export measurement is included."
        ]
    }
    write_json(run_dir / "performance.json", result)
    print(json.dumps({"status": result["status"], "performance_json": str(run_dir / "performance.json"),
                      "run_dir": str(run_dir),
                      "C_source_cache_build_seconds": source_complete_wall,
                      "existing_cache_new_process_wall_seconds": process_wall,
                      "same_process_repeat_seconds": repeat_walls,
                      "same_process_median_seconds": median_wall,
                      "hot_three_second_passed": repeats_mode["hot_three_second_criterion"]["passed"],
                      "multi_new_process_wall_seconds": multi_process_wall,
                      "multi_same_process_repeat_seconds": multi_repeat_walls,
                      "multi_same_process_median_seconds": multi_median,
                      "multi_three_second_passed": multi_repeats_mode["three_second_criterion_for_this_matched_scope"]["passed"]},
                     ensure_ascii=False, allow_nan=False))
    return result


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Measure cache import, new-process cached chart, and same-process PNG timings")
    p.add_argument("--snapshot-dir", type=Path, required=True)
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--code", required=True)
    p.add_argument("--cutoff", required=True)
    p.add_argument("--multi-code", default="002025.SZ")
    p.add_argument("--multi-cutoff", default="2026-09-18T10:07:30+08:00")
    p.add_argument("--minute-snapshot-dir", type=Path)
    p.add_argument("--minute-profile", type=Path)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        result = run_benchmark(args.snapshot_dir, args.output_dir, args.code, args.cutoff,
                               multi_code=args.multi_code, multi_cutoff=args.multi_cutoff,
                               minute_snapshot_dir=args.minute_snapshot_dir,
                               minute_profile=args.minute_profile)
    except Exception as exc:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        failure = {"task_id": "F-001", "status": "failed", "snapshot_dir": str(args.snapshot_dir),
                   "output_dir": str(args.output_dir), "fatal_error": {"type": type(exc).__name__, "message": str(exc)}}
        path = args.output_dir / ("benchmark_failure_" + uuid.uuid4().hex + ".json")
        write_json(path, failure)
        print(json.dumps({"status": "failed", "performance_json": str(path), "fatal_error": failure["fatal_error"]},
                         ensure_ascii=False, allow_nan=False))
        return 1
    return 0 if result.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
