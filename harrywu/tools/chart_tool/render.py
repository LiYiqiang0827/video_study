"""Render canonical daily ``ChartView`` objects as full-resolution PNGs."""
from __future__ import annotations

from bisect import bisect_right
from datetime import date
from math import ceil
from pathlib import Path
from time import perf_counter
from typing import Any
import os
import re
import uuid

import matplotlib
import matplotlib.colors as mcolors
import numpy as np
import pandas as pd
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.collections import LineCollection, PolyCollection
from matplotlib.figure import Figure
from matplotlib.ticker import FixedFormatter, FixedLocator, FuncFormatter, StrMethodFormatter
from PIL import Image

from .contracts import ChartError, ChartView, RenderManifest, RenderOptions
from .paths import writable_path


_VOLUME_DISPLAY_DIVISOR = 10_000.0
_PLOT_FRAC = 0.965 - 0.075
_MA_COLORS = ("#d5a600", "#2878b5", "#8752a1", "#dc6b2f", "#397d5b")
_WARNING_TRANSLATIONS = {
    "SYNTHETIC_ENGINEERING_ONLY": "工程合成样例，非真实证据",
    "INTRADAY_DAY_OMITTED": "截止在收盘前：当日K线未显示",
    "DAILY_GAPS_UNEXPLAINED": "有交易日无数据：已标出，未补造",
    "CALENDAR_UNAVAILABLE": "部分区间缺交易日历：缺口原因无法核实",
    "DENSE_MODE_BODIES_OMITTED": "K线过密：只画高低线",
    "DISPLAY_ZOOM_WITHIN_CUTOFF": "局部显示，不越过截止点",
    "VOLUME_MIN_1PX_DISPLAY": "极小成交量按1像素显示",
}
_WARNING_ORDER = tuple(_WARNING_TRANSLATIONS)
_STAGE_LABELS = {
    "restore": "还原阶段（只含截止前数据）",
    "reveal": "揭晓阶段（含提及后走势）",
    "study": "独立学习",
}
_CUTOFF_KIND_LABELS = {
    "screen_clock": "屏幕时钟",
    "live_offset": "开播时间+视频偏移",
    "last_formed_bar": "最后成形K线",
    "manual": "人工",
    "study_end": "学习终点",
}
_CONFIDENCE_LABELS = {"high": "高", "medium": "中", "low": "低"}
_ANNOTATION_STYLES = {
    "mention": {"color": "#7c3aed", "linestyle": "-", "linewidth_px": 1.2},
    "reference": {"color": "#0f766e", "linestyle": "--", "linewidth_px": 1.0},
    "horizon": {"color": "#9ca3af", "linestyle": ":", "linewidth_px": 1.0},
    "note": {"color": "#374151", "linestyle": "-", "linewidth_px": 1.0},
}


def _bar_colors(bars: pd.DataFrame, options: RenderOptions) -> list[str]:
    """Doji candles use the configured rising color."""
    return [
        options.up_color if float(close) >= float(open_) else options.down_color
        for open_, close in zip(bars["open"], bars["close"])
    ]


def _rgb(color: str) -> tuple[int, int, int]:
    return tuple(int(round(channel * 255)) for channel in mcolors.to_rgb(color))


def _blended_volume_rgb(color: str) -> tuple[int, int, int]:
    base = _rgb(color)
    alpha = 0.78
    return tuple(int(round(channel * alpha + 255 * (1.0 - alpha))) for channel in base)


def _historical_name(metadata: dict[str, Any]) -> str | None:
    label = metadata.get("labels", {}).get("name")
    if not label:
        return None
    value = label.get("value")
    if isinstance(value, list):
        parts = [str(part).strip() for part in value if str(part).strip()]
        return " / ".join(parts) or None
    text = str(value).strip() if value is not None else ""
    return text or None


def _warning_order_key(value: str, original_index: int) -> tuple[int, int, str]:
    code = value.split(":", 1)[0]
    try:
        return (_WARNING_ORDER.index(code), 0, "")
    except ValueError:
        if re.match(r"MA\d+_PREFIX_INSUFFICIENT", code):
            return (len(_WARNING_ORDER), 0, code)
        return (len(_WARNING_ORDER) + 1, original_index, code)


