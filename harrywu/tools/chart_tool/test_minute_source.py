"""SYNTHETIC logic checks and separately named real-source safety checks.

The historical disabled registry remains a boundary fixture. Production uses
mechanical row normalization and still validates every stock-day numerically.
"""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from . import minute_source as ms
from .contracts import ChartError, ChartRequest, DAILY_COLUMNS, FACTOR_COLUMNS, RawBundle
from .data import load_raw

V2 = Path("C:/Project Rocket/outputs/chart_tool_v2_20261009")
SNAP = Path("C:/Project Rocket/outputs/chart_tool_development_20261009/inputs")
DAYS = ["2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18"]
CHECK_KEYS = {"grid_ok", "auction_open_diff", "vol_rel_diff", "amount_rel_diff",
              "open_diff", "close_diff", "high_diff", "low_diff", "thresholds"}


def request(cutoff="2026-09-18T10:07:30+08:00", ts_code="002025.SZ", start=DAYS[0]):
    return ChartRequest(ts_code, cutoff, dict(kind="manual", confidence="high",
        evidence_refs=["SYNTHETIC:test:manual"], candidates=[], calibration_segment=None),
        minute_start=start, periods=("daily", "15m", "1m", "intraday"))


def synthetic_raw(*, ts_code="002025.SZ", auction=True):
    dates = pd.date_range("2026-09-07", "2026-09-20")
    calendar = pd.DataFrame(dict(trade_date=dates.strftime("%Y-%m-%d"),
                                 is_open=[d.weekday() < 5 for d in dates]))
    daily_dates = calendar.loc[calendar.is_open, "trade_date"].tolist()
    daily = pd.DataFrame([[d, 10., 12., 8., 11., 2500. if auction else 2400.,
                           25000. if auction else 24000.] for d in daily_dates], columns=DAILY_COLUMNS)
    factors = pd.DataFrame([[d, 1., None] for d in daily_dates], columns=FACTOR_COLUMNS)
    def series(identifier):
        return dict(source_id=identifier, source_family="SYNTHETIC_DAILY", source_version="SYNTHETIC:v1",
            price_basis="unadjusted", volume_unit="shares", amount_unit="CNY",
            original_volume_unit="shares", original_amount_unit="CNY", volume_scale=1.,
            amount_scale=1., unit_evidence_refs=["SYNTHETIC:test:units"],
            factor_source_id="SYNTHETIC:factor", source_by_trade_date={})
    metadata = dict(contract_version="chart.v1", source_version="SYNTHETIC:v1",
        series=dict(daily=series("SYNTHETIC:daily"), factors=series("SYNTHETIC:factor")),
        compatibility={}, calendar_source="SYNTHETIC:calendar", calendar_version="SYNTHETIC:v1",
        calendar_coverage=["2026-09-07", "2026-09-20"], warnings=[])
    raw = RawBundle(ts_code, daily, factors, metadata, calendar=calendar)
    raw.validate()
    return raw


