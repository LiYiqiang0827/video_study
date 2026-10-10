"""B-001 objective daily/cache tests; synthetic samples are engineering only."""
from copy import deepcopy
from pathlib import Path
import json
import sqlite3
import uuid

import pandas as pd
import pytest

from chart_tool import data
from chart_tool.contracts import ChartError, ChartRequest, frame_digest

OUT = Path(r"C:\Project Rocket\outputs\chart_tool_development_20261009\B")
INPUT = OUT.parent / "inputs"


@pytest.fixture
def work():
    path = OUT / "test-runs" / uuid.uuid4().hex
    path.mkdir(parents=True)
    return path


def request(cutoff="2026-09-24T15:00:00+08:00", *, code="600990.SH", window="all", stage="restore"):
    return ChartRequest(code, cutoff, {"kind": "manual", "confidence": "high",
                        "evidence_refs": ["synthetic-engineering-cutoff"], "candidates": [],
                        "calibration_segment": None}, window=window, stage=stage)


def snapshot(path, *, dates=None, factors=None, calendar=True):
    path.mkdir(parents=True, exist_ok=True)
    dates = dates or ["20260922", "20260924", "20260925"]
    factors = factors or [("20260922", 2.0), ("20260923", 4.0), ("20260924", 4.0), ("20260925", 8.0)]
    daily = pd.DataFrame({"ts_code": ["600990.SH"] * len(dates), "trade_date": dates,
                          "open": [10.0] * len(dates), "high": [12.0] * len(dates),
                          "low": [8.0] * len(dates), "close": [11.0] * len(dates),
                          "vol": [123.5] * len(dates), "amount": [456.7] * len(dates),
                          "name": ["DO_NOT_USE_FUTURE_NAME"] * len(dates)})
    factor = pd.DataFrame({"ts_code": ["600990.SH"] * len(factors), "trade_date": [d for d, _ in factors],
                           "adj_factor": [v for _, v in factors], "snap_ts": ["2030-01-01"] * len(factors)})
    daily.to_parquet(path / "600990.SH.daily.parquet", index=False)
    factor.to_parquet(path / "600990.SH.factors.parquet", index=False)
    if calendar:
        cal_dates = pd.date_range("2026-09-20", "2026-09-26")
        pd.DataFrame({"cal_date": cal_dates.strftime("%Y%m%d"), "exchange": "SSE",
                      "is_open": [0, 1, 1, 1, 1, 1, 0], "snap_ts": "2030-01-01"}).to_parquet(path / "calendar.parquet", index=False)
    return daily, factor


def load(path, req=None, **kwargs):
    return data.load_raw(req or request(), cache_dir=path / "cache", snapshot_dir=path / "source", **kwargs)


def test_independent_arithmetic_factor_only_and_nonmutation(work):
    snapshot(work / "source")
    raw = load(work)
    before = deepcopy(raw)
    req = request("2026-09-24T10:07:30+08:00")
    view = data.build_daily_view(raw, req)
    oracle = json.loads((INPUT / "independent_expectations.json").read_text(encoding="utf-8"))["price_adjustment"]
    assert view.bars.iloc[0][["open", "high", "low", "close"]].tolist() == oracle["expected_ohlc"]
    assert view.bars.vol.tolist() == [oracle["expected_volume_shares"]]
    assert view.bars.amount.tolist() == [oracle["expected_amount_CNY"]]
    assert view.metadata["factor_lineage"]["anchor_trade_date"] == "2026-09-23"
    assert view.metadata["factor_lineage"]["visible_factor_count"] == 2
    assert view.metadata["effective_cutoff"] == "2026-09-23T15:00:00+08:00"
    assert "INTRADAY_DAY_OMITTED" in view.metadata["warnings"]
    assert view.metadata["gap_dates"] == ["2026-09-23"]
    assert view.metadata["labels"] == {}
    assert view.metadata["labels_missing"] == ["name", "st_status", "themes", "boards"]
    assert "DO_NOT_USE" not in json.dumps(view.to_dict())
    pd.testing.assert_frame_equal(before.daily, raw.daily, check_exact=True)
    pd.testing.assert_frame_equal(before.factors, raw.factors, check_exact=True)
    assert before.metadata == raw.metadata


