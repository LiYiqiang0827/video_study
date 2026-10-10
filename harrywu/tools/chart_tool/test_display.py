from __future__ import annotations

import json
from pathlib import Path

import pytest

from chart_tool.contracts import ChartError, ChartView
from chart_tool.display import crop_dates, crop_last_bars, crop_sessions


VIEW_PATH = Path("C:/Project Rocket/outputs/chart_tool_v2_20261009/fixtures/views/a_002025_restore_20260109.view.json")


def load_view() -> ChartView:
    return ChartView.from_dict(json.loads(VIEW_PATH.read_text(encoding="utf-8")))


def test_crop_last_bars_crops_only_visible_tail_and_adds_warning() -> None:
    view = load_view()
    result = crop_last_bars(view, 250)
    assert len(result.bars) == 250
    assert result.bars.trade_date.iloc[-1] == view.bars.trade_date.iloc[-1]
    assert "DISPLAY_ZOOM_WITHIN_CUTOFF" in result.metadata["warnings"]
    assert len(result.metadata["gap_dates"]) <= len(view.metadata["gap_dates"])
    assert len(view.bars) == 2236


def test_crop_last_bars_returns_original_when_count_covers_view() -> None:
    view = load_view()
    assert crop_last_bars(view, len(view.bars)) is view
    assert crop_last_bars(view, len(view.bars) + 1) is view


@pytest.mark.parametrize("n", [0, -1, 1.5, True])
def test_crop_last_bars_requires_positive_integer(n: object) -> None:
    with pytest.raises(ChartError) as caught:
        crop_last_bars(load_view(), n)  # type: ignore[arg-type]
    assert caught.value.code == "INVALID_REQUEST"


def test_crop_dates_matches_inclusive_zoom_semantics() -> None:
    view = load_view()
    result = crop_dates(view, "2025-07-01", None)
    assert result.bars.trade_date.iloc[0] >= "2025-07-01"
    assert result.bars.trade_date.iloc[-1] == view.bars.trade_date.iloc[-1]
    assert "DISPLAY_ZOOM_WITHIN_CUTOFF" in result.metadata["warnings"]
    assert len(view.bars) == 2236


def test_crop_dates_empty_range_clears_gaps_and_marks_incomplete_false() -> None:
    result = crop_dates(load_view(), "2030-01-01", None)
    assert result.bars.empty
    assert result.metadata["gap_dates"] == []
    assert result.metadata["incomplete"] is False


def test_crop_dates_with_no_bounds_returns_same_view() -> None:
    view = load_view()
    assert crop_dates(view, None, None) is view


def _load_five_session_view() -> ChartView:
    path = Path(
        "C:/Project Rocket/outputs/chart_tool_v2_20261009/fixtures/views_a3/a3_15m_5sessions.view.json"
    )
    return ChartView.from_dict(json.loads(path.read_text(encoding="utf-8")))


def test_crop_sessions_keeps_last_trade_dates_and_does_not_change_input() -> None:
    view = _load_five_session_view()
    original = view.bars.copy(deep=True)
    result = crop_sessions(view, 2)
    assert result.bars.trade_date.drop_duplicates().tolist() == ["2026-09-17", "2026-09-18"]
    assert result.bars.iloc[-1].trade_date == view.bars.iloc[-1].trade_date
    assert "DISPLAY_ZOOM_WITHIN_CUTOFF" in result.metadata["warnings"]
    assert "DISPLAY_ZOOM_WITHIN_CUTOFF" not in view.metadata["warnings"]
    assert len(view.bars) == len(original)
    assert view.bars.equals(original)


def test_crop_sessions_returns_same_view_when_count_covers_sessions() -> None:
    view = _load_five_session_view()
    assert crop_sessions(view, 5) is view
    assert crop_sessions(view, 6) is view


@pytest.mark.parametrize("n", [0, -1, 1.5, True])
def test_crop_sessions_requires_positive_integer(n: object) -> None:
    with pytest.raises(ChartError) as caught:
        crop_sessions(_load_five_session_view(), n)  # type: ignore[arg-type]
    assert caught.value.code == "INVALID_REQUEST"


def test_crop_sessions_rejects_daily_views() -> None:
    daily = ChartView.from_dict(
        json.loads(
            Path(
                "C:/Project Rocket/outputs/chart_tool_v2_20261009/fixtures/views_a3/a3.daily.view.json"
            ).read_text(encoding="utf-8")
        )
    )
    with pytest.raises(ChartError) as caught:
        crop_sessions(daily, 1)
    assert caught.value.code == "INVALID_REQUEST"
