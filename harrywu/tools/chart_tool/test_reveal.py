"""R1--R12; source prices are read from snapshots, synthetic prices are labelled."""
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import shutil

import pandas as pd
import pytest

from chart_tool import cases, reveal
from chart_tool.contracts import DAILY_COLUMNS, MINUTE_COLUMNS, MinuteDay, ChartError, ChartView
from chart_tool.data import load_raw, build_daily_view
from chart_tool.test_data import request, snapshot
from chart_tool.test_cases import make_record, freeze

SNAP = Path("C:/Project Rocket/outputs/chart_tool_development_20261009/inputs")
V2 = Path("C:/Project Rocket/outputs/chart_tool_v2_20261009")
ACTUAL = Path("C:/Project Rocket/outputs/chart_replay_upload_20261009/delivery/01_复核/actual_intraday_reveal_boundary.json")
CASE = "yuncong_20260109_002025_000632"
OBSERVED = "2026-09-30T15:00:00+08:00"


@pytest.fixture
def raw(tmp_path):
    source = tmp_path / "source"
    snapshot(source)
    bundle = load_raw(request(), cache_dir=tmp_path / "cache", snapshot_dir=source)
    dates = ["2026-09-17", "2026-09-18", "2026-09-21", "2026-09-22", "2026-09-23"]
    # SYNTHETIC_ENGINEERING_ONLY fixed prices, independent of real stocks.
    bundle.daily = pd.DataFrame([{ "trade_date": day, "open": 10., "high": 12., "low": 8., "close": 11., "vol": 100., "amount": 1100.} for day in dates], columns=DAILY_COLUMNS)
    bundle.factors = pd.DataFrame({"trade_date": dates, "adj_factor": [1.] * len(dates), "available_at": [None] * len(dates)})
    bundle.calendar = None
    return bundle


def minimal(raw, clock="09:22:33", day="2026-09-18"):
    return {"request": {"ts_code": raw.ts_code, "cutoff": f"{day}T{clock}+08:00"},
            "view_identities": [{"metadata": {"factor_lineage": {"anchor_trade_date": "2026-09-17", "anchor_value": 1.}}}]}


def minute_day(raw, day="2026-09-18"):
    ends = list(pd.date_range(day + " 09:31", day + " 11:30", freq="min")) + list(pd.date_range(day + " 13:01", day + " 15:00", freq="min"))
    frame = pd.DataFrame([{ "trade_date": day, "bar_start": (end.to_pydatetime() - timedelta(minutes=1)).isoformat() + "+08:00",
                          "bar_end": end.isoformat() + "+08:00", "open": 10., "high": 11., "low": 9., "close": 10.,
                          "vol": 1., "amount": 10., "is_partial": False} for end in ends], columns=MINUTE_COLUMNS)
    item = MinuteDay(raw.ts_code, day, frame, {"price": 10., "vol": 0., "amount": 0.}, "synthetic-minute", "synthetic", "0" * 64, {})
    item.validate()
    return item


def test_r1_actual_post_mention_boundary(tmp_path):
    raw = load_raw(request(OBSERVED, code="002025.SZ"), cache_dir=tmp_path / "cache", snapshot_dir=SNAP)
    profile = json.loads((SNAP / "minute_profile_20260918.json").read_text())
    file = SNAP / profile["one_min_files"]["2026-09-18"]["file"]
    source = pd.read_parquet(file)
    # The profile snapshot already omitted 09:30; the original C fixture retains it.
    original = pd.read_parquet(SNAP / "002025.SZ.20260918.original_csv_rows.parquet")
    auction_row = original.loc[original.datetime.dt.strftime("%H:%M") == "09:30"].iloc[0]
    bars = source[["open", "high", "low", "close", "vol", "amount"]].copy()
    bars["trade_date"] = "2026-09-18"
    bars["bar_end"] = source.datetime.map(lambda x: x.isoformat() + "+08:00")
    bars["bar_start"] = source.datetime.map(lambda x: (x.to_pydatetime() - timedelta(minutes=1)).isoformat() + "+08:00")
    bars["is_partial"] = False
    minute = MinuteDay(raw.ts_code, "2026-09-18", bars[list(MINUTE_COLUMNS)],
                       {"price": float(auction_row.open), "vol": float(auction_row.vol), "amount": float(auction_row.amount)},
                       profile["source_id"], profile["source_family"], hashlib.sha256(file.read_bytes()).hexdigest(), {})
    minute.validate()
    value = float(raw.factors.set_index("trade_date").loc["2026-09-17", "adj_factor"])
    record = minimal(raw, "10:07:30")
    record["view_identities"][0]["metadata"]["factor_lineage"]["anchor_value"] = value
    mention = reveal.compute_mention_day(raw, record, lambda *_: minute)
    evidence = json.loads(ACTUAL.read_text(encoding="utf-8"))
    assert mention["policy"] == "split_by_minutes"
    assert mention["post_mention_low"] == evidence["after"]["low"]
    assert mention["post_mention_low"] != evidence["daily_raw_lots_thousand_cny"][0]["low"]
    expected = float(source.loc[source.datetime.dt.strftime("%H:%M") == "10:07", "close"].iloc[0])
    assert mention["reference"]["price"] == expected
    assert len(mention["_post_minutes"]) == evidence["after"]["rows"]