@pytest.mark.parametrize("clock,expected", [("09:00:00", "2026-09-22"), ("10:07:30", "2026-09-22"), ("12:20:00", "2026-09-22"), ("14:59:59", "2026-09-22"), ("15:00:00", "2026-09-24"), ("16:00:00", "2026-09-24")])
def test_close_boundaries(work, clock, expected):
    snapshot(work / "source")
    view = data.build_daily_view(load(work), request(f"2026-09-24T{clock}+08:00"))
    assert view.bars.trade_date.iloc[-1] == expected
    assert view.metadata["factor_lineage"]["anchor_value"] == 4


def test_future_changes_do_not_change_visible_values_or_digest(work):
    snapshot(work / "source")
    raw = load(work)
    first = data.build_daily_view(raw, request())
    changed = deepcopy(raw)
    changed.daily.loc[changed.daily.trade_date == "2026-09-25", ["open", "close"]] = 10.5
    changed.factors.loc[changed.factors.trade_date == "2026-09-25", "adj_factor"] = 8000.0
    changed.metadata["warnings"] = ["FUTURE_RESULT_WIN", "FUTURE_COVERAGE_2030"]
    changed.labels["name"] = {"value": "Future Name", "effective_date": "2026-09-24", "available_at": "2026-09-25T15:00:00+08:00", "source_id": "fixture", "evidence_ref": "fixture"}
    second = data.build_daily_view(changed, request())
    pd.testing.assert_frame_equal(first.bars, second.bars, check_exact=True)
    assert first.metadata == second.metadata
    exported = json.dumps(second.to_dict())
    assert "2026-09-25" not in exported and "FUTURE" not in exported and "Future Name" not in exported


def test_eligible_label_and_factor_availability(work):
    snapshot(work / "source")
    raw = load(work)
    raw.labels["name"] = {"value": "Known Name", "effective_date": "2026-09-22", "available_at": "2026-09-23T15:00:00+08:00", "source_id": "fixture", "evidence_ref": "fixture"}
    assert data.build_daily_view(raw, request()).metadata["labels"]["name"]["value"] == "Known Name"
    raw.factors.loc[raw.factors.trade_date == "2026-09-24", "available_at"] = "2026-09-25T15:00:00+08:00"
    with pytest.raises(ChartError, match="exact eligible") as error:
        data.build_daily_view(raw, request())
    assert error.value.code == "MISSING_FACTOR"


def test_exact_cache_roundtrip(work):
    original, _ = snapshot(work / "source")
    first = load(work)
    second = load(work)
    for key in ("daily", "factors", "calendar"):
        pd.testing.assert_frame_equal(getattr(first, key), getattr(second, key), check_exact=True)
    assert first.metadata == second.metadata
    assert len(second.factors) == 4 and second.factors.available_at.tolist() == [None] * 4
    assert second.daily.vol.tolist() == (original.vol * 100).tolist()
    timing = data.get_last_timings()
    assert timing["cache_hit"] is True and timing["cache_load_seconds"] > 0
    assert timing["source_read_seconds"] == 0