def _warning_tip(value: str) -> str:
    code = value.split(":", 1)[0]
    if code in _WARNING_TRANSLATIONS:
        return _WARNING_TRANSLATIONS[code]
    match = re.match(r"MA(\d+)_PREFIX_INSUFFICIENT: first (\d+)", value)
    if match:
        return f"MA{match.group(1)} 前 {match.group(2)} 根无值"
    return value


def _time_of_day_label(requested: str) -> str:
    hour, minute = int(requested[11:13]), int(requested[14:16])
    at = hour * 60 + minute
    if at < 9 * 60 + 25:
        return "盘前"
    if at < 9 * 60 + 30:
        return "竞价后"
    if at < 11 * 60 + 30:
        return "盘中"
    if at < 13 * 60:
        return "午休"
    if at < 15 * 60:
        return "盘中"
    return "收盘后"


def _display_headers(
    view: ChartView,
    warnings: list[str],
    gap_overflow: int,
) -> tuple[str, str, str, str, bool]:
    metadata = view.metadata
    stage = metadata["stage"]
    name = _historical_name(metadata)
    first = f"{view.ts_code}{' · ' + name if name else ''} · 日线 · {_STAGE_LABELS[stage]}"
    synthetic = "SYNTHETIC_ENGINEERING_ONLY" in metadata["warnings"]
    requested = metadata["requested_cutoff"][:19].replace("T", " ")
    effective_day = metadata["effective_cutoff"][:10]
    if stage == "restore":
        source = metadata["cutoff_source"]
        second = (
            f"可见截止：{requested}（{_time_of_day_label(metadata['requested_cutoff'])}）"
            f"｜K线截至：{effective_day} 收盘｜截止依据："
            f"{_CUTOFF_KIND_LABELS.get(source['kind'], source['kind'])}"
            f"（{_CONFIDENCE_LABELS.get(source['confidence'], source['confidence'])}）"
        )
    elif stage == "study":
        second = f"数据截至：{effective_day}"
    else:
        second = f"数据截至：{effective_day}｜价格按还原阶段基准日复权"

    first_bar = str(view.bars["trade_date"].iloc[0])
    last_bar = str(view.bars["trade_date"].iloc[-1])
    localized = "（局部）" if "DISPLAY_ZOOM_WITHIN_CUTOFF" in warnings else ""
    anchor = metadata["factor_lineage"]["anchor_trade_date"]
    third = (
        f"显示：{first_bar} 至 {last_bar}，共 {len(view.bars):,} 根{localized}｜"
        f"前复权，基准日 {anchor}｜成交量为真实量，单位万股"
    )

    ordered = [value for _, value in sorted(enumerate(dict.fromkeys(warnings)), key=lambda pair: _warning_order_key(pair[1], pair[0]))]
    shown = [_warning_tip(value) for value in ordered[:3]]
    overflow = max(0, len(ordered) - 3)
    if overflow:
        shown.append(f"另有 {overflow} 条，见清单")
    if gap_overflow:
        shown.append(f"另有 {gap_overflow} 处缺口")
    fourth = "提示：" + ("；".join(shown) if shown else "无")
    return first, second, third, fourth, synthetic


def _set_date_ticks(volume_ax: Any, trade_dates: list[str], n: int) -> None:
    parsed = [date.fromisoformat(value) for value in trade_dates]
    if n <= 60:
        step = max(1, ceil(n / 10))
        positions = list(range(0, n, step))
        labels = [parsed[index].isoformat() if index == 0 else parsed[index].strftime("%m-%d") for index in positions]
        minor_positions: list[int] = []
    elif n <= 520:
        positions = []
        month_labels = []
        last_key: tuple[int, int] | None = None
        for index, day in enumerate(parsed):
            key = (day.year, day.month)
            if key != last_key:
                positions.append(index)
                month_labels.append(f"{day.year:04d}-{day.month:02d}")
                last_key = key
        if len(positions) > 24:
            positions = positions[::2]
            month_labels = month_labels[::2]
        labels = month_labels
        minor_positions = []
    else:
        positions = []
        labels = []
        minor_positions = []
        last_year: int | None = None
        last_quarter: tuple[int, int] | None = None
        for index, day in enumerate(parsed):
            quarter = (day.month - 1) // 3 + 1
            if day.year != last_year:
                positions.append(index)
                labels.append(f"{day.year:04d}")
                last_year = day.year
            if (day.year, quarter) != last_quarter:
                if index not in positions:
                    minor_positions.append(index)
                last_quarter = (day.year, quarter)
    volume_ax.xaxis.set_major_locator(FixedLocator(positions))
    volume_ax.xaxis.set_major_formatter(FixedFormatter(labels))
    volume_ax.xaxis.set_minor_locator(FixedLocator(minor_positions))
    volume_ax.tick_params(axis="x", which="minor", length=2, color="#8693a3")


