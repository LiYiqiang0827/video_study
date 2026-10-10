"""Runnable local chart and staged-case entry point. No online fallback."""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
from time import perf_counter

from .contracts import ChartError, ChartRequest, ChartView, RenderOptions, parse_date, parse_timestamp, validate_cutoff_source
from .io import view_to_dict, write_json
from .paths import writable_path, output_bundle

CHINA = timezone(timedelta(hours=8))
DEFAULT_OUTPUT = Path(__file__).resolve().parents[3] / "outputs" / "chart_tool"


def _now() -> str:
    return datetime.now(CHINA).isoformat(timespec="seconds")


def make_request(args) -> ChartRequest:
    source_path = getattr(args, "cutoff_source_json", None)
    if args.stage == "restore" and (not args.cutoff or not (args.cutoff_evidence or source_path)):
        raise ChartError("CUTOFF_UNRESOLVED", "Restoration requires --cutoff and --cutoff-evidence")
    cutoff = args.cutoff or _now()
    source = json.loads(source_path.read_text(encoding="utf-8")) if source_path else {
        "kind": "manual" if args.stage == "restore" else "study_end",
        "confidence": "medium" if args.stage == "restore" else "high",
        "evidence_refs": [args.cutoff_evidence or "Independent study: explicit end or current China time"],
        "candidates": [], "calibration_segment": None}
    validate_cutoff_source(source, cutoff)
    request = ChartRequest(
        ts_code=args.code, cutoff=cutoff, stage=args.stage, window=args.window,
        periods=("daily", "15m", "1m", "intraday") if args.command == "chart-multi" else ("daily",),
        cutoff_source=source,
    )
    request.validate()
    return request


@output_bundle
def run_chart(request: ChartRequest, *, output_path: Path, cache_dir: Path,
              snapshot_dir: Path | None, refresh: bool = False,
              options: RenderOptions | None = None,
              zoom_start: str | None = None, zoom_end: str | None = None,
              last_bars: int | None = None) -> dict:
    from .data import load_raw, build_daily_view
    from .render import render_png_with_geometry
    from .display import crop_last_bars

    started = perf_counter()
    output_path = writable_path(output_path, protected_roots=[snapshot_dir], suffix=".png")
    options = options or RenderOptions()
    t = perf_counter()
    raw = load_raw(request, cache_dir=cache_dir, snapshot_dir=snapshot_dir, refresh=refresh)
    read_seconds = perf_counter() - t
    t = perf_counter()
    view = build_daily_view(raw, request)
    view = zoom_view(view, start=zoom_start, end=zoom_end)
    if last_bars is not None:
        view = crop_last_bars(view, last_bars)
    conversion_seconds = perf_counter() - t
    manifest, geometry = render_png_with_geometry(view, options, output_path)
    manifest_data = manifest.to_dict()
    view_path = output_path.with_suffix(".view.json")
    manifest_path = output_path.with_suffix(".manifest.json")
    write_json(view_path, view_to_dict(view))
    write_json(manifest_path, manifest_data)
    pixel_info = _geometry_receipt(output_path, geometry, manifest.output_path)
    return {"status": "pixel_check_failed" if pixel_info["pixel_check"]["status"] == "fail" else manifest.status, "png": manifest.output_path,
            "view": str(view_path), "manifest": str(manifest_path),
            "rows": manifest.input_rows, "actual_start": manifest.actual_start,
            "actual_end": manifest.actual_end, "stage": request.stage,
            "requested_cutoff": request.cutoff,
            "timings": {"reading_seconds": read_seconds,
                        "conversion_seconds": conversion_seconds,
                        **manifest.timings, "pipeline_total_seconds": perf_counter() - started},
            "warnings": list(manifest.warnings), "request": request.to_dict(), **pixel_info}


def zoom_view(view: ChartView, *, start: str | None = None,
              end: str | None = None) -> ChartView:
    """Compatibility wrapper for display-only date cropping."""
    from .display import crop_dates
    return crop_dates(view, start, end)


def _source_args(parser):
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_OUTPUT / "cache")
    parser.add_argument("--snapshot-dir", type=Path)
    parser.add_argument("--refresh", action="store_true")