def synthetic_registry(tmp_path, *, auction=True, kind="csv", layout="per_day_all_stocks",
                       ts_code="002025.SZ", scaled=False, days=DAYS):
    root = tmp_path / "SYNTHETIC_MINUTE_SOURCE"
    root.mkdir()
    template = {"per_day_all_stocks": "{yyyy}/{yyyymmdd}.EXT",
                "per_stock_day": "{market}/{code}/{yyyymmdd}.EXT",
                "per_stock_year": "{ts_code}/{yyyy}.EXT",
                "per_stock_all": "{ts_code}.EXT"}[layout].replace("EXT", kind)
    source = dict(source_id="SYNTHETIC:minute", family="SYNTHETIC_MINUTE",
        kind=kind, layout=layout, root=str(root), path_template=template,
        row_filter=None if layout == "per_stock_day" else "SYNTHETIC: select exact code/date",
        coverage=dict(first_date="2026-09-07", last_date="2026-09-20", gaps_note="SYNTHETIC"),
        encoding="utf-8" if kind == "csv" else None,
        columns=dict(datetime="timestamp", open="o", high="h", low="l", close="c",
                     vol="v", amount="a", code="code", name=None),
        timestamp_semantics="bar_end", has_auction_row=auction, auction_row_time="09:30",
        vol_unit="lots" if scaled else "shares",
        amount_unit="thousand_CNY" if scaled else "CNY", price_basis="unadjusted",
        evidence_refs=["SYNTHETIC:test:mechanical-241-merge-or-240-shift"])
    groups = {}
    for day in days:
        rows = []
        if auction:
            rows.append([day + " 09:30:00", 10., 10., 10., 10., 100., 1000., ts_code])
        # Independent literal endpoints, not the implementation's grid helper.
        starts = (" 09:31:00", " 13:01:00") if auction else (" 09:30:00", " 13:00:00")
        for start in (day + value for value in starts):
            for timestamp in pd.date_range(start, periods=120, freq="min"):
                rows.append([str(timestamp), 10., 12., 8., 11., 10., 100., ts_code])
        frame = pd.DataFrame(rows, columns=["timestamp", "o", "h", "l", "c", "v", "a", "code"])
        if scaled:
            frame["v"] /= 100
            frame["a"] /= 1000
        params = dict(ts_code=ts_code, code=ts_code.split(".")[0], market=ts_code.split(".")[1],
                      yyyy=day[:4], mm=day[5:7], dd=day[8:10], yyyymmdd=day.replace("-", ""))
        path = root / template.format(**params)
        groups.setdefault(path, []).append(frame)
    for path, frames in groups.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        frame = pd.concat(frames, ignore_index=True)
        # All-stock fixtures also include an unrelated stock whose values must be ignored.
        if layout == "per_day_all_stocks":
            distractor = frame.iloc[:2].copy()
            distractor["code"] = "000001.SZ"
            distractor[["o", "h", "l", "c"]] = 999.
            frame = pd.concat([distractor, frame], ignore_index=True)
        if kind == "csv":
            frame.to_csv(path, index=False)
        else:
            frame.to_parquet(path, index=False)
    registry = dict(registry_version="minute-src-v1", generated_at="2026-10-09T13:00:00+08:00",
        sources=[source], priority=[source["source_id"]],
        thresholds=dict(vol_rel=.005, amount_rel=.005, price_abs=.005, auction_open_abs=.005),
        threshold_evidence="SYNTHETIC:test:thresholds")
    path = tmp_path / "SYNTHETIC_registry.json"
    path.write_text(json.dumps(registry), encoding="utf-8")
    return path, registry


def load(raw, day, registry, tmp_path):
    return ms.load_minute_day(raw, day, registry_path=registry, cache_dir=tmp_path / "cache")


def test_synthetic_ma1_five_days_merge_grid_and_attachment_no_mutation(tmp_path):
    registry, _ = synthetic_registry(tmp_path)
    raw = synthetic_raw()
    before = deepcopy(raw)
    provider = ms.make_minute_provider(registry_path=registry, cache_dir=tmp_path / "cache")
    for day in DAYS:
        value = provider(raw, day)
        value.validate()
        assert len(value.one_min) == 240
        assert value.one_min.bar_end.iloc[0] == day + "T09:31:00+08:00"
        assert value.one_min.bar_end.iloc[-1] == day + "T15:00:00+08:00"
        assert value.auction is None
        assert value.one_min.vol.iloc[0] == 110.
        assert value.one_min.amount.iloc[0] == 1100.
        assert set(value.checks) == CHECK_KEYS
        assert value.checks["auction_open_diff"] is None
        assert all(value.checks[k] == 0 for k in CHECK_KEYS - {"grid_ok", "thresholds", "auction_open_diff"})
    attached = ms.attach_minutes(raw, request(), registry_path=registry, cache_dir=tmp_path / "cache")
    attached.validate(require_minutes=True)
    assert len(attached.one_min) == 1200
    assert attached.one_min.bar_end.is_monotonic_increasing
    assert attached.auctions == {}
    compatibility = attached.metadata["compatibility"]["one_min"]
    assert compatibility["checked_trade_dates"] == DAYS
    assert compatibility["status"] == "verified_overlap"
    assert compatibility["auction_policy"] == "verified_difference"
    assert compatibility["evidence_refs"][-1] == "auto_check:minute-vs-daily:v1"
    assert all(compatibility[k] is True for k in ["units_verified", "unadjusted_price_basis_verified", "factor_provenance_verified"])
    pd.testing.assert_frame_equal(raw.daily, before.daily)
    pd.testing.assert_frame_equal(raw.factors, before.factors)
    assert raw.metadata == before.metadata and raw.one_min is None and raw.auctions == {}