def _gap_records(view: ChartView) -> tuple[list[dict[str, Any]], int]:
    dates = [str(value) for value in view.bars["trade_date"].tolist()]
    metadata = view.metadata
    groups: dict[int, list[str]] = {}
    for value in metadata["gap_dates"]:
        index = bisect_right(dates, value) - 1
        if 0 <= index < len(dates) - 1 and dates[index] < value < dates[index + 1]:
            groups.setdefault(index, []).append(value)

    marks: dict[int, dict[str, Any]] = {}
    for index, missing_days in groups.items():
        gap_count = len(missing_days)
        label = f"缺 {gap_count} 个交易日（原因未核实）" if gap_count >= 5 else None
        marks[index] = {
            "index": index,
            "x": index + 0.5,
            "days": (date.fromisoformat(dates[index + 1]) - date.fromisoformat(dates[index])).days,
            "label": label,
        }

    if "CALENDAR_UNAVAILABLE" in metadata["warnings"]:
        for index in range(len(dates) - 1):
            if index in marks:
                continue
            days = (date.fromisoformat(dates[index + 1]) - date.fromisoformat(dates[index])).days
            if days > 20:
                marks[index] = {
                    "index": index,
                    "x": index + 0.5,
                    "days": days,
                    "label": f"间隔 {days} 天（无交易日历，原因未核实）",
                }

    labels = sorted(
        (mark for mark in marks.values() if mark["label"] is not None),
        key=lambda mark: (-mark["days"], mark["index"]),
    )
    keep = {mark["index"] for mark in labels[:10]}
    overflow = max(0, len(labels) - 10)
    result = []
    for index in sorted(marks):
        record = dict(marks[index])
        if record["index"] not in keep:
            record["label"] = None
        result.append(record)
    return result, overflow


def _annotation_date(value: str) -> str:
    return value[:10]


def _label_alignment(x: float, n: int) -> tuple[float, str]:
    return (-6.0, "right") if x >= n - 4 else (6.0, "left")