def _geometry_receipt(output_path, geometry, png):
    from .pixel_check import check_png
    geometry_path = output_path.with_suffix(".geometry.json")
    pixel_path = output_path.with_suffix(".pixel.json")
    pixel = check_png(output_path, geometry) if png else {"status": "skipped", "panels": {}}
    write_json(geometry_path, geometry)
    write_json(pixel_path, pixel)
    return {"geometry": str(geometry_path), "pixel": str(pixel_path),
            "pixel_check": pixel, "canvas_width_px": geometry.get("canvas", {}).get("width_px")}


@output_bundle
def run_multiperiod_chart(request: ChartRequest, *, output_path: Path, cache_dir: Path,
                          snapshot_dir: Path | None, minute_snapshot_dir: Path | None = None,
                          profile_path: Path | None = None, refresh: bool = False,
                          options: RenderOptions | None = None,
                          display_period: str = "all", minute_registry=None,
                          minute_sessions: int = 5, daily_bars: int = 250) -> dict:
    from .data import load_raw
    from .multiperiod import attach_snapshot_minutes, build_multiperiod_views
    from .render_multi import render_multiperiod_png_with_geometry, render_period_png_with_geometry
    started = perf_counter()
    output_path = writable_path(output_path, protected_roots=[snapshot_dir, minute_snapshot_dir, profile_path.parent if profile_path else None], suffix=".png")
    options = options or RenderOptions(height_px=2400)
    t = perf_counter()
    daily_raw = load_raw(request, cache_dir=cache_dir, snapshot_dir=snapshot_dir, refresh=refresh)
    if (minute_snapshot_dir is None) != (profile_path is None):
        raise ChartError("INVALID_REQUEST", "Legacy minute profile and snapshot directory must be supplied together")
    if profile_path is not None:
        raw = attach_snapshot_minutes(daily_raw, request, snapshot_dir=minute_snapshot_dir, profile_path=profile_path)
    else:
        from .minute_source import attach_minutes, minute_start_for_sessions
        request = replace(request, minute_start=minute_start_for_sessions(daily_raw, request.cutoff, minute_sessions))
        raw = attach_minutes(daily_raw, request, registry_path=minute_registry or _default_minute_registry(), cache_dir=cache_dir)
    reading_seconds = perf_counter() - t
    t = perf_counter()
    views = build_multiperiod_views(raw, request)
    conversion_seconds = perf_counter() - t
    if display_period == "all":
        manifest, geometry = render_multiperiod_png_with_geometry(views, options, output_path, combined_daily_bars=daily_bars)
    elif display_period in views:
        manifest, geometry = render_period_png_with_geometry(views[display_period], options, output_path)
    else:
        raise ChartError("INVALID_REQUEST", "Unknown display period")
    paths = {}
    for period, view in views.items():
        path = output_path.with_suffix(f".{period}.view.json")
        write_json(path, view_to_dict(view))
        paths[period] = str(path)
    manifest_path = output_path.with_suffix(".manifest.json")
    write_json(manifest_path, manifest)
    pixel_info = _geometry_receipt(output_path, geometry, manifest["output_path"])
    return {"status": "pixel_check_failed" if pixel_info["pixel_check"]["status"] == "fail" else manifest["status"], "png": manifest["output_path"],
            "views": paths, "manifest": str(manifest_path),
            "rows": {period: len(view.bars) for period, view in views.items()},
            "stage": request.stage, "requested_cutoff": request.cutoff,
            "timings": {"reading_seconds": reading_seconds,
                        "conversion_seconds": conversion_seconds,
                        **manifest["timings"], "pipeline_total_seconds": perf_counter() - started},
            "warnings": manifest["warnings"], "request": request.to_dict(), **pixel_info}


