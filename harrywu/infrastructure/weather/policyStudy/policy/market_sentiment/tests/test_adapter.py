"""Boundary tests for the frozen F1 -> MKT raw adapter."""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from policyStudy.policy.market_sentiment.adapter import FIELD_MAPPING, build_raw, load_formal


def event(code, day="20250102", **changes):
    row = dict(ts_code=code, trade_date=day, eligible=True, U=False, height=0, cC=1000, cP=1000)
    row.update(changes)
    return row


def member(code, height=1, day="20250102", **changes):
    row = dict(ts_code=code, date=day, prev_height=height, eligible=True, found=True,
               paused=False, missing=False, mid_obs=True, return_obs=True,
               cC=1000, cP=1000, Dn=False, H2L=False)
    row.update(changes)
    return row


def formal(day="2025-01-02", n=1, original=0, market=1, **changes):
    row = dict(trade_date=day, N=n, U=0, D=0, Z=0, amount=1000000.0, amt_ma20=900000.0,
               ratio20=10 / 9, lr20=np.log(10 / 9), Udens=0.0, Ddens=0.0, M1raw=0.4,
               ladder=0.0, max_h=0, score5=42.12345678912345, grade5="neutral", M1=50.0,
               quality_status="OK", quality_reasons="", cohort_original_n=original,
               market_observed_n=market)
    for height in range(1, 5):
        row.update({f"k_h{height}": 0, f"n_h{height}": 0})
    row.update(changes)
    return row


def test_exact_five_percent_union_high_chain_and_current_ineligible():
    # The first price is exactly -5%, though a separately recorded float return
    # lies just above -5%. The second has both Dn and H2L: it is counted once.
    c = pd.DataFrame([
        member("exact", 1, cC=950, ret=-0.049999999),
        member("both", 2, cC=900, Dn=True, H2L=True),
        member("five", 5, cC=960, Dn=True),
        member("six", 6, cC=940, eligible=False),
        member("near", 3, cC=951, ret=-0.05),
    ])
    result = build_raw(pd.DataFrame([event("today")]), c, pd.DataFrame([formal(original=5)])).iloc[0]
    assert result.mkt_all_original_n == result.mkt_all_observed_n == 5
    assert result.mkt_all_drop_k == 4
    assert result.mkt_all_drop_rate == 4 / 5
    assert result.mkt_chain_original_n == result.mkt_chain_observed_n == 4
    assert result.mkt_chain_drop_k == 3
    assert result.mkt_chain_drop_rate == 3 / 4


def test_mid_observability_paused_missing_partition():
    c = pd.DataFrame([
        member("visible", 2, cC=900),
        member("no_limit", 5, mid_obs=False, return_obs=True, cC=900),
        member("paused", 6, found=False, paused=True, mid_obs=False, cC=np.nan, cP=np.nan),
        member("missing", 1, found=False, missing=True, mid_obs=False, cC=np.nan, cP=np.nan),
    ])
    result = build_raw(pd.DataFrame([event("today")]), c, pd.DataFrame([formal(original=4)])).iloc[0]
    assert [result[f"mkt_all_{key}"] for key in ("original_n", "observed_n", "paused_n", "missing_n", "unobservable_n")] == [4, 1, 1, 1, 1]
    assert result.mkt_all_drop_rate == 1
    assert [result[f"mkt_chain_{key}"] for key in ("original_n", "observed_n", "paused_n", "missing_n", "unobservable_n")] == [3, 1, 1, 0, 1]
    assert result.mkt_adapter_quality == "DEGRADED"


def test_advances_exclude_flat_and_height_bins_retain_sixplus():
    e = pd.DataFrame([event(f"h{h}", U=True, height=h, cC=1100) for h in (1, 2, 3, 4, 5, 6, 9)] + [
        event("flat"), event("down", cC=990), event("ineligible", eligible=False, U=True, height=12, cC=1200)
    ])
    row = formal(n=9, market=10, U=7, max_h=9, ladder=1.0, Udens=7 / 9 * 1000)
    result = build_raw(e, pd.DataFrame(), pd.DataFrame([row])).iloc[0]
    assert result.mkt_advance_n == 7
    assert result.mkt_advance_pct == 7 / 9
    assert [result[f"mkt_height_{h}_n"] for h in range(1, 6)] == [1] * 5
    assert result.mkt_height_6plus_n == 2
    assert result.mkt_max_height == 9 and result.mkt_ladder == 1
    for target, source in FIELD_MAPPING.items():
        assert result[target] == row[source]


