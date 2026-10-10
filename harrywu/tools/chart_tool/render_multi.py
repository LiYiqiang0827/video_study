"""Render chart.v1 views on contiguous categorical and fixed-session axes."""
from __future__ import annotations

from datetime import date
import os
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

import matplotlib
import matplotlib.colors as mcolors
import numpy as np
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.collections import LineCollection, PolyCollection
from matplotlib.figure import Figure
from matplotlib.patches import Rectangle
from matplotlib.ticker import FixedFormatter, FixedLocator, FuncFormatter, StrMethodFormatter
from PIL import Image

from .contracts import ChartError, ChartView, RenderOptions, parse_timestamp
from .display import crop_last_bars, crop_sessions
from .paths import writable_path
from .render import (
    _CONFIDENCE_LABELS,
    _CUTOFF_KIND_LABELS,
    _MA_COLORS,
    _STAGE_LABELS,
    _WARNING_ORDER,
    _axes_px,
    _data_px,
    _set_date_ticks,
    _time_of_day_label,
    _warning_tip,
)


_PERIODS = ("daily", "15m", "1m", "intraday")
_LOWER_PERIODS = ("15m", "1m", "intraday")
_VOLUME_DIVISOR = 10_000.0
_INTRADAY_LINE = "#1f4e9c"
_AVERAGE_LINE = "#d5a600"
_PREVIOUS_CLOSE = "#6b7280"
_AUCTION_VOLUME = "#9ca3af"
_DAY_BOUNDARY = "#cbd5e1"
_DAY_HIGHLIGHT = "#ffd166"
_TIME_TICKS = (
    (0, "09:30"),
    (30, "10:00"),
    (60, "10:30"),
    (90, "11:00"),
    (120, "11:30/13:00"),
    (150, "13:30"),
    (180, "14:00"),
    (210, "14:30"),
    (240, "15:00"),
)
_WARNING_TRANSLATIONS = {
    "OPENING_AUCTION_FOLDED": "集合竞价已并入首个15分钟柱和当日日线",
    "AUCTION_NOT_YET_REVEALED": "截止早于09:25：本日竞价尚未揭示",
    "AUCTION_UNAVAILABLE": "分钟来源无集合竞价数据",
    "CURRENT_DAILY_FROM_OBSERVED_MINUTES": "当日K线由已观察分钟合成（未完成）",
    "CURRENT_DAILY_OBSERVED_SUBSET_NOT_OFFICIAL_DAY_OPEN": "当日开盘尚未确认：仅显示已观察分钟子集",
    "CURRENT_FACTOR_UNCHANGED_FROM_PREVIOUS": "当日复权因子与前一日相同（已核对）",
    "CURRENT_FACTOR_VERIFIED_UNCHANGED_PRIOR_ANCHOR": "当日复权因子与前一日相同（已核对）",
    "DISPLAY_SESSION_PREVIOUS": "分时显示上一交易日",
    "PREV_CLOSE_UNAVAILABLE": "缺昨收：不显示涨跌幅",
    "AVG_PRICE_UNAVAILABLE": "旧版视图无均价数据",
    "FIFTEEN_MINUTES_DERIVED_FROM_ONE_MINUTE": "15分钟由1分钟聚合",
    "OPENING_AUCTION_0930_EXCLUDED": "集合竞价未纳入（旧版口径）",
    "HISTORICAL_CALENDAR_COVERAGE_UNAVAILABLE": "历史交易日历覆盖不足",
    "CURRENT_DAILY_OBSERVED_SUBSET_NOT_OFFICIAL_DAY_OPEN": "当日开盘尚未确认：仅显示已观察分钟子集",
}
_WARNING_ORDER_MULTI = _WARNING_ORDER + tuple(_WARNING_TRANSLATIONS)


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _rgb(color: str) -> tuple[int, int, int]:
    return tuple(int(round(channel * 255)) for channel in mcolors.to_rgb(color))


def _blended_volume_rgb(color: str) -> tuple[int, int, int]:
    base = _rgb(color)
    alpha = 0.78
    return tuple(int(round(channel * alpha + 255 * (1.0 - alpha))) for channel in base)


def _bar_color(open_: float, close: float, options: RenderOptions) -> str:
    return options.up_color if close >= open_ else options.down_color


def _minute_slot(bar_end: str) -> int:
    """Map one-minute bar ends to the 240-slot Chinese session grid."""
    end = parse_timestamp(bar_end)
    minute = end.hour * 60 + end.minute
    if 9 * 60 + 31 <= minute <= 11 * 60 + 30:
        return minute - (9 * 60 + 30)
    if 13 * 60 + 1 <= minute <= 15 * 60:
        return 120 + minute - 13 * 60
    raise ChartError(
        "INVALID_SCHEMA",
        "Minute bar end is outside the fixed A-share session slots",
        details={"bar_end": bar_end},
    )


def _minute_dates(view: ChartView) -> list[str]:
    return list(dict.fromkeys(str(day) for day in view.bars["trade_date"].tolist()))


def _auction_for_view(view: ChartView) -> tuple[str, dict[str, Any]] | None:
    if view.bars.empty:
        return None
    visible_day = str(view.bars["trade_date"].iloc[-1])
    auction = view.metadata.get("auctions", {}).get(visible_day)
    if auction is None:
        return None
    return visible_day, auction


def _style_axis(axis: Any, *, grid: bool = True) -> None:
    axis.set_facecolor("#ffffff")
    axis.set_axisbelow(True)
    if grid:
        axis.grid(True, color="#d9dfe7", linewidth=0.55, alpha=0.82, zorder=0)
    axis.tick_params(axis="both", labelsize=7.2, colors="#334155", length=3)
    axis.spines["top"].set_visible(False)
    axis.spines["right"].set_visible(False)
    axis.spines["left"].set_color("#aab4c0")
    axis.spines["bottom"].set_color("#aab4c0")


def _historical_name(metadata: dict[str, Any]) -> str | None:
    label = metadata.get("labels", {}).get("name")
    if not label:
        return None
    value = label.get("value")
    if isinstance(value, list):
        names = [str(part).strip() for part in value if str(part).strip()]
        return " / ".join(names) or None
    text = str(value).strip() if value is not None else ""
    return text or None


def _translated_warning(value: str) -> str:
    code = value.split(":", 1)[0]
    if code == "SYNTHETIC_ENGINEERING_ONLY":
        return "工程合成样例，非真实证据"
    if code == "DISPLAY_ZOOM_WITHIN_CUTOFF":
        return "局部显示，不越过截止点"
    if code in _WARNING_TRANSLATIONS:
        return _WARNING_TRANSLATIONS[code]
    translated = _warning_tip(value)
    if translated == value:
        return "其他提示"
    return translated


def _header_warnings(views: list[ChartView], panel_warnings: dict[str, list[str]]) -> list[str]:
    warnings = _unique(
        [warning for view in views for warning in view.metadata.get("warnings", [])]
        + [warning for values in panel_warnings.values() for warning in values]
    )
    return [
        _translated_warning(value)
        for _index, value in sorted(
            enumerate(warnings),
            key=lambda pair: (
                _WARNING_ORDER_MULTI.index(pair[1].split(":", 1)[0])
                if pair[1].split(":", 1)[0] in _WARNING_ORDER_MULTI
                else len(_WARNING_ORDER_MULTI) + pair[0]
            ),
        )
    ]


