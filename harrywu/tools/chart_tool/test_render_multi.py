from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime
from pathlib import Path

import matplotlib
import matplotlib.colors as mcolors
import numpy as np
import pytest
from PIL import Image

from chart_tool.contracts import ChartView, RenderOptions
from chart_tool.pixel_check import check_png
from chart_tool.render_multi import (
    _build_figure,
    _minute_slot,
    render_multiperiod_png_with_geometry,
    render_period_png_with_geometry,
)


V2 = Path("C:/Project Rocket/outputs/chart_tool_v2_20261009")
FIXTURES = V2 / "fixtures" / "views_a3"
LEGACY = Path("C:/Project Rocket/outputs/chart_tool_development_20261009/F2")
PERIODS = ("daily", "15m", "1m", "intraday")


def _load_view(period: str, *, filename: str | None = None) -> ChartView:
    path = FIXTURES / (filename or f"a3.{period}.view.json")
    return ChartView.from_dict(json.loads(path.read_text(encoding="utf-8")))


def _load_a3_views() -> dict[str, ChartView]:
    return {period: _load_view(period) for period in PERIODS}


def _load_legacy_views() -> dict[str, ChartView]:
    return {
        period: ChartView.from_dict(
            json.loads((LEGACY / f"multi.{period}.view.json").read_text(encoding="utf-8"))
        )
        for period in PERIODS
    }


def _options(*, width: int = 1200, height: int = 1200, dpi: int = 100) -> RenderOptions:
    return RenderOptions(width_px=width, height_px=height, dpi=dpi, auto_width=False)


def _build(views: list[ChartView], options: RenderOptions, *, combined: bool):
    with matplotlib.rc_context({"font.family": [options.font_family], "axes.unicode_minus": False}):
        return _build_figure(views, options, combined=combined,
                             height_px=2400 if combined else options.height_px)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_u1_five_day_15m_uses_contiguous_slots_and_four_session_boundaries(tmp_path: Path) -> None:
    view = _load_view("15m", filename="a3_15m_5sessions.view.json")
    target = tmp_path / "15m.png"
    _manifest, geometry = render_period_png_with_geometry(view, _options(), target)
    panel = geometry["panels"]["15m"]
    centers = np.asarray([bar["x_px"] for bar in panel["bars"]])
    assert len(centers) == 67
    assert np.diff(centers) == pytest.approx([panel["slot_px"]] * 66, abs=1e-6)

    figure, canvas, axes, _warnings = _build([view], _options(), combined=False)
    canvas.draw()
    boundary_lines = [line for line in axes["15m"]["price"].lines if line.get_color() == "#cbd5e1"]
    assert len(boundary_lines) == 4
    assert axes["15m"]["price"].get_xlim() == pytest.approx((-0.6, 66.6))
    figure.clear()


def test_u2_fixed_slots_follow_session_end_times_and_right_tail_is_clear(tmp_path: Path) -> None:
    assert _minute_slot("2026-09-18T09:31:00+08:00") == 1
    assert _minute_slot("2026-09-18T11:30:00+08:00") == 120
    assert _minute_slot("2026-09-18T13:01:00+08:00") == 121
    assert _minute_slot("2026-09-18T15:00:00+08:00") == 240
    view = _load_view("1m")  # The fixture ends at 10:07.
    target = tmp_path / "1m.png"
    _manifest, geometry = render_period_png_with_geometry(view, _options(width=2400), target)
    panel = geometry["panels"]["1m"]
    bars = {bar["key"]: bar for bar in panel["bars"]}
    first = bars["2026-09-18T09:31:00+08:00"]
    last = bars["2026-09-18T10:07:00+08:00"]
    x0, _top, x1, _bottom = panel["price_axes_px"]
    slot_px = (x1 - x0) / 241.2
    assert first["x_px"] == pytest.approx(x0 + (1.0 + 0.6) * slot_px)
    assert last["x_px"] == pytest.approx(x0 + (37.0 + 0.6) * slot_px)
    pixel = check_png(target, geometry)
    assert pixel["panels"]["1m"]["colored_in_fixed_slot_tail"] == 0
    assert pixel["panels"]["1m"]["status"] == "pass"