def _default_minute_registry():
    return Path(os.environ.get("CHART_TOOL_MINUTE_REGISTRY", str(Path(__file__).with_name("minute_sources.json"))))


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="只读看图工具：十年日K、真实量柱、先还原后揭晓")
    sub = p.add_subparsers(dest="command", required=True)
    chart = sub.add_parser("chart", help="生成完整日K PNG和经过截止处理的输入JSON")
    chart.add_argument("code", help="证券代码，如600990.SH")
    chart.add_argument("--cutoff", help="北京时间ISO时刻，如2026-09-24T10:07:30+08:00")
    chart.add_argument("--stage", choices=["study", "restore"], default="study")
    chart.add_argument("--cutoff-evidence")
    chart.add_argument("--cutoff-source-json", type=Path)
    chart.add_argument("--window", choices=["10y", "5y", "all"], default="10y")
    chart.add_argument("--out", type=Path, required=True)
    chart.add_argument("--ma", type=int, nargs="*", default=[])
    chart.add_argument("--width", type=int, default=3840)
    chart.add_argument("--price-scale", choices=["linear", "log"], default="linear")
    chart.add_argument("--no-auto-width", action="store_true")
    chart.add_argument("--last-bars", type=int)
    chart.add_argument("--zoom-start", help="在已截止视图内局部放大，YYYY-MM-DD")
    chart.add_argument("--zoom-end", help="在已截止视图内局部放大，YYYY-MM-DD")
    _source_args(chart)
    multi = sub.add_parser("chart-multi", help="已核验分钟来源的日K/15m/1m/分时组合图")
    multi.add_argument("code")
    multi.add_argument("--cutoff", required=True)
    multi.add_argument("--stage", choices=["study", "restore"], default="study")
    multi.add_argument("--cutoff-evidence")
    multi.add_argument("--cutoff-source-json", type=Path)
    multi.add_argument("--window", choices=["10y", "5y", "all"], default="10y")
    multi.add_argument("--out", type=Path, required=True)
    multi.add_argument("--width", type=int, default=3840)
    multi.add_argument("--price-scale", choices=["linear", "log"], default="linear")
    multi.add_argument("--minute-registry", type=Path, default=_default_minute_registry())
    multi.add_argument("--minute-sessions", type=int, default=5)
    multi.add_argument("--daily-bars", type=int, default=250)
    multi.add_argument("--period", choices=["all", "daily", "15m", "1m", "intraday"], default="all")
    multi.add_argument("--ma", type=int, nargs="*", default=[])
    multi.add_argument("--minute-profile", type=Path)
    multi.add_argument("--minute-snapshot-dir", type=Path)
    _source_args(multi)
    prefetch = sub.add_parser("prefetch", help="按给定代码串行准备日线与完整因子缓存")
    prefetch.add_argument("codes", nargs="+")
    prefetch.add_argument("--cutoff", default=None)
    _source_args(prefetch)
    for command in ("case-draft", "case-revise"):
        case = sub.add_parser(command)
        case.add_argument("--store", type=Path, required=True)
        case.add_argument("--record", type=Path, required=True)
    freeze = sub.add_parser("case-freeze", help="核验输入并冻结；失败不揭晓")
    freeze.add_argument("--store", type=Path, required=True)
    freeze.add_argument("--case-id", required=True)
    freeze.add_argument("--revision", type=int, default=1)
    freeze.add_argument("--expected-sha256", required=True)
    export = sub.add_parser("case-input", help="导出还原专用新上下文输入；不读取结果")
    export.add_argument("--store", type=Path, required=True)
    export.add_argument("--case-id", required=True)
    export.add_argument("--revision", type=int, default=1)
    export.add_argument("--export-dir", type=Path, required=True)
    pending = sub.add_parser("case-pending", help="列出所有已冻结但未揭晓的案例版本")
    pending.add_argument("--store", type=Path, required=True)
    reveal = sub.add_parser("case-reveal", help="自动追加已冻结案例的后续走势；默认覆盖全部未揭晓版本")
    reveal.add_argument("--store", type=Path, required=True)
    reveal.add_argument("--case-id")
    reveal.add_argument("--revision", type=int, default=1)
    reveal.add_argument("--observed-through", default=None)
    reveal.add_argument("--minute-registry", type=Path)
    _source_args(reveal)
    reveal_chart = sub.add_parser("case-reveal-chart", help="以冻结还原锚点绘制揭晓日线和路径标注")
    reveal_chart.add_argument("--store", type=Path, required=True)
    reveal_chart.add_argument("--case-id", required=True)
    reveal_chart.add_argument("--revision", type=int, default=1)
    reveal_chart.add_argument("--out", type=Path, required=True)
    reveal_chart.add_argument("--window", choices=["10y", "5y", "all"], default="10y")
    reveal_chart.add_argument("--zoom-start")
    reveal_chart.add_argument("--zoom-end")
    reveal_chart.add_argument("--last-bars", type=int)
    reveal_chart.add_argument("--observed-through", required=True)
    _source_args(reveal_chart)
    return p