def _auction_header_status(view: ChartView) -> str:
    warnings = set(view.metadata.get("warnings", []))
    requested_day = str(view.metadata["requested_cutoff"][:10])
    if "OPENING_AUCTION_FOLDED" in warnings:
        return "已并入首个15分钟与当日日线"
    if "AUCTION_NOT_YET_REVEALED" in warnings:
        return "截止时尚未揭示"
    auctions = view.metadata.get("auctions")
    if auctions is None or requested_day not in auctions:
        return "来源缺失"
    return "已揭示，图中单独显示"


def _metadata_header(views: list[ChartView], figure: Figure) -> tuple[float, list[str]]:
    metadata = views[0].metadata
    stage = _STAGE_LABELS.get(metadata["stage"], "独立学习")
    name = _historical_name(metadata)
    if len(views) == 1:
        period_name = {"daily": "日线", "15m": "15分钟", "1m": "1分钟", "intraday": "分时"}[views[0].period]
        first = f"{views[0].ts_code} · {period_name} · {stage}"
    else:
        first = f"{views[0].ts_code} · 多周期 · {stage}"
    if name:
        first = f"{views[0].ts_code} · {name} · {first.split(' · ', 1)[1]}"
    synthetic = any(
        "SYNTHETIC_ENGINEERING_ONLY" in view.metadata.get("warnings", [])
        for view in views
    )
    title = figure.text(
        0.052,
        0.978,
        first,
        ha="left",
        va="top",
        fontsize=13,
        fontweight="bold",
        color="#172033",
    )
    requested = str(metadata["requested_cutoff"])
    effective = str(metadata["effective_cutoff"])
    source = metadata["cutoff_source"]
    requested_text = requested[:19].replace("T", " ")
    effective_text = effective[:19].replace("T", " ")
    kind = _CUTOFF_KIND_LABELS.get(source["kind"], "人工")
    confidence = _CONFIDENCE_LABELS.get(source["confidence"], "低")
    figure.text(
        0.052,
        0.954,
        f"可见截止：{requested_text}（{_time_of_day_label(requested)}）｜K线截至：{effective_text}｜截止依据：{kind}（{confidence}）",
        ha="left",
        va="top",
        fontsize=8.2,
        color="#344256",
    )
    anchor_lineage = metadata.get("factor_lineage") or {}
    anchor = anchor_lineage.get("anchor_trade_date", "未知")
    folded_or_status = _auction_header_status(views[-1])
    figure.text(
        0.052,
        0.932,
        f"前复权，基准日 {anchor}｜成交量为真实量（万股）｜15分钟由1分钟聚合；集合竞价{folded_or_status}",
        ha="left",
        va="top",
        fontsize=8.0,
        color="#344256",
    )
    return (0.895 if synthetic else 0.895), [first, synthetic, title]


def _set_volume_axis(axis: Any) -> None:
    axis.yaxis.set_major_formatter(
        FuncFormatter(
            lambda value, _position: (
                f"{value:.3f}" if 0 < abs(value) < 0.1 else
                (f"{value:.2f}" if 0 < abs(value) < 10 else f"{value:,.0f}")
            )
        )
    )
    axis.set_ylabel("成交量（万股）", fontsize=7.2, color="#243247", labelpad=4)


def _price_limits(
    values: list[float],
    options: RenderOptions,
) -> tuple[float, float]:
    finite = [float(value) for value in values if np.isfinite(float(value))]
    if not finite:
        return 0.0, 1.0
    low, high = min(finite), max(finite)
    if options.price_scale == "log":
        if low <= 0:
            raise ChartError("RENDER_INVALID_PRICE", "Log price scale requires positive prices")
        return low / 1.03, high * 1.03
    span = high - low
    pad = span * 0.055 if span > 0 else max(abs(high) * 0.012, 0.01)
    return low - pad, high + pad


def _body_width(slot_px: float, mode: str) -> float:
    return float(max(1, round(0.7 * slot_px)) if mode == "normal" else 1)


def _draw_candles(
    price_axis: Any,
    rows: list[dict[str, Any]],
    x: list[float],
    widths_data: list[float],
    colors: list[str],
    *,
    dpi: int,
    partial_labels: bool = True,
) -> None:
    wick_segments = [
        ((center, row["low"]), (center, row["high"]))
        for center, row in zip(x, rows)
    ]
    if wick_segments:
        price_axis.add_collection(
            LineCollection(
                wick_segments,
                colors=colors,
                linewidths=72.0 / dpi,
                antialiaseds=False,
                zorder=3,
                label="candle_wicks",
            )
        )
    body_polygons: list[list[tuple[float, float]]] = []
    body_colors: list[str] = []
    doji_segments: list[list[tuple[float, float]]] = []
    doji_colors: list[str] = []
    for index, (center, row, width, color) in enumerate(zip(x, rows, widths_data, colors)):
        open_, close = float(row["open"]), float(row["close"])
        left, right = center - width / 2.0, center + width / 2.0
        if open_ == close:
            doji_segments.append([(left, open_), (right, open_)])
            doji_colors.append(color)
        elif bool(row.get("is_partial", False)):
            low, high = sorted((open_, close))
            patch = Rectangle(
                (left, low),
                right - left,
                high - low,
                facecolor="#ffffff",
                edgecolor=color,
                linewidth=72.0 / dpi,
                linestyle="--",
                hatch="////",
                antialiased=False,
                zorder=4,
            )
            price_axis.add_patch(patch)
            if partial_labels:
                price_axis.annotate(
                    "未完成",
                    (center, float(row["high"])),
                    xytext=(-2, 4),
                    textcoords="offset points",
                    ha="right",
                    va="bottom",
                    fontsize=6.5,
                    color="#374151",
                    clip_on=False,
                    zorder=8,
                )
        else:
            low, high = sorted((open_, close))
            body_polygons.append([(left, low), (left, high), (right, high), (right, low)])
            body_colors.append(color)
    if body_polygons:
        price_axis.add_collection(
            PolyCollection(
                body_polygons,
                closed=True,
                facecolors=body_colors,
                edgecolors="none",
                linewidths=0.0,
                antialiaseds=False,
                zorder=4,
                label="candle_bodies",
            )
        )
    if doji_segments:
        price_axis.add_collection(
            LineCollection(
                doji_segments,
                colors=doji_colors,
                linewidths=72.0 / dpi,
                antialiaseds=False,
                zorder=5,
                label="candle_doji",
            )
        )