@pytest.mark.parametrize("kind,layout,ts_code,scaled", [
    ("csv", "per_stock_day", "600990.SH", True),
    ("csv", "per_stock_year", "002025.SZ", False),
    ("parquet", "per_stock_all", "600990.SH", True),
    ("parquet", "per_day_all_stocks", "002025.SZ", False)])
def test_synthetic_ma3_generic_layout_code_and_unit_conversion(tmp_path, kind, layout, ts_code, scaled):
    registry, _ = synthetic_registry(tmp_path, kind=kind, layout=layout, ts_code=ts_code, scaled=scaled)
    value = load(synthetic_raw(ts_code=ts_code), DAYS[-1], registry, tmp_path)
    assert value.ts_code == ts_code
    assert value.one_min.vol.tolist() == [110.] + [10.] * 239
    assert value.one_min.amount.tolist() == [1100.] + [100.] * 239
    assert value.auction is None


def test_synthetic_ma4_noauction_verified_difference(tmp_path):
    registry, _ = synthetic_registry(tmp_path, auction=False, kind="parquet")
    raw = synthetic_raw(auction=False)
    value = load(raw, DAYS[-1], registry, tmp_path)
    assert value.auction is None and value.checks["auction_open_diff"] is None
    assert value.one_min.bar_end.iloc[[0, 119, 120, 239]].tolist() == [
        DAYS[-1] + "T" + t + ":00+08:00" for t in ["09:31", "11:30", "13:01", "15:00"]]
    assert value.one_min.vol.tolist() == [10.] * 240
    attached = ms.attach_minutes(raw, request(), registry_path=registry, cache_dir=tmp_path / "cache")
    assert attached.auctions == {}
    assert attached.metadata["compatibility"]["one_min"]["auction_policy"] == "verified_difference"


@pytest.mark.parametrize("cutoff,sessions,expected", [
    ("2026-09-18T09:24:59+08:00", 5, "2026-09-11"),
    ("2026-09-18T09:25:00+08:00", 5, "2026-09-14"),
    ("2026-09-19T10:00:00+08:00", 5, "2026-09-14"),
    ("2026-09-18T09:24:59+08:00", 1, "2026-09-17"),
    ("2026-09-18T09:25:00+08:00", 1, "2026-09-18")])
def test_ma5_display_session_boundaries(tmp_path, cutoff, sessions, expected):
    assert ms.minute_start_for_sessions(synthetic_raw(), cutoff, sessions) == expected


def test_ma6_cache_hit_equal_and_recheck_daily(tmp_path, monkeypatch):
    registry, _ = synthetic_registry(tmp_path)
    raw = synthetic_raw()
    first = load(raw, DAYS[-1], registry, tmp_path)
    caches = list((tmp_path / "cache" / "minutes" / raw.ts_code).glob("*.parquet"))
    assert len(caches) == 1
    assert caches[0].name == DAYS[-1] + "." + first.source_sha256[:12] + ".parquet"
    def prohibited(*args):
        raise AssertionError("second read must use cache instead of source CSV parser")
    monkeypatch.setattr(ms, "_read_source_rows", prohibited)
    second = load(raw, DAYS[-1], registry, tmp_path)
    pd.testing.assert_frame_equal(first.one_min, second.one_min, check_exact=True)
    assert first.auction == second.auction and first.checks == second.checks
    assert first.source_sha256 == second.source_sha256
    changed = deepcopy(raw)
    changed.daily.loc[changed.daily.trade_date.eq(DAYS[-1]), "vol"] = 5000.
    with pytest.raises(ChartError) as exc:
        load(changed, DAYS[-1], registry, tmp_path)
    assert exc.value.code == "SOURCE_UNVERIFIED"
    assert exc.value.details["check"] == "vol_rel_diff"


