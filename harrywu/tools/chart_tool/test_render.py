from __future__ import annotations

import copy
import hashlib
import json
import math
from datetime import date, timedelta
from pathlib import Path

import matplotlib
import pandas as pd
import pytest
from PIL import Image, ImageDraw

from chart_tool.contracts import (
    DAILY_COLUMNS,
    MINUTE_COLUMNS,
    ChartError,
    ChartView,
    RenderOptions,
)
from chart_tool.pixel_check import check_png
from chart_tool.render import (
    _build_figure,
    _display_headers,
    render_png,
    render_png_with_geometry,
)


OLD_INPUT_DIR = Path("C:/Project Rocket/outputs/chart_tool_development_20261009/inputs/render")
VIEW_DIR = Path("C:/Project Rocket/outputs/chart_tool_v2_20261009/fixtures/views")


def load_old_view(name: str) -> ChartView:
    payload = json.loads((OLD_INPUT_DIR / f"{name}.json").read_text(encoding="utf-8"))
    bars = pd.DataFrame(payload["bars"], columns=DAILY_COLUMNS)
    view = ChartView(payload["ts_code"], payload["period"], bars, payload["metadata"])
    view.validate()
    return view


def load_v2_view(name: str) -> ChartView:
    payload = json.loads((VIEW_DIR / name).read_text(encoding="utf-8"))
    return ChartView.from_dict(payload)


def _synthetic_view(
    days: list[str],
    *,
    volumes: list[float] | None = None,
    prices: list[float] | None = None,
    gap_dates: list[str] | None = None,
    warnings: list[str] | None = None,
    validate: bool = True,
) -> ChartView:
    source = load_v2_view("b_600990_study_10y.view.json")
    template = source.bars.iloc[0].to_dict()
    records = []
    for index, day in enumerate(days):
        row = dict(template)
        row["trade_date"] = day
        if volumes is not None:
            row["vol"] = volumes[index]
        if prices is not None:
            price = prices[index]
            row.update(open=price, high=price, low=price, close=price)
        records.append(row)
    bars = pd.DataFrame(records, columns=DAILY_COLUMNS)
    metadata = copy.deepcopy(source.metadata)
    metadata["gap_dates"] = list(gap_dates or [])
    metadata["warnings"] = list(warnings or [])
    result = ChartView(source.ts_code, "daily", bars, metadata)
    if validate:
        result.validate()
    return result


def _long_synthetic_view(count: int) -> ChartView:
    dates = [value.strftime("%Y-%m-%d") for value in pd.bdate_range("2010-01-04", periods=count)]
    view = _synthetic_view(dates, validate=False)
    last_day = dates[-1]
    cutoff = f"{last_day}T15:00:00+08:00"
    view.metadata["requested_cutoff"] = cutoff
    view.metadata["effective_cutoff"] = cutoff
    view.metadata["cutoff_source"] = {
        "kind": "study_end",
        "confidence": "high",
        "evidence_refs": ["renderer-test"],
        "candidates": [],
        "calibration_segment": None,
    }
    view.metadata["factor_lineage"]["anchor_trade_date"] = last_day
    view.validate()
    return view


@pytest.fixture(scope="module")
def a_auto_render(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, object, dict]:
    view = load_v2_view("a_002025_restore_20260109.view.json")
    output = tmp_path_factory.mktemp("a_auto") / "a_auto.png"
    manifest, geometry = render_png_with_geometry(view, RenderOptions(), output)
    return output, manifest, geometry


def test_t1_calendar_days_do_not_change_adjacent_bar_spacing(tmp_path: Path) -> None:
    view = _synthetic_view(["2026-01-09", "2026-01-12"])
    _, geometry = render_png_with_geometry(
        view, RenderOptions(width_px=1200, height_px=800, auto_width=False), tmp_path / "weekend.png"
    )
    panel = geometry["panels"]["daily"]
    spacing = panel["bars"][1]["x_px"] - panel["bars"][0]["x_px"]
    assert abs(spacing - panel["slot_px"]) <= 0.01


