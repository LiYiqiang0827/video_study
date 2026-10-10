"""All synthetic Li observations are confined to the isolated pytest base."""
import json

import numpy as np
import pandas as pd
import pytest

from policyStudy.policy.market_sentiment.li_review import FIXED_DATES, LABELS, SPEC, evaluate_li_labels


def daily():
    return pd.DataFrame({"trade_date": FIXED_DATES, "mkt_hit": 50., "mkt_cont": 50., "mkt_act": 50., "mkt_weather": "sunny"})


def labels(dates=FIXED_DATES, **changes):
    result = pd.DataFrame({"trade_date": dates, "hit": "", "cont": "", "act": ""})
    for column, value in changes.items():
        result[column] = value
    return result


def complete():
    return labels(hit="中", cont="一般", act="中")


def evaluate(tmp_path, source, human, ledger=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "Li.csv"
    human.to_csv(path, index=False)
    return evaluate_li_labels(source, path, tmp_path / "output", ledger)


def test_fixed_requested_order_and_empty_optional_weather(tmp_path):
    expected = ("2026-08-27", "2026-08-31", "2026-09-03", "2026-09-07", "2026-09-09", "2025-11-14", "2025-12-03", "2026-05-07", "2026-06-10", "2026-06-11")
    assert FIXED_DATES == expected
    result = evaluate(tmp_path, daily(), labels())
    assert result["reference_only"] is True and result["status"] == "awaiting_labels"
    assert result["incomplete_dates"] == list(FIXED_DATES)
    for dimension in LABELS:
        metric = result["dimensions"][dimension]
        assert metric["total_requested"] == metric["missing_n"] == 10
        assert metric["matches"] == metric["labeled_n"] == metric["evaluable_n"] == 0
        assert metric["basic_consistency"] is None
        assert not metric["adjustment_allowed"]
    assert result["weather"] == {"matches": 0, "denominator": 0, "labeled_n": 0, "unavailable_machine_n": 0}
    forbidden = {"pass", "primary", "harry", "all_dimensions_pass", "primary_threshold"}
    assert not forbidden & set(result)
    assert pd.read_csv(result["paths"]["disagreements"]).empty
    assert set(path.name for path in (tmp_path / "output").iterdir()) == {"human_evaluation.json", "agreement_metrics.csv", "disagreements.csv", "README.md"}


def test_skip_omitted_dates_and_partially_filled_core_are_preserved(tmp_path):
    human = labels(FIXED_DATES[:3], hit=["中", "跳过", "轻"], cont=["", "skip", "一般"], act=["高", "", "中"])
    result = evaluate(tmp_path, daily(), human)
    assert result["status"] == "partial_labels"
    assert FIXED_DATES[0] in result["partially_labeled_dates"]
    assert FIXED_DATES[1] in result["blank_or_skipped_dates"]
    assert FIXED_DATES[3] in result["blank_or_skipped_dates"]
    assert result["dimensions"]["hit"]["labeled_n"] == 2
    assert result["dimensions"]["cont"]["labeled_n"] == 1
    assert result["dimensions"]["act"]["labeled_n"] == 2
    assert result["dimensions"]["hit"]["matches"] == 1
    assert result["dimensions"]["act"]["lower_disagreement_n"] == 1
    assert all(metric["total_requested"] == 10 for metric in result["dimensions"].values())


@pytest.mark.parametrize("matching", [6, 7, 8])
def test_six_seven_eight_matches_use_fixed_ten_reference(tmp_path, matching):
    human = complete()
    human["hit"] = ["中"] * matching + ["轻"] * (10 - matching)
    result = evaluate(tmp_path, daily(), human)
    assert result["status"] == "review_recorded"
    metric = result["dimensions"]["hit"]
    assert metric["matches"] == matching and metric["total_requested"] == metric["evaluable_n"] == 10
    assert metric["basic_consistency"] is (matching >= 7)
    assert metric["higher_disagreement_n"] == 10 - matching
    assert result["reference_only"]


def test_partial_six_of_six_is_unresolved_not_six_of_six_success(tmp_path):
    result = evaluate(tmp_path / "six", daily(), complete().iloc[:6])
    metric = result["dimensions"]["hit"]
    assert metric["matches"] == metric["evaluable_n"] == 6
    assert metric["total_requested"] == 10 and metric["missing_n"] == 4
    assert metric["basic_consistency"] is None
    result = evaluate(tmp_path / "seven", daily(), complete().iloc[:7])
    assert result["dimensions"]["hit"]["basic_consistency"] is True
    assert result["status"] == "partial_labels"


def test_three_same_direction_and_each_dimension_one_revision(tmp_path):
    human = complete()
    human["hit"] = ["轻"] * 3 + ["中"] * 7
    human["cont"] = ["好"] * 3 + ["一般"] * 7
    source = daily()
    snapshot = source.copy(deep=True)
    ledger = {"hit": 1, "cont": 0}
    ledger_before = ledger.copy()
    result = evaluate(tmp_path, source, human, ledger)
    hit, cont, act = (result["dimensions"][dimension] for dimension in ("hit", "cont", "act"))
    assert hit["higher_disagreement_n"] == 3 and hit["eligible_directions"] == ["higher"]
    assert hit["revision_used"] == 1 and not hit["adjustment_allowed"]
    assert hit["reason"] == "dimension_revision_already_used"
    assert cont["lower_disagreement_n"] == 3 and cont["eligible_directions"] == ["lower"]
    assert cont["revision_used"] == 0 and cont["adjustment_allowed"]
    assert not act["adjustment_allowed"]
    assert not result["thresholds_changed"] and not result["machine_scores_changed"]
    assert ledger == ledger_before
    pd.testing.assert_frame_equal(source, snapshot)


def test_opposite_two_plus_two_does_not_combine_evidence(tmp_path):
    human = complete()
    human["hit"] = ["轻"] * 2 + ["重"] * 2 + ["中"] * 6
    result = evaluate(tmp_path, daily(), human)
    metric = result["dimensions"]["hit"]
    assert metric["higher_disagreement_n"] == metric["lower_disagreement_n"] == 2
    assert metric["eligible_directions"] == [] and not metric["adjustment_allowed"]


def test_machine_missing_never_counts_as_disagreement_or_direction(tmp_path):
    source = daily()
    source.loc[:2, "mkt_hit"] = np.nan
    human = complete()
    human.loc[:2, "hit"] = "轻"
    result = evaluate(tmp_path, source, human)
    metric = result["dimensions"]["hit"]
    assert metric["labeled_n"] == 10 and metric["evaluable_n"] == metric["matches"] == 7
    assert metric["missing_n"] == 0 and metric["unavailable_machine_n"] == 3
    assert metric["unavailable_machine_dates"] == list(FIXED_DATES[:3])
    assert metric["higher_disagreement_n"] == metric["lower_disagreement_n"] == 0
    assert not metric["adjustment_allowed"] and metric["basic_consistency"] is True
    assert pd.read_csv(result["paths"]["disagreements"]).empty


def test_weather_optional_supplement_has_its_own_denominator(tmp_path):
    human = complete()
    human["weather"] = ["晴", "阴"] + [""] * 8
    result = evaluate(tmp_path / "weather", daily(), human)
    assert result["weather"] == {"matches": 1, "denominator": 2, "labeled_n": 2, "unavailable_machine_n": 0}
    assert all(metric["matches"] == 10 for metric in result["dimensions"].values())
    source = daily().drop(columns="mkt_weather")
    result = evaluate(tmp_path / "unknown_weather", source, human)
    assert result["weather"]["denominator"] == 0 and result["weather"]["unavailable_machine_n"] == 2


@pytest.mark.parametrize("kind", ["duplicate", "outside", "invalid_core", "invalid_weather", "missing_daily"])
def test_invalid_label_dates_values_or_missing_fixed_daily_raise(tmp_path, kind):
    source, human = daily(), complete()
    if kind == "duplicate":
        human = pd.concat([human, human.iloc[:1]], ignore_index=True)
        match = "duplicate dates"
    elif kind == "outside":
        human.loc[0, "trade_date"] = "2026-09-30"
        match = "outside fixed ten"
    elif kind == "invalid_core":
        human.loc[0, "hit"] = "好"
        match = "Invalid Li hit"
    elif kind == "invalid_weather":
        human.loc[0, "weather"] = "晴转暴雨"
        match = "Invalid Li weather"
    else:
        source = source.iloc[1:]
        match = "Fixed review dates absent"
    with pytest.raises(ValueError, match=match):
        evaluate(tmp_path, source, human)
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("ledger", [{"hit": 2}, {"hit": -1}, {"hit": True}, {"other": 0}, [0, 0, 0]])
def test_revision_ledger_rejects_invalid_values(tmp_path, ledger):
    with pytest.raises(ValueError, match="revision count|threshold_revision_counts"):
        evaluate(tmp_path, daily(), complete(), ledger)


def test_reading_cutpoints_and_future_dimension_dictionary(tmp_path, monkeypatch):
    source = daily()
    source.loc[0, "mkt_hit"] = 35
    source.loc[1, "mkt_hit"] = 65
    human = complete()
    human.loc[1, "hit"] = "重"
    result = evaluate(tmp_path / "initial", source, human)
    assert result["dimensions"]["hit"]["matches"] == 10
    monkeypatch.setitem(SPEC, "human_bins", {"hit": [40, 70], "cont": [35, 65], "act": [35, 65]})
    human.loc[0, "hit"] = "轻"
    human.loc[1, "hit"] = "中"
    result = evaluate(tmp_path / "dimension_config", source, human)
    assert result["machine_band_cutpoints"]["hit"] == [40, 70]
    assert result["dimensions"]["hit"]["matches"] == 10
    assert result["dimensions"]["cont"]["matches"] == 10