@pytest.mark.parametrize("cutoff,count", [
    ("2026-09-18T09:24:59+08:00", 4),
    ("2026-09-18T09:25:00+08:00", 5),
    ("2026-09-18T09:30:00+08:00", 5),
    ("2026-09-19T10:00:00+08:00", 5)])
def test_attach_open_date_range_and_auction_availability_loading(tmp_path, cutoff, count):
    registry, _ = synthetic_registry(tmp_path)
    attached = ms.attach_minutes(synthetic_raw(), request(cutoff), registry_path=registry,
                                 cache_dir=tmp_path / "cache")
    assert len(attached.one_min) == count * 240
    assert attached.auctions == {}


def test_attach_uses_daily_dates_not_guessed_suspensions(tmp_path):
    registry, _ = synthetic_registry(tmp_path)
    raw = synthetic_raw()
    raw.daily = raw.daily.loc[raw.daily.trade_date.ne(DAYS[1])].reset_index(drop=True)
    attached = ms.attach_minutes(raw, request(), registry_path=registry, cache_dir=tmp_path / "cache")
    assert attached.metadata["compatibility"]["one_min"]["checked_trade_dates"] == [d for d in DAYS if d != DAYS[1]]


def test_empty_priority_does_not_read_sources_or_existing_cache(tmp_path, monkeypatch):
    registry_path, registry = synthetic_registry(tmp_path)
    raw = synthetic_raw()
    assert load(raw, DAYS[-1], registry_path, tmp_path) is not None
    registry["priority"] = []
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    def prohibited(*args):
        raise AssertionError("disabled source/cache must not be read")
    monkeypatch.setattr(ms, "_hash_file", prohibited)
    monkeypatch.setattr(ms, "_read_cache", prohibited)
    assert ms.load_registry(registry_path)["priority"] == []
    assert load(raw, DAYS[-1], registry_path, tmp_path) is None


def test_priority_fallback_only_on_absence_invalid_selected_source_fails(tmp_path):
    registry_path, registry = synthetic_registry(tmp_path)
    missing = deepcopy(registry["sources"][0])
    missing["source_id"] = "SYNTHETIC:missing"
    missing["root"] = str(tmp_path / "nonexistent")
    registry["sources"].insert(0, missing)
    registry["priority"].insert(0, missing["source_id"])
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    assert load(synthetic_raw(), DAYS[-1], registry_path, tmp_path).source_id == "SYNTHETIC:minute"
    raw = synthetic_raw()
    raw.daily.loc[raw.daily.trade_date.eq(DAYS[-1]), "vol"] = 5000
    with pytest.raises(ChartError) as exc:
        load(raw, DAYS[-1], registry_path, tmp_path)
    assert exc.value.code == "SOURCE_UNVERIFIED"


@pytest.mark.parametrize("mutation,check", [
    ("missing_grid", "grid_ok"),
    ("amount", "amount_rel_diff"), ("open", "open_diff"),
    ("close", "close_diff"), ("high", "high_diff"), ("low", "low_diff")])
def test_specific_source_failure_evidence(tmp_path, mutation, check):
    registry_path, registry = synthetic_registry(tmp_path)
    source_path = Path(registry["sources"][0]["root"]) / "2026/20260918.csv"
    frame = pd.read_csv(source_path)
    selected = frame.code.eq("002025.SZ")
    if mutation == "missing_grid":
        frame = frame.drop(frame.index[selected][-1])
    elif mutation == "open":
        frame.loc[selected & frame.timestamp.str.endswith("09:30:00"), ["o", "h", "l", "c"]] = 10.01
    elif mutation == "amount":
        frame.loc[selected, "a"] *= 2
    elif mutation == "close":
        frame.loc[frame.index[selected][-1], "c"] = 11.01
    elif mutation == "high":
        frame.loc[frame.index[selected][-1], "h"] = 12.01
    else:
        frame.loc[frame.index[selected][-1], "l"] = 7.99
    frame.to_csv(source_path, index=False)
    with pytest.raises(ChartError) as exc:
        load(synthetic_raw(), DAYS[-1], registry_path, tmp_path)
    assert exc.value.code == ("MISSING_MINUTE" if mutation == "missing_grid" else "SOURCE_UNVERIFIED")
    assert exc.value.details["check"] == check
    if check != "grid_ok":
        assert "actual" in exc.value.details and "threshold" in exc.value.details