def test_t2_fixture_a_auto_width_is_7538_and_keeps_three_pixel_slots(a_auto_render) -> None:
    _, _, geometry = a_auto_render
    panel = geometry["panels"]["daily"]
    assert geometry["canvas"]["width_px"] == 7538
    assert panel["slot_px"] >= 3.0


def test_t3_dense_mode_fixture_a_passes_pixel_check_and_warns(tmp_path: Path) -> None:
    view = load_v2_view("a_002025_restore_20260109.view.json")
    output = tmp_path / "dense.png"
    manifest, geometry = render_png_with_geometry(
        view,
        RenderOptions(width_px=3840, height_px=1600, auto_width=False),
        output,
    )
    result = check_png(output, geometry)
    assert geometry["panels"]["daily"]["mode"] == "dense"
    assert "DENSE_MODE_BODIES_OMITTED" in manifest.warnings
    assert result["status"] == "pass"


def test_t4_twelve_thousand_bars_below_one_pixel_raises_render_too_dense(tmp_path: Path) -> None:
    view = _long_synthetic_view(12_000)
    with pytest.raises(ChartError) as caught:
        render_png(view, RenderOptions(max_width_px=9600), tmp_path / "too_dense.png")
    assert caught.value.code == "RENDER_TOO_DENSE"
    assert caught.value.details["required_width_px"] > 9600
    assert not (tmp_path / "too_dense.png").exists()


def test_t5_every_fixture_a_positive_volume_and_candle_is_visible(a_auto_render) -> None:
    output, _, geometry = a_auto_render
    result = check_png(output, geometry)
    panel = result["panels"]["daily"]
    expected = sum(float(value) > 0 for value in load_v2_view("a_002025_restore_20260109.view.json").bars.vol)
    assert result["status"] == "pass"
    assert panel["volume_expected"] == expected
    assert panel["volume_visible"] == expected
    assert panel["candle_visible"] == panel["bars"] == 2236


def test_t6_zero_volume_is_not_expected_but_candle_remains_visible(tmp_path: Path) -> None:
    view = _synthetic_view(
        ["2026-01-05", "2026-01-06", "2026-01-07"],
        volumes=[100_000.0, 0.0, 100_000.0],
    )
    output = tmp_path / "zero.png"
    _, geometry = render_png_with_geometry(
        view, RenderOptions(width_px=1200, height_px=800, auto_width=False), output
    )
    result = check_png(output, geometry)
    panel = result["panels"]["daily"]
    assert result["status"] == "pass"
    assert panel["volume_expected"] == panel["volume_visible"] == 2
    assert panel["candle_visible"] == 3


def test_t7_right_boundary_checker_passes_and_detects_an_added_red_pixel(tmp_path: Path) -> None:
    view = _synthetic_view(["2026-01-05", "2026-01-06"])
    output = tmp_path / "right.png"
    _, geometry = render_png_with_geometry(
        view, RenderOptions(width_px=1200, height_px=800, auto_width=False), output
    )
    assert check_png(output, geometry)["status"] == "pass"
    panel = geometry["panels"]["daily"]
    x = int(math.ceil(panel["last_bar_right_px"])) + 1
    top, bottom = panel["price_axes_px"][1], panel["price_axes_px"][3]
    y = round((top + bottom) / 2)
    assert x <= int(panel["price_axes_px"][2]) - 2
    with Image.open(output) as source:
        image = source.convert("RGB")
    ImageDraw.Draw(image).point((x, y), fill=tuple(panel["bars"][0]["candle_rgb"]))
    altered = tmp_path / "right_with_pixel.png"
    image.save(altered)
    result = check_png(altered, geometry)
    assert result["status"] == "fail"
    assert result["panels"]["daily"]["colored_right_of_last_bar"] >= 1