def _draw_volume(
    volume_axis: Any,
    x: list[float],
    values: list[float],
    colors: list[str],
    widths_data: list[float],
    *,
    height_px: int,
    auction_flags: list[bool] | None = None,
) -> tuple[list[float], float]:
    raw_heights = [max(0.0, value / _VOLUME_DIVISOR) for value in values]
    maximum = max(raw_heights, default=0.0)
    top = maximum * 1.06 if maximum > 0 else 1.0
    axis_height = max(1.0, volume_axis.get_position().height * height_px)
    minimum = top * 1.25 / axis_height
    heights = [max(height, minimum) if value > 0 else 0.0 for value, height in zip(values, raw_heights)]
    polygons = [
        [
            (center - width / 2.0, 0.0),
            (center - width / 2.0, height),
            (center + width / 2.0, height),
            (center + width / 2.0, 0.0),
        ]
        for center, width, height in zip(x, widths_data, heights)
    ]
    if polygons:
        auction_flags = auction_flags or [False] * len(colors)
        face_colors = [
            tuple(channel / 255.0 for channel in _rgb(_AUCTION_VOLUME))
            if is_auction else tuple(channel / 255.0 for channel in _blended_volume_rgb(color))
            for color, is_auction in zip(colors, auction_flags)
        ]
        volume_axis.add_collection(
            PolyCollection(
                polygons,
                closed=True,
                facecolors=face_colors,
                edgecolors="none",
                linewidths=0.0,
                antialiaseds=False,
                zorder=3,
                label="volume_bars",
            )
        )
    volume_axis.set_ylim(0.0, top)
    return heights, top


def _rows_for_view(view: ChartView) -> list[dict[str, Any]]:
    if view.bars.empty:
        return []
    return [row.to_dict() for _index, row in view.bars.iterrows()]


def _daily_auction(view: ChartView, rows: list[dict[str, Any]]) -> tuple[str, dict[str, Any]] | None:
    auctions = view.metadata.get("auctions")
    if not isinstance(auctions, dict):
        return None
    day = str(view.metadata["requested_cutoff"][:10])
    if day in {str(row["trade_date"]) for row in rows}:
        return None
    auction = auctions.get(day)
    return (day, auction) if auction is not None else None


def _day_slots(dates: list[str]) -> dict[str, list[int]]:
    groups: dict[str, list[int]] = {}
    for index, day in enumerate(dates):
        groups.setdefault(day, []).append(index)
    return groups


def _draw_15m_axis_labels(volume_axis: Any, rows: list[dict[str, Any]]) -> None:
    dates = [str(row["trade_date"]) for row in rows]
    groups = _day_slots(dates)
    if not rows:
        return
    session_centers = [(indices[0] + indices[-1]) / 2.0 for indices in groups.values()]
    date_labels = [date.fromisoformat(day).strftime("%m-%d") for day in groups]
    if len(groups) > 2:
        volume_axis.xaxis.set_major_locator(FixedLocator(session_centers))
        volume_axis.xaxis.set_major_formatter(FixedFormatter(date_labels))
        return

    tick_positions: list[float] = []
    tick_labels: list[str] = []
    for day, indices in groups.items():
        day_rows = [rows[index] for index in indices]
        times = [parse_timestamp(str(row["bar_end"])) for row in day_rows]
        for target, label in ((10 * 60 + 30, "10:30"), (14 * 60, "14:00")):
            nearest = min(range(len(times)), key=lambda i: abs(times[i].hour * 60 + times[i].minute - target))
            distance = abs(times[nearest].hour * 60 + times[nearest].minute - target)
            if distance <= 15:
                tick_positions.append(float(indices[nearest]))
                tick_labels.append(label)
        morning = [idx for idx, item in zip(indices, times) if item.hour < 12]
        afternoon = [idx for idx, item in zip(indices, times) if item.hour >= 13]
        if morning:
            boundary = (morning[-1] + afternoon[0]) / 2.0 if afternoon else morning[-1] + 0.45
            tick_positions.append(float(boundary))
            tick_labels.append("11:30/13:00")
    order = sorted(zip(tick_positions, tick_labels), key=lambda item: item[0])
    if order:
        volume_axis.xaxis.set_major_locator(FixedLocator([item[0] for item in order]))
        volume_axis.xaxis.set_major_formatter(FixedFormatter([item[1] for item in order]))
    volume_axis.tick_params(axis="x", labelsize=6.5, pad=2)
    for (day, indices), center in zip(groups.items(), session_centers):
        volume_axis.text(
            center,
            -0.43,
            date.fromisoformat(day).strftime("%m-%d"),
            transform=volume_axis.get_xaxis_transform(),
            ha="center",
            va="top",
            fontsize=6.7,
            color="#334155",
            clip_on=False,
        )


def _draw_fixed_ticks(axis: Any) -> None:
    axis.xaxis.set_major_locator(FixedLocator([item[0] for item in _TIME_TICKS]))
    axis.xaxis.set_major_formatter(FixedFormatter([item[1] for item in _TIME_TICKS]))
    axis.tick_params(axis="x", labelsize=6.1, pad=2, labelrotation=0)


def _panel_warnings(view: ChartView, period: str, auction: tuple[str, dict[str, Any]] | None) -> list[str]:
    warnings = list(view.metadata.get("warnings", []))
    if period in {"1m", "intraday"} and auction is None:
        if "AUCTION_NOT_YET_REVEALED" not in warnings and "AUCTION_UNAVAILABLE" not in warnings:
            warnings.append("AUCTION_UNAVAILABLE")
    if period == "intraday":
        if "prev_close" not in view.metadata or not isinstance(view.metadata.get("prev_close"), dict):
            warnings.append("PREV_CLOSE_UNAVAILABLE")
        if "avg_price" not in view.bars.columns:
            warnings.append("AVG_PRICE_UNAVAILABLE")
    return _unique(warnings)


def _panel_title(period: str, view: ChartView, rows: list[dict[str, Any]]) -> str:
    titles = {"daily": "日线", "15m": "15分钟", "1m": "1分钟", "intraday": "分时"}
    if period == "intraday" and rows:
        visible_day = str(rows[-1]["trade_date"])
        requested_day = str(view.metadata["requested_cutoff"][:10])
        if visible_day != requested_day:
            return f"分时 · 上一交易日 {visible_day[5:]}（截止时本日尚无成交）"
    return titles[period]