@pytest.mark.parametrize("mutation", ["extra", "unknown_priority", "duplicate", "negative_threshold",
                                      "bad_layout", "bad_time", "bad_template", "missing_evidence"])
def test_registry_rejects_schema_drift(tmp_path, mutation):
    path, registry = synthetic_registry(tmp_path)
    if mutation == "extra":
        registry["extra"] = 1
    elif mutation == "unknown_priority":
        registry["priority"] = ["unknown"]
    elif mutation == "duplicate":
        registry["sources"].append(deepcopy(registry["sources"][0]))
    elif mutation == "negative_threshold":
        registry["thresholds"]["vol_rel"] = -.1
    elif mutation == "bad_layout":
        registry["sources"][0]["layout"] = "invented"
    elif mutation == "bad_time":
        registry["sources"][0]["timestamp_semantics"] = "bar_start"
    elif mutation == "bad_template":
        registry["sources"][0]["path_template"] = "{unknown}.csv"
    else:
        registry["sources"][0]["evidence_refs"] = []
    path.write_text(json.dumps(registry), encoding="utf-8")
    with pytest.raises(ChartError) as exc:
        ms.load_registry(path)
    assert exc.value.code == "INVALID_SCHEMA"


@pytest.mark.parametrize("cutoff,sessions", [("2026-09-21T09:25:00+08:00", 1),
                                            ("2026-09-07T09:24:59+08:00", 1),
                                            ("2026-09-18T09:25:00+08:00", 11)])
def test_calendar_bounds_fail_closed(cutoff, sessions):
    with pytest.raises(ChartError) as exc:
        ms.minute_start_for_sessions(synthetic_raw(), cutoff, sessions)
    assert exc.value.code == "CALENDAR_UNAVAILABLE"


@pytest.mark.parametrize("fixture,code,check", [
    ("registry_corrupt_1.json", "MISSING_MINUTE", "grid_ok"),
    ("registry_corrupt_2.json", "SOURCE_UNVERIFIED", "vol_rel_diff")])
def test_real_engineering_ma2_corrupt_fixtures(tmp_path, fixture, code, check):
    raw = load_raw(request(), cache_dir=tmp_path / "daily_cache", snapshot_dir=SNAP)
    registry = V2 / "inputs/minutes/corrupt" / fixture
    with pytest.raises(ChartError) as exc:
        load(raw, DAYS[-1], registry, tmp_path)
    assert exc.value.code == code
    assert exc.value.details["check"] == check


@pytest.mark.parametrize("ts_code,day,purpose", [
    ("002025.SZ", day, "REG-1") for day in DAYS] + [
    ("600990.SH", "2026-09-18", "SH-1"), ("002025.SZ", "2021-06-25", "OLD-1"),
    ("002025.SZ", "2026-01-08", "CASE-1")])
def test_real_acceptance_unavailable_disabled_registry(tmp_path, monkeypatch, ts_code, day, purpose):
    raw = load_raw(request(ts_code=ts_code), cache_dir=tmp_path / "daily_cache", snapshot_dir=SNAP)
    def prohibited(*args):
        raise AssertionError("real source is disabled; no source file may be read")
    monkeypatch.setattr(ms, "_hash_file", prohibited)
    path = V2 / "inputs/minutes/registry_fixture.json"
    assert ms.load_registry(path)["priority"] == []
    assert load(raw, day, path, tmp_path) is None


