"""Synthetic labels exist only in pytest's tests/tmp output directory."""
import json

import numpy as np
import pandas as pd
import pytest

from policyStudy.policy.market_sentiment.human import (
    BAND_LABELS, LABEL_COLUMNS, NEUTRAL_FIELDS, WEATHER_ORDER,
    create_packets, machine_band,
)
from policyStudy.policy.market_sentiment.cards import reference_2025, card_rows


def daily(n=25, codes=None):
    weather = codes or [code for code in WEATHER_ORDER for _ in range(5)]
    dates = pd.date_range("2025-01-02", periods=n).strftime("%Y-%m-%d")
    result = pd.DataFrame({"trade_date": dates, "mkt_weather": weather[:n],
                           "mkt_hit": [0.] * n, "mkt_cont": [0.] * n, "mkt_act": [0.] * n,
                           "mkt_relay_score": [99.] * n, "mkt_summary": ["SECRET_MACHINE_DESCRIPTION"] * n})
    for field in NEUTRAL_FIELDS:
        result[field] = 1.0
    return result


def labels(dates, **changes):
    frame = pd.DataFrame({"trade_date": dates, "hit": "", "cont": "", "act": "", "weather": "", "notes": ""})
    for column, values in changes.items():
        frame[column] = values
    return frame




@pytest.mark.parametrize("dimension", ["hit", "cont", "act"])
def test_fixed_35_65_edges(dimension):
    low, middle, high = BAND_LABELS[dimension]
    assert [machine_band(value, dimension) for value in (0, 34.999999, 35, 64.999999, 65, 100)] == [low, low, middle, middle, high, high]
    assert machine_band(np.nan, dimension) is None
    with pytest.raises(ValueError, match="0..100"):
        machine_band(100.1, dimension)


def test_packets_reproducible_sorted_neutral_and_independently_blank(tmp_path):
    source = daily()
    snapshot = source.copy(deep=True)
    a = create_packets(source, tmp_path / "a")
    b = create_packets(source.iloc[::-1], tmp_path / "b")
    assert a["status"] == "optional_ready"
    assert a["counts"] == {"total": 15, "random": 15, "nominated": 0}
    assert all(stratum["final_random_n"] == 3 for stratum in a["strata"].values())
    key_a = pd.read_csv(a["paths"]["selection_manifest"])
    key_b = pd.read_csv(b["paths"]["selection_manifest"])
    pd.testing.assert_frame_equal(key_a, key_b)
    assert key_a.trade_date.is_monotonic_increasing and key_a.trade_date.is_unique
    facts = pd.read_csv(a["paths"]["facts_csv"])
    assert facts.columns.tolist() == ["trade_date", *NEUTRAL_FIELDS]
    assert not {"mkt_hit", "mkt_cont", "mkt_act", "mkt_relay_score", "mkt_weather", "mkt_forecast_n", "mkt_summary"} & set(facts.columns)
    text = (tmp_path / "a/packets/cards.html").read_text(encoding="utf-8")
    assert "SECRET_MACHINE_DESCRIPTION" not in text
    assert not any(code in text for code in WEATHER_ORDER)
    assert text.count("<section class='card'>") == 15
    assert text.count("<tr><td>") == 150
    assert not (tmp_path / "a/packets/facts.csv").exists()
    assert not (tmp_path / "a/packets/facts.md").exists()
    assert (tmp_path / "a/packets/reference_2025.csv").exists()
    for name in ("Li",):
        empty = pd.read_csv(a["paths"][f"{name}_labels"], keep_default_na=False)
        assert empty.columns.tolist() == LABEL_COLUMNS
        assert empty[LABEL_COLUMNS[1:]].eq("").to_numpy().all()
    pd.testing.assert_frame_equal(source, snapshot)


def test_nomination_overlap_replaced_within_same_class_and_deduplicated(tmp_path):
    source = daily()
    initial = create_packets(source, tmp_path / "initial")
    selected = pd.read_csv(initial["paths"]["selection_manifest"])
    overlap = selected.loc[selected.machine_weather.eq("sunny"), "trade_date"].iloc[0]
    nominated = pd.DataFrame({"trade_date": [overlap, overlap], "nominated_by": ["Harry", "Li"]})
    result = create_packets(source, tmp_path / "with_nominations", nominated)
    manifest = pd.read_csv(result["paths"]["selection_manifest"])
    assert result["counts"] == {"total": 16, "random": 15, "nominated": 1}
    assert result["duplicate_nominations_n"] == 1
    assert manifest.trade_date.is_unique
    assert manifest.loc[manifest.trade_date.eq(overlap), "selection"].item() == "nominated"
    replacements = manifest[manifest.selection.eq("replacement")]
    assert len(replacements) == 1 and replacements.machine_weather.item() == "sunny"
    assert result["strata"]["sunny"]["overlap_with_nominations_n"] == 1