def _draw_intraday(
    view: ChartView,
    rows: list[dict[str, Any]],
    price_axis: Any,
    volume_axis: Any,
    options: RenderOptions,
    auction: tuple[str, dict[str, Any]] | None,
    *,
    height_px: int,
) -> dict[str, Any]:
    x = [_minute_slot(str(row["bar_end"])) for row in rows]
    close = [float(row["close"]) for row in rows]
    average = [float(row["avg_price"]) if row.get("avg_price") is not None else float("nan") for row in rows]
    visible_day = str(rows[-1]["trade_date"]) if rows else None
    prev_close = view.metadata.get("prev_close", {}).get(visible_day) if visible_day else None
    auction_row = None
    if auction is not None:
        auction_day, value = auction
        auction_row = {"trade_date": auction_day, "price": float(value["price"]),
                       "vol": float(value["vol"]), "amount": float(value.get("amount", 0.0))}
    price_axis.set_xlim(-0.6, 240.6)
    volume_axis.set_xlim(-0.6, 240.6)
    slot_px = price_axis.get_window_extent().width / 241.2
    body_width_px = max(1, round(0.7 * slot_px))
    body_width_data = body_width_px / slot_px

    line_x = ([0.0] if auction_row is not None else []) + [float(item) for item in x]
    line_y = ([auction_row["price"]] if auction_row is not None else []) + close
    if line_x:
        segments = [((line_x[index], line_y[index]), (line_x[index + 1], line_y[index + 1]))
                    for index in range(len(line_x) - 1)]
        if segments:
            price_axis.add_collection(LineCollection(
                segments, colors=_INTRADAY_LINE, linewidths=1.2 * 72.0 / options.dpi,
                antialiaseds=True, zorder=4, label="分时收盘价"))
        price_axis.scatter(line_x, line_y, s=5.0, color=_INTRADAY_LINE, linewidths=0, zorder=5)

    has_average = "avg_price" in view.bars.columns
    if has_average:
        valid = [(float(slot), value) for slot, value in zip(x, average) if np.isfinite(value)]
        if valid:
            segments = [((valid[index][0], valid[index][1]), (valid[index + 1][0], valid[index + 1][1]))
                        for index in range(len(valid) - 1)]
            if segments:
                price_axis.add_collection(LineCollection(
                    segments, colors=_AVERAGE_LINE, linewidths=1.1 * 72.0 / options.dpi,
                    antialiaseds=True, zorder=4, label="均价"))
            price_axis.scatter([item[0] for item in valid], [item[1] for item in valid],
                               s=4.0, color=_AVERAGE_LINE, linewidths=0, zorder=5)

    if prev_close is not None:
        prev = float(prev_close)
        deltas = [abs(value - prev) for value in close]
        deltas.extend(abs(value - prev) for value in average if np.isfinite(value))
        if auction_row is not None:
            deltas.append(abs(auction_row["price"] - prev))
        half = max(max(deltas, default=0.0) * 1.08, abs(prev) * 0.01)
        price_axis.set_ylim(prev - half, prev + half)
        price_axis.axhline(prev, color=_PREVIOUS_CLOSE, linestyle="--", linewidth=0.85, zorder=2)
        price_axis.text(0.01, prev, f"昨收 {prev:.2f}", transform=price_axis.get_yaxis_transform(),
                        ha="left", va="bottom", fontsize=6.4, color=_PREVIOUS_CLOSE,
                        clip_on=False, zorder=7)
        percent_axis = price_axis.twinx()
        percent_axis.set_ylim(((prev - half) / prev - 1.0) * 100.0,
                               ((prev + half) / prev - 1.0) * 100.0)
        percent_axis.yaxis.set_major_formatter(FuncFormatter(lambda value, _pos: f"{value:+.2f}%"))
        percent_axis.tick_params(axis="y", labelsize=6.7, length=2, colors="#334155")
        percent_axis.spines["top"].set_visible(False)
        percent_axis.spines["left"].set_visible(False)
        percent_axis.spines["right"].set_color("#aab4c0")
        percent_axis.grid(False)
        for tick, value in zip(percent_axis.get_yticklabels(), percent_axis.get_yticks()):
            tick.set_color(options.up_color if value >= 0 else options.down_color)
    else:
        values = close + [value for value in average if np.isfinite(value)]
        if auction_row is not None:
            values.append(auction_row["price"])
        price_axis.set_ylim(*_price_limits(values, options))

    volume_colors: list[str] = []
    previous = auction_row["price"] if auction_row is not None else prev_close
    for value in close:
        comparison = float(previous) if previous is not None else value
        volume_colors.append(options.up_color if value >= comparison else options.down_color)
        previous = value
    entries: list[dict[str, Any]] = []
    if auction_row is not None:
        entries.append({"key": f"auction:{auction_row['trade_date']}", "x": 0.0,
                        "close": auction_row["price"], "avg_price": None,
                        "vol": auction_row["vol"], "candle_rgb": _rgb(_INTRADAY_LINE),
                        "volume_rgb": _rgb(_AUCTION_VOLUME), "volume_color": _AUCTION_VOLUME,
                        "auction": True})
        price_axis.annotate("竞价", (0.0, auction_row["price"]), xytext=(3, 5),
                            textcoords="offset points", ha="left", va="bottom", fontsize=6.4,
                            color="#374151", clip_on=False, zorder=8)
        volume_axis.text(0.0, 0.96, "竞价", transform=volume_axis.get_xaxis_transform(),
                         ha="center", va="top", fontsize=6.0, color="#6b7280")
    for index, (row, slot, color) in enumerate(zip(rows, x, volume_colors)):
        entries.append({"key": str(row["bar_end"]), "x": float(slot), "close": close[index],
                        "avg_price": average[index] if np.isfinite(average[index]) else None,
                        "vol": float(row["vol"]), "candle_rgb": _rgb(_INTRADAY_LINE),
                        "volume_rgb": _blended_volume_rgb(color), "volume_color": color,
                        "auction": False, "row": row})
    volume_values = [float(entry["vol"]) for entry in entries]
    volume_x = [float(entry["x"]) for entry in entries]
    volume_colors_for_draw = [str(entry["volume_color"]) for entry in entries]
    widths_px = [body_width_px] * len(entries)
    widths_data = [body_width_data] * len(entries)
    display_heights, _volume_top = _draw_volume(
        volume_axis, volume_x, volume_values, volume_colors_for_draw, widths_data,
        height_px=height_px, auction_flags=[bool(entry.get("auction")) for entry in entries])
    for entry, height in zip(entries, display_heights):
        entry["display_height"] = float(height)
    _draw_fixed_ticks(volume_axis)
    price_axis.tick_params(axis="x", labelbottom=False)
    volume_axis.set_xlabel("交易时段", fontsize=7.0, color="#243247", labelpad=5)

    warnings: list[str] = []
    if prev_close is None:
        warnings.append("PREV_CLOSE_UNAVAILABLE")
    if not has_average:
        warnings.append("AVG_PRICE_UNAVAILABLE")
    last_right = max((entry["x"] for entry in entries), default=-1.0) + 2.5 / slot_px if entries else -1.0
    return {"entries": entries, "x": volume_x, "widths_data": widths_data,
            "widths_px": widths_px, "display_heights": display_heights,
            "slot_px": float(slot_px), "mode": "line", "line_rgb": _rgb(_INTRADAY_LINE),
            "warnings": warnings, "auction": auction_row,
            "prev_close": float(prev_close) if prev_close is not None else None,
            "avg_price_available": has_average, "last_right_data": float(last_right),
            "fixed_slot": True}


