"""Pixel-level checks for renderer geometry, using PIL only."""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from PIL import Image


def _rgb(value: Any) -> tuple[int, int, int]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError("geometry colors must be RGB triples")
    result = tuple(int(channel) for channel in value)
    if any(channel < 0 or channel > 255 for channel in result):
        raise ValueError("geometry RGB channels must be between 0 and 255")
    return result  # type: ignore[return-value]


def _hit(pixel: tuple[int, int, int], colors: set[tuple[int, int, int]]) -> bool:
    return any(sum(abs(pixel[channel] - color[channel]) for channel in range(3)) <= 90 for color in colors)


def _rows_for_span(top: float, bottom: float, height: int, *, include_bottom: bool) -> range:
    first = max(0, min(height - 1, int(round(top))))
    last = int(round(bottom)) - (0 if include_bottom else 1)
    last = max(-1, min(height - 1, last))
    return range(first, last + 1) if last >= first else range(0)


def _right_boundary_count(
    pixels: Any,
    image_width: int,
    image_height: int,
    axes: list[float],
    start_x: int,
    colors: set[tuple[int, int, int]],
) -> int:
    left, top, right, bottom = (float(value) for value in axes)
    x0 = max(0, start_x, int(math.ceil(left)))
    x1 = min(image_width - 1, int(math.floor(right)) - 2)
    y0 = max(0, int(math.ceil(top)))
    y1 = min(image_height - 1, int(math.ceil(bottom)) - 1)
    if x1 < x0 or y1 < y0:
        return 0
    count = 0
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            if _hit(pixels[x, y], colors):
                count += 1
    return count


