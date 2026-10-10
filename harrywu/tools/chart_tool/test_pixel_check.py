from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageDraw

from chart_tool.pixel_check import check_png


RED = [214, 39, 40]
BLUE = [31, 78, 156]
PALE_RED = [230, 127, 128]
X2_DIR = Path("C:/Project Rocket/outputs/chart_tool_v2_20261009/X2")


def _geometry(*, xs: tuple[float, ...] = (5.0,), volumes: tuple[float, ...] | None = None) -> dict:
    volumes = volumes or tuple(100.0 for _ in xs)
    return {
        "geometry_version": "geom.v1",
        "canvas": {"width_px": 20, "height_px": 20},
        "panels": {
            "daily": {
                "price_axes_px": [1, 1, 18, 10],
                "volume_axes_px": [1, 10, 18, 18],
                "price_scale": "linear",
                "mode": "normal",
                "slot_px": 3.0,
                "volume_baseline_y_px": 17.0,
                "last_bar_right_px": max(xs) + 0.5,
                "bars": [
                    {
                        "key": f"2026-01-{index + 1:02d}",
                        "x_px": x,
                        "high_y_px": 2.0,
                        "low_y_px": 8.0,
                        "vol_top_y_px": 12.0,
                        "vol": volumes[index],
                        "candle_rgb": RED,
                        "volume_rgb": RED,
                    }
                    for index, x in enumerate(xs)
                ],
                "gap_marks": [],
                "annotations": [],
            }
        },
    }


def _write_image(path: Path, *, missing_volume: bool = False, right_dot: bool = False) -> None:
    image = Image.new("RGB", (20, 20), "white")
    draw = ImageDraw.Draw(image)
    draw.line((5, 2, 5, 8), fill=tuple(RED), width=1)
    if not missing_volume:
        draw.line((5, 12, 5, 16), fill=tuple(RED), width=1)
    if right_dot:
        draw.point((8, 5), fill=tuple(RED))
    image.save(path)


def test_checker_counts_visible_bars_and_zero_volume_is_not_expected(tmp_path: Path) -> None:
    path = tmp_path / "visible.png"
    _write_image(path)
    result = check_png(path, _geometry(volumes=(100.0,)))
    panel = result["panels"]["daily"]
    assert result["status"] == "pass"
    assert panel["bars"] == panel["volume_expected"] == panel["volume_visible"] == panel["candle_visible"] == 1

    zero = check_png(path, _geometry(volumes=(0.0,)))
    assert zero["status"] == "pass"
    assert zero["panels"]["daily"]["volume_expected"] == 0


def test_checker_detects_missing_volume_and_right_boundary_pixel(tmp_path: Path) -> None:
    missing = tmp_path / "missing.png"
    _write_image(missing, missing_volume=True)
    result = check_png(missing, _geometry())
    assert result["status"] == "fail"
    assert result["panels"]["daily"]["volume_visible"] == 0

    right = tmp_path / "right.png"
    _write_image(right, right_dot=True)
    result = check_png(right, _geometry())
    panel = result["panels"]["daily"]
    assert result["status"] == "fail"
    assert panel["colored_right_of_last_bar"] == 1


def test_checker_rejects_duplicate_floor_columns(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.png"
    _write_image(path)
    result = check_png(path, _geometry(xs=(5.0, 5.2), volumes=(100.0, 0.0)))
    panel = result["panels"]["daily"]
    assert result["status"] == "fail"
    assert panel["duplicate_columns"] == 1


def test_dense_checker_catches_saved_missing_volume_without_neighbor_substitution() -> None:
    geometry = json.loads(
        (X2_DIR / "dense_geometry.json").read_text(encoding="utf-8")
    )
    intact = check_png(X2_DIR / "dense_intact.png", geometry)
    missing = check_png(X2_DIR / "dense_missing_volume.png", geometry)

    intact_panel = intact["panels"]["daily"]
    missing_panel = missing["panels"]["daily"]
    assert intact["status"] == "pass"
    assert intact_panel["volume_visible"] == intact_panel["volume_expected"] == 500
    assert missing["status"] == "fail"
    assert missing_panel["volume_visible"] == 499
    assert missing_panel["volume_expected"] == 500
    assert "volume:2023-12-19" in missing_panel["failures"]


def _line_geometry(*, fixed_tail: bool = False) -> dict:
    panel = {
        "price_axes_px": [1, 1, 28, 10],
        "volume_axes_px": [1, 10, 28, 28],
        "price_scale": "linear",
        "mode": "line",
        "line_rgb": BLUE,
        "slot_px": 4.0,
        "volume_baseline_y_px": 26.0,
        "last_bar_right_px": 9.5,
        "bars": [
            {
                "key": "2026-01-01T09:31:00+08:00",
                "x_px": 5.0,
                "close_y_px": 5.0,
                "high_y_px": None,
                "low_y_px": None,
                "vol_top_y_px": 12.0,
                "vol": 100.0,
                "candle_rgb": BLUE,
                "volume_rgb": PALE_RED,
            },
            {
                "key": "2026-01-01T09:32:00+08:00",
                "x_px": 9.0,
                "close_y_px": 6.0,
                "high_y_px": None,
                "low_y_px": None,
                "vol_top_y_px": 14.0,
                "vol": 120.0,
                "candle_rgb": BLUE,
                "volume_rgb": PALE_RED,
            },
        ],
        "gap_marks": [],
        "annotations": [],
    }
    if fixed_tail:
        panel["fixed_slot_right_px"] = 27.0
        panel["fixed_slot_boundary_rgb"] = [RED, [44, 160, 44]]
    return {
        "geometry_version": "geom.v1",
        "canvas": {"width_px": 30, "height_px": 30},
        "panels": {"intraday": panel},
    }


def _write_line_image(path: Path, *, line: bool = True, tail_dot: bool = False) -> None:
    image = Image.new("RGB", (30, 30), "white")
    draw = ImageDraw.Draw(image)
    if line:
        draw.line((5, 5, 9, 6), fill=tuple(BLUE), width=1)
        draw.line((5, 12, 5, 24), fill=tuple(PALE_RED), width=1)
        draw.line((9, 14, 9, 24), fill=tuple(PALE_RED), width=1)
    if tail_dot:
        draw.point((12, 4), fill=tuple(RED))
    image.save(path)


def test_checker_validates_line_points_and_volume_bars(tmp_path: Path) -> None:
    path = tmp_path / "line.png"
    _write_line_image(path)
    result = check_png(path, _line_geometry())
    panel = result["panels"]["intraday"]
    assert result["status"] == "pass"
    assert panel["mode"] == "line"
    assert panel["line_visible"] == panel["volume_visible"] == panel["bars"] == 2


def test_checker_detects_missing_line_points_and_fixed_slot_tail_pixels(tmp_path: Path) -> None:
    missing = tmp_path / "missing-line.png"
    _write_line_image(missing, line=False)
    result = check_png(missing, _line_geometry())
    assert result["status"] == "fail"
    assert result["panels"]["intraday"]["line_visible"] == 0

    tail = tmp_path / "tail.png"
    _write_line_image(tail, tail_dot=True)
    result = check_png(tail, _line_geometry(fixed_tail=True))
    panel = result["panels"]["intraday"]
    assert result["status"] == "fail"
    assert panel["colored_in_fixed_slot_tail"] == 1