def _build_figure(
    view: ChartView,
    options: RenderOptions,
    width_px: int | None = None,
) -> tuple[Figure, FigureCanvasAgg, dict[str, Any]]:
    bars = view.bars
    metadata = view.metadata
    n = len(bars)
    width_px = options.width_px if width_px is None else width_px
    x = np.arange(n, dtype=float)
    slot_px = _PLOT_FRAC * width_px / (n + 0.2)
    mode = "normal" if slot_px >= 3.0 else "dense"
    candle_hex = _bar_colors(bars, options)
    candle_rgb = [_rgb(color) for color in candle_hex]
    volume_rgb = [
        _blended_volume_rgb(color) if mode == "normal" else _rgb(color)
        for color in candle_hex
    ]
    body_width_px = max(1, round(0.7 * slot_px)) if mode == "normal" else 1
    body_width_data = body_width_px / slot_px
    wick_linewidth = 72.0 / options.dpi

    figure = Figure(
        figsize=(width_px / options.dpi, options.height_px / options.dpi),
        dpi=options.dpi,
        facecolor="#ffffff",
    )
    canvas = FigureCanvasAgg(figure)
    grid = figure.add_gridspec(
        2,
        1,
        height_ratios=(3, 1),
        left=0.075,
        right=0.965,
        bottom=0.095,
        top=0.875,
        hspace=0.035,
    )
    price_ax = figure.add_subplot(grid[0, 0])
    volume_ax = figure.add_subplot(grid[1, 0], sharex=price_ax)
    for axis in (price_ax, volume_ax):
        axis.set_facecolor("#ffffff")
        axis.set_axisbelow(True)
        axis.grid(True, which="major", color="#d9dfe7", linewidth=0.55, alpha=0.82)
        axis.tick_params(axis="both", labelsize=8, colors="#334155", length=3)
        axis.spines["top"].set_visible(False)
        axis.spines["right"].set_visible(False)
        axis.spines["left"].set_color("#aab4c0")
        axis.spines["bottom"].set_color("#aab4c0")

    lows = bars["low"].to_numpy(dtype=float)
    highs = bars["high"].to_numpy(dtype=float)
    opens = bars["open"].to_numpy(dtype=float)
    closes = bars["close"].to_numpy(dtype=float)
    values = bars["vol"].to_numpy(dtype=float)
    trade_dates = [str(value) for value in bars["trade_date"].tolist()]

    wick_segments = [((center, low), (center, high)) for center, low, high in zip(x, lows, highs)]
    price_ax.add_collection(
        LineCollection(
            wick_segments,
            colors=candle_hex,
            linewidths=wick_linewidth,
            antialiaseds=False,
            zorder=3,
        )
    )
    if mode == "dense":
        flat_ranges = np.flatnonzero(highs == lows)
        if len(flat_ranges):
            # A zero-height wick is a single price point. Agg drops a degenerate
            # line, so represent that point with one pixel at the same coordinate.
            price_ax.scatter(
                x[flat_ranges],
                highs[flat_ranges],
                s=(72.0 / options.dpi) ** 2,
                marker="s",
                c=[candle_hex[index] for index in flat_ranges],
                linewidths=0.0,
                antialiaseds=False,
                zorder=3,
            )
    if mode == "normal":
        body_polygons: list[list[tuple[float, float]]] = []
        body_colors: list[str] = []
        doji_segments: list[list[tuple[float, float]]] = []
        doji_colors: list[str] = []
        for center, open_, close, color in zip(x, opens, closes, candle_hex):
            left, right = center - body_width_data / 2.0, center + body_width_data / 2.0
            if open_ == close:
                doji_segments.append([(left, float(open_)), (right, float(open_))])
                doji_colors.append(color)
            else:
                low, high = sorted((float(open_), float(close)))
                body_polygons.append([(left, low), (left, high), (right, high), (right, low)])
                body_colors.append(color)
        if body_polygons:
            price_ax.add_collection(
                PolyCollection(
                    body_polygons,
                    closed=True,
                    facecolors=body_colors,
                    edgecolors="none",
                    linewidths=0.0,
                    antialiaseds=False,
                    zorder=4,
                )
            )
        if doji_segments:
            price_ax.add_collection(
                LineCollection(
                    doji_segments,
                    colors=doji_colors,
                    linewidths=wick_linewidth,
                    antialiaseds=False,
                    zorder=5,
                )
            )

    for period_index, period in enumerate(options.ma_periods):
        rolling = bars["close"].rolling(window=period, min_periods=period).mean()
        price_ax.plot(
            x,
            rolling.to_numpy(dtype=float),
            color=_MA_COLORS[period_index % len(_MA_COLORS)],
            linewidth=1.05,
            label=f"MA{period}",
            zorder=6,
        )

    volume_max = float(values.max()) / _VOLUME_DISPLAY_DIVISOR if n else 0.0
    volume_ylim_top = volume_max * 1.06 if volume_max > 0 else 1.0
    volume_ax.set_ylim(0.0, volume_ylim_top)
    volume_ax_height_px = volume_ax.get_position().height * options.height_px
    min_volume_height_data = volume_ylim_top * 1.25 / volume_ax_height_px
    volume_heights = values / _VOLUME_DISPLAY_DIVISOR
    small_volume_mask = (values > 0) & (volume_heights < min_volume_height_data)
    if small_volume_mask.any():
        volume_heights = volume_heights.copy()
        volume_heights[small_volume_mask] = min_volume_height_data
    if mode == "normal":
        polygons = [
            [
                (center - body_width_data / 2.0, 0.0),
                (center - body_width_data / 2.0, height),
                (center + body_width_data / 2.0, height),
                (center + body_width_data / 2.0, 0.0),
            ]
            for center, height in zip(x, volume_heights)
        ]
        volume_ax.add_collection(
            PolyCollection(
                polygons,
                closed=True,
                facecolors=[tuple(channel / 255 for channel in color) for color in volume_rgb],
                edgecolors="none",
                linewidths=0.0,
                antialiaseds=False,
                zorder=3,
            )
        )
    else:
        volume_ax.add_collection(
            LineCollection(
                [((center, 0.0), (center, height)) for center, height in zip(x, volume_heights)],
                colors=candle_hex,
                linewidths=wick_linewidth,
                antialiaseds=False,
                zorder=3,
            )
        )

    zero_segments = [
        [
            (center - body_width_data / 2.0, 0.0),
            (center + body_width_data / 2.0, 0.0),
        ]
        for center, value in zip(x, values)
        if value == 0.0
    ]
    if zero_segments:
        zero_collection = LineCollection(
            zero_segments,
            colors="#334155",
            linewidths=wick_linewidth,
            antialiaseds=False,
            label="成交量为零",
            zorder=5,
        )
        volume_ax.add_collection(zero_collection)
        volume_ax.legend(
            loc="upper left",
            fontsize=7.5,
            frameon=True,
            framealpha=0.87,
            facecolor="#ffffff",
            edgecolor="#d9dfe7",
        )

    base_price_min, base_price_max = float(lows.min()), float(highs.max())
    if options.price_scale == "log":
        if base_price_min <= 0:
            raise ChartError("RENDER_INVALID_PRICE", "Log price scale requires positive prices")
        price_min = base_price_min / 1.03
        price_max = base_price_max * 1.03
        price_ax.set_yscale("log")
    else:
        span = base_price_max - base_price_min
        pad = span * 0.055 if span > 0 else max(abs(base_price_max) * 0.012, 0.01)
        price_min, price_max = base_price_min - pad, base_price_max + pad

    annotation_indices: dict[int, int] = {}
    for annotation_index, annotation in enumerate(options.annotations):
        if annotation["kind"] in {"vline", "marker"}:
            requested_date = _annotation_date(annotation["at"])
            if requested_date not in trade_dates:
                raise ChartError(
                    "ANNOTATION_OUT_OF_VIEW",
                    "Annotation date is not present in the displayed view",
                    details={"at": annotation["at"]},
                )
            annotation_indices[annotation_index] = trade_dates.index(requested_date)
        elif annotation["kind"] == "hline":
            price = float(annotation["price"])
            if price < price_min:
                price_min = price / 1.03 if options.price_scale == "log" else price
            if price > price_max:
                price_max = price * 1.03 if options.price_scale == "log" else price

    if options.price_scale == "linear":
        expanded_min = min(price_min, min(float(a["price"]) for a in options.annotations if a["kind"] == "hline") if any(a["kind"] == "hline" for a in options.annotations) else price_min)
        expanded_max = max(price_max, max(float(a["price"]) for a in options.annotations if a["kind"] == "hline") if any(a["kind"] == "hline" for a in options.annotations) else price_max)
        span = expanded_max - expanded_min
        pad = span * 0.055 if span > 0 else max(abs(expanded_max) * 0.012, 0.01)
        price_min, price_max = expanded_min - pad, expanded_max + pad
    price_ax.set_ylim(price_min, price_max)
    price_ax.set_xlim(-0.6, n - 0.4)
    volume_ax.set_xlim(-0.6, n - 0.4)

    if options.price_scale == "log":
        price_formatter = FuncFormatter(lambda value, _: f"{value:,.2f}")
        price_ax.yaxis.set_major_formatter(price_formatter)
        price_ax.yaxis.set_minor_formatter(price_formatter)
    else:
        price_ax.yaxis.set_major_formatter(StrMethodFormatter("{x:,.2f}"))
    volume_ax.yaxis.set_major_formatter(
        FuncFormatter(lambda value, _: f"{value:,.1f}" if 0 < abs(value) < 10 else f"{value:,.0f}")
    )
    _set_date_ticks(volume_ax, trade_dates, n)
    price_ax.tick_params(axis="x", labelbottom=False)
    price_ax.set_ylabel("价格（元）", fontsize=9, color="#243247", labelpad=9)
    volume_ax.set_ylabel("成交量（万股）", fontsize=9, color="#243247", labelpad=9)
    volume_ax.set_xlabel("交易日期", fontsize=9, color="#243247", labelpad=7)

    gap_records, gap_overflow = _gap_records(view)
    for mark in gap_records:
        for axis in (price_ax, volume_ax):
            axis.axvline(
                mark["x"],
                color="#6b7280",
                linewidth=0.75,
                linestyle=(0, (3, 3)),
                alpha=0.6,
                zorder=1,
            )
        if mark["label"] is not None:
            price_ax.text(
                mark["x"],
                0.995,
                mark["label"],
                transform=price_ax.get_xaxis_transform(),
                ha="center",
                va="top",
                fontsize=7.0,
                color="#374151",
                clip_on=False,
                zorder=10,
            )

    annotation_geometry: list[dict[str, Any]] = []
    for annotation_index, annotation in enumerate(options.annotations):
        kind, style = annotation["kind"], annotation["style"]
        style_info = _ANNOTATION_STYLES[style]
        color = style_info["color"]
        linewidth = style_info["linewidth_px"] * 72.0 / options.dpi
        text_value = annotation["text"]
        if kind == "vline":
            bar_index = annotation_indices[annotation_index]
            line_x = bar_index - 0.5
            for axis in (price_ax, volume_ax):
                axis.axvline(line_x, color=color, linestyle=style_info["linestyle"], linewidth=linewidth, zorder=8)
            dx, align = _label_alignment(line_x, n)
            if style == "horizon":
                price_ax.annotate(
                    text_value,
                    (line_x, 0.995),
                    xycoords=price_ax.get_xaxis_transform(),
                    xytext=(dx, 0),
                    textcoords="offset points",
                    ha=align,
                    va="top",
                    fontsize=7.5,
                    color=color,
                    clip_on=False,
                    zorder=11,
                )
            else:
                price_ax.annotate(
                    text_value,
                    (line_x, 0.98),
                    xycoords=price_ax.get_xaxis_transform(),
                    xytext=(dx, 0),
                    textcoords="offset points",
                    ha=align,
                    va="top",
                    fontsize=7.5,
                    color=color,
                    clip_on=False,
                    zorder=11,
                )
            annotation_geometry.append({"kind": kind, "x_data": line_x, "y_data": None, "text": text_value})
        elif kind == "marker":
            bar_index = annotation_indices[annotation_index]
            price = float(annotation["price"])
            price_ax.scatter(
                [bar_index], [price], s=26, marker="o", color=color, edgecolors=color, linewidths=0.6, zorder=9
            )
            dx, align = _label_alignment(float(bar_index), n)
            price_ax.annotate(
                text_value,
                (bar_index, price),
                xytext=(dx, 4),
                textcoords="offset points",
                ha=align,
                va="bottom",
                fontsize=7.5,
                color=color,
                clip_on=False,
                zorder=10,
            )
            annotation_geometry.append({"kind": kind, "x_data": float(bar_index), "y_data": price, "text": text_value})
        else:
            price = float(annotation["price"])
            price_ax.axhline(price, color=color, linestyle=style_info["linestyle"], linewidth=linewidth, zorder=7)
            if style == "horizon":
                text_y = 0.995
                transform = price_ax.get_xaxis_transform()
                position = (-0.55, text_y)
            else:
                transform = price_ax.transData
                position = (-0.55, price)
            price_ax.text(
                position[0],
                position[1],
                text_value,
                transform=transform,
                ha="left",
                va="top" if style == "horizon" else "bottom",
                fontsize=7.5,
                color=color,
                clip_on=False,
                zorder=10,
            )
            annotation_geometry.append({"kind": kind, "x_data": None, "y_data": price, "text": text_value})

    warnings = list(dict.fromkeys(metadata["warnings"]))
    if mode == "dense":
        warnings.append("DENSE_MODE_BODIES_OMITTED")
    if small_volume_mask.any():
        warnings.append("VOLUME_MIN_1PX_DISPLAY")
    for period in options.ma_periods:
        missing_prefix = min(max(period - 1, 0), n)
        if missing_prefix:
            warnings.append(
                f"MA{period}_PREFIX_INSUFFICIENT: first {missing_prefix} visible bar(s) have no {period}-close history"
            )
    warnings = list(dict.fromkeys(warnings))

    header_lines = _display_headers(view, warnings, gap_overflow)
    title_artist = figure.text(
        0.076,
        0.974,
        header_lines[0],
        ha="left",
        va="top",
        fontsize=14,
        fontweight="bold",
        color="#172033",
    )
    figure.text(0.076, 0.947, header_lines[1], ha="left", va="top", fontsize=8.2, color="#344256")
    figure.text(0.076, 0.926, header_lines[2], ha="left", va="top", fontsize=8.2, color="#344256")
    figure.text(0.076, 0.905, header_lines[3], ha="left", va="top", fontsize=7.8, color="#566274")
    if header_lines[4]:
        canvas.draw()
        title_bbox = title_artist.get_window_extent(canvas.get_renderer())
        figure.text(
            title_bbox.x1 / width_px + 8.0 / width_px,
            0.974,
            "【工程合成样例，非真实证据】",
            ha="left",
            va="top",
            fontsize=14,
            fontweight="bold",
            color="#b91c1c",
        )

    state = {
        "price_ax": price_ax,
        "volume_ax": volume_ax,
        "x": x,
        "lows": lows,
        "highs": highs,
        "volume_heights": volume_heights,
        "values": values,
        "trade_dates": trade_dates,
        "candle_rgb": candle_rgb,
        "volume_rgb": volume_rgb,
        "mode": mode,
        "slot_px": slot_px,
        "body_width_px": body_width_px,
        "gap_records": gap_records,
        "annotations": annotation_geometry,
        "warnings": warnings,
    }
    return figure, canvas, state