def test_t8_gap_dates_group_to_one_mark_and_calendar_gap_gets_label(tmp_path: Path) -> None:
    missing_days = ["2026-01-12", "2026-01-13", "2026-01-14", "2026-01-15", "2026-01-16", "2026-01-19"]
    explicit = _synthetic_view(
        ["2026-01-09", "2026-01-20"], gap_dates=missing_days, warnings=["DAILY_GAPS_UNEXPLAINED"]
    )
    _, explicit_geometry = render_png_with_geometry(
        explicit, RenderOptions(width_px=1200, height_px=800, auto_width=False), tmp_path / "explicit.png"
    )
    marks = explicit_geometry["panels"]["daily"]["gap_marks"]
    assert len(marks) == 1
    assert marks[0]["label"] == "缺 6 个交易日（原因未核实）"

    start = date(2025, 1, 1)
    end = start + timedelta(days=298)
    calendar_view = _synthetic_view(
        [start.isoformat(), end.isoformat()], warnings=["CALENDAR_UNAVAILABLE"]
    )
    _, calendar_geometry = render_png_with_geometry(
        calendar_view, RenderOptions(width_px=1200, height_px=800, auto_width=False), tmp_path / "calendar.png"
    )
    calendar_marks = calendar_geometry["panels"]["daily"]["gap_marks"]
    assert len(calendar_marks) == 1
    assert calendar_marks[0]["label"] == "间隔 298 天（无交易日历，原因未核实）"


def test_t9_restore_header_is_chinese_and_contains_requested_cutoff(a_auto_render) -> None:
    view = load_v2_view("a_002025_restore_20260109.view.json")
    warnings = list(view.metadata["warnings"])
    first, second, _, _, _ = _display_headers(view, warnings, 0)
    text = first + second
    assert "还原阶段（只含截止前数据）" in text
    assert "可见截止：2026-01-09 09:22:33（盘前）" in text
    assert "Requested cutoff" not in text


def test_t10_log_prices_20_40_80_are_evenly_spaced(tmp_path: Path) -> None:
    view = _synthetic_view(
        ["2026-01-05", "2026-01-06", "2026-01-07"],
        prices=[20.0, 40.0, 80.0],
        volumes=[100_000.0, 100_000.0, 100_000.0],
    )
    options = RenderOptions(width_px=1200, height_px=800, auto_width=False, price_scale="log")
    _, geometry = render_png_with_geometry(view, options, tmp_path / "log.png")
    ys = [bar["high_y_px"] for bar in geometry["panels"]["daily"]["bars"]]
    assert abs((ys[0] - ys[1]) - (ys[1] - ys[2])) <= 1.0
    with matplotlib.rc_context({"font.family": [options.font_family]}):
        figure, canvas, state = _build_figure(view, options, 1200)
        canvas.draw()
        labels = [
            label.get_text()
            for label in state["price_ax"].get_yticklabels(which="both")
            if label.get_visible() and label.get_text()
        ]
        figure.clear()
    assert "20.00" in labels and "40.00" in labels and "80.00" in labels
    assert all("×" not in label and "e" not in label.lower() for label in labels)


def test_t11_annotations_require_visible_date_and_hline_expands_price_limits(tmp_path: Path) -> None:
    view = _synthetic_view(["2026-01-05", "2026-01-06"])
    bad = RenderOptions(
        width_px=1200,
        height_px=800,
        auto_width=False,
        annotations=({"kind": "vline", "at": "2026-01-07", "price": None, "text": "越界", "style": "mention"},),
    )
    with pytest.raises(ChartError) as caught:
        render_png(view, bad, tmp_path / "bad_annotation.png")
    assert caught.value.code == "ANNOTATION_OUT_OF_VIEW"

    line = {"kind": "hline", "at": None, "price": 500.0, "text": "目标", "style": "mention"}
    options = RenderOptions(width_px=1200, height_px=800, auto_width=False, annotations=(line,))
    _, geometry = render_png_with_geometry(view, options, tmp_path / "hline.png")
    panel = geometry["panels"]["daily"]
    annotation_y = panel["annotations"][0]["y_px"]
    assert panel["price_axes_px"][1] < annotation_y < panel["price_axes_px"][3]