def check_png(png_path: str | Path, geometry: dict) -> dict:
    """Check candle or line visibility, volume bars, and unused right space."""
    with Image.open(png_path) as source:
        image = source.convert("RGB")
        width, height = image.size
        if geometry.get("geometry_version") != "geom.v1":
            raise ValueError("unsupported geometry version")
        expected_size = geometry.get("canvas", {})
        if (expected_size.get("width_px"), expected_size.get("height_px")) != (width, height):
            raise ValueError("PNG dimensions do not match geometry canvas")
        pixels = image.load()
        panel_results: dict[str, dict] = {}
        for panel_name, panel in geometry.get("panels", {}).items():
            bars = panel.get("bars", [])
            mode = panel.get("mode", "normal")
            if not bars:
                panel_results[panel_name] = {
                    "status": "skipped",
                    "bars": 0,
                    "volume_expected": 0,
                    "volume_visible": 0,
                    "candle_visible": 0,
                    "line_visible": 0,
                    "colored_right_of_last_bar": 0,
                    "duplicate_columns": 0,
                    "failures": [],
                }
                continue
            failures: list[str] = []
            candle_colors = {_rgb(bar["candle_rgb"]) for bar in bars}
            volume_colors = {_rgb(bar["volume_rgb"]) for bar in bars}
            expected_colors = candle_colors | volume_colors
            candle_visible = 0
            line_visible = 0
            volume_visible = 0
            volume_expected = 0
            floor_columns: list[int] = []
            for bar in bars:
                key = str(bar.get("key", "?"))
                x = float(bar["x_px"])
                floor_x = math.floor(x)
                floor_columns.append(floor_x)
                # Dense one-pixel strokes are rasterized at their nearest
                # pixel column. Sampling the adjacent floor column can let a
                # same-colored neighbor stand in for a missing stroke.
                columns = (
                    [int(round(x))]
                    if mode == "dense"
                    else sorted({floor_x, int(round(x))})
                )
                volume_colors_for_bar = {
                    _rgb(bar["volume_rgb"]),
                    _rgb(bar["candle_rgb"]),
                }
                if mode == "line":
                    close_y = bar.get("close_y_px")
                    line_color = _rgb(panel.get("line_rgb", bar["candle_rgb"]))
                    if close_y is None:
                        failures.append(f"line_geometry:{key}")
                    else:
                        center_y = int(round(float(close_y)))
                        rows = range(max(0, center_y - 2), min(height - 1, center_y + 2) + 1)
                        line_hit = any(
                            0 <= column < width
                            and _hit(pixels[column, row], {line_color})
                            for column in columns
                            for row in rows
                        )
                        if line_hit:
                            line_visible += 1
                        else:
                            failures.append(f"line:{key}")
                else:
                    high_y, low_y = bar.get("high_y_px"), bar.get("low_y_px")
                    if high_y is None or low_y is None:
                        failures.append(f"candle_geometry:{key}")
                    else:
                        high_rows = _rows_for_span(
                            float(high_y), float(low_y), height, include_bottom=True
                        )
                        candle_hit = any(
                            0 <= column < width
                            and _hit(pixels[column, row], {_rgb(bar["candle_rgb"])})
                            for column in columns
                            for row in high_rows
                        )
                        if candle_hit:
                            candle_visible += 1
                        else:
                            failures.append(f"candle:{key}")

                if float(bar["vol"]) > 0:
                    volume_expected += 1
                    volume_rows = _rows_for_span(
                        float(bar["vol_top_y_px"]),
                        float(panel["volume_baseline_y_px"]),
                        height,
                        include_bottom=False,
                    )
                    volume_hit = any(
                        0 <= column < width
                        and _hit(pixels[column, row], volume_colors_for_bar)
                        for column in columns
                        for row in volume_rows
                    )
                    if volume_hit:
                        volume_visible += 1
                    else:
                        failures.append(f"volume:{key}")

            column_counts: dict[int, int] = {}
            for column in floor_columns:
                column_counts[column] = column_counts.get(column, 0) + 1
            duplicate_columns = sum(count - 1 for count in column_counts.values() if count > 1)
            if duplicate_columns:
                duplicated = [str(column) for column, count in column_counts.items() if count > 1]
                failures.append("duplicate_columns:" + ",".join(duplicated[:10]))

            last_right = float(panel["last_bar_right_px"])
            right_start = int(math.ceil(last_right)) + 1
            colored_right = 0
            boundary_colors = {
                _rgb(value) for value in panel.get("fixed_slot_boundary_rgb", [])
            }
            fixed_slot_right = panel.get("fixed_slot_right_px")
            if fixed_slot_right is None:
                for axes_key in ("price_axes_px", "volume_axes_px"):
                    colored_right += _right_boundary_count(
                        pixels,
                        width,
                        height,
                        panel[axes_key],
                        right_start,
                        expected_colors,
                    )
            if colored_right:
                failures.append(f"colored_right_of_last_bar:{colored_right}")

            fixed_slot_colored = 0
            if fixed_slot_right is not None and boundary_colors:
                fixed_x1 = min(width - 1, int(math.floor(float(fixed_slot_right))))
                for axes_key in ("price_axes_px", "volume_axes_px"):
                    left, top, _right, bottom = (float(value) for value in panel[axes_key])
                    x0 = max(0, right_start, int(math.ceil(left)))
                    y0 = max(0, int(math.ceil(top)))
                    y1 = min(height - 1, int(math.ceil(bottom)) - 1)
                    if fixed_x1 < x0 or y1 < y0:
                        continue
                    for y in range(y0, y1 + 1):
                        for x in range(x0, fixed_x1 + 1):
                            if _hit(pixels[x, y], boundary_colors):
                                fixed_slot_colored += 1
                if fixed_slot_colored:
                    failures.append(f"colored_in_fixed_slot_tail:{fixed_slot_colored}")

            panel_results[panel_name] = {
                "status": "pass" if not failures else "fail",
                "mode": mode,
                "bars": len(bars),
                "volume_expected": volume_expected,
                "volume_visible": volume_visible,
                "candle_visible": candle_visible,
                "line_visible": line_visible,
                "colored_right_of_last_bar": colored_right,
                "colored_in_fixed_slot_tail": fixed_slot_colored,
                "duplicate_columns": duplicate_columns,
                "failures": failures[:20],
            }

    return {
        "status": "pass" if all(panel["status"] != "fail" for panel in panel_results.values()) else "fail",
        "panels": panel_results,
    }