def _axes_px(axis: Any, height_px: int) -> list[float]:
    box = axis.get_window_extent()
    return [float(box.x0), float(height_px - box.y1), float(box.x1), float(height_px - box.y0)]


def _data_px(axis: Any, x: float, y: float, height_px: int) -> tuple[float, float]:
    x_display, y_display = axis.transData.transform((x, y))
    return float(x_display), float(height_px - y_display)


def _geometry(view: ChartView, options: RenderOptions, width_px: int, canvas: FigureCanvasAgg, state: dict[str, Any]) -> dict:
    actual_width, actual_height = canvas.get_width_height()
    price_ax = state["price_ax"]
    volume_ax = state["volume_ax"]
    baseline_x, baseline_display_y = volume_ax.transData.transform((0.0, 0.0))
    baseline_y = float(actual_height - baseline_display_y)
    bars = []
    for index, row in view.bars.iterrows():
        x, _ = _data_px(price_ax, float(index), float(row["close"]), actual_height)
        _, high_y = _data_px(price_ax, float(index), float(row["high"]), actual_height)
        _, low_y = _data_px(price_ax, float(index), float(row["low"]), actual_height)
        _, volume_top_y = _data_px(volume_ax, float(index), float(state["volume_heights"][index]), actual_height)
        bars.append(
            {
                "key": str(row["trade_date"]),
                "x_px": x,
                "high_y_px": high_y,
                "low_y_px": low_y,
                "vol_top_y_px": volume_top_y,
                "vol": float(state["values"][index]),
                "candle_rgb": list(state["candle_rgb"][index]),
                "volume_rgb": list(state["volume_rgb"][index]),
            }
        )
    last_right = bars[-1]["x_px"] + (state["body_width_px"] / 2.0 if state["mode"] == "normal" else 0.5)
    gap_marks = []
    for record in state["gap_records"]:
        gap_x, _ = _data_px(price_ax, float(record["x"]), float(view.bars["close"].iloc[record["index"]]), actual_height)
        gap_marks.append({"x_px": gap_x, "label": record["label"]})
    annotations = []
    for record in state["annotations"]:
        kind = record["kind"]
        x_data, y_data = record["x_data"], record["y_data"]
        x_px = None
        y_px = None
        if x_data is not None:
            x_px, _ = _data_px(price_ax, float(x_data), float(view.bars["close"].iloc[0]), actual_height)
        if y_data is not None:
            _, y_px = _data_px(price_ax, float(view.bars["trade_date"].index[0]) if False else 0.0, float(y_data), actual_height)
        annotations.append({"kind": kind, "x_px": x_px, "y_px": y_px, "text": record["text"]})
    return {
        "geometry_version": "geom.v1",
        "canvas": {"width_px": int(actual_width), "height_px": int(actual_height)},
        "background": "#ffffff",
        "panels": {
            "daily": {
                "price_axes_px": _axes_px(price_ax, actual_height),
                "volume_axes_px": _axes_px(volume_ax, actual_height),
                "price_scale": options.price_scale,
                "mode": state["mode"],
                "slot_px": float(state["slot_px"]),
                "volume_baseline_y_px": baseline_y,
                "last_bar_right_px": float(last_right),
                "bars": bars,
                "gap_marks": gap_marks,
                "annotations": annotations,
            }
        },
    }