def test_t12_render_does_not_change_bars_or_metadata(tmp_path: Path) -> None:
    view = load_v2_view("a_002025_restore_20260109.view.json")
    before_bars = view.bars.copy(deep=True)
    before_metadata = copy.deepcopy(view.metadata)
    render_png_with_geometry(view, RenderOptions(width_px=3840, auto_width=False), tmp_path / "immutable.png")
    pd.testing.assert_frame_equal(view.bars, before_bars)
    assert view.metadata == before_metadata


def test_t13_empty_view_returns_empty_and_writes_no_png(tmp_path: Path) -> None:
    view = load_old_view("empty")
    target = tmp_path / "empty.png"
    manifest, geometry = render_png_with_geometry(view, RenderOptions(), target)
    assert manifest.status == "empty"
    assert manifest.output_path is None
    assert not target.exists()
    assert geometry["panels"] == {}


def test_t15_same_input_produces_identical_png_bytes(tmp_path: Path) -> None:
    view = _synthetic_view(["2026-01-05", "2026-01-06", "2026-01-07"])
    options = RenderOptions(width_px=1200, height_px=800, auto_width=False)
    first, second = tmp_path / "one.png", tmp_path / "two.png"
    render_png(view, options, first)
    render_png(view, options, second)
    assert hashlib.sha256(first.read_bytes()).hexdigest() == hashlib.sha256(second.read_bytes()).hexdigest()


def test_legacy_render_fixtures_still_load_and_empty_fixture_is_valid() -> None:
    expected = json.loads((OLD_INPUT_DIR / "expected.json").read_text(encoding="utf-8"))
    for name in ("long_history", "short_history", "gap", "zero_volume", "single", "empty"):
        view = load_old_view(name)
        assert len(view.bars) == expected[name]["input_rows"]
        assert view.bars["vol"].tolist() == expected[name]["volume_values"]
    assert load_old_view("empty").bars.empty


def test_old_zero_volume_fixture_has_one_zero_volume_row(tmp_path: Path) -> None:
    view = load_old_view("zero_volume")
    output = tmp_path / "zero_volume.png"
    _, geometry = render_png_with_geometry(
        view, RenderOptions(width_px=1200, height_px=800, auto_width=False), output
    )
    result = check_png(output, geometry)
    panel = result["panels"]["daily"]
    assert panel["volume_expected"] == 2
    assert panel["candle_visible"] == 3


def test_daily_renderer_still_rejects_minute_period(tmp_path: Path) -> None:
    source = load_old_view("short_history")
    bars = source.bars.iloc[[0]].copy()
    day = str(bars.trade_date.iloc[0])
    bars["bar_start"] = f"{day}T09:30:00+08:00"
    bars["bar_end"] = f"{day}T09:45:00+08:00"
    bars["is_partial"] = False
    metadata = copy.deepcopy(source.metadata)
    metadata["cutoff_policy"] = "minute_end_v1"
    minute_view = ChartView(source.ts_code, "15m", bars.loc[:, MINUTE_COLUMNS], metadata)
    with pytest.raises(ChartError) as caught:
        render_png(minute_view, RenderOptions(), tmp_path / "minute.png")
    assert caught.value.code == "UNSUPPORTED_PERIOD"


def test_legacy_view_validation_still_rejects_unsorted_rows() -> None:
    source = load_old_view("short_history")
    bars = source.bars.iloc[::-1].reset_index(drop=True)
    invalid = ChartView(source.ts_code, source.period, bars, copy.deepcopy(source.metadata))
    with pytest.raises(ChartError) as caught:
        render_png(invalid, RenderOptions(), Path("unused.png"))
    assert caught.value.code == "UNSORTED_BARS"


def test_ma_prefix_warning_and_name_omission_are_preserved(tmp_path: Path) -> None:
    view = _synthetic_view(["2026-01-05", "2026-01-06", "2026-01-07"])
    manifest = render_png(view, RenderOptions(width_px=1200, height_px=800, auto_width=False, ma_periods=(5,)), tmp_path / "ma.png")
    assert any(value.startswith("MA5_PREFIX_INSUFFICIENT:") for value in manifest.warnings)
    assert "name" in view.metadata["labels_missing"]
    assert view.metadata["labels"] == {}