def two_cases(tmp_path):
    store = tmp_path / "store"; store.mkdir()
    a = make_record(store, "case_a"); freeze(store, a)
    b = make_record(store, "case_b"); freeze(store, b)
    source = tmp_path / "source"
    value = a["view_identities"][0]["metadata"]["factor_lineage"]["anchor_value"]
    snapshot(source, dates=["20260930", "20261001", "20261002"], factors=[("20260930", value), ("20261001", value), ("20261002", value)])
    return store, source, a, b


def test_r2_one_source_failure_isolated(tmp_path, monkeypatch):
    store, source, a, b = two_cases(tmp_path)
    original = reveal.load_raw
    calls = []
    def faulty(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise ChartError("SOURCE_UNVERIFIED", "synthetic failure")
        return original(*args, **kwargs)
    monkeypatch.setattr(reveal, "load_raw", faulty)
    report, code = reveal.run_reveal(store, observed_through="2026-10-02T15:00:00+08:00", cache_dir=tmp_path / "cache", snapshot_dir=source)
    assert code == 3 and report["status"] == "partial"
    assert [x["status"] for x in report["results"]] == ["failed", "revealed"]
    assert report["results"][0]["error"]["code"] == "SOURCE_UNVERIFIED"
    assert [x["case_id"] for x in report["pending_after"]] == [a["case_id"]]


def test_r3_same_day_roles_are_preserved(raw):
    record = minimal(raw)
    anchor = reveal.restore_anchor(record, raw)
    mention = reveal.compute_mention_day(raw, record, None)
    rows = reveal._observation(raw, anchor, mention, "2026-09-18T15:00:00+08:00")
    events = reveal.compute_events(rows, anchor, mention, ["synthetic.json"])
    assert [x["kind"] for x in events] == ["first_available", "interval_high", "interval_low", "latest_available"]
    assert len({x["at"] for x in events}) == 1


def test_r4_ex_rights_keeps_adjusted_path_continuous(raw):
    record = minimal(raw)
    raw.factors.loc[raw.factors.trade_date >= "2026-09-22", "adj_factor"] = 2.
    raw.daily.loc[raw.daily.trade_date >= "2026-09-22", ["open", "high", "low", "close"]] /= 2
    anchor = reveal.restore_anchor(record, raw)
    mention = reveal.compute_mention_day(raw, record, None)
    path = reveal.compute_path_snapshot(raw, record, anchor, mention, OBSERVED)
    assert path["horizons"][1]["close_return"] == pytest.approx(11 / 10 - 1)
    events = reveal.compute_events(reveal._observation(raw, anchor, mention, OBSERVED), anchor, mention, ["synthetic"])
    assert next(x for x in events if x["kind"] == "interval_low")["at"].startswith("2026-09-18")
    view = reveal.build_reveal_daily_view(raw, record, observed_through=OBSERVED, window="all")
    assert view.bars.close.tolist() == [11.] * 5
    assert view.metadata["factor_lineage"]["selection"] == "restore_anchor"
    assert view.metadata["effective_cutoff"] == "2026-09-23T15:00:00+08:00"
    assert view.metadata["auctions"] == view.metadata["prev_close"] == {}


def test_r5_preopen_includes_current_day(raw):
    mention = reveal.compute_mention_day(raw, minimal(raw), None)
    assert mention["policy"] == "preopen_full_day"
    assert mention["reference"]["price"] == 10.
    path = reveal.compute_path_snapshot(raw, minimal(raw), reveal.restore_anchor(minimal(raw), raw), mention, OBSERVED)
    assert path["horizons"][0]["trade_date"] == "2026-09-18"


def test_r6_auction_window_uses_minutes(raw):
    minute = minute_day(raw)
    mention = reveal.compute_mention_day(raw, minimal(raw, "09:27:00"), lambda *_: minute)
    assert mention["policy"] == "auction_window"
    assert mention["reference"]["kind"] == "auction_price" and mention["reference"]["price"] == 10.
    assert mention["_first_high"] == 11. and mention["_first_low"] == 9.


def test_r7_lunch_uses_1130_close_and_afternoon(raw):
    minute = minute_day(raw)
    minute.one_min.loc[119, "close"] = 10.5
    mention = reveal.compute_mention_day(raw, minimal(raw, "12:10:00"), lambda *_: minute)
    assert mention["policy"] == "lunch_split"
    assert mention["reference"]["kind"] == "lunch_close" and mention["reference"]["price"] == 10.5
    assert mention["reference"]["at"] == "2026-09-18T11:30:00+08:00"
    assert mention["_post_minutes"].bar_end.iloc[0] == "2026-09-18T13:01:00+08:00"


def test_r8_postclose_starts_next_session(raw):
    mention = reveal.compute_mention_day(raw, minimal(raw, "15:30:00"), None)
    assert mention["policy"] == "postclose_excluded"
    assert mention["reference"]["trade_date"] == "2026-09-21"


def test_r9_no_minutes_falls_back(raw):
    record = minimal(raw, "10:07:30")
    mention = reveal.compute_mention_day(raw, record, lambda *_: None)
    path = reveal.compute_path_snapshot(raw, record, reveal.restore_anchor(record, raw), mention, OBSERVED)
    assert mention["policy"] == "excluded_no_minutes"
    assert mention["reference"]["trade_date"] == "2026-09-21"
    assert "REF_FALLBACK_NEXT_SESSION_OPEN" in path["notes"]


def test_r10_short_coverage_has_null_horizons(tmp_path, raw, monkeypatch):
    store, source, a, b = two_cases(tmp_path)
    report, code = reveal.run_reveal(store, case_id=a["case_id"], observed_through="2026-10-02T15:00:00+08:00", cache_dir=tmp_path / "cache", snapshot_dir=source)
    result = cases.load_results(store, a["case_id"], 1)[0]
    assert code == 0 and result["complete"] is False and result["coverage_warning"]
    assert report["results"][0]["rows"] == 2
    for horizon in result["path_snapshot"]["horizons"][1:]:
        assert horizon["complete"] is False
        assert all(v is None for k, v in horizon.items() if k not in {"k", "complete"})


def test_r11_old_case_append_restores_frozen_anchor(tmp_path):
    store = tmp_path / "store"
    # W7 appends new observations to V2; copy its immutable W0 rev1 baseline.
    inventory = json.loads((V2 / "W0" / "case_store_copy.json").read_text(encoding="utf-8"))
    for item in inventory:
        source = V2 / "case_store" / item["path"]
        assert hashlib.sha256(source.read_bytes()).hexdigest() == item["copy_sha256"]
        target = store / item["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        assert hashlib.sha256(target.read_bytes()).hexdigest() == item["copy_sha256"]
    first_path = store / "results" / CASE / "revision-1" / "result-1.json"
    before = first_path.read_bytes()
    record = cases.load_restore_input(store, CASE, 1)
    anchor = record["view_identities"][0]["metadata"]["factor_lineage"]
    report, code = reveal.run_reveal(store, case_id=CASE, revision=1, observed_through=OBSERVED, snapshot_dir=SNAP, cache_dir=tmp_path / "cache")
    assert code == 0 and report["results"][0]["result_path"].endswith("result-2.json")
    result = cases.load_results(store, CASE, 1)[-1]
    assert result["price_anchor"] == {"anchor_trade_date": "2026-01-08", "anchor_value": anchor["anchor_value"], "source": "frozen_restore_view"}
    daily = pd.read_parquet(SNAP / "002025.SZ.daily.parquet")
    factors = pd.read_parquet(SNAP / "002025.SZ.factors.parquet")
    day = daily.trade_date.astype(str).str.replace("-", "")
    factor_day = factors.trade_date.astype(str).str.replace("-", "")
    expected = float(daily.loc[day == "20260109", "open"].iloc[0]) * float(factors.loc[factor_day == "20260109", "adj_factor"].iloc[0]) / anchor["anchor_value"]
    assert result["path_snapshot"]["reference"]["price"] == expected
    assert first_path.read_bytes() == before


def test_r12_frozen_anchor_mismatch_isolated(tmp_path, monkeypatch):
    store, source, a, b = two_cases(tmp_path)
    original = cases._load_frozen
    def tampered(root, case_id, revision):
        wrapper, cutoff = original(root, case_id, revision)
        if case_id == a["case_id"]:
            wrapper = deepcopy(wrapper)
            wrapper["record"]["view_identities"][0]["metadata"]["factor_lineage"]["anchor_value"] += 1
        return wrapper, cutoff
    monkeypatch.setattr(cases, "_load_frozen", tampered)
    report, code = reveal.run_reveal(store, observed_through="2026-10-02T15:00:00+08:00", cache_dir=tmp_path / "cache", snapshot_dir=source)
    assert code == 3
    assert report["results"][0]["error"]["code"] == "ANCHOR_MISMATCH"
    assert report["results"][1]["status"] == "revealed"


@pytest.mark.parametrize("clock,kind", [("09:30:00", "auction_price"), ("09:30:59", "auction_price"), ("09:31:00", "last_minute_close")])
def test_minute_reference_boundaries(raw, clock, kind):
    mention = reveal.compute_mention_day(raw, minimal(raw, clock), lambda *_: minute_day(raw))
    assert mention["reference"]["kind"] == kind


def test_invalid_start_returns_exit_two(tmp_path):
    report, code = reveal.run_reveal(tmp_path, observed_through="invalid", cache_dir=tmp_path / "cache")
    assert code == 2 and report["status"] == "failed"


def test_empty_store_returns_nothing_pending(tmp_path):
    report, code = reveal.run_reveal(tmp_path, observed_through=OBSERVED, cache_dir=tmp_path / "cache")
    assert code == 0 and report == {"status": "nothing_pending", "results": [], "pending_after": []}


def test_malformed_frozen_case_does_not_stop_other_cases(tmp_path):
    store, source, a, b = two_cases(tmp_path)
    (store / "restore" / a["case_id"] / "revision-1" / "frozen.json").write_text("{}", encoding="utf-8")
    report, code = reveal.run_reveal(store, observed_through="2026-10-02T15:00:00+08:00", cache_dir=tmp_path / "cache", snapshot_dir=source)
    assert code == 3 and [x["status"] for x in report["results"]] == ["failed", "revealed"]
    assert report["results"][0]["error"]["code"] == "INVALID_SCHEMA"


def test_conflicting_artifact_directory_is_preserved(tmp_path):
    store, source, a, b = two_cases(tmp_path)
    artifact = store / "result_artifacts" / a["case_id"] / "revision-1" / "observation-1"
    artifact.mkdir(parents=True)
    report, code = reveal.run_reveal(store, case_id=a["case_id"], observed_through="2026-10-02T15:00:00+08:00", cache_dir=tmp_path / "cache", snapshot_dir=source)
    assert code == 3 and report["results"][0]["error"]["code"] == "CASE_CONFLICT"
    assert artifact.exists() and not list(artifact.iterdir())


def test_registry_provider_receives_expected_adapter_arguments(tmp_path, monkeypatch):
    from chart_tool import minute_source
    store, source, a, b = two_cases(tmp_path)
    called = []
    def factory(*, registry_path, cache_dir):
        called.append((registry_path, cache_dir))
        return lambda *_: None
    monkeypatch.setattr(minute_source, "make_minute_provider", factory)
    report, code = reveal.run_reveal(store, case_id=a["case_id"], observed_through="2026-10-02T15:00:00+08:00", cache_dir=tmp_path / "cache", snapshot_dir=source, minute_registry="synthetic.json")
    assert code == 0 and called == [("synthetic.json", tmp_path / "cache")]