def render_png_with_geometry(
    view: ChartView,
    options: RenderOptions,
    output_path: str | Path,
) -> tuple[RenderManifest, dict]:
    """Render one daily view and return both its manifest and pixel geometry."""
    view.validate()
    options.validate()
    if view.period != "daily":
        raise ChartError(
            "UNSUPPORTED_PERIOD",
            "The daily PNG renderer supports period='daily' only",
            details={"period": view.period},
        )

    identity = view.identity()
    row_count = len(view.bars)
    empty_geometry = {
        "geometry_version": "geom.v1",
        "canvas": {"width_px": options.width_px, "height_px": options.height_px},
        "background": "#ffffff",
        "panels": {},
    }
    if row_count == 0:
        if Path(output_path).exists():
            raise ChartError("OUTPUT_PATH", "Empty output cannot reuse an existing PNG path")
        return (
            RenderManifest(
                ts_code=view.ts_code,
                period=view.period,
                status="empty",
                output_path=None,
                input_rows=0,
                candle_count=0,
                volume_bar_count=0,
                actual_start=None,
                actual_end=None,
                view_identity=identity,
                timings={"drawing_seconds": 0.0, "encoding_seconds": 0.0},
                warnings=tuple(view.metadata["warnings"]),
            ),
            empty_geometry,
        )

    needed_width = ceil((row_count + 0.2) * options.min_slot_px / _PLOT_FRAC)
    width_px = (
        min(max(options.width_px, needed_width), max(options.max_width_px, options.width_px))
        if options.auto_width
        else options.width_px
    )
    slot_px = _PLOT_FRAC * width_px / (row_count + 0.2)
    if slot_px < 1.0:
        required_width = ceil((row_count + 0.2) / _PLOT_FRAC)
        raise ChartError(
            "RENDER_TOO_DENSE",
            "The requested canvas cannot assign one pixel to every daily bar",
            details={
                "required_width_px": required_width,
                "actual_width_px": width_px,
                "actual_slot_px": slot_px,
                "max_width_px": max(options.max_width_px, options.width_px),
            },
        )

    target = writable_path(output_path, suffix=".png")
    target.parent.mkdir(parents=True, exist_ok=True)
    draw_started = perf_counter()
    with matplotlib.rc_context(
        {
            "font.family": [options.font_family],
            "axes.unicode_minus": False,
            "figure.facecolor": "#ffffff",
            "savefig.facecolor": "#ffffff",
        }
    ):
        figure, canvas, state = _build_figure(view, options, width_px)
        canvas.draw()
        geometry = _geometry(view, options, width_px, canvas, state)
    drawing_seconds = perf_counter() - draw_started

    encode_started = perf_counter()
    rgba = canvas.buffer_rgba()
    canvas_width, canvas_height = canvas.get_width_height()
    image = Image.frombuffer(
        "RGBA",
        (canvas_width, canvas_height),
        rgba,
        "raw",
        "RGBA",
        0,
        1,
    )
    temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        image.save(temporary, format="PNG", compress_level=6)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    encoding_seconds = perf_counter() - encode_started
    figure.clear()

    bars = view.bars
    return (
        RenderManifest(
            ts_code=view.ts_code,
            period=view.period,
            status="ok",
            output_path=str(target),
            input_rows=row_count,
            candle_count=row_count,
            volume_bar_count=row_count,
            actual_start=str(bars["trade_date"].iloc[0]),
            actual_end=str(bars["trade_date"].iloc[-1]),
            view_identity=identity,
            timings={"drawing_seconds": drawing_seconds, "encoding_seconds": encoding_seconds},
            warnings=tuple(state["warnings"]),
        ),
        geometry,
    )


def render_png(
    view: ChartView,
    options: RenderOptions,
    output_path: str | Path,
) -> RenderManifest:
    """Render a daily PNG and return its value-level manifest."""
    manifest, _ = render_png_with_geometry(view, options, output_path)
    return manifest