def test_complete_zero_events_is_distinct_from_whole_day_missing():
    d = pd.DataFrame([formal(n=2, market=2), formal("2025-01-03", n=0, market=0)])
    e = pd.DataFrame([event("flat"), event("down", cC=999)])
    result = build_raw(e, pd.DataFrame(), d).set_index("trade_date")
    complete = result.loc["2025-01-02"]
    absent = result.loc["2025-01-03"]
    assert complete.mkt_event_input_status == "complete"
    assert complete.mkt_advance_n == complete.mkt_advance_pct == 0
    assert complete.mkt_height_1_n == complete.mkt_height_6plus_n == 0
    assert complete.mkt_up_n == complete.mkt_ladder == complete.mkt_max_height == 0
    assert absent.mkt_event_input_status == "missing"
    assert pd.isna(absent.mkt_advance_n) and pd.isna(absent.mkt_height_1_n)
    assert pd.isna(absent.mkt_up_n) and pd.isna(absent.mkt_ladder) and pd.isna(absent.mkt_max_height)
    assert absent.U == absent.ladder == absent.max_h == 0  # original evidence retained


def test_empty_groups_have_missing_rates_and_absent_cohort_is_not_empty():
    e = pd.DataFrame([event("a"), event("b", "20250103")])
    d = pd.DataFrame([formal(original=0), formal("2025-01-03", original=3)])
    result = build_raw(e, pd.DataFrame(), d)
    for group in ("all", "chain"):
        assert result.iloc[0][f"mkt_{group}_original_n"] == 0
        assert result.iloc[0][f"mkt_{group}_observed_n"] == 0
        assert result.iloc[0][f"mkt_{group}_drop_k"] == 0
        assert pd.isna(result.iloc[0][f"mkt_{group}_drop_rate"])
        assert pd.isna(result.iloc[1][f"mkt_{group}_original_n"])
        assert pd.isna(result.iloc[1][f"mkt_{group}_drop_k"])
    assert result.iloc[0].mkt_cohort_input_status == "empty"
    assert result.iloc[1].mkt_cohort_input_status == "missing"


def test_partial_inputs_do_not_masquerade_as_complete():
    result = build_raw(pd.DataFrame([event("a")]), pd.DataFrame([member("a")]),
                       pd.DataFrame([formal(n=2, market=2, original=2)])).iloc[0]
    assert result.mkt_event_input_status == result.mkt_cohort_input_status == "partial"
    assert pd.isna(result.mkt_advance_n) and pd.isna(result.mkt_up_n)
    assert pd.isna(result.mkt_all_drop_k) and pd.isna(result.mkt_chain_original_n)


def test_dates_calendar_old_fields_and_inputs_unchanged():
    e = pd.DataFrame([event("a", 20250102), event("b", "2025-01-03 00:00:00")])
    c = pd.DataFrame([member("a", day=date(2025, 1, 3), cC=950)])
    d = pd.DataFrame([formal("2025-01-03", original=1), formal("2025-01-02")])
    d["date"] = [20250103.0, 20250102.0]
    snapshots = [frame.copy(deep=True) for frame in (e, c, d)]
    result = build_raw(e, c, d, calendar=[date(2025, 1, 6), 20250103, "2025-01-02"])
    assert result.trade_date.tolist() == ["2025-01-02", "2025-01-03", "2025-01-06"]
    assert pd.isna(result.iloc[2].mkt_advance_n)
    assert result.iloc[1].mkt_all_drop_k == 1
    # Adding an entirely unavailable calendar row promotes integer columns to
    # nullable float storage; every original value still remains unchanged.
    pd.testing.assert_frame_equal(result.loc[:1, d.columns].reset_index(drop=True), d.sort_values("trade_date").reset_index(drop=True), check_dtype=False)
    for original, snapshot in zip((e, c, d), snapshots):
        pd.testing.assert_frame_equal(original, snapshot)
    with pytest.raises(ValueError, match="duplicate trading dates"):
        build_raw(e, c, pd.concat([d, d]))
    contradictory = d.copy()
    contradictory.loc[0, "date"] = 20250102
    with pytest.raises(ValueError, match="disagree"):
        build_raw(e, c, contradictory)


def test_load_formal_reads_only_named_version_and_float_roundtrip(tmp_path):
    e, c, d = pd.DataFrame([event("a")]), pd.DataFrame([member("a")]), pd.DataFrame([formal(original=1)])
    e.to_parquet(tmp_path / "events.parquet", index=False)
    c.to_parquet(tmp_path / "cohorts.parquet", index=False)
    d.to_csv(tmp_path / "sentiment_daily.csv", index=False)
    loaded_e, loaded_c, loaded_d = load_formal(tmp_path)
    pd.testing.assert_frame_equal(loaded_e, e)
    pd.testing.assert_frame_equal(loaded_c, c)
    assert loaded_d.loc[0, "score5"] == d.loc[0, "score5"]
    assert loaded_d.loc[0, "lr20"] == d.loc[0, "lr20"]
    with pytest.raises(FileNotFoundError):
        load_formal(tmp_path / "unselected_version")