@pytest.mark.parametrize("corruption", ["file", "manifest", "pointer", "metadata"])
def test_corrupt_cache_rebuilt(work, corruption):
    snapshot(work / "source")
    original = load(work)
    partition = work / "cache" / "600990.SH"
    current = (partition / "CURRENT").read_text()
    directory = partition / current
    if corruption == "file":
        (directory / "factors.parquet").write_bytes(b"corrupt")
    elif corruption == "pointer":
        (partition / "CURRENT").write_text("../../source")
    elif corruption == "metadata":
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        manifest["metadata"]["series"]["daily"]["volume_scale"] = 1
        (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    else:
        (directory / "manifest.json").write_text("{}")
    rebuilt = load(work)
    assert data.get_last_timings()["cache_rebuilt"] is True
    assert (partition / "CURRENT").read_text() != current
    pd.testing.assert_frame_equal(rebuilt.daily, original.daily, check_exact=True)


def test_fail_fast_lock_and_owner_preserved(work):
    snapshot(work / "source")
    partition = work / "cache" / "600990.SH"
    partition.mkdir(parents=True)
    (partition / "LOCK").write_text("other-owner")
    with pytest.raises(ChartError) as error:
        load(work)
    assert error.value.code == "CACHE_LOCKED"
    assert (partition / "LOCK").read_text() == "other-owner"


def test_failed_atomic_pointer_retains_original(work, monkeypatch):
    snapshot(work / "source")
    original = load(work)
    partition = work / "cache" / "600990.SH"
    current = (partition / "CURRENT").read_text()
    real_replace = data.os.replace
    def fail_pointer(src, dst):
        if Path(dst).name == "CURRENT":
            raise OSError("synthetic atomic failure")
        return real_replace(src, dst)
    with monkeypatch.context() as context:
        context.setattr(data.os, "replace", fail_pointer)
        with pytest.raises(ChartError) as error:
            load(work, refresh=True)
        assert error.value.code == "CACHE_IO"
    assert (partition / "CURRENT").read_text() == current
    assert sorted(p.name for p in partition.iterdir()) == ["CURRENT", current]
    pd.testing.assert_frame_equal(load(work).daily, original.daily, check_exact=True)


@pytest.mark.parametrize("refresh", [False, True])
def test_historical_revision_same_max_date(work, refresh):
    snapshot(work / "source")
    original = load(work)
    source_path = work / "source" / "600990.SH.daily.parquet"
    source = pd.read_parquet(source_path)
    source.loc[0, "close"] = 10.0
    source.to_parquet(source_path, index=False)
    revised = load(work, refresh=refresh)
    assert revised.daily.trade_date.iloc[-1] == original.daily.trade_date.iloc[-1]
    assert revised.daily.close.iloc[0] == 10 and original.daily.close.iloc[0] == 11
    assert revised.metadata["source_version"] != original.metadata["source_version"]


def test_changed_source_during_import_rejected(work, monkeypatch):
    snapshot(work / "source")
    original_read = data._read_source
    def changed_read(*args):
        result = original_read(*args)
        path = work / "source" / "600990.SH.factors.parquet"
        factors = pd.read_parquet(path)
        factors.loc[0, "adj_factor"] = 3
        factors.to_parquet(path, index=False)
        return result
    monkeypatch.setattr(data, "_read_source", changed_read)
    with pytest.raises(ChartError) as error:
        load(work)
    assert error.value.code == "SOURCE_CHANGED"
    assert not (work / "cache" / "600990.SH" / "CURRENT").exists()


@pytest.mark.parametrize("kind,code", [("missing", "MISSING_FACTOR"), ("unordered", "UNSORTED_FACTORS"), ("duplicate", "DUPLICATE_FACTORS"), ("bars", "UNSORTED_BARS")])
def test_invalid_source_rejected_without_repair(work, kind, code):
    daily, factors = snapshot(work / "source")
    if kind == "missing":
        factors = factors.iloc[1:]
    elif kind == "unordered":
        factors = factors.iloc[::-1]
    elif kind == "duplicate":
        factors = pd.concat([factors.iloc[:1], factors])
    else:
        daily = daily.iloc[::-1]
    factors.to_parquet(work / "source" / "600990.SH.factors.parquet", index=False)
    daily.to_parquet(work / "source" / "600990.SH.daily.parquet", index=False)
    with pytest.raises(ChartError) as error:
        load(work)
    assert error.value.code == code


def test_unknown_calendar_not_suspension_and_empty_view(work):
    snapshot(work / "source", calendar=False)
    raw = load(work)
    view = data.build_daily_view(raw, request())
    assert view.metadata["gap_dates"] == []
    assert "CALENDAR_UNAVAILABLE" in view.metadata["warnings"]
    empty = data.build_daily_view(raw, request("2020-01-01T15:00:00+08:00"))
    assert empty.bars.empty and empty.metadata["factor_lineage"] is None
    assert "suspension" not in json.dumps(view.to_dict()).lower()


@pytest.mark.parametrize("window,dates", [("10y", ["2016-09-30", "2021-09-30", "2026-09-30"]), ("5y", ["2021-09-30", "2026-09-30"]), ("all", ["2015-09-30", "2016-09-30", "2021-09-30", "2026-09-30"])])
def test_inclusive_window(work, window, dates):
    all_dates = ["20150930", "20160930", "20210930", "20260930"]
    snapshot(work / "source", dates=all_dates, factors=[(d, 1.0) for d in all_dates], calendar=False)
    view = data.build_daily_view(load(work), request("2026-09-30T15:00:00+08:00", window=window))
    assert view.bars.trade_date.tolist() == dates


def test_february_leap_window(work):
    dates = ["20150227", "20150228", "20200229"]
    snapshot(work / "source", dates=dates, factors=[(d, 1.0) for d in dates], calendar=False)
    view = data.build_daily_view(load(work), request("2020-02-29T15:00:00+08:00", window="5y"))
    assert view.bars.trade_date.tolist() == ["2015-02-28", "2020-02-29"]


def test_sqlite_adapter_read_only_frozen_and_sidecars(work, monkeypatch):
    daily, factors = snapshot(work / "source")
    calendar = pd.read_parquet(work / "source" / "calendar.parquet")
    source_directory = work / "db-source"
    source_directory.mkdir()
    db = source_directory / "db_cn_basic.db"
    with sqlite3.connect(db) as connection:
        daily.drop(columns=["name"]).assign(pre_close=10.0, change=1.0, pct_chg=10.0).to_sql("tbl_cn_day", connection, index=False)
        factors.to_sql("tbl_cn_adj_factor", connection, index=False)
        calendar.assign(pretrade_date="20260921").to_sql("tbl_cn_tradecal", connection, index=False)
    before = db.read_bytes()
    monkeypatch.setenv("TRADING_AGENT_DATA_DIR", str(source_directory))
    monkeypatch.setenv("TRADING_AGENT_FROZEN", "1")
    raw = data.load_raw(request(), cache_dir=work / "sqlite-cache")
    assert len(raw.daily) == 3 and len(raw.factors) == 4
    assert db.read_bytes() == before
    for suffix in ("-wal", "-journal"):
        sidecar = Path(str(db) + suffix)
        sidecar.write_bytes(b"active")
        with pytest.raises(ChartError) as error:
            data.load_raw(request(), cache_dir=work / "sqlite-cache", refresh=True)
        assert error.value.code == "SOURCE_ACTIVE"
        sidecar.unlink()


@pytest.mark.parametrize("code", ["002025.SZ", "600990.SH", "002326.SZ"])
def test_real_c_snapshot_complete_history(work, code):
    raw = data.load_raw(request(code=code), cache_dir=work / "real-cache", snapshot_dir=INPUT)
    oracle = json.loads((INPUT / "independent_source_checks.json").read_text(encoding="utf-8"))["sources"][code]
    assert len(raw.daily) == oracle["daily_rows"] and len(raw.factors) == oracle["factor_rows"]
    assert len(set(raw.factors.trade_date) - set(raw.daily.trade_date)) == oracle["factor_only_dates"]
    for year, count in oracle["years"].items():
        assert raw.daily.trade_date.str.startswith(year).sum() == count
    source = pd.read_parquet(INPUT / f"{code}.daily.parquet")
    for column in ("open", "high", "low", "close"):
        assert raw.daily[column].tolist() == source[column].tolist()
    assert raw.daily.vol.tolist() == (source.vol * 100).tolist()
    assert raw.daily.amount.tolist() == (source.amount * 1000).tolist()
    factors = pd.read_parquet(INPUT / f"{code}.factors.parquet")
    assert raw.factors.adj_factor.tolist() == factors.adj_factor.tolist()


def test_real_default_ten_year_count_and_volume(work):
    req = request("2026-09-30T15:00:00+08:00", window="10y", stage="study")
    raw = data.load_raw(req, cache_dir=work / "cache", snapshot_dir=INPUT)
    view = data.build_daily_view(raw, req)
    assert len(view.bars) == 2421
    assert view.bars.vol.sum() == 10429386611
    assert view.bars.trade_date.iloc[0] >= "2016-09-30"