def _export_restore(args) -> dict:
    from .cases import load_restore_input
    record = load_restore_input(args.store, args.case_id, args.revision)
    target = writable_path(args.export_dir, protected_roots=[args.store])
    if target.exists():
        raise ChartError("CASE_CONFLICT", "Fresh-context export directory must not already exist")
    target.mkdir(parents=True)
    for item in record["input_refs"]:
        relative = Path(item["path"])
        source = (args.store / relative).resolve()
        source.relative_to(args.store.resolve())
        destination = (target / relative).resolve()
        destination.relative_to(target)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        if hashlib.sha256(destination.read_bytes()).hexdigest() != item["sha256"]:
            raise ChartError("INPUT_CHANGED", "Input changed during restoration export")
    write_json(target / "record.json", record)
    (target / "CONTEXT_BOUNDARY.txt").write_text(
        "还原分析必须使用新上下文。仅提供本目录的冻结记录、截止内图表、转写和帧。"
        "不得附加结果目录、后续走势或已经看过结果的完整对话。"
        "本目录边界是工作流约束，不是操作系统隔离。\n", encoding="utf-8")
    return {"status": "exported", "directory": str(target), "case_id": args.case_id,
            "revision": args.revision, "results_read": False}


def _reveal(args) -> dict:
    from .reveal import run_reveal
    report, _ = run_reveal(args.store, observed_through=args.observed_through or _now(),
                          cache_dir=args.cache_dir, snapshot_dir=args.snapshot_dir,
                          refresh=args.refresh, case_id=args.case_id, revision=args.revision,
                          minute_registry=getattr(args, "minute_registry", None))
    return report


@output_bundle
def run_reveal_chart(*, output_path, store, case_id, revision, observed_through,
                     snapshot_dir, cache_dir, window="10y", zoom_start=None,
                     zoom_end=None, last_bars=None, refresh=False):
    from .cases import load_restore_input, load_results
    from .data import load_raw
    from .display import crop_dates, crop_last_bars
    from .reveal import build_reveal_daily_view
    from .render import render_png_with_geometry
    started = perf_counter()
    record = load_restore_input(store, case_id, revision)
    results = load_results(store, case_id, revision)
    if not results or "path_snapshot" not in results[-1]:
        raise ChartError("NEEDS_V3_RESULT", "Reveal charts require the latest result to be Amendment 3")
    result = results[-1]
    request = ChartRequest(record["request"]["ts_code"], observed_through,
        {"kind": "study_end", "confidence": "high", "evidence_refs": ["Frozen restoration and v3 reveal result"],
         "candidates": [], "calibration_segment": None}, stage="reveal", window=window)
    t = perf_counter()
    raw = load_raw(request, cache_dir=cache_dir, snapshot_dir=snapshot_dir, refresh=refresh)
    reading = perf_counter() - t
    t = perf_counter()
    view = build_reveal_daily_view(raw, record, observed_through=observed_through, window=window)
    request = replace(request, cutoff_source=view.metadata["cutoff_source"])
    view = crop_dates(view, zoom_start, zoom_end)
    if last_bars is not None:
        view = crop_last_bars(view, last_bars)
    conversion = perf_counter() - t
    visible = set(view.bars.trade_date)
    ref = result["path_snapshot"]["reference"]
    kind_text = {"next_session_open":"开盘价", "auction_price":"竞价", "last_minute_close":"提及时价", "lunch_close":"午休收盘"}[ref["kind"]]
    annotations = [{"kind":"hline", "at":None, "price":ref["price"],
                    "text":f"参考价 {ref['price']:.2f}（{kind_text}）", "style":"reference"}]
    if ref["trade_date"] in visible:
        annotations.append({"kind":"vline", "at":ref["trade_date"], "price":None,
                            "text":"提及 " + record["request"]["cutoff"][5:16].replace("T", " "), "style":"mention"})
    for horizon in result["path_snapshot"]["horizons"]:
        if horizon["complete"] and horizon["trade_date"] in visible:
            annotations.append({"kind":"vline", "at":horizon["trade_date"], "price":None,
                                "text":f"第{horizon['k']}日", "style":"horizon"})
    manifest, geometry = render_png_with_geometry(view, RenderOptions(annotations=tuple(annotations)), output_path)
    view_path = output_path.with_suffix(".view.json")
    manifest_path = output_path.with_suffix(".manifest.json")
    write_json(view_path, view.to_dict())
    write_json(manifest_path, manifest.to_dict())
    receipt = _geometry_receipt(output_path, geometry, manifest.output_path)
    return {"status":"pixel_check_failed" if receipt["pixel_check"]["status"] == "fail" else manifest.status,
            "png":manifest.output_path, "view":str(view_path), "manifest":str(manifest_path),
            "rows":manifest.input_rows, "request":request.to_dict(), "case_id":case_id, "revision":revision,
            "result_sequence":result["result_sequence"], "warnings":list(manifest.warnings),
            "timings":{"reading_seconds":reading,"conversion_seconds":conversion,**manifest.timings,
                       "pipeline_total_seconds":perf_counter()-started}, **receipt}