def _draw_candle_panel(
    view: ChartView,
    period: str,
    rows: list[dict[str, Any]],
    price_axis: Any,
    volume_axis: Any,
    options: RenderOptions,
    *,
    combined: bool,
    height_px: int,
    minute_dates: set[str],
    include_daily_auction: bool = False,
) -> dict[str, Any]:
    date_labels = [str(row["trade_date"]) for row in rows]
    if period == "1m":
        x = [_minute_slot(str(row["bar_end"])) for row in rows]
        price_axis.set_xlim(-0.6, 240.6)
        volume_axis.set_xlim(-0.6, 240.6)
    else:
        x = [float(index) for index in range(len(rows))]
    auction = _daily_auction(view, rows) if include_daily_auction else (
        _auction_for_view(view) if period == "1m" else None
    )
    warnings = _panel_warnings(view, period, auction)
    price_axis.set_title(_panel_title(period, view, rows), loc="left", fontsize=9.0,
                         color="#172033", pad=4)
    price_axis.set_ylabel("价格（元）", fontsize=7.2, color="#243247", labelpad=4)
    price_axis.yaxis.set_major_formatter(StrMethodFormatter("{x:,.2f}"))
    _set_volume_axis(volume_axis)
    if not rows:
        price_axis.text(0.5, 0.5, "无数据", transform=price_axis.transAxes,
                        ha="center", va="center", fontsize=10, color="#64748b")
        volume_axis.text(0.5, 0.5, "无数据", transform=volume_axis.transAxes,
                         ha="center", va="center", fontsize=8, color="#64748b")
        if period != "1m":
            price_axis.set_xlim(-0.6, 0.6)
            volume_axis.set_xlim(-0.6, 0.6)
        price_axis.set_ylim(0.0, 1.0)
        volume_axis.set_ylim(0.0, 1.0)
        if period == "1m":
            _draw_fixed_ticks(volume_axis)
        return {"entries": [], "x": [], "widths_data": [], "widths_px": [],
                "display_heights": [], "slot_px": 0.0, "mode": "normal",
                "warnings": warnings, "auction": None, "last_right_data": -1.0,
                "fixed_slot": period == "1m"}

    auction_row = None
    if auction is not None:
        auction_day, value = auction
        auction_row = {"key": f"auction:{auction_day}", "trade_date": auction_day,
                       "price": float(value["price"]), "vol": float(value["vol"]),
                       "amount": float(value.get("amount", 0.0))}
    if period == "daily" and auction_row is not None:
        right = len(rows) + 0.65
        price_axis.set_xlim(-0.6, right)
        volume_axis.set_xlim(-0.6, right)
    elif period != "1m":
        right = max(len(rows) - 0.4, 0.6)
        price_axis.set_xlim(-0.6, right)
        volume_axis.set_xlim(-0.6, right)

    if period == "1m":
        slot_px = price_axis.get_window_extent().width / 241.2
    else:
        slot_px = price_axis.get_window_extent().width / max(price_axis.get_xlim()[1] - price_axis.get_xlim()[0], 1.0)
    mode = "normal" if slot_px >= 3.0 else "dense"
    body_width_px = _body_width(slot_px, mode)
    body_width_data = body_width_px / slot_px
    widths_data = [body_width_data] * len(rows)
    raw_colors = [_bar_color(float(row["open"]), float(row["close"]), options) for row in rows]
    _draw_candles(price_axis, rows, x, widths_data, raw_colors, dpi=options.dpi)

    if period == "daily" and combined and minute_dates:
        for index, day in enumerate(date_labels):
            if day in minute_dates:
                for axis in (price_axis, volume_axis):
                    axis.axvspan(index - 0.5, index + 0.5, color=_DAY_HIGHLIGHT,
                                 alpha=0.20, linewidth=0, zorder=0)
    if period == "15m":
        prior_day = None
        for index, day in enumerate(date_labels):
            if prior_day is not None and day != prior_day:
                for axis in (price_axis, volume_axis):
                    axis.axvline(index - 0.5, color=_DAY_BOUNDARY, linewidth=0.72,
                                 linestyle="-", zorder=2)
            prior_day = day

    for ma_index, period_value in enumerate(options.ma_periods):
        closes = np.asarray([float(row["close"]) for row in rows], dtype=float)
        rolling = np.full(len(closes), np.nan, dtype=float)
        for end in range(period_value - 1, len(closes)):
            rolling[end] = float(np.mean(closes[end - period_value + 1:end + 1]))
        price_axis.plot(x, rolling, color=_MA_COLORS[ma_index % len(_MA_COLORS)],
                        linewidth=0.9, zorder=5, label=f"MA{period_value}")

    values = [float(row["low"]) for row in rows] + [float(row["high"]) for row in rows]
    if auction_row is not None:
        values.append(auction_row["price"])
    price_axis.set_ylim(*_price_limits(values, options))
    if options.price_scale == "log":
        price_axis.set_yscale("log")

    auction_color = options.up_color
    if auction_row is not None:
        previous_close = view.metadata.get("prev_close", {}).get(auction_row["trade_date"])
        if previous_close is not None and auction_row["price"] < float(previous_close):
            auction_color = options.down_color
        auction_x = float(len(rows)) if period == "daily" else 0.0
        price_axis.plot([auction_x - body_width_data / 2.0, auction_x + body_width_data / 2.0],
                        [auction_row["price"], auction_row["price"]], color=auction_color,
                        linewidth=1.35, solid_capstyle="butt", zorder=7)
        label = f"竞价 {auction_row['price']:.2f}（09:25）" if period == "daily" else "竞价"
        price_axis.annotate(label, (auction_x, auction_row["price"]), xytext=(3, 4),
                            textcoords="offset points", fontsize=6.2, color="#374151",
                            ha="left", va="bottom", clip_on=False, zorder=8)

    if period == "1m":
        _draw_fixed_ticks(volume_axis)
        volume_axis.set_xlabel("交易时段", fontsize=7.0, color="#243247", labelpad=5)
    elif period == "daily":
        _set_date_ticks(volume_axis, date_labels, len(rows))
        volume_axis.set_xlabel("交易日期", fontsize=7.1, color="#243247", labelpad=5)
    else:
        _draw_15m_axis_labels(volume_axis, rows)
        volume_axis.set_xlabel("交易日与时段", fontsize=7.0, color="#243247", labelpad=5)
    price_axis.tick_params(axis="x", labelbottom=False)

    entries: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        entries.append({"key": str(row["trade_date"]) if period == "daily" else str(row["bar_end"]),
                        "x": float(x[index]), "row": row, "vol": float(row["vol"]),
                        "candle_rgb": _rgb(raw_colors[index]),
                        "volume_rgb": _blended_volume_rgb(raw_colors[index]),
                        "volume_color": raw_colors[index], "auction": False})
    if auction_row is not None:
        entries.append({"key": auction_row["key"],
                        "x": float(len(rows)) if period == "daily" else 0.0,
                        "row": None, "price": auction_row["price"], "vol": auction_row["vol"],
                        "candle_rgb": _rgb(auction_color), "volume_rgb": _rgb(_AUCTION_VOLUME),
                        "volume_color": _AUCTION_VOLUME, "auction": True})
    entries.sort(key=lambda entry: float(entry["x"]))
    volume_x = [float(entry["x"]) for entry in entries]
    volume_values = [float(entry["vol"]) for entry in entries]
    volume_draw_colors = [str(entry["volume_color"]) for entry in entries]
    volume_widths = [body_width_data] * len(entries)
    display_heights, _volume_top = _draw_volume(
        volume_axis, volume_x, volume_values, volume_draw_colors, volume_widths,
        height_px=height_px, auction_flags=[bool(entry.get("auction")) for entry in entries])
    for entry, height in zip(entries, display_heights):
        entry["display_height"] = float(height)
    if auction_row is not None and period == "1m":
        volume_axis.text(0.0, 0.96, "竞价", transform=volume_axis.get_xaxis_transform(),
                         ha="center", va="top", fontsize=6.0, color="#6b7280")
    if period == "1m":
        price_axis.set_xlim(-0.6, 240.6)
        volume_axis.set_xlim(-0.6, 240.6)
    last_right = max((float(entry["x"]) + body_width_data / 2.0 for entry in entries), default=-1.0)
    return {"entries": entries, "x": volume_x, "widths_data": volume_widths,
            "widths_px": [body_width_px] * len(entries), "display_heights": display_heights,
            "slot_px": float(slot_px), "mode": mode, "warnings": warnings,
            "auction": auction_row, "last_right_data": float(last_right),
            "fixed_slot": period == "1m"}


