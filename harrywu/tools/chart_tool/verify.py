"""Independent, deterministic acceptance checks for the chart.v1 C snapshot.

This verifier reads only the supplied C snapshot and root-created integration
artifacts. It writes its report and temporary caches below --output-dir.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import uuid

import numpy as np
import pandas as pd
from PIL import Image

from .contracts import (
    ChartError,
    ChartRequest,
    ChartView,
    DAILY_COLUMNS,
    FACTOR_COLUMNS,
    PRICE_ATOL,
    PRICE_RTOL,
)
from .data import build_daily_view, get_last_timings, load_raw
from .io import write_json


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = Path(r"C:\Project Rocket\outputs\chart_tool_development_20261009")
EXPECTED_CODES = ("002025.SZ", "600990.SH", "002326.SZ")
OHLC = ("open", "high", "low", "close")
APPROVED_CONTRACT_SHA256 = {
    "chart_tool/contracts.py": frozenset({
        "b8a81854390066b34de3eec12be6e8c919517f6ffa7fdc2fc76778931263c166",
        "bc8b79e613e5913d53aec2c4fe4a669a7f2ed2af4c358269765878d313a8b28f",
    }),
    "chart_tool/CONTRACT.md": frozenset({
        "82d9058e4e141d6dd6ea4d4e4e3962657cf0c87f44f389f2e334680de3d47fc2",
        "c5120d2fa94a997d8538a1a1a8c6dcece10156fca3d46ddf89e0459c1d706712",
    }),
}


def _contract_digest_allowed(relative: str, digest: str) -> bool:
    """Accept only the frozen amendment 2 or exact approved amendment 3 bytes."""
    return digest in APPROVED_CONTRACT_SHA256.get(relative, frozenset())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _iso_date(value: object) -> str:
    raw = str(value)
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m-%d")
    return date.fromisoformat(raw[:10]).isoformat()


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _request(code: str, cutoff: str, *, window: str = "all", stage: str = "restore") -> ChartRequest:
    return ChartRequest(
        ts_code=code,
        cutoff=cutoff,
        cutoff_source={
            "kind": "manual" if stage == "restore" else "study_end",
            "confidence": "high",
            "evidence_refs": ["F-001 independent verification cutoff fixture"],
            "candidates": [],
            "calibration_segment": None,
        },
        stage=stage,
        window=window,
    )


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _exact_array(actual, expected, label: str) -> None:
    left = np.asarray(actual)
    right = np.asarray(expected)
    _expect(left.shape == right.shape, f"{label}: shape {left.shape} != {right.shape}")
    _expect(np.array_equal(left, right), f"{label}: raw source/cache values differ")


def _check_normalized_source(code: str, daily_source: pd.DataFrame, factor_source: pd.DataFrame,
                             raw, expected: dict, tolerances: dict) -> dict:
    source_dates = [_iso_date(value) for value in daily_source.trade_date.tolist()]
    factor_dates = [_iso_date(value) for value in factor_source.trade_date.tolist()]
    _expect(source_dates == sorted(set(source_dates)), f"{code}: daily keys are not unique and ascending")
    _expect(factor_dates == sorted(set(factor_dates)), f"{code}: factor keys are not unique and ascending")
    _expect(tuple(raw.daily.columns) == DAILY_COLUMNS, f"{code}: normalized daily schema changed")
    _expect(tuple(raw.factors.columns) == FACTOR_COLUMNS, f"{code}: normalized factor schema changed")
    _expect(raw.daily.trade_date.tolist() == source_dates, f"{code}: daily key set/order differs from C source")
    _expect(raw.factors.trade_date.tolist() == factor_dates, f"{code}: full factor key set/order differs from C source")
    _expect(len(raw.daily) == expected["daily_rows"], f"{code}: daily row count differs from independent oracle")
    _expect(len(raw.factors) == expected["factor_rows"], f"{code}: factor row count differs from independent oracle")

    source_prices = daily_source.loc[:, list(OHLC)].to_numpy(dtype=np.float64)
    _expect(np.isfinite(source_prices).all() and (source_prices > 0).all(), f"{code}: source OHLC has missing/nonpositive values")
    _expect((source_prices[:, 1] >= source_prices[:, [0, 2, 3]].max(axis=1)).all(), f"{code}: source high violates OHLC ordering")
    _expect((source_prices[:, 2] <= source_prices[:, [0, 1, 3]].min(axis=1)).all(), f"{code}: source low violates OHLC ordering")
    _expect(np.isfinite(daily_source[["vol", "amount"]].to_numpy(dtype=np.float64)).all(), f"{code}: source amount/volume is missing")
    _expect((daily_source[["vol", "amount"]].to_numpy(dtype=np.float64) >= 0).all(), f"{code}: source amount/volume is negative")

    for column in OHLC:
        _exact_array(raw.daily[column].to_numpy(), daily_source[column].to_numpy(), f"{code} {column}")
    _exact_array(raw.daily.vol.to_numpy(), (daily_source.vol * 100.0).to_numpy(), f"{code} volume shares")
    _exact_array(raw.daily.amount.to_numpy(), (daily_source.amount * 1000.0).to_numpy(), f"{code} amount CNY")
    _exact_array(raw.factors.adj_factor.to_numpy(), factor_source.adj_factor.to_numpy(), f"{code} full factor sequence")
    _expect(all(value is None for value in raw.factors.available_at.tolist()), f"{code}: ingestion timestamps were misused as factor availability")
    _expect(raw.daily.trade_date.isin(raw.factors.trade_date).all(), f"{code}: daily date has no exact factor")
    factor_only = len(set(factor_dates) - set(source_dates))
    _expect(factor_only == expected["factor_only_dates"], f"{code}: factor-only date count differs from independent oracle")

    years = expected.get("years", {})
    year_counts = {}
    for year, count in years.items():
        observed = sum(value.startswith(str(year)) for value in source_dates)
        year_counts[str(year)] = observed
        _expect(observed == count, f"{code}: {year} sample count {observed} != {count}")

    daily_meta = raw.metadata["series"]["daily"]
    _expect(daily_meta["price_basis"] == "unadjusted", f"{code}: raw source is not labelled unadjusted")
    _expect(daily_meta["volume_unit"] == "shares" and daily_meta["amount_unit"] == "CNY", f"{code}: cache normalized units are wrong")
    _expect(daily_meta["original_volume_unit"] == "lots" and daily_meta["volume_scale"] == 100.0, f"{code}: volume conversion metadata is wrong")
    _expect(daily_meta["original_amount_unit"] == "thousand_CNY" and daily_meta["amount_scale"] == 1000.0, f"{code}: amount conversion metadata is wrong")
    return {"daily_rows": len(raw.daily), "factor_rows": len(raw.factors),
            "factor_only_dates": factor_only, "years": year_counts,
            "daily_first": raw.daily.trade_date.iloc[0] if len(raw.daily) else None,
            "daily_last": raw.daily.trade_date.iloc[-1] if len(raw.daily) else None,
            "daily_keys_unique_sorted": True, "factor_keys_unique_sorted": True,
            "raw_numeric_source_cache_exact": True,
            "derived_tolerance": {"rtol": tolerances["rtol"], "atol": tolerances["atol"]}}


def _independent_daily_expected(daily_source: pd.DataFrame, factor_source: pd.DataFrame,
                                *, end_date: str,
                                window: str = "all", cutoff_date: str | None = None) -> pd.DataFrame:
    """Derive qfq bars from frozen raw parquets, without calling production transforms."""
    end = date.fromisoformat(end_date)
    source = daily_source.copy()
    source["trade_date"] = source.trade_date.map(_iso_date)
    factors = factor_source.copy()
    factors["trade_date"] = factors.trade_date.map(_iso_date)
    visible = source.loc[source.trade_date <= end.isoformat()].copy()
    if window != "all":
        years = 10 if window == "10y" else 5
        requested = date.fromisoformat(cutoff_date or end_date)
        try:
            start = requested.replace(year=requested.year - years)
        except ValueError:
            start = requested.replace(year=requested.year - years, day=28)
        visible = visible.loc[visible.trade_date >= start.isoformat()].copy()
    eligible = factors.loc[factors.trade_date <= end.isoformat()].copy()
    factor_by_date = dict(zip(eligible.trade_date, eligible.adj_factor))
    _expect(bool(len(visible)), "independent expected view is empty")
    _expect(set(visible.trade_date) <= set(factor_by_date), "independent oracle has a daily date without exact factor")
    anchor_date = eligible.trade_date.iloc[-1]
    anchor_factor = float(eligible.adj_factor.iloc[-1])
    row_factors = visible.trade_date.map(factor_by_date).to_numpy(dtype=np.float64)
    result = pd.DataFrame({"trade_date": visible.trade_date.tolist()})
    for column in OHLC:
        result[column] = visible[column].to_numpy(dtype=np.float64) * row_factors / anchor_factor
    result["vol"] = visible.vol.to_numpy(dtype=np.float64) * 100.0
    result["amount"] = visible.amount.to_numpy(dtype=np.float64) * 1000.0
    result = result.loc[:, list(DAILY_COLUMNS)].reset_index(drop=True)
    return result


def _compare_view_to_oracle(view: ChartView, expected: pd.DataFrame, label: str) -> None:
    view.validate()
    _expect(view.bars.trade_date.tolist() == expected.trade_date.tolist(), f"{label}: actual dates differ from independent source slice")
    for column in OHLC:
        np.testing.assert_allclose(view.bars[column].to_numpy(dtype=np.float64),
                                   expected[column].to_numpy(dtype=np.float64),
                                   rtol=PRICE_RTOL, atol=PRICE_ATOL,
                                   err_msg=f"{label}: {column} differs from independent qfq formula")
    _exact_array(view.bars.vol.to_numpy(), expected.vol.to_numpy(), f"{label} true volume shares")
    _exact_array(view.bars.amount.to_numpy(), expected.amount.to_numpy(), f"{label} true amount CNY")


def _validate_case_cli_evidence() -> dict:
    evidence_path = OUTPUT_ROOT / "integration" / "case_cli_evidence.json"
    evidence = _read_json(evidence_path)
    _expect(evidence.get("status") == "passed", "root case CLI evidence status is not passed")
    _expect(evidence.get("case_kind") == "SYNTHETIC_ENGINEERING_ONLY", "root case CLI example is not labelled synthetic")
    commands = evidence.get("commands", [])
    names = [item.get("command", [None, None, None, None])[3] for item in commands]
    expected_names = ["case-draft", "case-reveal", "case-freeze", "case-pending", "case-input", "case-reveal", "case-input"]
    _expect(names == expected_names, f"case CLI workflow/order differs: {names}")
    exits = [item.get("exit_code") for item in commands]
    _expect(exits == [0, 2, 0, 0, 0, 0, 0], f"case CLI exit sequence differs: {exits}")
    _expect("CASE_NOT_FOUND" in commands[1].get("stderr", ""), "reveal before verified freeze was not rejected")

    frozen = json.loads(commands[2]["stdout"])
    _expect(frozen["restore_sha256"] == json.loads(commands[0]["stdout"])["draft_bytes_sha256"], "freeze hash differs from draft bytes")
    first_export = json.loads(commands[4]["stdout"])["directory"]
    reveal = json.loads(commands[5]["stdout"])
    _expect(reveal["status"] == "completed" and reveal["pending_after"] == [], "case reveal left a frozen case pending")
    _expect(len(reveal["results"]) == 1 and reveal["results"][0]["rows"] == 4 and reveal["results"][0]["complete"] is False,
            "case reveal did not append the expected sparse incomplete result")
    second_export = json.loads(commands[6]["stdout"])["directory"]
    _expect(json.loads(commands[4]["stdout"])["results_read"] is False and json.loads(commands[6]["stdout"])["results_read"] is False,
            "fresh-context export reported reading result fields")

    store = Path(evidence["case_store"])
    frozen_path = Path(frozen["restore_path"])
    frozen_wrapper = _read_json(frozen_path)
    _expect(frozen_wrapper["restore_sha256"] == frozen["restore_sha256"], "persisted frozen wrapper hash differs")
    result_path = Path(reveal["results"][0]["result"])
    result_record = _read_json(result_path)
    _expect(result_record["complete"] is False and bool(result_record["coverage_warning"]), "result does not mark its outcome incomplete")
    _expect(result_record["restore_sha256"] == frozen["restore_sha256"], "result points to another restore version")
    before_root, after_root = Path(first_export), Path(second_export)
    before_files = sorted(path.relative_to(before_root).as_posix() for path in before_root.rglob("*") if path.is_file())
    after_files = sorted(path.relative_to(after_root).as_posix() for path in after_root.rglob("*") if path.is_file())
    _expect(before_files == after_files, "fresh-context export file set changed after reveal")
    _expect(not any("result" in name.lower() for name in before_files), "fresh-context export contains a result artifact")
    for relative in before_files:
        _expect((before_root / relative).read_bytes() == (after_root / relative).read_bytes(),
                f"fresh-context input changed after reveal: {relative}")
    restore_record = _read_json(before_root / "record.json")
    _expect(restore_record == _read_json(after_root / "record.json"), "restore record changed after reveal")
    _expect(not any(key in restore_record for key in ("results", "result", "outcome", "result_events")), "restore input contains result fields")
    chart = evidence.get("chart", {})
    _expect(chart.get("requested_cutoff") == "2026-09-24T10:07:30+08:00" and chart.get("actual_end") == "2026-09-23",
            "case chart does not separate actual mention from prior-day daily cutoff")
    _expect(evidence.get("frozen_bytes_unchanged") is True and evidence.get("restore_export_equal_before_after_reveal") is True,
            "case CLI evidence does not preserve frozen bytes and restore export")
    return {"case_kind": evidence["case_kind"], "workflow": names,
            "exit_codes": exits, "revealed_daily_rows": reveal["results"][0]["rows"],
            "result_complete": result_record["complete"], "pending_after": reveal["pending_after"],
            "fresh_context_files": before_files, "restore_export_equal_before_after_reveal": True,
            "frozen_bytes_unchanged": True, "case_store": str(store), "result_record": str(result_path)}


def _check_actual_source_chart(snapshot_dir: Path) -> dict:
    integration = OUTPUT_ROOT / "integration"
    run_path = integration / "daily_actual_source.run.json"
    manifest_path = integration / "daily_actual_source.manifest.json"
    view_path = integration / "daily_actual_source.view.json"
    run = _read_json(run_path)
    manifest = _read_json(manifest_path)
    payload = _read_json(view_path)
    view = ChartView.from_dict(payload)
    expected_rows = 2421
    _expect(run["status"] == "ok" and run["rows"] == expected_rows, "root actual-source CLI run status/row count differs")
    _expect(run["actual_start"] == "2016-09-30" and run["actual_end"] == "2026-09-30", "root actual-source CLI coverage differs")
    _expect(manifest["input_rows"] == manifest["candle_count"] == manifest["volume_bar_count"] == expected_rows,
            "root actual-source manifest candle/volume counts differ")
    _expect(manifest["actual_start"] == "2016-09-30" and manifest["actual_end"] == "2026-09-30", "root actual-source manifest dates differ")
    _expect(manifest["view_identity"] == view.identity(), "manifest identity does not match serialized view")
    _expect(view.ts_code == "600990.SH" and view.period == "daily" and view.metadata["window"] == "10y", "root actual-source view identity differs")
    _expect(view.metadata["volume_unit"] == "shares" and view.metadata["amount_unit"] == "CNY", "root actual-source view units differ")
    _expect(set(view.metadata["labels_missing"]) == {"name", "st_status", "themes", "boards"} and not view.metadata["labels"],
            "actual-source chart uses an unverified current label")

    daily_source = pd.read_parquet(snapshot_dir / "600990.SH.daily.parquet")
    factor_source = pd.read_parquet(snapshot_dir / "600990.SH.factors.parquet")
    oracle = _independent_daily_expected(daily_source, factor_source, end_date="2026-09-30",
                                         window="10y", cutoff_date="2026-09-30")
    _compare_view_to_oracle(view, oracle, "root original-source 600990.SH 10y PNG input")
    _expect(len(view.bars) == expected_rows and int(view.bars.vol.sum()) == 10429386611,
            "root actual-source 10y count or exact share total differs")
    expected_first = np.asarray([40.335561786085144, 40.77805192107995, 40.30108203530633, 40.45624091381101])
    np.testing.assert_allclose(view.bars.iloc[0][list(OHLC)].to_numpy(dtype=np.float64), expected_first,
                               rtol=PRICE_RTOL, atol=PRICE_ATOL,
                               err_msg="root actual-source first adjusted OHLC differs from frozen oracle")
    png_path = Path(run["png"])
    _expect(png_path.is_file() and png_path.stat().st_size > 0, "root actual-source PNG is missing or empty")
    with Image.open(png_path) as image:
        dimensions = image.size
    _expect(dimensions == (3840, 1600), f"root actual-source PNG dimensions are {dimensions}")
    return {"run": str(run_path), "manifest": str(manifest_path), "view": str(view_path),
            "png": str(png_path), "png_dimensions": list(dimensions), "rows": len(view.bars),
            "actual_start": view.bars.trade_date.iloc[0], "actual_end": view.bars.trade_date.iloc[-1],
            "volume_shares": int(view.bars.vol.sum()), "first_adjusted_ohlc": view.bars.iloc[0][list(OHLC)].tolist(),
            "source_cache_oracle_comparison": "exact keys/volume/amount; price atol=1e-10 rtol=1e-12"}


def run_verification(snapshot_dir: Path, output_dir: Path) -> dict:
    snapshot_dir = snapshot_dir.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    run_dir = output_dir / ("verification_run_" + uuid.uuid4().hex)
    run_dir.mkdir()
    checks: list[dict] = []
    state: dict = {}

    def check(name: str, function) -> None:
        try:
            details = function()
            checks.append({"name": name, "status": "passed", "details": details})
        except Exception as exc:
            checks.append({"name": name, "status": "failed",
                           "error": {"type": type(exc).__name__, "message": str(exc)}})

    def frozen_snapshot_check() -> dict:
        _expect(snapshot_dir.is_dir(), f"C snapshot directory missing: {snapshot_dir}")
        manifest = _read_json(snapshot_dir / "source_probe.json")
        checked = {}
        for name, expected in manifest["files"].items():
            path = snapshot_dir / name
            _expect(path.is_file(), f"frozen C input missing: {name}")
            observed_size = path.stat().st_size
            observed_hash = _sha256(path)
            _expect(observed_size == expected["size"], f"frozen C input size changed: {name}")
            _expect(observed_hash == expected["sha256"], f"frozen C input digest changed: {name}")
            checked[name] = observed_hash
        expectations = _read_json(snapshot_dir / "independent_expectations.json")
        source_checks = _read_json(snapshot_dir / "independent_source_checks.json")
        _expect("root" in expectations["purpose"].lower() and "oracle" in expectations["purpose"].lower(),
                "independent expectation provenance is missing")
        _expect(source_checks["status"] == "passed", "root independent source scan did not pass")
        freeze = _read_json(OUTPUT_ROOT / "A" / "freeze.json")
        _expect(freeze["contract_version"] == "chart.v1" and freeze["amendment"] == 2,
                "frozen contract amendment is not chart.v1 amendment 2")
        _expect(set(freeze["sha256"]) == set(APPROVED_CONTRACT_SHA256),
                "frozen contract file list differs from the approved amendment 2 baseline")
        accepted_amendment3_updates = {}
        for relative, expected_hash in freeze["sha256"].items():
            path = PROJECT_ROOT / Path(relative)
            observed_hash = _sha256(path)
            _expect(_contract_digest_allowed(relative, expected_hash),
                    f"frozen contract digest is not an approved amendment 2 or amendment 3 hash: {relative}")
            _expect(_contract_digest_allowed(relative, observed_hash),
                    f"contract file digest is not an approved amendment 2 or amendment 3 hash: {relative}")
            if observed_hash != expected_hash:
                accepted_amendment3_updates[relative] = {"frozen_sha256": expected_hash,
                                                          "observed_sha256": observed_hash}
        state["expectations"] = expectations
        state["source_checks"] = source_checks
        state["checked_input_hashes"] = checked
        return {"files_sha256_checked": checked, "contract_version": freeze["contract_version"],
                "amendment": freeze["amendment"],
                "contract_hashes_match": not bool(accepted_amendment3_updates),
                "approved_amendment3_updates": accepted_amendment3_updates}

    def source_cache_check() -> dict:
        expectations = state["expectations"]
        source_checks = state["source_checks"]
        tolerances = {"atol": 1e-10, "rtol": 1e-12}
        cache_root = run_dir / "cache"
        source_reports = {}
        raw_cached = {}
        for code in EXPECTED_CODES:
            daily_source = pd.read_parquet(snapshot_dir / f"{code}.daily.parquet")
            factor_source = pd.read_parquet(snapshot_dir / f"{code}.factors.parquet")
            _expect(daily_source.ts_code.eq(code).all() and factor_source.ts_code.eq(code).all(),
                    f"{code}: C snapshot contains another security")
            expected = source_checks["sources"][code]
            raw_request = _request(code, "2026-09-30T15:00:00+08:00", window="all", stage="study")
            first = load_raw(raw_request, cache_dir=cache_root, snapshot_dir=snapshot_dir)
            build_timing = deepcopy(get_last_timings())
            _expect(not build_timing["cache_hit"] and build_timing["source_read_seconds"] > 0,
                    f"{code}: C snapshot did not build a fresh cache")
            second = load_raw(raw_request, cache_dir=cache_root, snapshot_dir=snapshot_dir)
            hit_timing = deepcopy(get_last_timings())
            _expect(hit_timing["cache_hit"] and hit_timing["source_read_seconds"] == 0,
                    f"{code}: second load did not read the existing cache")
            for component in ("daily", "factors", "calendar"):
                pd.testing.assert_frame_equal(getattr(first, component), getattr(second, component), check_exact=True)
            _expect(first.metadata == second.metadata, f"{code}: cache metadata changed on roundtrip")
            source_report = _check_normalized_source(code, daily_source, factor_source, second,
                                                     expected, tolerances)
            source_report["first_load_timings"] = build_timing
            source_report["existing_cache_timings"] = hit_timing
            source_reports[code] = source_report
            raw_cached[code] = second
        # The specific independent expectation includes both known 2017 and 2018 intervals.
        for code in EXPECTED_CODES:
            independent = expectations["real_source_expected"][code]
            _expect(source_reports[code]["years"]["2017"] == independent["daily_2017"],
                    f"{code}: 2017 daily count differs from the independent expectation")
            _expect(source_reports[code]["years"]["2018"] == independent["daily_2018"],
                    f"{code}: 2018 daily count differs from the independent expectation")
        state["raw_cached"] = raw_cached
        state["source_reports"] = source_reports
        return {"stocks": source_reports, "source_to_cache_numeric_comparison": "exact for raw OHLCVA and every factor row",
                "price_adjustment_tolerance": tolerances}

    def long_history_check() -> dict:
        code = "600990.SH"
        req = _request(code, "2026-09-30T15:00:00+08:00", window="10y", stage="study")
        raw = state["raw_cached"][code]
        view = build_daily_view(raw, req)
        daily_source = pd.read_parquet(snapshot_dir / f"{code}.daily.parquet")
        factor_source = pd.read_parquet(snapshot_dir / f"{code}.factors.parquet")
        oracle = _independent_daily_expected(daily_source, factor_source, end_date="2026-09-30",
                                             window="10y", cutoff_date="2026-09-30")
        _compare_view_to_oracle(view, oracle, "600990.SH ten-year daily view")
        _expect(len(view.bars) == 2421, f"ten-year row count {len(view.bars)} != 2421")
        _expect(view.bars.trade_date.iloc[0] == "2016-09-30" and view.bars.trade_date.iloc[-1] == "2026-09-30",
                "ten-year inclusive date coverage differs")
        _expect(int(view.bars.vol.sum()) == 10429386611, "ten-year true volume sum differs from independent expectation")
        state["long_view"] = view
        state["source_daily_600990"] = daily_source
        state["source_factors_600990"] = factor_source
        return {"code": code, "rows": len(view.bars), "first_date": view.bars.trade_date.iloc[0],
                "last_date": view.bars.trade_date.iloc[-1], "volume_shares": int(view.bars.vol.sum()),
                "price_tolerance": {"atol": PRICE_ATOL, "rtol": PRICE_RTOL},
                "oracle_source": "frozen C daily/factor parquet, independent fixed-anchor formula"}

    def cutoff_check() -> dict:
        expectations = state["expectations"]
        raw = state["raw_cached"]["600990.SH"]
        sessions = expectations["sessions"]
        daily_expected = expectations["v01_daily"]
        cases = [
            ("preopen", sessions["preopen"], daily_expected["preopen_last_day"]),
            ("lunch", sessions["lunch"], daily_expected["lunch_last_day"]),
            ("intraday_incomplete", expectations["minute_cutoff"]["mention"], daily_expected["intraday_last_day"]),
            ("after_close", sessions["after_close"], daily_expected["after_close_last_day"]),
        ]
        observed = {}
        for label, cutoff, expected_last in cases:
            req = _request("600990.SH", cutoff, window="all", stage="restore")
            view = build_daily_view(raw, req)
            effective_day = expected_last
            expected_effective = effective_day + "T15:00:00+08:00"
            _expect(view.metadata["effective_cutoff"] == expected_effective,
                    f"{label}: effective daily cutoff {view.metadata['effective_cutoff']} != {expected_effective}")
            _expect(not view.bars.empty and view.bars.trade_date.iloc[-1] == expected_last,
                    f"{label}: last visible daily bar differs")
            _expect(all(day <= expected_last for day in view.bars.trade_date.tolist()),
                    f"{label}: daily view contains a bar after the independent cutoff")
            if label == "intraday_incomplete":
                _expect("INTRADAY_DAY_OMITTED" in view.metadata["warnings"],
                        "intraday daily view omitted the explicit conservative-cutoff warning")
                _expect(view.metadata["requested_cutoff"] == "2026-09-24T10:07:30+08:00",
                        "actual mention cutoff changed")
            daily_source = state["source_daily_600990"]
            factor_source = state["source_factors_600990"]
            oracle = _independent_daily_expected(daily_source, factor_source, end_date=expected_last)
            _compare_view_to_oracle(view, oracle, f"{label} independent cutoff slice")
            observed[label] = {"requested_cutoff": cutoff, "effective_cutoff": view.metadata["effective_cutoff"],
                               "last_visible_daily_bar": view.bars.trade_date.iloc[-1],
                               "rows": len(view.bars), "warnings": view.metadata["warnings"]}

        mention_cutoff = "2026-09-24T10:07:30+08:00"
        before = build_daily_view(raw, _request("600990.SH", mention_cutoff, window="all", stage="restore"))
        altered = deepcopy(raw)
        future_mask = altered.factors.trade_date > "2026-09-23"
        altered.factors.loc[future_mask, "adj_factor"] *= 7.0
        after_future_factor_change = build_daily_view(altered, _request("600990.SH", mention_cutoff, window="all", stage="restore"))
        pd.testing.assert_frame_equal(before.bars, after_future_factor_change.bars, check_exact=True)
        _expect(before.metadata == after_future_factor_change.metadata,
                "a later factor change altered the earlier view metadata/digest")

        post_close = build_daily_view(raw, _request("600990.SH", "2026-09-24T16:00:00+08:00", window="all", stage="restore"))
        injected = post_close.bars.copy()
        future_row = injected.iloc[[-1]].copy()
        future_row.loc[:, "trade_date"] = "2026-09-25"
        injected = pd.concat([injected, future_row], ignore_index=True)
        try:
            ChartView(post_close.ts_code, post_close.period, injected, deepcopy(post_close.metadata)).validate()
        except ChartError as exc:
            _expect(exc.code == "CUTOFF_LEAK", f"future-bar rejection returned {exc.code}")
        else:
            raise AssertionError("a processed 2026-09-25 bar was accepted by the 2026-09-24 view")

        duplicate = pd.concat([post_close.bars, post_close.bars.iloc[[-1]]], ignore_index=True)
        try:
            ChartView(post_close.ts_code, post_close.period, duplicate, deepcopy(post_close.metadata)).validate()
        except ChartError as exc:
            _expect(exc.code == "DUPLICATE_BARS", f"duplicate-bar rejection returned {exc.code}")
        else:
            raise AssertionError("duplicate daily bars were accepted")
        unsorted = post_close.bars.iloc[::-1].reset_index(drop=True)
        try:
            ChartView(post_close.ts_code, post_close.period, unsorted, deepcopy(post_close.metadata)).validate()
        except ChartError as exc:
            _expect(exc.code == "UNSORTED_BARS", f"unsorted-bar rejection returned {exc.code}")
        else:
            raise AssertionError("unsorted daily bars were accepted")

        return {"sessions": observed,
                "future_factor_mutation": "unchanged bars, factor lineage and visible digest",
                "injected_future_bar": "rejected with CUTOFF_LEAK",
                "duplicate_bar": "rejected with DUPLICATE_BARS",
                "unsorted_bar": "rejected with UNSORTED_BARS",
                "minute_policy": "daily checks remain scoped to F-001; see chart_tool.verify_multi / F-002 for independent synthetic and scoped real minute verification"}

    def actual_case_check() -> dict:
        return _validate_case_cli_evidence()

    def actual_source_check() -> dict:
        return _check_actual_source_chart(snapshot_dir)

    check("C snapshot and frozen contract fingerprints", frozen_snapshot_check)
    if state.get("expectations") is not None:
        check("independent source and exact cache comparisons; 2017/2018 coverage", source_cache_check)
    else:
        checks.append({"name": "independent source and exact cache comparisons; 2017/2018 coverage",
                       "status": "blocked", "error": {"type": "DependencyError", "message": "Frozen snapshot/oracle check did not pass"}})
    if state.get("raw_cached") is not None:
        check("600990.SH ten-year independent OHLCV oracle", long_history_check)
        check("v0.1 cutoff boundaries and adversarial future row", cutoff_check)
    else:
        for name in ("600990.SH ten-year independent OHLCV oracle", "v0.1 cutoff boundaries and adversarial future row"):
            checks.append({"name": name, "status": "blocked", "error": {"type": "DependencyError", "message": "Source/cache comparison did not pass"}})
    check("root actual-source PNG, manifest, and exact frozen-source view", actual_source_check)
    check("root case CLI fields, freeze/reveal separation, and fresh-context export", actual_case_check)

    status = "passed" if all(item["status"] == "passed" for item in checks) else "failed"
    result = {"task_id": "F-001", "contract_version": "chart.v1", "status": status,
              "snapshot_dir": str(snapshot_dir), "output_dir": str(output_dir),
              "temporary_cache_dir": str(run_dir / "cache"), "checks": checks,
              "limitations": [
                  "F-001 remains the daily verifier; F-002 chart_tool.verify_multi independently checks minute transformation and minute cutoffs against root literals and scoped real evidence.",
                  "Historical labels are absent from this snapshot; no current name/category fallback is verified or used.",
                  "The supplied exchange calendar begins 2018-01-01; existing 2017 daily bars are retained, but no 2017 session calendar is inferred.",
                  "Root case CLI evidence is explicitly SYNTHETIC_ENGINEERING_ONLY and is workflow validation, not a real transcript case.",
                  "Full-market export, HTML rendering, and minute panels are not delivered by v0.1."
              ]}
    write_json(output_dir / "checks.json", result)
    return result


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Run deterministic F-001 checks against a frozen C snapshot")
    p.add_argument("--snapshot-dir", type=Path, required=True)
    p.add_argument("--output-dir", type=Path, required=True)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        result = run_verification(args.snapshot_dir, args.output_dir)
    except Exception as exc:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        result = {"task_id": "F-001", "contract_version": "chart.v1", "status": "failed",
                  "snapshot_dir": str(args.snapshot_dir), "output_dir": str(args.output_dir),
                  "checks": [], "fatal_error": {"type": type(exc).__name__, "message": str(exc)}}
        write_json(args.output_dir / "checks.json", result)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