def test_small_class_and_exhausted_replacement_pool_are_disclosed(tmp_path):
    source = daily(n=9, codes=["sunny"] * 2 + ["cloudy"] * 4 + ["storm"] * 3)
    initial = create_packets(source, tmp_path / "initial")
    assert initial["counts"]["random"] == 8
    assert initial["strata"]["sunny"]["random_shortfall_n"] == 1
    assert initial["strata"]["thunder"]["candidate_n"] == 0
    assert initial["strata"]["thunder"]["random_shortfall_n"] == 3
    manifest = pd.read_csv(initial["paths"]["selection_manifest"])
    overlap = manifest.loc[manifest.machine_weather.eq("sunny"), "trade_date"].tolist()
    result = create_packets(source, tmp_path / "all_sunny_nominated", pd.DataFrame({"trade_date": overlap}))
    assert result["counts"] == {"total": 8, "random": 6, "nominated": 2}
    assert result["strata"]["sunny"]["replacement_n"] == 0
    assert result["strata"]["sunny"]["random_shortfall_n"] == 3








def test_real_annotation_never_overwritten_and_invalid_labels_rejected(tmp_path):
    source = daily()
    result = create_packets(source, tmp_path / "pack")
    path = result["paths"]["Li_labels"]
    actual = pd.read_csv(path, keep_default_na=False)
    actual.loc[0, "notes"] = "Remembered independently"
    actual.to_csv(path, index=False)
    before = open(path, "rb").read()
    with pytest.raises(FileExistsError, match="Preserve"):
        create_packets(source, tmp_path / "pack")
    assert open(path, "rb").read() == before


def test_card_reference_independent_linear_hand_values_and_2026_excluded():
    source = daily(n=4, codes=["sunny"] * 4)
    source["trade_date"] = ["2025-01-02", "2025-01-03", "2025-01-06", "2026-01-02"]
    source["mkt_up_n"] = [0, 10, 20, 10**9]
    source["mkt_all_drop_k"] = [0, 1, 9, 10**9]
    source["mkt_all_observed_n"] = [0, 2, 10, 10**9]
    source["mkt_turnover_cny"] = [1e12, 2e12, 3e12, 1e20]
    # A stale/pre-smoothed rate must not be read by card references.
    source["mkt_all_drop_rate"] = 1.0
    result = reference_2025(source).set_index("metric")
    assert result.loc["up", ["p10", "p50", "p90"]].tolist() == [2, 10, 18]
    assert result.loc["all_pct", "n_valid"] == 2
    assert result.loc["all_pct", "n_missing"] == 1
    assert result.loc["all_pct", ["p10", "p50", "p90"]].tolist() == [54, 70, 86]
    assert result.loc["turnover", ["p10", "p50", "p90"]].tolist() == [1.2, 2, 2.8]
    source.loc[3, "mkt_up_n"] = -10**8
    pd.testing.assert_frame_equal(result, reference_2025(source).set_index("metric"))


def test_ten_card_items_zero_denominator_units_and_no_answers():
    source = daily(n=3, codes=["sunny"] * 3)
    source.loc[0, ["mkt_all_drop_k", "mkt_all_observed_n", "mkt_chain_drop_k", "mkt_chain_observed_n", "mkt_promo_h1_k", "mkt_promo_h1_n"]] = 0
    source.loc[0, "mkt_turnover_cny"] = 1.234e12
    source.loc[0, "mkt_index_sh_pct"] = -1.5
    source.loc[0, ["mkt_advance_n", "mkt_eligible_n"]] = [25, 100]
    rows = card_rows(source.iloc[0], reference_2025(source))
    assert len(rows) == 10
    assert "比例 缺失" in rows[4]["current"] and "比例 缺失" in rows[5]["current"]
    assert rows[6]["current"].count("板:") == 4
    assert "≥4板" in rows[6]["current"]
    assert "1.234万亿元" in rows[7]["current"]
    assert "25.00%" in rows[8]["current"]
    assert "上证 -1.50%" in rows[9]["current"]
    text = json.dumps(rows, ensure_ascii=False)
    assert "SECRET_MACHINE_DESCRIPTION" not in text
    assert "mkt_hit" not in text and "sunny" not in text


def test_rebuilding_cards_preserves_selected_dates_and_blank_labels(tmp_path):
    source = daily()
    result = create_packets(source, tmp_path / "preserve")
    paths = [result["paths"]["selection_manifest"], result["paths"]["Li_labels"]]
    before = [open(path, "rb").read() for path in paths]
    create_packets(source, tmp_path / "preserve")
    assert before == [open(path, "rb").read() for path in paths]