def _draw_15m_axis(
    view: ChartView,
    rows: list[dict[str, Any]],
    price_axis: Any,
    volume_axis: Any,
    options: RenderOptions,
    *,
    height_px: int,
) -> dict[str, Any]:
    return _draw_candle_panel(view, "15m", rows, price_axis, volume_axis, options,
                              combined=False, height_px=height_px, minute_dates=set())


def _draw_intraday_panel(
    view: ChartView,
    rows: list[dict[str, Any]],
    price_axis: Any,
    volume_axis: Any,
    options: RenderOptions,
    *,
    height_px: int,
) -> dict[str, Any]:
    auction = _auction_for_view(view)
    warnings = _panel_warnings(view, "intraday", auction)
    price_axis.set_title(_panel_title("intraday", view, rows), loc="left", fontsize=9.0,
                         color="#172033", pad=4)
    price_axis.set_ylabel("价格（元）", fontsize=7.2, color="#243247", labelpad=4)
    price_axis.yaxis.set_major_formatter(StrMethodFormatter("{x:,.2f}"))
    _set_volume_axis(volume_axis)
    if not rows:
        price_axis.text(0.5, 0.5, "无数据", transform=price_axis.transAxes,
                        ha="center", va="center", fontsize=10, color="#64748b")
        volume_axis.text(0.5, 0.5, "无数据", transform=volume_axis.transAxes,
                         ha="center", va="center", fontsize=8, color="#64748b")
        price_axis.set_xlim(-0.6, 240.6)
        volume_axis.set_xlim(-0.6, 240.6)
        price_axis.set_ylim(0.0, 1.0)
        volume_axis.set_ylim(0.0, 1.0)
        _draw_fixed_ticks(volume_axis)
        return {"entries": [], "x": [], "widths_data": [], "widths_px": [],
                "display_heights": [], "slot_px": 0.0, "mode": "line",
                "line_rgb": _rgb(_INTRADAY_LINE), "warnings": warnings,
                "auction": None, "last_right_data": -1.0, "fixed_slot": True}

    state = _draw_intraday(view, rows, price_axis, volume_axis, options, auction,
                           height_px=height_px)
    state["warnings"] = _unique(state["warnings"] + warnings)
    state["line_rgb"] = _rgb(_INTRADAY_LINE)
    return state


def _validate_options(options: RenderOptions) -> None:
    if not isinstance(options, RenderOptions):
        raise ChartError("INVALID_REQUEST", "Expected RenderOptions")
    options.validate()


def _validate_view(view: ChartView, expected_period: str | None = None) -> None:
    if not isinstance(view, ChartView):
        raise ChartError("INVALID_SCHEMA", "Expected a ChartView")
    view.validate()
    if expected_period is not None and view.period != expected_period:
        raise ChartError("INVALID_SCHEMA", "View period does not match the selected panel")


def _validate_compatible_views(views: dict[str, ChartView]) -> list[ChartView]:
    if not isinstance(views, dict) or set(views) != set(_PERIODS):
        raise ChartError(
            "INVALID_SCHEMA",
            "Multiperiod input keys must be exactly daily, 15m, 1m, and intraday",
            details={"required_keys": list(_PERIODS)},
        )
    ordered = [views[period] for period in _PERIODS]
    for period, view in zip(_PERIODS, ordered):
        _validate_view(view, period)
    baseline = ordered[0]
    for view in ordered[1:]:
        if view.ts_code != baseline.ts_code:
            raise ChartError("INVALID_SCHEMA", "Multiperiod views have different stock codes")
        for field in ("requested_cutoff", "effective_cutoff", "stage", "source_version", "factor_lineage"):
            if view.metadata[field] != baseline.metadata[field]:
                raise ChartError("INVALID_SCHEMA", "Multiperiod views have incompatible metadata",
                                 details={"field": field})
    return ordered


def _prepare_views(views: list[ChartView], *, combined: bool, combined_daily_bars: int) -> list[ChartView]:
    prepared: list[ChartView] = []
    for view in views:
        if combined and view.period == "daily":
            prepared.append(crop_last_bars(view, combined_daily_bars))
        elif view.period in {"1m", "intraday"}:
            prepared.append(crop_sessions(view, 1))
        else:
            prepared.append(view)
    return prepared


