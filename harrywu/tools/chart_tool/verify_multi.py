"""Independent F-002 verification for multi-period cutoff behavior.

The expected synthetic values come from the root-authored literal oracle in
inputs/minute/expected.json. Real checks are restricted to the one profile
approved stock-day (002025.SZ / 2026-09-18).
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, time, timedelta
import hashlib
import json
from pathlib import Path
import uuid

import numpy as np
import pandas as pd

from .contracts import ChartError, ChartRequest, ChartView
from .data import load_raw
from .io import write_json
from .multiperiod import attach_snapshot_minutes, build_multiperiod_views


OUTPUT_ROOT = Path(r"C:\Project Rocket\outputs\chart_tool_development_20261009")
DEFAULT_SNAPSHOT = OUTPUT_ROOT / "inputs"
PERIODS = ("daily", "15m", "1m", "intraday")
OHLCV = ("open", "high", "low", "close", "vol", "amount")
MINUTE_COLUMNS = ("trade_date", *OHLCV, "bar_start", "bar_end", "is_partial")


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _need(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _request(ts_code: str, cutoff: str, source: dict, *, periods=PERIODS) -> ChartRequest:
    return ChartRequest(ts_code, cutoff, deepcopy(source), stage="restore", window="10y",
                        periods=tuple(periods))


def _synthetic_raw(root: Path):
    """Load canonical fixture arrays directly, without the tested adapter."""
    from .contracts import RawBundle

    minute_root = root / "minute"
    metadata = _read_json(minute_root / "metadata.json")
    raw = RawBundle(
        "000001.SZ",
        pd.read_parquet(minute_root / "daily.parquet"),
        pd.read_parquet(minute_root / "factors.parquet"),
        metadata,
        one_min=pd.read_parquet(minute_root / "one_min.parquet"),
        fifteen_min=pd.read_parquet(minute_root / "fifteen_min.parquet"),
        calendar=pd.read_parquet(minute_root / "calendar.parquet"),
    )
    raw.validate(require_minutes=True)
    return raw


def _stamp(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _independent_effective(raw, cutoff: str) -> datetime:
    instant = _stamp(cutoff)
    day = instant.date().isoformat()
    calendar = raw.calendar
    row = calendar.loc[calendar.trade_date == day]
    _need(not row.empty, f"oracle calendar missing requested date {day}")
    if not bool(row.is_open.iloc[0]) or instant.time() < time(9, 30):
        prior = calendar.loc[(calendar.trade_date < day) & calendar.is_open]
        _need(not prior.empty, "oracle calendar has no prior open session")
        return datetime.fromisoformat(prior.trade_date.iloc[-1] + "T15:00:00+08:00")
    if time(11, 30) <= instant.time() < time(13, 0):
        return datetime.fromisoformat(day + "T11:30:00+08:00")
    close = datetime.fromisoformat(day + "T15:00:00+08:00")
    return min(instant, close)


def _aggregate(frame: pd.DataFrame, start: str, partial: bool) -> dict:
    return {
        "trade_date": frame.trade_date.iloc[0],
        "open": float(frame.open.iloc[0]),
        "high": float(frame.high.max()),
        "low": float(frame.low.min()),
        "close": float(frame.close.iloc[-1]),
        "vol": float(frame.vol.sum()),
        "amount": float(frame.amount.sum()),
        "bar_start": start,
        "bar_end": frame.bar_end.iloc[-1],
        "is_partial": bool(partial),
    }


def _independent_fifteen(minutes: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for day, day_frame in minutes.groupby("trade_date", sort=False):
        for base_clock in (time(9, 30), time(13, 0)):
            base = datetime.fromisoformat(day + "T" + base_clock.isoformat() + "+08:00")
            for index in range(8):
                start = base + timedelta(minutes=15 * index)
                end = start + timedelta(minutes=15)
                group = day_frame.loc[
                    (day_frame.bar_end.map(_stamp) > start)
                    & (day_frame.bar_end.map(_stamp) <= end)
                ]
                if group.empty:
                    continue
                is_partial = _stamp(group.bar_end.iloc[-1]) != end
                _need(is_partial or len(group) == 15,
                      f"independent complete 15m bin has {len(group)} rows")
                rows.append(_aggregate(group, start.isoformat(), is_partial))
    return pd.DataFrame(rows, columns=list(MINUTE_COLUMNS))


def _independent_frames(raw, cutoff: str) -> tuple[dict[str, pd.DataFrame], datetime]:
    """Cut source minute arrays, then aggregate them independently of producer."""
    effective = _independent_effective(raw, cutoff)
    request_day = _stamp(cutoff).date().isoformat()
    current_needed = effective.date().isoformat() == request_day
    historical = raw.daily.loc[
        raw.daily.trade_date < effective.date().isoformat() if current_needed
        else raw.daily.trade_date <= effective.date().isoformat()
    ].copy().reset_index(drop=True)
    historical["bar_start"] = historical.trade_date.map(lambda d: f"{d}T09:30:00+08:00")
    historical["bar_end"] = historical.trade_date.map(lambda d: f"{d}T15:00:00+08:00")
    historical["is_partial"] = False
    daily = historical.loc[:, list(MINUTE_COLUMNS)].copy()

    visible = raw.one_min.loc[raw.one_min.bar_end.map(lambda value: _stamp(value) <= effective)].copy()
    visible = visible.loc[visible.trade_date >= _stamp(cutoff).date().isoformat()].reset_index(drop=True)
    if current_needed and not visible.empty:
        partial_day = effective.time() < time(15)
        current = _aggregate(visible, effective.date().isoformat() + "T09:30:00+08:00", partial_day)
        daily = pd.concat([daily, pd.DataFrame([current], columns=MINUTE_COLUMNS)], ignore_index=True)
    fifteen = _independent_fifteen(visible)

    factors = raw.factors.loc[raw.factors.trade_date <= effective.date().isoformat()].copy()
    closed_date = effective.date()
    if current_needed and effective.time() < time(15):
        closed_date -= timedelta(days=1)
    availability = factors.available_at.map(
        lambda value: (value is not None and not pd.isna(value)
                       and _stamp(str(value)) <= effective)
    )
    eligible = factors.loc[
        ((factors.trade_date <= closed_date.isoformat()) & factors.available_at.isna())
        | availability
    ].copy()
    # The fixture uses timestamped factors and the real profile proves a same-
    # value current factor. The eligible anchor remains the latest proven row.
    if eligible.empty:
        raise AssertionError("independent factor oracle has no eligible anchor")
    anchor = float(eligible.adj_factor.iloc[-1])
    factor_by_date = dict(zip(factors.trade_date, factors.adj_factor))
    eligible_by_date = set(eligible.trade_date)
    date_frames = {"daily": daily, "15m": fifteen, "1m": visible, "intraday": visible.copy()}
    for period, frame in date_frames.items():
        if frame.empty:
            continue
        for day in frame.trade_date.unique():
            _need(day in factor_by_date, f"independent factor oracle lacks exact {period} factor for {day}")
            if day not in eligible_by_date:
                compatibility = raw.metadata.get("compatibility", {}).get("one_min", {})
                proof = f"ROOT_VERIFIED_UNCHANGED_FACTOR:{day}"
                prior = eligible.loc[eligible.trade_date < day]
                _need(proof in compatibility.get("evidence_refs", []) and not prior.empty
                      and float(factor_by_date[day]) == float(prior.adj_factor.iloc[-1]),
                      f"independent oracle cannot establish factor availability for {day}")
                multiplier = float(prior.adj_factor.iloc[-1]) / anchor
            else:
                multiplier = float(factor_by_date[day]) / anchor
            mask = frame.trade_date == day
            for column in ("open", "high", "low", "close"):
                frame.loc[mask, column] = frame.loc[mask, column].to_numpy(dtype=float) * multiplier
    date_frames["daily"] = daily
    date_frames["15m"] = fifteen
    date_frames["1m"] = visible
    date_frames["intraday"] = visible.copy()
    return date_frames, effective


def _compare_frame(actual: pd.DataFrame, expected: pd.DataFrame, label: str) -> None:
    # Amendment 3 adds avg_price only to newly built intraday views. Keep this
    # F-002 verifier focused on its frozen canonical columns and allow that one
    # schema extension without weakening row/value comparisons.
    actual_columns = actual.columns.tolist()
    expected_columns = expected.columns.tolist()
    extra = [column for column in actual_columns if column not in expected_columns]
    _need(actual_columns == expected_columns or
          (actual_columns == expected_columns + ["avg_price"] and extra == ["avg_price"]),
          f"{label}: canonical columns differ")
    _need(len(actual) == len(expected), f"{label}: row count {len(actual)} != {len(expected)}")
    for column in expected_columns:
        if column in {"open", "high", "low", "close"}:
            np.testing.assert_allclose(actual[column].to_numpy(dtype=float),
                                       expected[column].to_numpy(dtype=float),
                                       rtol=1e-12, atol=1e-10,
                                       err_msg=f"{label}: {column} differs from independent oracle")
        elif column in {"vol", "amount"}:
            np.testing.assert_array_equal(actual[column].to_numpy(), expected[column].to_numpy(),
                                          err_msg=f"{label}: {column} differs from canonical input")
        else:
            _need(actual[column].tolist() == expected[column].tolist(),
                  f"{label}: {column} differs from source interval/order")


def _synthetic_checks(snapshot_dir: Path, output_dir: Path, oracle: dict) -> dict:
    minute_root = snapshot_dir / "minute"
    raw = _synthetic_raw(snapshot_dir)
    input_hashes = {name: _sha256(minute_root / name) for name in
                    ("metadata.json", "expected.json", "daily.parquet", "factors.parquet",
                     "calendar.parquet", "one_min.parquet", "fifteen_min.parquet")}
    _need("root_literal_oracle" in raw.metadata["source_version"],
          "synthetic source version is not the root literal oracle")
    _need(len(raw.one_min) == 240 and raw.one_min.bar_end.is_unique
          and raw.one_min.bar_end.tolist() == sorted(raw.one_min.bar_end.tolist()),
          "synthetic one-minute source arrays are not 240 unique ordered intervals")
    _need(len(raw.fifteen_min) == 16 and raw.fifteen_min.bar_end.is_unique,
          "synthetic source fifteen-minute array is not 16 unique bars")

    source = oracle["request"]["cutoff_source"]
    request = _request(oracle["request"]["ts_code"], oracle["request"]["cutoff"], source)
    views = build_multiperiod_views(raw, request)
    expected_frames, independent_effective = _independent_frames(raw, request.cutoff)
    compared = {}
    for period in PERIODS:
        view = views[period]
        _compare_frame(view.bars, expected_frames[period], f"synthetic {period}")
        roundtrip = ChartView.from_dict(view.to_dict())
        _need(roundtrip.identity() == view.identity(), f"synthetic {period}: canonical view roundtrip changed identity")
        _need(view.metadata["requested_cutoff"] == request.cutoff,
              f"synthetic {period}: requested cutoff changed")
        _need(view.metadata["effective_cutoff"] == independent_effective.isoformat(),
              f"synthetic {period}: effective cutoff differs from source calendar oracle")
        compared[period] = {"rows": len(view.bars),
                            "actual_first": view.bars.iloc[0].get("bar_end") if len(view.bars) and "bar_end" in view.bars else (view.bars.iloc[0].get("trade_date") if len(view.bars) else None),
                            "actual_last": view.bars.iloc[-1].get("bar_end") if len(view.bars) and "bar_end" in view.bars else (view.bars.iloc[-1].get("trade_date") if len(view.bars) else None),
                            "effective_cutoff": view.metadata["effective_cutoff"],
                            "identity_roundtrip": True}
    anchor_dates = {views[p].metadata["factor_lineage"]["anchor_trade_date"] for p in PERIODS}
    anchor_values = {views[p].metadata["factor_lineage"]["anchor_value"] for p in PERIODS}
    _need(anchor_dates == {oracle["at_100730"]["anchor_date"]},
          f"synthetic views do not share the literal anchor date: {anchor_dates}")
    _need(anchor_values == {oracle["at_100730"]["anchor_value"]},
          f"synthetic views do not share the literal anchor factor: {anchor_values}")

    # A date-only current factor is not evidence that it was known intraday.
    # If its value is unchanged and the explicit proof is present, rows can use
    # that unchanged value while the anchor remains on the prior closed date.
    date_only_unchanged = deepcopy(raw)
    current_factor = date_only_unchanged.factors.trade_date == "2026-09-24"
    date_only_unchanged.factors.loc[current_factor, "adj_factor"] = 2.0
    date_only_unchanged.factors.loc[current_factor, "available_at"] = None
    date_only_unchanged.metadata["compatibility"]["one_min"]["evidence_refs"].append(
        "ROOT_VERIFIED_UNCHANGED_FACTOR:2026-09-24"
    )
    unchanged_views = build_multiperiod_views(date_only_unchanged, request)
    unchanged_expected, unchanged_effective = _independent_frames(date_only_unchanged, request.cutoff)
    for period in PERIODS:
        _compare_frame(unchanged_views[period].bars, unchanged_expected[period],
                       f"date-only unchanged current factor {period}")
        lineage = unchanged_views[period].metadata["factor_lineage"]
        _need(lineage["anchor_trade_date"] == "2026-09-23" and lineage["anchor_value"] == 2.0,
              f"date-only unchanged factor incorrectly became the {period} anchor")
        _need(unchanged_views[period].metadata["effective_cutoff"] == unchanged_effective.isoformat(),
              f"date-only unchanged factor changed the {period} effective cutoff")

    literal = oracle["at_100730"]
    daily, fifteen, one_min = views["daily"].bars, views["15m"].bars, views["1m"].bars
    _need(len(one_min) == literal["one_min_count"] and len(fifteen) == literal["15m_count"],
          "10:07:30 literal minute/15m row counts differ")
    _need(one_min.bar_end.iloc[-1] == literal["max_bar_end"]
          and fifteen.bar_end.iloc[-1] == literal["max_bar_end"],
          "10:07:30 literal last visible bar time differs")
    last15 = fifteen.iloc[-1]
    np.testing.assert_allclose(last15[list(("open", "high", "low", "close"))].to_numpy(dtype=float),
                               [literal[k] for k in ("partial15_open", "partial15_high", "partial15_low", "partial15_close")],
                               rtol=0, atol=1e-12)
    _need(float(last15.vol) == literal["partial15_vol"] and bool(last15.is_partial),
          "10:07:30 partial 15m volume/flag differs")
    last_day = daily.iloc[-1]
    np.testing.assert_allclose(last_day[list(("open", "high", "low", "close"))].to_numpy(dtype=float),
                               [literal[k] for k in ("partial_day_open", "partial_day_high", "partial_day_low", "partial_day_close")],
                               rtol=0, atol=1e-12)
    _need(float(last_day.vol) == literal["partial_day_vol"] and bool(last_day.is_partial),
          "10:07:30 partial daily volume/flag differs")
    previous = daily.loc[daily.trade_date == "2026-09-23"].iloc[0]
    np.testing.assert_allclose(previous[["open", "high", "low", "close"]].to_numpy(dtype=float),
                               literal["previous_day_ohlc"], rtol=0, atol=1e-12)
    _need(float(raw.one_min.loc[raw.one_min.bar_end == "2026-09-24T10:08:00+08:00", "high"].iloc[0])
          == literal["excluded_future_high"], "future sentinel source row does not match literal")

    session_reports = {}
    for label, cutoff, expected in (
        ("preopen", "2026-09-24T09:00:00+08:00", None),
        ("lunch", "2026-09-24T12:20:00+08:00", oracle["lunch_1220"]),
        ("postclose", "2026-09-24T16:00:00+08:00", oracle["close_1600"]),
    ):
        req = _request("000001.SZ", cutoff, source)
        produced = build_multiperiod_views(raw, req)
        independently_expected, effective = _independent_frames(raw, cutoff)
        for period in PERIODS:
            _compare_frame(produced[period].bars, independently_expected[period], f"{label} {period}")
            _need(produced[period].metadata["effective_cutoff"] == effective.isoformat(),
                  f"{label} {period}: effective cutoff differs")
        if label == "preopen":
            _need(effective.isoformat() == "2026-09-23T15:00:00+08:00",
                  "pre-open effective daily cutoff is not prior session close")
            _need(all(produced[p].bars.empty for p in ("15m", "1m", "intraday")),
                  "pre-open request fabricated minute observations")
            _need(produced["daily"].bars.trade_date.iloc[-1] == "2026-09-23",
                  "pre-open daily view did not end on prior session")
        else:
            _need(len(produced["1m"].bars) == expected["one_min_count"], f"{label}: one-minute row count differs")
            _need(len(produced["15m"].bars) == expected["15m_count"], f"{label}: 15m row count differs")
            _need(produced["1m"].bars.bar_end.iloc[-1] == expected["max_bar_end"],
                  f"{label}: last one-minute endpoint differs")
            _need(produced["1m"].metadata["effective_cutoff"] == expected["effective_cutoff"],
                  f"{label}: effective cutoff differs from literal root value")
        session_reports[label] = {"effective_cutoff": effective.isoformat(),
                                  "rows": {p: len(produced[p].bars) for p in PERIODS},
                                  "last_minute": produced["1m"].bars.bar_end.iloc[-1] if len(produced["1m"].bars) else None}

    # A change confined to strictly future source rows/factors cannot change a
    # restoration. Keep metadata and every canonical view identical as well.
    before = build_multiperiod_views(raw, request)
    altered = deepcopy(raw)
    later = altered.one_min.bar_end > literal["max_bar_end"]
    altered.one_min.loc[later, "high"] = 999999.0
    future_daily = altered.daily.trade_date > "2026-09-24"
    altered.daily.loc[future_daily, "high"] = 9999999.0
    future_factor = altered.factors.trade_date > "2026-09-24"
    altered.factors.loc[future_factor, "adj_factor"] *= 100.0
    after = build_multiperiod_views(altered, request)
    for period in PERIODS:
        pd.testing.assert_frame_equal(before[period].bars, after[period].bars, check_exact=True)
        _need(before[period].metadata == after[period].metadata,
              f"future-row mutation changed {period} metadata or digest")

    failures = {}
    mutations = []
    missing_factor = deepcopy(raw)
    missing_factor.factors.loc[missing_factor.factors.trade_date == "2026-09-24", "available_at"] = "2026-09-24T10:08:00+08:00"
    mutations.append(("changed_unavailable_current_factor", missing_factor, "FACTOR_UNAVAILABLE"))
    unverified = deepcopy(raw)
    unverified.metadata["compatibility"]["one_min"]["status"] = "unverified"
    mutations.append(("source_unverified", unverified, "SOURCE_UNVERIFIED"))
    missing_minute = deepcopy(raw)
    missing_minute.one_min = missing_minute.one_min.loc[
        missing_minute.one_min.bar_end != literal["max_bar_end"]
    ].reset_index(drop=True)
    mutations.append(("missing_minute", missing_minute, "MISSING_MINUTE"))
    for label, candidate, expected_code in mutations:
        try:
            build_multiperiod_views(candidate, request)
        except ChartError as exc:
            _need(exc.code == expected_code, f"{label}: expected {expected_code}, got {exc.code}")
            failures[label] = {"status": "rejected", "error_code": exc.code}
        else:
            raise AssertionError(f"{label}: invalid evidence was accepted")
    return {"status": "passed", "source_input_sha256": input_hashes,
            "source_counts": {"daily": len(raw.daily), "factors": len(raw.factors),
                              "calendar": len(raw.calendar), "one_min": len(raw.one_min),
                              "native_fifteen_min_available_but_not_used": len(raw.fifteen_min)},
            "synthetic_10_07_30": {"views": compared, "shared_anchor_date": next(iter(anchor_dates)),
                                   "shared_anchor_value": next(iter(anchor_values)),
                                   "last_15m_ohlcv": last15[list(("open", "high", "low", "close", "vol"))].tolist(),
                                   "partial_daily_ohlcv": last_day[list(("open", "high", "low", "close", "vol"))].tolist()},
            "session_cutoffs": session_reports,
            "future_mutation": "all four canonical views and metadata unchanged after later rows/factors changed",
            "date_only_unchanged_current_factor": {
                "status": "passed", "anchor_trade_date": "2026-09-23",
                "anchor_value": 2.0,
                "meaning": "date-only current factor did not become the intraday anchor; explicit unchanged proof allowed the current bars",
            },
            "required_failures": failures}


def _real_profile_checks(snapshot_dir: Path, output_dir: Path) -> dict:
    profile_path = snapshot_dir / "minute_profile_20260918.json"
    profile = _read_json(profile_path)
    code = "002025.SZ"
    cutoff = "2026-09-18T10:07:30+08:00"
    source = {"kind": "manual", "confidence": "high",
              "evidence_refs": ["F-002 scoped real minute profile verification"],
              "candidates": [], "calibration_segment": None}
    request = _request(code, cutoff, source)
    cache = output_dir / "real_cache"
    daily_raw = load_raw(request, cache_dir=cache, snapshot_dir=snapshot_dir)
    raw = attach_snapshot_minutes(daily_raw, request, snapshot_dir=snapshot_dir,
                                  profile_path=profile_path)
    views = build_multiperiod_views(raw, request)
    expected = {
        "daily_rows": 2240, "minute_rows": 37, "fifteen_rows": 3,
        "last_end": "2026-09-18T10:07:00+08:00",
        "last15_ohlc": [73.8, 74.22, 73.7, 73.97], "last15_vol": 610100.0,
        "partial_daily_ohlc": [73.71, 74.59, 72.69, 73.97],
        "partial_daily_vol": 5239110.0, "partial_daily_amount": 386157653.0,
        "anchor_date": "2026-09-17", "anchor_value": 6.7061,
    }
    _need(len(views["daily"].bars) == expected["daily_rows"], "real daily count differs from independent literal")
    _need(len(views["1m"].bars) == expected["minute_rows"]
          and len(views["intraday"].bars) == expected["minute_rows"],
          "real one-minute/intraday counts differ from independent literal")
    _need(len(views["15m"].bars) == expected["fifteen_rows"], "real derived 15m count differs")
    _need(views["1m"].bars.bar_end.iloc[-1] == expected["last_end"]
          and views["15m"].bars.bar_end.iloc[-1] == expected["last_end"],
          "real requested/effective/last actual bar times differ")
    last15 = views["15m"].bars.iloc[-1]
    np.testing.assert_allclose(last15[["open", "high", "low", "close"]].to_numpy(dtype=float),
                               expected["last15_ohlc"], rtol=0, atol=1e-10)
    _need(float(last15.vol) == expected["last15_vol"] and bool(last15.is_partial),
          "real partial 15m volume or flag differs")
    partial = views["daily"].bars.iloc[-1]
    np.testing.assert_allclose(partial[["open", "high", "low", "close"]].to_numpy(dtype=float),
                               expected["partial_daily_ohlc"], rtol=0, atol=1e-10)
    _need(float(partial.vol) == expected["partial_daily_vol"]
          and float(partial.amount) == expected["partial_daily_amount"]
          and bool(partial.is_partial), "real partial daily volume/amount/flag differs")
    for period in PERIODS:
        lineage = views[period].metadata["factor_lineage"]
        _need(lineage["anchor_trade_date"] == expected["anchor_date"]
              and lineage["anchor_value"] == expected["anchor_value"],
              f"real {period} shared anchor differs")
        _need(views[period].metadata["requested_cutoff"] == cutoff
              and views[period].metadata["effective_cutoff"] == cutoff,
              f"real {period} requested/effective cutoff differs")
        _need(ChartView.from_dict(views[period].to_dict()).identity() == views[period].identity(),
              f"real {period} canonical roundtrip changed identity")
    # Check raw source array lineage and the declared 09:30 auction exclusion.
    entry = profile["one_min_files"]["2026-09-18"]
    source_path = snapshot_dir / entry["file"]
    _need(_sha256(source_path) == entry["sha256"], "real minute profile input hash differs")
    source_minutes = pd.read_parquet(source_path)
    source_minutes = source_minutes.loc[~source_minutes.datetime.astype(str).str.startswith("2026-09-18 09:30")].copy()
    normalized = source_minutes.loc[:, ["open", "high", "low", "close", "vol", "amount"]].reset_index(drop=True)
    for column in OHLCV:
        np.testing.assert_array_equal(raw.one_min[column].to_numpy(), normalized[column].to_numpy(),
                                      err_msg=f"real one-minute {column} differs from profile source array")
    _need("OPENING_AUCTION_0930_EXCLUDED" in views["1m"].metadata["warnings"],
          "real 09:30 auction exclusion warning is absent")
    _need("FIFTEEN_MINUTES_DERIVED_FROM_ONE_MINUTE" in views["15m"].metadata["warnings"],
          "real 15m provenance does not identify derivation from the 1m input")
    return {"status": "passed", "code": code, "trade_date": "2026-09-18",
            "profile_id": profile.get("profile_id"), "profile_minute_sha256": entry["sha256"],
            "raw_source_comparison": "exact OHLCV arrays after scoped 09:30 exclusion",
            "expected": expected,
            "observed": {"rows": {p: len(views[p].bars) for p in PERIODS},
                         "last_1m": views["1m"].bars.bar_end.iloc[-1],
                         "last_15m_ohlcv": last15[["open", "high", "low", "close", "vol"]].tolist(),
                         "partial_daily_ohlcv": partial[["open", "high", "low", "close", "vol", "amount"]].tolist(),
                         "warnings": views["daily"].metadata["warnings"]},
            "native_15m_used": False, "roundtrip_all_periods": True}


def run_verification(snapshot_dir: Path, output_dir: Path) -> dict:
    snapshot_dir = snapshot_dir.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    oracle = _read_json(snapshot_dir / "minute" / "expected.json")
    checks = []
    for name, function in (
        ("root literal multi-period cutoff oracle", lambda: _synthetic_checks(snapshot_dir, output_dir, oracle)),
        ("scoped real 002025.SZ 2026-09-18 multi-period profile", lambda: _real_profile_checks(snapshot_dir, output_dir)),
    ):
        try:
            details = function()
            checks.append({"name": name, "status": "passed", "details": details})
        except Exception as exc:
            checks.append({"name": name, "status": "failed",
                           "error": {"type": type(exc).__name__, "message": str(exc)}})
    result = {"task_id": "F-002", "contract_version": "chart.v1 amendment 2",
              "status": "passed" if all(item["status"] == "passed" for item in checks) else "failed",
              "snapshot_dir": str(snapshot_dir), "output_dir": str(output_dir),
              "oracle": "root-authored inputs/minute/expected.json; expected values derived before implementation",
              "checks": checks,
              "limits": ["Synthetic values are engineering-only.",
                         "Real minute approval is restricted to 002025.SZ / 2026-09-18 under the supplied profile.",
                         "No other stock-day, native 15m feed, or real transcript case is certified."]}
    write_json(output_dir / "checks.json", result)
    return result


def parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Independent F-002 multi-period verification")
    parser.add_argument("--snapshot-dir", "--input-dir", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        result = run_verification(args.snapshot_dir, args.output_dir)
    except Exception as exc:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        result = {"task_id": "F-002", "contract_version": "chart.v1 amendment 2",
                  "status": "failed", "snapshot_dir": str(args.snapshot_dir),
                  "output_dir": str(args.output_dir), "checks": [],
                  "fatal_error": {"type": type(exc).__name__, "message": str(exc)}}
        write_json(args.output_dir / "checks.json", result)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