def main(argv=None, *, process_started: float | None = None) -> int:
    args = parser().parse_args(argv)
    exit_code = 0
    try:
        if args.command == "chart":
            report = run_chart(make_request(args), output_path=args.out, cache_dir=args.cache_dir,
                               snapshot_dir=args.snapshot_dir, refresh=args.refresh,
                               options=RenderOptions(width_px=args.width, ma_periods=tuple(args.ma),
                                                     price_scale=args.price_scale, auto_width=not args.no_auto_width),
                               zoom_start=args.zoom_start, zoom_end=args.zoom_end,
                               last_bars=args.last_bars, process_started=process_started)
        elif args.command == "chart-multi":
            report = run_multiperiod_chart(make_request(args), output_path=args.out,
                                          cache_dir=args.cache_dir, snapshot_dir=args.snapshot_dir,
                                          minute_snapshot_dir=args.minute_snapshot_dir,
                                          profile_path=args.minute_profile, refresh=args.refresh,
                                          options=RenderOptions(width_px=args.width,
                                                                height_px=2400 if args.period == "all" else 1600,
                                                                price_scale=args.price_scale,
                                                                ma_periods=tuple(args.ma)),
                                          display_period=args.period, minute_registry=args.minute_registry,
                                          minute_sessions=args.minute_sessions, daily_bars=args.daily_bars,
                                          process_started=process_started)
        elif args.command == "case-reveal-chart":
            report = run_reveal_chart(output_path=args.out, store=args.store, case_id=args.case_id,
                                      revision=args.revision, observed_through=args.observed_through,
                                      snapshot_dir=args.snapshot_dir, cache_dir=args.cache_dir,
                                      window=args.window, zoom_start=args.zoom_start, zoom_end=args.zoom_end,
                                      last_bars=args.last_bars, refresh=args.refresh, process_started=process_started)
        elif args.command == "prefetch":
            from .data import load_raw
            reports = []
            for code in args.codes:
                request = ChartRequest(code, args.cutoff or _now(), {"kind": "study_end", "confidence": "high", "evidence_refs": ["User supplied prefetch code list"], "candidates": [], "calibration_segment": None})
                t = perf_counter()
                raw = load_raw(request, cache_dir=args.cache_dir, snapshot_dir=args.snapshot_dir, refresh=args.refresh)
                reports.append({"code": code, "rows": len(raw.daily), "factor_rows": len(raw.factors), "seconds": perf_counter() - t})
            report = {"status": "completed", "serial": True, "stocks": reports}
        elif args.command in {"case-draft", "case-revise"}:
            from .cases import save_draft, append_revision
            fn = save_draft if args.command == "case-draft" else append_revision
            path = fn(args.store, json.loads(args.record.read_text(encoding="utf-8")))
            report = {"draft_path": str(path), "draft_bytes_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        elif args.command == "case-freeze":
            from .cases import freeze_restore
            report = freeze_restore(args.store, args.case_id, args.revision, expected_sha256=args.expected_sha256)
        elif args.command == "case-pending":
            from .cases import list_pending_reveals
            report = {"pending": list_pending_reveals(args.store)}
        elif args.command == "case-input":
            report = _export_restore(args)
        else:
            from .reveal import run_reveal
            report, exit_code = run_reveal(args.store, observed_through=args.observed_through or _now(),
                                          cache_dir=args.cache_dir, snapshot_dir=args.snapshot_dir,
                                          refresh=args.refresh, case_id=args.case_id, revision=args.revision,
                                          minute_registry=args.minute_registry)
        print(json.dumps(report, ensure_ascii=False, allow_nan=False))
        return 4 if report.get("status") == "pixel_check_failed" else exit_code
    except (ChartError, OSError, ValueError) as exc:
        error = exc.to_dict() if isinstance(exc, ChartError) else {"code": type(exc).__name__, "message": str(exc)}
        print(json.dumps({"status": "error", "error": error}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