def _build_figure(
    views: list[ChartView],
    options: RenderOptions,
    *,
    combined: bool,
    height_px: int,
) -> tuple[Figure, FigureCanvasAgg, dict[str, dict[str, Any]], dict[str, list[str]]]:
    width_px = options.width_px
    figure = Figure(figsize=(width_px / options.dpi, height_px / options.dpi), dpi=options.dpi,
                    facecolor="#ffffff")
    canvas = FigureCanvasAgg(figure)
    top, header = _metadata_header(views, figure)
    axes: dict[str, dict[str, Any]] = {}
    states: dict[str, dict[str, Any]] = {}
    panel_warnings: dict[str, list[str]] = {}
    for axis in ():
        _style_axis(axis)

    if combined:
        grid = figure.add_gridspec(2, 1, height_ratios=(1.55, 1.0), left=0.075, right=0.985,
                                    bottom=0.12, top=top, hspace=0.18)
        daily_spec = grid[0, 0].subgridspec(2, 1, height_ratios=(3.0, 1.0), hspace=0.04)
        daily_price = figure.add_subplot(daily_spec[0, 0])
        daily_volume = figure.add_subplot(daily_spec[1, 0], sharex=daily_price)
        minute_dates = {
            day for view in views if view.period in {"15m", "1m", "intraday"}
            for day in _minute_dates(view)
        }
        for axis in (daily_price, daily_volume):
            _style_axis(axis)
        daily_view = views[0]
        daily_rows = _rows_for_view(daily_view)
        states["daily"] = _draw_candle_panel(
            daily_view, "daily", daily_rows, daily_price, daily_volume, options,
            combined=True, height_px=height_px, minute_dates=minute_dates,
            include_daily_auction=True,
        )
        axes["daily"] = {"price": daily_price, "volume": daily_volume}
        panel_warnings["daily"] = states["daily"]["warnings"]

        lower_spec = grid[1, 0].subgridspec(1, 3, width_ratios=(1.3, 1.0, 1.0), wspace=0.18)
        for index, period in enumerate(_LOWER_PERIODS):
            panel_spec = lower_spec[0, index].subgridspec(2, 1, height_ratios=(2.65, 1.0), hspace=0.04)
            price_axis = figure.add_subplot(panel_spec[0, 0])
            volume_axis = figure.add_subplot(panel_spec[1, 0], sharex=price_axis)
            _style_axis(price_axis)
            _style_axis(volume_axis)
            view = views[_PERIODS.index(period)]
            rows = _rows_for_view(view)
            if period == "intraday":
                state = _draw_intraday_panel(view, rows, price_axis, volume_axis, options,
                                             height_px=height_px)
            else:
                state = _draw_candle_panel(view, period, rows, price_axis, volume_axis, options,
                                           combined=True, height_px=height_px,
                                           minute_dates=set())
            states[period] = state
            axes[period] = {"price": price_axis, "volume": volume_axis}
            panel_warnings[period] = state["warnings"]
    else:
        view = views[0]
        period = view.period
        if period == "daily":
            height_ratios = (3.2, 1.0)
        else:
            height_ratios = (3.0, 1.0)
        right_margin = 0.965 if period == "daily" else 0.94
        # A pre-open intraday title includes the previous session and cutoff
        # explanation. Keep it below the fourth header row instead of allowing
        # the long title to collide with the warning line.
        panel_top = min(top, 0.855) if period == "intraday" else top
        grid = figure.add_gridspec(2, 1, height_ratios=height_ratios, left=0.075, right=right_margin,
                                   bottom=0.12, top=panel_top, hspace=0.04)
        price_axis = figure.add_subplot(grid[0, 0])
        volume_axis = figure.add_subplot(grid[1, 0], sharex=price_axis)
        _style_axis(price_axis)
        _style_axis(volume_axis)
        rows = _rows_for_view(view)
        if period == "intraday":
            state = _draw_intraday_panel(view, rows, price_axis, volume_axis, options,
                                         height_px=height_px)
        else:
            state = _draw_candle_panel(view, period, rows, price_axis, volume_axis, options,
                                       combined=False, height_px=height_px, minute_dates=set())
        states[period] = state
        axes[period] = {"price": price_axis, "volume": volume_axis}
        panel_warnings[period] = state["warnings"]

    if header[1]:
        # Measure the first line after the first draw so the synthetic notice never overlaps it.
        canvas.draw()
        title_artist = header[2]
        bbox = title_artist.get_window_extent(canvas.get_renderer())
        figure.text(bbox.x1 / width_px + 8.0 / width_px, 0.978,
                    "【工程合成样例，非真实证据】", ha="left", va="top", fontsize=11.5,
                    fontweight="bold", color="#b91c1c")
    warning_texts = _header_warnings(views, panel_warnings)
    shown = warning_texts[:3]
    if len(warning_texts) > 3:
        shown.append(f"另有 {len(warning_texts) - 3} 条提示")
    figure.text(0.052, 0.910, "提示：" + ("；".join(shown) if shown else "无"),
                ha="left", va="top", fontsize=7.7, color="#566274")
    return figure, canvas, axes | {"_states": states}, panel_warnings


def _panel_geometry(
    period: str,
    state: dict[str, Any],
    price_axis: Any,
    volume_axis: Any,
    canvas: FigureCanvasAgg,
    options: RenderOptions,
    *,
    height_px: int,
) -> dict[str, Any]:
    actual_width, actual_height = canvas.get_width_height()
    baseline_display_y = volume_axis.transData.transform((0.0, 0.0))[1]
    baseline_y = float(actual_height - baseline_display_y)
    mode = str(state["mode"])
    xlim = price_axis.get_xlim()
    x0 = float(xlim[0])
    x1 = float(xlim[1])
    axis_slot_px = price_axis.get_window_extent().width / max(x1 - x0, 1e-9)
    body_width_px = float(state.get("widths_px", [max(1, round(0.7 * axis_slot_px))])[0]) if state.get("widths_px") else max(1.0, 0.7 * axis_slot_px)
    bars: list[dict[str, Any]] = []
    for index, entry in enumerate(state.get("entries", [])):
        x_data = float(entry["x"])
        x_px, _ = _data_px(price_axis, x_data, 0.0, actual_height)
        volume_top_data = float(entry.get("display_height", state.get("display_heights", [0.0])[index] if index < len(state.get("display_heights", [])) else 0.0))
        _, volume_top_y = _data_px(volume_axis, x_data, volume_top_data, actual_height)
        close_y = None
        high_y = None
        low_y = None
        if mode == "line":
            close_y = _data_px(price_axis, x_data, float(entry["close"]), actual_height)[1]
            if entry.get("avg_price") is not None:
                entry["avg_price_y_px"] = _data_px(price_axis, x_data, float(entry["avg_price"]), actual_height)[1]
        elif entry.get("auction"):
            point_y = _data_px(price_axis, x_data, float(entry["price"]), actual_height)[1]
            high_y = low_y = point_y
        else:
            row = entry["row"]
            high_y = _data_px(price_axis, x_data, float(row["high"]), actual_height)[1]
            low_y = _data_px(price_axis, x_data, float(row["low"]), actual_height)[1]
        bar: dict[str, Any] = {
            "key": str(entry["key"]),
            "x_px": float(x_px),
            "high_y_px": float(high_y) if high_y is not None else None,
            "low_y_px": float(low_y) if low_y is not None else None,
            "vol_top_y_px": float(volume_top_y),
            "vol": float(entry["vol"]),
            "candle_rgb": list(entry["candle_rgb"]),
            "volume_rgb": list(entry["volume_rgb"]),
        }
        if mode == "line":
            bar["close_y_px"] = float(close_y)
            bar["avg_price_y_px"] = entry.get("avg_price_y_px")
        bars.append(bar)
    if state.get("entries"):
        last_bar_right = max(
            float(bar["x_px"])
            + (body_width_px / 2.0 if mode != "line" else 2.5)
            + (1.5 if str(bar.get("key", "")).startswith("auction:") else 0.0)
            for bar in bars
        )
    else:
        last_bar_right = float(_axes_px(price_axis, actual_height)[0]) - 1.0
    panel = {
        "price_axes_px": _axes_px(price_axis, actual_height),
        "volume_axes_px": _axes_px(volume_axis, actual_height),
        "price_scale": "linear" if mode == "line" else options.price_scale,
        "mode": mode,
        "slot_px": float(state.get("slot_px", axis_slot_px)),
        "body_width_px": float(body_width_px),
        "volume_baseline_y_px": baseline_y,
        "last_bar_right_px": float(last_bar_right),
        "bars": bars,
        "gap_marks": [],
        "annotations": [],
    }
    if mode == "line":
        panel["line_rgb"] = list(state.get("line_rgb", _rgb(_INTRADAY_LINE)))
    if state.get("fixed_slot"):
        panel["fixed_slot_right_px"] = float(_data_px(price_axis, 240.6, 0.0, actual_height)[0])
        panel["fixed_slot_boundary_rgb"] = [_rgb(options.up_color), _rgb(options.down_color)]
    return panel