def test_u3_auction_occupies_slot_zero_and_missing_auction_is_reported(tmp_path: Path) -> None:
    views = _load_a3_views()
    target = tmp_path / "auction.png"
    _manifest, geometry = render_multiperiod_png_with_geometry(views, _options(width=3840), target)
    for period in ("1m", "intraday"):
        auction = [bar for bar in geometry["panels"][period]["bars"] if bar["key"] == "auction:2026-09-18"]
        assert len(auction) == 1
        x0, _top, x1, _bottom = geometry["panels"][period]["price_axes_px"]
        slot_px = (x1 - x0) / 241.2
        assert auction[0]["x_px"] == pytest.approx(x0 + 0.6 * slot_px)
        assert auction[0]["volume_rgb"] == [156, 163, 175]

    missing = {}
    for period, view in views.items():
        metadata = copy.deepcopy(view.metadata)
        metadata["auctions"] = {}
        missing[period] = ChartView(view.ts_code, view.period, view.bars.copy(deep=True), metadata)
    missing_target = tmp_path / "missing-auction.png"
    missing_manifest, missing_geometry = render_multiperiod_png_with_geometry(
        missing, _options(width=3840), missing_target
    )
    for period in ("1m", "intraday"):
        assert not any(bar["key"].startswith("auction:") for bar in missing_geometry["panels"][period]["bars"])
        assert "AUCTION_UNAVAILABLE" in missing_manifest["panels"][period]["warnings"]
    figure, _canvas, _axes, _warnings = _build(list(missing.values()), _options(width=3840), combined=True)
    assert any("集合竞价来源缺失" in item.get_text() for item in figure.texts)
    figure.clear()


def test_daily_auction_marker_uses_the_next_sequence_slot_when_daily_bar_is_absent(tmp_path: Path) -> None:
    views = _load_a3_views()
    daily = views["daily"]
    metadata = copy.deepcopy(daily.metadata)
    metadata["incomplete"] = False
    views["daily"] = ChartView(daily.ts_code, "daily", daily.bars.iloc[:-1].copy(deep=True), metadata)
    views["daily"].validate()
    target = tmp_path / "daily-auction.png"
    _manifest, geometry = render_multiperiod_png_with_geometry(views, _options(width=3840), target)
    panel = geometry["panels"]["daily"]
    auction = [bar for bar in panel["bars"] if bar["key"] == "auction:2026-09-18"]
    assert len(auction) == 1
    assert auction[0]["vol"] == 277_200.0
    assert auction[0]["volume_rgb"] == [156, 163, 175]
    assert auction[0]["x_px"] > panel["bars"][-2]["x_px"]
    assert panel["last_bar_right_px"] > auction[0]["x_px"]
    assert check_png(target, geometry)["status"] == "pass"


def test_u4_avg_price_geometry_matches_supplied_values_within_one_pixel(tmp_path: Path) -> None:
    view = _load_view("intraday")
    target = tmp_path / "intraday.png"
    _manifest, geometry = render_period_png_with_geometry(view, _options(width=2400), target)
    bars = {bar["key"]: bar for bar in geometry["panels"]["intraday"]["bars"]}
    figure, canvas, axes, _warnings = _build([view], _options(width=2400), combined=False)
    canvas.draw()
    axis = axes["intraday"]["price"]
    height = canvas.get_width_height()[1]
    for row in view.bars.to_dict(orient="records"):
        slot = _minute_slot(str(row["bar_end"]))
        _x, display_y = axis.transData.transform((slot, float(row["avg_price"])))
        expected_y = height - float(display_y)
        actual_y = bars[str(row["bar_end"])]["avg_price_y_px"]
        assert abs(actual_y - expected_y) <= 1.0
    figure.clear()


def test_u5_intraday_scale_is_symmetric_and_percent_axis_is_visible() -> None:
    view = _load_view("intraday")
    figure, canvas, axes, _warnings = _build([view], _options(width=2400), combined=False)
    canvas.draw()
    price_axis = axes["intraday"]["price"]
    prev_close = float(view.metadata["prev_close"]["2026-09-18"])
    lower, upper = price_axis.get_ylim()
    assert (lower + upper) / 2.0 == pytest.approx(prev_close, abs=1e-10)
    percent_axis = next(axis for axis in figure.axes if axis is not price_axis and axis not in (axes["intraday"]["volume"],))
    assert any("%" in tick.get_text() for tick in percent_axis.get_yticklabels())
    assert any(tick.get_color() in {"#d62728", "#2ca02c"} for tick in percent_axis.get_yticklabels())
    time_labels = [tick.get_text() for tick in axes["intraday"]["volume"].get_xticklabels()]
    assert time_labels == ["09:30", "10:00", "10:30", "11:00", "11:30/13:00",
                           "13:30", "14:00", "14:30", "15:00"]
    figure.clear()


def test_u6_15m_candle_bodies_do_not_overlap_and_u7_partial_body_is_hatched() -> None:
    view = _load_view("15m", filename="a3_15m_5sessions.view.json")
    figure, canvas, axes, _warnings = _build([view], _options(width=2400), combined=False)
    canvas.draw()
    price_axis = axes["15m"]["price"]
    slot_px = price_axis.get_window_extent().width / (len(view.bars) + 0.2)
    body_width_px = round(0.7 * slot_px)
    assert body_width_px < slot_px
    intervals = [(index * slot_px - body_width_px / 2, index * slot_px + body_width_px / 2)
                 for index in range(len(view.bars))]
    assert all(right < next_left for (_left, right), (next_left, _next_right) in zip(intervals, intervals[1:]))
    partials = [patch for patch in price_axis.patches if patch.get_hatch() == "////"]
    assert partials
    assert all(patch.get_linestyle() == "--" for patch in partials)
    assert any("未完成" in text.get_text() for text in price_axis.texts)
    figure.clear()


