"""Risk-focused checks against root's frozen literal oracle and C snapshots."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from .contracts import ChartError, ChartRequest, ChartView, INTRADAY_COLUMNS, MINUTE_COLUMNS, RawBundle
from .data import load_raw
from .multiperiod import attach_snapshot_minutes, build_multiperiod_views

INPUTS = Path("C:/Project Rocket/outputs/chart_tool_development_20261009/inputs")
SYNTH = INPUTS / "minute"
OUT = Path("C:/Project Rocket/outputs/chart_tool_development_20261009/E")


def synthetic():
    return RawBundle("000001.SZ", pd.read_parquet(SYNTH / "daily.parquet"),
                     pd.read_parquet(SYNTH / "factors.parquet"),
                     json.loads((SYNTH / "metadata.json").read_text()),
                     one_min=pd.read_parquet(SYNTH / "one_min.parquet"),
                     fifteen_min=pd.read_parquet(SYNTH / "fifteen_min.parquet"),
                     calendar=pd.read_parquet(SYNTH / "calendar.parquet"))


def request():
    data = json.loads((SYNTH / "expected.json").read_text())["request"]
    data["periods"] = tuple(data["periods"])
    return ChartRequest(**data)


def real_request():
    return ChartRequest("002025.SZ", "2026-09-18T10:07:30+08:00",
                        {"kind": "manual", "confidence": "high",
                         "evidence_refs": ["root scoped real minute request"],
                         "candidates": [], "calibration_segment": None},
                        stage="restore", periods=("daily", "15m", "1m", "intraday"))


def unchanged(a, b):
    for key in ("daily", "factors", "calendar", "one_min", "fifteen_min"):
        if getattr(a, key) is None:
            assert getattr(b, key) is None
        else:
            pd.testing.assert_frame_equal(getattr(a, key), getattr(b, key), check_exact=True)
    assert a.metadata == b.metadata and a.labels == b.labels and a.auctions == b.auctions


def test_root_independent_literal_oracle_and_no_mutation():
    raw = synthetic()
    before = deepcopy(raw)
    views = build_multiperiod_views(raw, request())
    unchanged(raw, before)
    assert set(views) == {"daily", "15m", "1m", "intraday"}
    assert len(views["1m"].bars) == 37
    assert len(views["15m"].bars) == 3
    row = views["15m"].bars.iloc[-1]
    assert list(row[["open", "high", "low", "close"]]) == pytest.approx([10.30, 10.41, 10.25, 10.38], rel=1e-12, abs=1e-10)
    assert row.vol == 931 and row.is_partial
    assert row.bar_start == "2026-09-24T10:00:00+08:00"
    assert row.bar_end == "2026-09-24T10:07:00+08:00"
    day = views["daily"].bars.iloc[-1]
    assert list(day[["open", "high", "low", "close"]]) == pytest.approx([10, 10.41, 9.95, 10.38], rel=1e-12, abs=1e-10)
    assert day.vol == 4366 and day.is_partial
    assert list(views["daily"].bars.iloc[0][["open", "high", "low", "close"]]) == [10, 12, 8, 11]
    pd.testing.assert_frame_equal(views["1m"].bars, views["intraday"].bars.loc[:, list(MINUTE_COLUMNS)])
    assert tuple(views["intraday"].bars.columns) == INTRADAY_COLUMNS
    for view in views.values():
        assert view.metadata["effective_cutoff"] == request().cutoff
        assert view.metadata["factor_lineage"]["anchor_trade_date"] == "2026-09-24"
        assert view.metadata["factor_lineage"]["anchor_value"] == 4
        assert view.metadata["labels_missing"] == ["name", "st_status", "themes", "boards"]
        pd.testing.assert_frame_equal(view.bars, ChartView.from_dict(view.to_dict()).bars, check_dtype=False)
        assert view.bars.bar_end.max() <= request().cutoff
        assert not view.bars.high.eq(999).any()
        assert "2026-09-25" not in json.dumps(view.to_dict())


@pytest.mark.parametrize("cutoff,count,bins,effective", [
    ("2026-09-24T12:20:00+08:00", 120, 8, "2026-09-24T11:30:00+08:00"),
    ("2026-09-24T16:00:00+08:00", 240, 16, "2026-09-24T15:00:00+08:00"),
    ("2026-09-24T09:31:30+08:00", 1, 1, "2026-09-24T09:31:30+08:00"),
    ("2026-09-24T13:00:00+08:00", 120, 8, "2026-09-24T13:00:00+08:00"),
    ("2026-09-24T10:00:00+08:00", 30, 2, "2026-09-24T10:00:00+08:00"),
])
def test_session_resolution(cutoff, count, bins, effective):
    views = build_multiperiod_views(synthetic(), replace(request(), cutoff=cutoff))
    assert len(views["1m"].bars) == count and len(views["15m"].bars) == bins
    assert {v.metadata["effective_cutoff"] for v in views.values()} == {effective}
    if count == 240:
        # Even after close it is synthesized from actual retained minutes.
        assert views["daily"].bars.iloc[-1].vol == synthetic().one_min.vol.sum()
        assert views["daily"].bars.iloc[-1].close != 666
        assert not views["daily"].bars.iloc[-1].is_partial


@pytest.mark.parametrize("cutoff,effective", [
    ("2026-09-24T09:00:00+08:00", "2026-09-23T15:00:00+08:00"),
    ("2026-09-26T10:00:00+08:00", "2026-09-25T15:00:00+08:00"),
])
def test_preopen_and_closed_date(cutoff, effective):
    views = build_multiperiod_views(synthetic(), replace(request(), cutoff=cutoff))
    assert {v.metadata["effective_cutoff"] for v in views.values()} == {effective}
    assert views["1m"].bars.empty and views["15m"].bars.empty
    assert not views["daily"].metadata["incomplete"]


def test_open_at_0930_has_no_completed_minute():
    views = build_multiperiod_views(synthetic(), replace(request(), cutoff="2026-09-24T09:30:00+08:00"))
    assert views["1m"].bars.empty
    assert views["daily"].bars.trade_date.tolist() == ["2026-09-23"]
    assert "DAILY_GAPS_UNEXPLAINED" not in views["daily"].metadata["warnings"]


def test_future_values_do_not_change_visible_bars_or_factor_digest():
    raw = synthetic()
    before = build_multiperiod_views(raw, request())
    raw.one_min.loc[raw.one_min.bar_end > request().cutoff, ["high", "vol", "amount"]] = [99999, 99999999, 999999999]
    raw.factors.loc[raw.factors.trade_date == "2026-09-25", "adj_factor"] = 8000
    raw.daily.loc[raw.daily.trade_date >= "2026-09-24", "high"] = 999999
    after = build_multiperiod_views(raw, request())
    for key in before:
        assert before[key].to_dict() == after[key].to_dict()


def test_native_fifteen_values_never_enter_output():
    raw = synthetic()
    before = build_multiperiod_views(raw, request())["15m"].to_dict()
    raw.fifteen_min.loc[:, "high"] = 1e10
    raw.fifteen_min.loc[:, "vol"] = 1e12
    assert before == build_multiperiod_views(raw, request())["15m"].to_dict()


@pytest.mark.parametrize("mutation,code", [
    ("compatibility", "SOURCE_UNVERIFIED"), ("lineage", "SOURCE_UNVERIFIED"),
    ("changed_factor", "FACTOR_UNAVAILABLE"), ("gap", "MISSING_MINUTE"),
    ("calendar", "CALENDAR_UNAVAILABLE"), ("lunch", "INVALID_INTERVAL"),
    ("none", "SOURCE_UNAVAILABLE"), ("unknownlabel", "INVALID_SCHEMA"),
])
def test_fail_closed(mutation, code):
    raw = synthetic()
    if mutation == "compatibility":
        raw.metadata["compatibility"]["one_min"]["status"] = "unverified"
    elif mutation == "lineage":
        raw.metadata["series"]["one_min"]["source_by_trade_date"]["2026-09-24"] = "unknown"
    elif mutation == "changed_factor":
        raw.factors.loc[raw.factors.trade_date == "2026-09-24", "available_at"] = None
    elif mutation == "gap":
        raw.one_min = raw.one_min.drop(index=3).reset_index(drop=True)
    elif mutation == "calendar":
        raw.calendar = None
    elif mutation == "lunch":
        raw.one_min.loc[119, "bar_start"] = "2026-09-24T11:30:00+08:00"
        raw.one_min.loc[119, "bar_end"] = "2026-09-24T11:31:00+08:00"
    elif mutation == "none":
        raw.one_min = None
    else:
        raw.labels["currentname"] = {}
    before = deepcopy(raw)
    with pytest.raises(ChartError) as error:
        build_multiperiod_views(raw, request())
    assert error.value.code == code
    unchanged(raw, before)


def test_calendar_range_not_extrapolated():
    with pytest.raises(ChartError, match="calendar evidence") as error:
        build_multiperiod_views(synthetic(), replace(request(), cutoff="2026-09-27T10:00:00+08:00"))
    assert error.value.code == "CALENDAR_UNAVAILABLE"


def test_label_availability_sliced_and_no_current_fallback():
    raw = synthetic()
    label = {"value": "Historical name", "effective_date": "2026-09-23",
             "available_at": "2026-09-24T10:08:00+08:00", "source_id": "test", "evidence_ref": "test"}
    raw.labels["name"] = label
    raw.labels["themes"] = {**label, "value": ["known"], "available_at": "2026-09-24T10:07:00+08:00"}
    views = build_multiperiod_views(raw, request())
    for view in views.values():
        assert set(view.metadata["labels"]) == {"themes"}
        assert "name" in view.metadata["labels_missing"]
        assert "Historical name" not in json.dumps(view.to_dict())


def test_include_partial_false_and_exact_requested_periods():
    views = build_multiperiod_views(synthetic(), replace(request(), periods=("daily", "15m"), include_partial=False))
    assert list(views) == ["daily", "15m"]
    assert len(views["15m"].bars) == 2
    assert len(views["daily"].bars) == 1
    assert not any(v.metadata["incomplete"] for v in views.values())


def test_minute_start_does_not_change_daily_window_or_anchor():
    raw = synthetic()
    views = build_multiperiod_views(raw, replace(request(), minute_start="2026-09-24", window="all"))
    assert len(views["1m"].bars) == 37
    assert len(views["daily"].bars) == 2
    with pytest.raises(ChartError) as error:
        build_multiperiod_views(raw, replace(request(), minute_start="2026-09-21"))
    assert error.value.code == "CALENDAR_UNAVAILABLE"


@pytest.fixture
def real_raw():
    return load_raw(real_request(), cache_dir=OUT / "test_cache", snapshot_dir=INPUTS)


def test_real_profile_units_factor_prior_anchor_no_mutation(real_raw):
    before = deepcopy(real_raw)
    raw = attach_snapshot_minutes(real_raw, real_request(), snapshot_dir=INPUTS,
                                  profile_path=INPUTS / "minute_profile_20260918.json")
    unchanged(real_raw, before)
    assert raw.fifteen_min is None
    assert "name" not in raw.one_min and "adj_factor" not in raw.one_min
    assert len(raw.one_min) == 240
    before_attached = deepcopy(raw)
    views = build_multiperiod_views(raw, real_request())
    unchanged(raw, before_attached)
    assert len(views["1m"].bars) == 37 and len(views["15m"].bars) == 3
    source = pd.read_parquet(INPUTS / "002025.SZ.20260918.1m.parquet").iloc[:37]
    assert views["daily"].bars.iloc[-1].vol == source.vol.sum()
    assert views["daily"].bars.iloc[-1].amount == source.amount.sum()
    last = views["15m"].bars.iloc[-1]
    assert last.vol == source.iloc[30:].vol.sum()
    assert last.open == source.iloc[30].open and last.close == source.iloc[-1].close
    for view in views.values():
        lineage = view.metadata["factor_lineage"]
        assert lineage["anchor_trade_date"] == "2026-09-17" and lineage["anchor_value"] == 6.7061
        assert lineage["selection"] == "last_closed_date" and lineage["anchor_available_at"] is None
        assert "CURRENT_FACTOR_VERIFIED_UNCHANGED_PRIOR_ANCHOR" in view.metadata["warnings"]
        text = json.dumps(view.to_dict())
        assert "profile_id" not in text and "native TDX" not in text and "minute_overlap" not in text
        assert "original_csv_rows" not in text and "checked_trade_dates" not in text


@pytest.mark.parametrize("change,code", [
    ("stock", "SOURCE_UNVERIFIED"), ("day", "SOURCE_UNVERIFIED"),
    ("path", "SOURCE_UNVERIFIED"), ("hash", "SOURCE_UNVERIFIED"),
    ("missing", "SOURCE_UNAVAILABLE"), ("units", "UNIT_UNVERIFIED"),
    ("source", "SOURCE_UNVERIFIED"), ("factor", "SOURCE_UNVERIFIED"),
])
def test_profile_rejects_out_of_scope_and_changed_evidence(real_raw, tmp_path, change, code):
    profile = json.loads((INPUTS / "minute_profile_20260918.json").read_text())
    req = real_request()
    if change == "stock":
        profile["ts_code"] = "000001.SZ"
    elif change == "day":
        req = replace(req, cutoff="2026-09-24T10:07:30+08:00")
    elif change == "path":
        profile["one_min_files"]["2026-09-18"]["file"] = "../not-allowed.parquet"
    elif change == "hash":
        profile["one_min_files"]["2026-09-18"]["sha256"] = "0" * 64
    elif change == "missing":
        profile["one_min_files"]["2026-09-18"]["file"] = "missing.parquet"
    elif change == "units":
        profile["volume_unit"] = "lots"
    elif change == "source":
        profile["source_id"] = "unknown"
    else:
        real_raw.factors.loc[real_raw.factors.trade_date == "2026-09-18", "adj_factor"] = 7.0
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(profile))
    before = deepcopy(real_raw)
    with pytest.raises(ChartError) as error:
        attach_snapshot_minutes(real_raw, req, snapshot_dir=INPUTS, profile_path=path)
    assert error.value.code == code
    unchanged(real_raw, before)


def test_new_path_equal_current_factor_automatically_proven():
    raw = synthetic()
    raw.factors.loc[raw.factors.trade_date == "2026-09-24", ["adj_factor", "available_at"]] = [2.0, None]
    views = build_multiperiod_views(raw, request())
    assert all("CURRENT_FACTOR_UNCHANGED_FROM_PREVIOUS" in view.metadata["warnings"] for view in views.values())


def test_factor_availability_after_cutoff_rejected():
    raw = synthetic()
    raw.factors.loc[raw.factors.trade_date == "2026-09-24", "available_at"] = "2026-09-24T10:08:00+08:00"
    with pytest.raises(ChartError) as error:
        build_multiperiod_views(raw, request())
    assert error.value.code == "FACTOR_UNAVAILABLE"


@pytest.mark.parametrize("payload", [None, [], {"one_min_files": None}, {"one_min_files": {"2026-09-18": None}}])
def test_malformed_profile_is_stable_error(real_raw, tmp_path, payload):
    path = tmp_path / "malformed.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ChartError) as error:
        attach_snapshot_minutes(real_raw, real_request(), snapshot_dir=INPUTS, profile_path=path)
    assert error.value.code == "SOURCE_UNVERIFIED"


# These M-b engineering tests deliberately use declared SYNTHETIC sources.
# They do not substitute for real-source per-day numerical acceptance.
def a3_bundle(tmp_path, *, auction=True, cutoff="2026-09-18T10:07:30+08:00", sessions=5,
              separate_auction=False):
    from .minute_source import attach_minutes, minute_start_for_sessions
    from .paths import writable_path
    from .test_minute_source import synthetic_raw, synthetic_registry, request as source_request
    raw = synthetic_raw(auction=auction)
    raw.metadata["warnings"].append("SYNTHETIC_ENGINEERING_ONLY")
    start = minute_start_for_sessions(raw, cutoff, sessions)
    req = replace(source_request(cutoff), minute_start=start)
    dates = raw.calendar.loc[raw.calendar.is_open & raw.calendar.trade_date.ge(start), "trade_date"].tolist()
    registry, _ = synthetic_registry(writable_path(tmp_path), auction=auction, days=dates)
    attached = attach_minutes(raw, req, registry_path=registry, cache_dir=writable_path(tmp_path / "cache"))
    if separate_auction:
        # Explicit contract fixture for sources that truly supply auction data.
        # Production row normalization never creates these separate auctions.
        assert auction
        for day, frame in attached.one_min.groupby("trade_date"):
            attached.one_min.loc[frame.index[0], ["vol", "amount"]] -= [100., 1000.]
            attached.auctions[day] = dict(price=10., vol=100., amount=1000., source_id="SYNTHETIC:separate-auction")
        attached.metadata["compatibility"]["one_min"]["auction_policy"] = "same"
    return attached, req


def test_synthetic_mb1_intraday_merged_first_bin_and_old_1m_equality(tmp_path):
    raw, req = a3_bundle(tmp_path)
    before = deepcopy(raw)
    views = build_multiperiod_views(raw, req)
    unchanged(raw, before)
    one = views["1m"].bars
    assert len(one) == 37 and one.bar_end.iloc[-1] == "2026-09-18T10:07:00+08:00"
    fifteen = views["15m"].bars.loc[views["15m"].bars.trade_date.eq("2026-09-18")]
    assert len(fifteen) == 3 and fifteen.is_partial.tolist() == [False, False, True]
    assert fifteen.iloc[-1].bar_start == "2026-09-18T10:00:00+08:00"
    assert fifteen.iloc[0].vol == 15 * 10 + 100
    assert fifteen.iloc[0].amount == 15 * 100 + 1000
    assert views["daily"].bars.iloc[-1].open == 10
    assert views["daily"].bars.iloc[-1].vol == 37 * 10 + 100
    assert views["intraday"].bars.avg_price.iloc[-1] == pytest.approx((1000 + 37 * 100) / (100 + 37 * 10), rel=1e-10)
    # The old algorithm is a numerical oracle for unchanged one-minute OHLCVA.
    from .multiperiod import _build_legacy_multiperiod_views
    legacy_raw = deepcopy(raw)
    legacy_raw.factors.loc[legacy_raw.factors.trade_date.eq("2026-09-18"), "available_at"] = "2026-09-18T09:25:00+08:00"
    old = _build_legacy_multiperiod_views(legacy_raw, replace(req, minute_start="2026-09-18"))
    pd.testing.assert_frame_equal(one, old["1m"].bars, check_exact=True)


def test_synthetic_mb2_five_sessions_volume_matches_daily(tmp_path):
    raw, req = a3_bundle(tmp_path)
    views = build_multiperiod_views(raw, req)
    assert sorted(set(views["15m"].bars.trade_date)) == [f"2026-09-{day}" for day in range(14, 19)]
    assert set(views["1m"].bars.trade_date) == {"2026-09-18"}
    assert set(views["intraday"].bars.trade_date) == {"2026-09-18"}
    for day, frame in views["15m"].bars.groupby("trade_date"):
        if day < "2026-09-18":
            assert len(frame) == 16 and not frame.is_partial.any()
            assert frame.vol.sum() == raw.daily.loc[raw.daily.trade_date.eq(day), "vol"].iloc[0]
    assert "OPENING_AUCTION_0930_EXCLUDED" not in views["15m"].metadata["warnings"]


@pytest.mark.parametrize("clock,count,display,auction_visible", [
    ("09:24:59", 240, "2026-09-17", False),
    ("09:25:00", 0, "2026-09-18", True),
    ("09:30:00", 0, "2026-09-18", True),
    ("09:31:00", 1, "2026-09-18", True)])
@pytest.mark.parametrize("separate_auction", [False, True])
def test_synthetic_mb3_auction_visibility_and_display_boundary(tmp_path, clock, count, display, auction_visible, separate_auction):
    raw, req = a3_bundle(tmp_path, cutoff=f"2026-09-18T{clock}+08:00", separate_auction=separate_auction)
    views = build_multiperiod_views(raw, req)
    assert len(views["1m"].bars) == count
    if count:
        assert set(views["1m"].bars.trade_date) == {display}
    for view in views.values():
        assert ("2026-09-18" in view.metadata["auctions"]) == (auction_visible and separate_auction)
        if not separate_auction:
            assert view.metadata["auctions"] == {}
            assert "AUCTION_UNAVAILABLE" in view.metadata["warnings"]
        assert ("AUCTION_NOT_YET_REVEALED" in view.metadata["warnings"]) == (not auction_visible)
        assert ("DISPLAY_SESSION_PREVIOUS" in view.metadata["warnings"]) == (display != "2026-09-18")
    if clock == "09:31:00":
        assert views["daily"].bars.iloc[-1].vol == 110
        last = views["15m"].bars.iloc[-1]
        assert last.vol == 110 and last.amount == 1100 and last.is_partial
    else:
        assert "2026-09-18" not in set(views["daily"].bars.trade_date)


def test_synthetic_mb4_saturday_previous_friday_full_session(tmp_path):
    raw, req = a3_bundle(tmp_path, cutoff="2026-09-19T10:00:00+08:00")
    views = build_multiperiod_views(raw, req)
    assert len(views["1m"].bars) == 240
    assert set(views["1m"].bars.trade_date) == {"2026-09-18"}
    assert len(views["15m"].bars) == 80
    assert all(not view.metadata["incomplete"] for view in views.values())
    assert "DISPLAY_SESSION_PREVIOUS" in views["intraday"].metadata["warnings"]


def test_synthetic_mb5_exdate_intraday_fails_close_available(tmp_path):
    raw, req = a3_bundle(tmp_path, separate_auction=True)
    raw.factors.loc[raw.factors.trade_date.ge("2026-09-18"), "adj_factor"] = 2.
    with pytest.raises(ChartError) as error:
        build_multiperiod_views(raw, req)
    assert error.value.code == "FACTOR_UNAVAILABLE"
    views = build_multiperiod_views(raw, replace(req, cutoff="2026-09-18T15:00:00+08:00"))
    assert all(view.metadata["factor_lineage"]["anchor_value"] == 2 for view in views.values())
    assert views["15m"].bars.iloc[0].open == 5
    assert views["15m"].metadata["auctions"]["2026-09-14"]["price"] == 5
    assert views["15m"].metadata["auctions"]["2026-09-18"]["price"] == 10


def test_synthetic_mb6_prev_close_adjusted_and_missing_warned(tmp_path):
    raw, req = a3_bundle(tmp_path)
    raw.factors.loc[raw.factors.trade_date.ge("2026-09-17"), "adj_factor"] = 2.
    views = build_multiperiod_views(raw, req)
    prev = views["intraday"].metadata["prev_close"]
    assert prev["2026-09-18"] == 11 and prev["2026-09-17"] == 5.5
    assert set(prev) == {f"2026-09-{day}" for day in range(14, 19)}
    raw.daily = raw.daily.loc[raw.daily.trade_date.ge("2026-09-14")].reset_index(drop=True)
    views = build_multiperiod_views(raw, req)
    assert "2026-09-14" not in views["intraday"].metadata["prev_close"]
    assert "PREV_CLOSE_UNAVAILABLE" in views["intraday"].metadata["warnings"]


def test_synthetic_mb7_schema_roundtrip_shared_cutoffs_anchor_and_no_future(tmp_path):
    raw, req = a3_bundle(tmp_path)
    views = build_multiperiod_views(raw, req)
    for view in views.values():
        view.validate()
        assert view.to_dict() == ChartView.from_dict(view.to_dict()).to_dict()
        assert view.metadata["requested_cutoff"] == req.cutoff
        assert view.metadata["effective_cutoff"] == req.cutoff
        assert view.metadata["factor_lineage"] == views["daily"].metadata["factor_lineage"]
        assert "SYNTHETIC_ENGINEERING_ONLY" in view.metadata["warnings"]
        assert "CURRENT_FACTOR_UNCHANGED_FROM_PREVIOUS" in view.metadata["warnings"]
        assert all(day <= "2026-09-18" for day in view.metadata["auctions"])
    raw.one_min.loc[raw.one_min.bar_end.gt(req.cutoff), ["high", "vol", "amount"]] = [999, 9999, 99999]
    raw.daily.loc[raw.daily.trade_date.ge("2026-09-18"), ["high", "vol", "amount"]] = [999, 9999, 99999]
    assert raw.auctions == {}
    after = build_multiperiod_views(raw, req)
    assert {key: value.to_dict() for key, value in views.items()} == {key: value.to_dict() for key, value in after.items()}


def test_synthetic_auction_extremes_and_zero_cumulative_volume(tmp_path):
    raw, req = a3_bundle(tmp_path, separate_auction=True)
    raw.auctions["2026-09-18"].update(price=15., vol=0., amount=0.)
    visible = raw.one_min.trade_date.eq("2026-09-18") & raw.one_min.bar_end.le("2026-09-18T09:32:00+08:00")
    raw.one_min.loc[visible, ["vol", "amount"]] = 0.
    views = build_multiperiod_views(raw, req)
    assert views["15m"].bars.loc[views["15m"].bars.trade_date.eq("2026-09-18")].iloc[0].high == 15
    assert views["daily"].bars.iloc[-1].open == 15
    assert views["intraday"].bars.avg_price.iloc[:2].tolist() == [11., 11.]
    assert views["intraday"].bars.avg_price.iloc[2] == 10.
    raw.auctions["2026-09-18"]["price"] = 5.
    views = build_multiperiod_views(raw, req)
    assert views["daily"].bars.iloc[-1].low == 5


def test_synthetic_noauction_new_path_warns_and_has_avg(tmp_path):
    raw, req = a3_bundle(tmp_path, auction=False)
    views = build_multiperiod_views(raw, req)
    assert views["intraday"].bars.avg_price.tolist() == [10.] * 37
    for view in views.values():
        assert view.metadata["auctions"] == {}
        assert "AUCTION_UNAVAILABLE" in view.metadata["warnings"]
        assert "OPENING_AUCTION_0930_EXCLUDED" not in view.metadata["warnings"]


def test_synthetic_avg_auction_seed_changes_mean_and_future_auction_hidden(tmp_path):
    raw, req = a3_bundle(tmp_path, separate_auction=True)
    raw.auctions["2026-09-18"].update(vol=200., amount=1800.)
    views = build_multiperiod_views(raw, req)
    expected = (1800 + 37 * 100) / (200 + 37 * 10)
    assert views["intraday"].bars.avg_price.iloc[-1] == pytest.approx(expected, rel=1e-10)
    assert views["intraday"].bars.avg_price.iloc[0] == pytest.approx(1900 / 210)
    assert views["daily"].bars.iloc[-1].amount == 5500
    raw.auctions["2026-09-18"].update(price=999., vol=999999., amount=999999.)
    early = replace(req, cutoff="2026-09-18T09:24:59+08:00")
    before = build_multiperiod_views(raw, early)
    raw.auctions["2026-09-18"].update(price=1., vol=0., amount=0.)
    after = build_multiperiod_views(raw, early)
    assert {p: v.to_dict() for p, v in before.items()} == {p: v.to_dict() for p, v in after.items()}


@pytest.mark.parametrize("cutoff", ["2026-09-18T09:24:59+08:00", "2026-09-19T10:00:00+08:00"])
def test_synthetic_default_minute_start_remains_civil_cutoff_date(tmp_path, cutoff):
    raw, req = a3_bundle(tmp_path, cutoff=cutoff)
    from .minute_source import attach_minutes
    empty = attach_minutes(raw, replace(req, minute_start=None),
                           registry_path=tmp_path / "SYNTHETIC_registry.json", cache_dir=tmp_path / "empty_cache")
    views = build_multiperiod_views(empty, replace(req, minute_start=None))
    assert views["1m"].bars.empty and views["15m"].bars.empty
    assert tuple(views["intraday"].bars.columns) == INTRADAY_COLUMNS


def test_real_mb8_legacy_profile_keeps_original_columns_and_0930_policy(real_raw):
    req = real_request()
    raw = attach_snapshot_minutes(real_raw, req, snapshot_dir=INPUTS,
                                  profile_path=INPUTS / "minute_profile_20260918.json")
    views = build_multiperiod_views(raw, req)
    assert tuple(views["intraday"].bars.columns) == MINUTE_COLUMNS
    for view in views.values():
        assert "OPENING_AUCTION_0930_EXCLUDED" in view.metadata["warnings"]
        assert "CURRENT_FACTOR_VERIFIED_UNCHANGED_PRIOR_ANCHOR" in view.metadata["warnings"]