def _geometry(
    views: list[ChartView],
    options: RenderOptions,
    canvas: FigureCanvasAgg,
    axes: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    actual_width, actual_height = canvas.get_width_height()
    states = axes["_states"]
    panels: dict[str, dict[str, Any]] = {}
    for period in _PERIODS:
        if period not in axes:
            continue
        panel = _panel_geometry(period, states[period], axes[period]["price"], axes[period]["volume"],
                                canvas, options, height_px=actual_height)
        panels[period] = panel
    return {
        "geometry_version": "geom.v1",
        "canvas": {"width_px": int(actual_width), "height_px": int(actual_height)},
        "background": "#ffffff",
        "panels": panels,
    }


def _write_png(target: Path, canvas: FigureCanvasAgg) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    image = Image.frombuffer("RGBA", canvas.get_width_height(), canvas.buffer_rgba(), "raw", "RGBA", 0, 1)
    temporary = target.with_name(f".{target.stem}.{uuid4().hex}.tmp")
    try:
        image.save(temporary, format="PNG", compress_level=6)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _panel_manifest(view: ChartView, state: dict[str, Any]) -> dict[str, Any]:
    rows = state.get("entries", [])
    bar_rows = [entry for entry in rows if not entry.get("auction")]
    start = str(bar_rows[0]["key"]) if bar_rows else None
    end = str(bar_rows[-1]["key"]) if bar_rows else None
    is_line = state.get("mode") == "line"
    return {
        "input_rows": len(view.bars),
        "displayed_rows": len(bar_rows),
        "candle_count": 0 if is_line else len(bar_rows),
        "line_point_count": len(bar_rows) + sum(bool(entry.get("auction")) for entry in rows) if is_line else 0,
        "volume_bar_count": len(rows),
        "actual_start": start,
        "actual_end": end,
        "partial_count": int(sum(bool(value) for value in view.bars.get("is_partial", []))) if "is_partial" in view.bars else 0,
        "view_identity": view.identity(),
        "warnings": _unique(state.get("warnings", [])),
    }


def _render(
    input_views: list[ChartView],
    options: RenderOptions,
    output_path: str | Path,
    *,
    combined: bool,
    combined_daily_bars: int = 250,
) -> tuple[dict[str, Any], dict[str, Any]]:
    prepared = _prepare_views(input_views, combined=combined, combined_daily_bars=combined_daily_bars)
    target = writable_path(output_path, suffix=".png")
    width_px = options.width_px
    height_px = 2400 if combined else options.height_px
    if not combined and prepared[0].period == "daily" and options.auto_width:
        rows = len(prepared[0].bars)
        needed = int(np.ceil((rows + 0.2) * options.min_slot_px / 0.89)) if rows else width_px
        width_px = min(max(width_px, needed), max(options.max_width_px, width_px))
        options_for_draw = RenderOptions(
            width_px=width_px, height_px=options.height_px, dpi=options.dpi,
            font_family=options.font_family, up_color=options.up_color, down_color=options.down_color,
            ma_periods=options.ma_periods, price_scale=options.price_scale, auto_width=False,
            min_slot_px=options.min_slot_px, max_width_px=options.max_width_px,
            annotations=options.annotations,
        )
    else:
        options_for_draw = RenderOptions(
            width_px=width_px, height_px=height_px, dpi=options.dpi, font_family=options.font_family,
            up_color=options.up_color, down_color=options.down_color, ma_periods=options.ma_periods,
            price_scale=options.price_scale, auto_width=False, min_slot_px=options.min_slot_px,
            max_width_px=options.max_width_px, annotations=options.annotations,
        )
    if not combined and prepared[0].period == "daily":
        slot_px = 0.89 * width_px / (len(prepared[0].bars) + 0.2) if len(prepared[0].bars) else 0.89 * width_px
        if slot_px < 1.0:
            required_width = int(np.ceil((len(prepared[0].bars) + 0.2) / 0.89))
            raise ChartError("RENDER_TOO_DENSE",
                             "The requested canvas cannot assign one pixel to every daily bar",
                             details={"required_width_px": required_width,
                                      "actual_width_px": width_px,
                                      "actual_slot_px": slot_px,
                                      "max_width_px": max(options.max_width_px, options.width_px)})
    for view in prepared:
        view.validate()
    started = perf_counter()
    with matplotlib.rc_context({"font.family": [options.font_family], "axes.unicode_minus": False,
                                "figure.facecolor": "#ffffff", "savefig.facecolor": "#ffffff"}):
        figure, canvas, axes, panel_warnings = _build_figure(
            prepared, options_for_draw, combined=combined, height_px=height_px
        )
        canvas.draw()
        geometry = _geometry(prepared, options_for_draw, canvas, axes)
    drawing_seconds = perf_counter() - started
    encode_started = perf_counter()
    _write_png(target, canvas)
    encoding_seconds = perf_counter() - encode_started
    states = axes["_states"]
    panels = {view.period: _panel_manifest(view, states[view.period]) for view in prepared}
    warnings = _unique([warning for state in states.values() for warning in state.get("warnings", [])])
    manifest = {
        "status": "ok",
        "output_path": str(target),
        "ts_code": prepared[0].ts_code,
        "requested_cutoff": prepared[0].metadata["requested_cutoff"],
        "effective_cutoff": prepared[0].metadata["effective_cutoff"],
        "stage": prepared[0].metadata["stage"],
        "panels": panels,
        "timings": {"drawing_seconds": drawing_seconds, "encoding_seconds": encoding_seconds},
        "warnings": warnings,
    }
    figure.clear()
    return manifest, geometry


def render_multiperiod_png_with_geometry(
    views: dict[str, ChartView],
    options: RenderOptions,
    output_path: str | Path,
    *,
    combined_daily_bars: int = 250,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Render the four compatible views at fixed canvas width and 2400 px height."""
    _validate_options(options)
    if type(combined_daily_bars) is not int or combined_daily_bars <= 0:
        raise ChartError("INVALID_REQUEST", "combined_daily_bars must be a positive integer")
    ordered = _validate_compatible_views(views)
    return _render(ordered, options, output_path, combined=True,
                   combined_daily_bars=combined_daily_bars)


def render_multiperiod_png(
    views: dict[str, ChartView],
    options: RenderOptions,
    output_path: str | Path,
    *,
    combined_daily_bars: int = 250,
) -> dict[str, Any]:
    """Render daily, 15-minute, 1-minute, and intraday views on one canvas."""
    manifest, _geometry_value = render_multiperiod_png_with_geometry(
        views, options, output_path, combined_daily_bars=combined_daily_bars
    )
    return manifest


def render_period_png_with_geometry(
    view: ChartView,
    options: RenderOptions,
    output_path: str | Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Render one period using the full canvas and return its pixel geometry."""
    _validate_options(options)
    _validate_view(view)
    return _render([view], options, output_path, combined=False)


def render_period_png(
    view: ChartView,
    options: RenderOptions,
    output_path: str | Path,
) -> dict[str, Any]:
    """Render one period using the full canvas."""
    manifest, _geometry_value = render_period_png_with_geometry(view, options, output_path)
    return manifest