def test_u8_u9_u10_combined_layout_headers_geometry_and_pixels(tmp_path: Path) -> None:
    views = _load_a3_views()
    target = tmp_path / "combined.png"
    manifest, geometry = render_multiperiod_png_with_geometry(
        views, _options(width=3840), target, combined_daily_bars=250
    )
    with Image.open(target) as image:
        assert image.size == (3840, 2400)
    assert len(geometry["panels"]["daily"]["bars"]) == 250
    assert [geometry["panels"][period]["mode"] for period in PERIODS] == ["normal", "normal", "normal", "line"]
    pixel = check_png(target, geometry)
    assert pixel["status"] == "pass"
    assert all(value["status"] == "pass" for value in pixel["panels"].values())

    figure, _canvas, axes, _warnings = _build(list(views.values()), _options(width=3840), combined=True)
    header_text = "\n".join(item.get_text() for item in figure.texts)
    assert "Requested cutoff" not in header_text
    assert "Warnings:" not in header_text
    assert "可见截止：" in header_text and "截止依据：" in header_text
    assert "工程合成样例，非真实证据" in header_text
    expected_yellow = mcolors.to_rgba("#ffd166", alpha=0.20)
    yellow_spans = [patch for patch in axes["daily"]["price"].patches
                    if np.allclose(patch.get_facecolor(), expected_yellow, atol=0.01)]
    assert yellow_spans
    figure.clear()
    assert manifest["panels"]["daily"]["displayed_rows"] == 250


def test_u11_preopen_intraday_titles_the_previous_session(tmp_path: Path) -> None:
    view = _load_view("intraday", filename="a3_preopen_prev_session.view.json")
    target = tmp_path / "preopen.png"
    manifest, geometry = render_period_png_with_geometry(view, _options(width=2400), target)
    figure, canvas, axes, _warnings = _build([view], _options(width=2400), combined=False)
    canvas.draw()
    title = axes["intraday"]["price"].get_title(loc="left")
    assert "上一交易日" in title and "09-17" in title
    title_box = axes["intraday"]["price"].title.get_window_extent(canvas.get_renderer())
    hint_box = figure.texts[-1].get_window_extent(canvas.get_renderer())
    assert title_box.y1 < hint_box.y0 or hint_box.y1 < title_box.y0
    assert "DISPLAY_SESSION_PREVIOUS" in manifest["panels"]["intraday"]["warnings"]
    assert geometry["panels"]["intraday"]["mode"] == "line"
    figure.clear()


def test_u12_render_does_not_mutate_inputs_and_is_byte_deterministic(tmp_path: Path) -> None:
    views = _load_a3_views()
    before = {period: view.to_dict() for period, view in views.items()}
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    render_multiperiod_png_with_geometry(views, _options(width=3840), first)
    render_multiperiod_png_with_geometry(views, _options(width=3840), second)
    assert {period: view.to_dict() for period, view in views.items()} == before
    assert _sha256(first) == _sha256(second)


def test_legacy_f2_views_render_with_missing_field_warnings_and_pixel_geometry(tmp_path: Path) -> None:
    views = _load_legacy_views()
    target = tmp_path / "legacy.png"
    manifest, geometry = render_multiperiod_png_with_geometry(views, _options(width=3840), target)
    for warning in ("AUCTION_UNAVAILABLE", "PREV_CLOSE_UNAVAILABLE", "AVG_PRICE_UNAVAILABLE"):
        assert warning in manifest["warnings"]
    assert geometry["panels"]["intraday"]["mode"] == "line"
    assert check_png(target, geometry)["status"] == "pass"
    figure, _canvas, _axes, _warnings = _build(list(views.values()), _options(width=3840), combined=True)
    header_text = "\n".join(item.get_text() for item in figure.texts)
    assert "集合竞价来源缺失" in header_text
    figure.clear()


def test_empty_panels_show_no_data_and_pixel_check_skips_them(tmp_path: Path) -> None:
    views = _load_a3_views()
    empty = {}
    for period, view in views.items():
        metadata = copy.deepcopy(view.metadata)
        metadata["incomplete"] = False
        empty[period] = ChartView(view.ts_code, period, view.bars.iloc[:0].copy(), metadata)
    target = tmp_path / "empty.png"
    _manifest, geometry = render_multiperiod_png_with_geometry(empty, _options(width=1200), target)
    pixel = check_png(target, geometry)
    assert pixel["status"] == "pass"
    assert all(panel["status"] == "skipped" for panel in pixel["panels"].values())
    figure, _canvas, _axes, _warnings = _build(list(empty.values()), _options(width=1200), combined=True)
    assert sum(item.get_text() == "无数据" for item in figure.axes[0].texts) == 1
    figure.clear()