def test_nonconstant_0930_merges_all_fields_without_auction(tmp_path):
    path, registry = synthetic_registry(tmp_path)
    source = Path(registry["sources"][0]["root"]) / "2026/20260918.csv"
    frame = pd.read_csv(source)
    mask = frame.code.eq("002025.SZ")
    frame.loc[mask & frame.timestamp.str.endswith("09:30:00"), ["o", "h", "l", "c"]] = [10., 12., 8., 9.]
    frame.loc[mask & frame.timestamp.str.endswith("09:31:00"), ["o", "h", "l", "c"]] = [9., 11., 9., 11.]
    frame.to_csv(source, index=False)
    value = load(synthetic_raw(), DAYS[-1], path, tmp_path)
    assert value.auction is None
    assert value.one_min.iloc[0][["open", "high", "low", "close", "vol", "amount"]].tolist() == [10., 12., 8., 11., 110., 1100.]


@pytest.mark.parametrize("mutation", ["end_grid_240", "duplicate", "unordered"])
def test_row_count_alone_cannot_hide_wrong_raw_grid(tmp_path, mutation):
    path, registry = synthetic_registry(tmp_path)
    source = Path(registry["sources"][0]["root"]) / "2026/20260918.csv"
    frame = pd.read_csv(source)
    indices = frame.index[frame.code.eq("002025.SZ")]
    if mutation == "end_grid_240":
        frame = frame.drop(indices[0])
    elif mutation == "duplicate":
        frame.loc[indices[-1], "timestamp"] = frame.loc[indices[-2], "timestamp"]
    else:
        frame.loc[indices[[1, 2]], "timestamp"] = frame.loc[indices[[2, 1]], "timestamp"].tolist()
    frame.to_csv(source, index=False)
    with pytest.raises(ChartError) as exc:
        load(synthetic_raw(), DAYS[-1], path, tmp_path)
    assert exc.value.code == "MISSING_MINUTE" and exc.value.details["check"] == "grid_ok"


def test_previous_split_auction_cache_is_rebuilt(tmp_path):
    import pyarrow.parquet as pq
    path, _ = synthetic_registry(tmp_path)
    raw = synthetic_raw()
    load(raw, DAYS[-1], path, tmp_path)
    cache = next((tmp_path / "cache/minutes" / raw.ts_code).glob("*.parquet"))
    table = pq.read_table(cache)
    metadata = dict(table.schema.metadata)
    payload = json.loads(metadata[ms._CACHE_KEY])
    payload["identity"]["cache_version"] = 1
    payload["auction"] = dict(price=10., vol=100., amount=1000.)
    payload["auction_ohlc"] = [10.] * 4
    metadata[ms._CACHE_KEY] = json.dumps(payload).encode()
    frame = table.to_pandas()
    frame.loc[0, ["vol", "amount"]] = [10., 100.]
    import pyarrow as pa
    pq.write_table(pa.Table.from_pandas(frame, preserve_index=False).replace_schema_metadata(metadata), cache)
    value = load(raw, DAYS[-1], path, tmp_path)
    assert value.auction is None and value.one_min.vol.iloc[0] == 110.
    assert json.loads(pq.read_schema(cache).metadata[ms._CACHE_KEY])["identity"]["cache_version"] == 2


@pytest.mark.parametrize("auction", [True, False])
def test_mechanical_sources_warn_auction_unavailable(tmp_path, auction):
    from .multiperiod import build_multiperiod_views
    path, _ = synthetic_registry(tmp_path, auction=auction)
    raw = ms.attach_minutes(synthetic_raw(auction=auction), request(), registry_path=path, cache_dir=tmp_path / "cache")
    views = build_multiperiod_views(raw, request())
    for period in ["15m", "1m", "intraday"]:
        assert "AUCTION_UNAVAILABLE" in views[period].metadata["warnings"]
        assert views[period].metadata["auctions"] == {}


def test_production_registry_enables_only_validated_original_csv():
    registry = ms.load_registry(Path(__file__).with_name("minute_sources.json"))
    assert registry["priority"] == ["csv_1min_original"]
    assert registry["thresholds"] == dict(vol_rel=.005, amount_rel=.005, price_abs=.005, auction_open_abs=.005)
