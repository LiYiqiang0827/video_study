"""Independent checks for chart-tool V2 integration artifacts.

The calculations in this module are intentionally local to this file. It does
not call the renderers, multiperiod transforms, reveal calculations, or pixel
checker. Only contracts/cases are imported for schema validation.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, time
import json
import math
from pathlib import Path
from typing import Any

import pandas as pd
from PIL import Image, ImageColor

from chart_tool.contracts import ChartView
from chart_tool.cases import load_restore_input, load_results


OHLC = ("open", "high", "low", "close")
HORIZONS = (1, 3, 5, 10, 20)
EVENT_KINDS = ("first_available", "mention_day_after_cutoff", "interval_high",
               "interval_low", "latest_available")
DISCLAIMER = "本快照不判定交易成败、不代表云聪的实际买卖价、不计算胜率。"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def iso_day(value: Any) -> str:
    raw = str(value)
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    if isinstance(value, (date, datetime)):
        return value.strftime("%Y-%m-%d")
    return date.fromisoformat(raw[:10]).isoformat()


def stamp(value: Any) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def records(view_data: dict) -> tuple[ChartView, pd.DataFrame]:
    view = ChartView.from_dict(view_data)
    view.validate()
    return view, view.bars.copy().reset_index(drop=True)


def view_json(path: Path) -> tuple[dict, ChartView, pd.DataFrame]:
    data = read_json(path)
    view, frame = records(data)
    return data, view, frame


def finite_close(left: float, right: float, *, rtol: float = 1e-12,
                 atol: float = 1e-10) -> bool:
    return math.isfinite(float(left)) and math.isfinite(float(right)) and math.isclose(
        float(left), float(right), rel_tol=rtol, abs_tol=atol)


class Snapshot:
    def __init__(self, root: Path):
        self.root = root
        self.cache: dict[str, tuple[pd.DataFrame, pd.DataFrame]] = {}

    def series(self, code: str) -> tuple[pd.DataFrame, pd.DataFrame]:
        if code not in self.cache:
            daily = pd.read_parquet(self.root / f"{code}.daily.parquet").copy()
            factors = pd.read_parquet(self.root / f"{code}.factors.parquet").copy()
            daily["trade_date"] = daily.trade_date.map(iso_day)
            factors["trade_date"] = factors.trade_date.map(iso_day)
            daily = daily.sort_values("trade_date", kind="stable").reset_index(drop=True)
            factors = factors.sort_values("trade_date", kind="stable").reset_index(drop=True)
            self.cache[code] = (daily, factors)
        return self.cache[code]

    def factor_map(self, code: str) -> dict[str, float]:
        _, factors = self.series(code)
        return dict(zip(factors.trade_date.astype(str), factors.adj_factor.astype(float)))


def target_name(target: dict, index: int) -> str:
    paths = [target.get("png"), target.get("result")]
    for raw in paths:
        if raw:
            return Path(raw).name
    return f"target-{index + 1}"


def append_check(output: list[dict], target: str, name: str, fn) -> None:
    try:
        detail = fn()
        output.append({"target": target, "check": name, "status": "passed", "detail": detail})
    except Exception as exc:
        output.append({"target": target, "check": name, "status": "failed",
                       "detail": {"error": type(exc).__name__, "message": str(exc)}})


def check_cutoffs(data: dict, view: ChartView, frame: pd.DataFrame) -> dict:
    meta = view.metadata
    requested = stamp(meta["requested_cutoff"])
    effective = stamp(meta["effective_cutoff"])
    period = view.period
    if period in {"daily", "reveal"}:
        days = [iso_day(value) for value in frame.trade_date.tolist()]
        late = [day for day in days if date.fromisoformat(day) > effective.date()]
        if late:
            raise AssertionError(f"daily bars after effective cutoff: {late[:3]}")
    else:
        ends = [stamp(value) for value in frame.bar_end.tolist()]
        late = [value.isoformat() for value in ends if value > effective]
        if late:
            raise AssertionError(f"minute bars after effective cutoff: {late[:3]}")
    lineage = meta.get("factor_lineage") or {}
    anchor = lineage.get("anchor_trade_date")
    if meta.get("stage") == "restore" and anchor and iso_day(anchor) > effective.date().isoformat():
        raise AssertionError("restore anchor is later than the effective cutoff")
    auctions = meta.get("auctions")
    if auctions is not None:
        if not isinstance(auctions, dict):
            raise AssertionError("auctions is not a mapping")
        for day, auction in auctions.items():
            available = stamp(auction["available_at"])
            if available > requested:
                raise AssertionError(f"auction {day} became available after requested cutoff")
    return {"period": period, "bars": len(frame), "requested": requested.isoformat(),
            "effective": effective.isoformat(), "anchor_trade_date": anchor,
            "auctions_checked": len(auctions or {})}


def check_daily_formula(view: ChartView, frame: pd.DataFrame, snapshot: Snapshot,
                        *, legacy_profile: bool = False,
                        views: dict[str, tuple[dict, ChartView, pd.DataFrame]] | None = None) -> dict:
    code = view.ts_code
    daily, factors = snapshot.series(code)
    factor_by_day = dict(zip(factors.trade_date.astype(str), factors.adj_factor.astype(float)))
    daily_by_day = daily.set_index("trade_date", drop=False)
    lineage = view.metadata.get("factor_lineage") or {}
    anchor_day = iso_day(lineage["anchor_trade_date"])
    anchor_value = float(lineage["anchor_value"])
    if anchor_day not in factor_by_day:
        raise AssertionError(f"anchor factor is absent from SNAP: {anchor_day}")
    if not finite_close(factor_by_day[anchor_day], anchor_value, rtol=0.0, atol=1e-14):
        raise AssertionError("view anchor factor differs from SNAP")
    excluded: list[str] = []
    if legacy_profile and len(frame) and "is_partial" in frame and bool(frame.iloc[-1].is_partial):
        last_day = iso_day(frame.iloc[-1].trade_date)
        if last_day == iso_day(view.metadata["requested_cutoff"]):
            excluded = [last_day]
    checked = 0
    for row in frame.to_dict("records"):
        day = iso_day(row["trade_date"])
        if day in excluded:
            continue
        if day not in daily_by_day.index:
            raise AssertionError(f"SNAP daily row is missing: {day}")
        if day not in factor_by_day:
            raise AssertionError(f"SNAP factor is missing: {day}")
        source = daily_by_day.loc[day]
        multiplier = float(factor_by_day[day]) / anchor_value
        for col in OHLC:
            expected = float(source[col]) * multiplier
            actual = float(row[col])
            if not finite_close(actual, expected, rtol=1e-12, atol=1e-10):
                raise AssertionError(f"{day} {col}: {actual} != SNAP formula {expected}")
        expected_vol = float(source.vol) * 100.0
        actual_vol = float(row["vol"])
        if actual_vol != expected_vol:
            raise AssertionError(f"{day} volume {actual_vol} != raw lots * 100 ({expected_vol})")
        checked += 1
    partial_detail = None
    if excluded:
        if not views or "1m" not in views:
            raise AssertionError("legacy partial daily bar has no target 1m view for independent reconstruction")
        _minute_data, minute_view, minute_frame = views["1m"]
        if minute_view.ts_code != view.ts_code:
            raise AssertionError("legacy daily and 1m views have different securities")
        minute_lineage = minute_view.metadata.get("factor_lineage") or {}
        if (iso_day(minute_lineage.get("anchor_trade_date")) != anchor_day or
                not finite_close(minute_lineage.get("anchor_value"), anchor_value, rtol=0.0, atol=1e-14)):
            raise AssertionError("legacy daily and 1m views do not share the same adjustment anchor")
        day = excluded[0]
        minute_day = minute_frame.loc[minute_frame.trade_date.map(iso_day) == day].copy()
        if minute_day.empty:
            raise AssertionError(f"legacy partial daily bar {day} has no 1m rows")
        minute_day = minute_day.sort_values("bar_end", kind="stable")
        auction = (minute_view.metadata.get("auctions") or {}).get(day)
        high_values = minute_day.high.astype(float).tolist()
        low_values = minute_day.low.astype(float).tolist()
        expected_open = float(minute_day.iloc[0].open)
        expected_vol = float(minute_day.vol.sum())
        expected_amount = float(minute_day.amount.sum())
        if auction:
            auction_price = float(auction["price"])
            expected_open = auction_price
            high_values.append(auction_price)
            low_values.append(auction_price)
            expected_vol += float(auction["vol"])
            expected_amount += float(auction["amount"])
        expected_partial = {
            "open": expected_open,
            "high": max(high_values),
            "low": min(low_values),
            "close": float(minute_day.iloc[-1].close),
            "vol": expected_vol,
            "amount": expected_amount,
        }
        partial_row = frame.iloc[-1]
        for field in OHLC:
            if not finite_close(partial_row[field], expected_partial[field], rtol=1e-12, atol=1e-10):
                raise AssertionError(f"legacy partial daily {day} {field} differs from independent 1m aggregate")
        if float(partial_row.vol) != expected_partial["vol"]:
            raise AssertionError(f"legacy partial daily {day} volume differs from independent 1m sum")
        if not finite_close(partial_row.amount, expected_partial["amount"], rtol=0.0, atol=1e-8):
            raise AssertionError(f"legacy partial daily {day} amount differs from independent 1m sum")
        partial_detail = {"trade_date": day, "minute_rows": len(minute_day),
                          "auction_folded": bool(auction), "ohlc_volume_amount_rebuilt": True}
    return {"source": "SNAP raw daily × factor / anchor", "price_rtol": 1e-12,
            "price_atol": 1e-10, "volume": "exact raw lots × 100", "rows_checked": checked,
            "legacy_partial_rows_rebuilt_from_1m": partial_detail}


def session_slot(end_text: str) -> int:
    end = stamp(end_text)
    minute = end.hour * 60 + end.minute
    if 571 <= minute <= 690:
        return minute - 570
    if 781 <= minute <= 900:
        return 120 + minute - 780
    raise AssertionError(f"minute bar outside A-share session: {end_text}")


def expected_15m(one: pd.DataFrame, auction_map: dict) -> list[dict]:
    result = []
    if one.empty:
        return result
    work = one.copy()
    work["_slot"] = work.bar_end.map(session_slot)
    work["_group"] = (work["_slot"] - 1) // 15
    for (day, group_id), group in work.groupby(["trade_date", "_group"], sort=True):
        group = group.sort_values("bar_end", kind="stable")
        auction = auction_map.get(str(day)) if group_id == 0 else None
        first = group.iloc[0]
        highs = group.high.astype(float).tolist()
        lows = group.low.astype(float).tolist()
        opening = float(first.open)
        volume = float(group.vol.sum())
        amount = float(group.amount.sum())
        if auction:
            price = float(auction["price"])
            opening = price
            highs.append(price)
            lows.append(price)
            volume += float(auction["vol"])
            amount += float(auction["amount"])
        result.append({"trade_date": str(day), "open": opening, "high": max(highs),
                       "low": min(lows), "close": float(group.iloc[-1].close),
                       "vol": volume, "amount": amount,
                       "bar_start": str(first.bar_start), "bar_end": str(group.iloc[-1].bar_end),
                       "is_partial": len(group) < 15})
    return result


def check_15m_rebuild(views: dict[str, tuple[dict, ChartView, pd.DataFrame]]) -> dict:
    if "1m" not in views or "15m" not in views:
        return {"status": "not_applicable", "reason": "target has no 1m and 15m view pair"}
    one_data, one_view, one = views["1m"]
    fifteen_data, fifteen_view, fifteen = views["15m"]
    auctions = one_view.metadata.get("auctions") or {}
    expected = expected_15m(one, auctions)
    if len(expected) != len(fifteen):
        raise AssertionError(f"15m row count {len(fifteen)} != independently grouped 1m count {len(expected)}")
    fields = ("trade_date", *OHLC, "vol", "amount", "bar_start", "bar_end", "is_partial")
    for index, (actual, want) in enumerate(zip(fifteen.to_dict("records"), expected)):
        for field in fields:
            if field in OHLC:
                if not finite_close(actual[field], want[field], rtol=1e-12, atol=1e-10):
                    raise AssertionError(f"15m[{index}] {field}: {actual[field]} != {want[field]}")
            elif field in {"vol", "amount"}:
                if not finite_close(actual[field], want[field], rtol=0.0, atol=1e-8):
                    raise AssertionError(f"15m[{index}] {field}: {actual[field]} != {want[field]}")
            elif actual[field] != want[field]:
                raise AssertionError(f"15m[{index}] {field}: {actual[field]} != {want[field]}")
    return {"rows": len(expected), "source": "target 1m rows plus view auction metadata",
            "auction_days_used": sorted(auctions)}


def check_avg_price(views: dict[str, tuple[dict, ChartView, pd.DataFrame]],
                    snapshot: Snapshot) -> dict:
    checked, legacy = [], []
    for period, (_data, view, frame) in views.items():
        if period != "intraday":
            continue
        if "avg_price" not in frame.columns:
            legacy.append({"period": period, "compatibility": "legacy intraday schema omits avg_price"})
            continue
        code = view.ts_code
        factor_by_day = snapshot.factor_map(code)
        lineage = view.metadata.get("factor_lineage") or {}
        anchor = float(lineage["anchor_value"])
        auctions = view.metadata.get("auctions") or {}
        for day, group in frame.groupby("trade_date", sort=True):
            running_amount = float((auctions.get(str(day)) or {}).get("amount", 0.0))
            running_volume = float((auctions.get(str(day)) or {}).get("vol", 0.0))
            multiplier = float(factor_by_day[str(day)]) / anchor
            for row in group.sort_values("bar_end", kind="stable").to_dict("records"):
                running_amount += float(row["amount"])
                running_volume += float(row["vol"])
                if running_volume <= 0:
                    raise AssertionError(f"{day} cumulative volume is not positive")
                expected = running_amount / running_volume * multiplier
                if not finite_close(row["avg_price"], expected, rtol=1e-10, atol=1e-10):
                    raise AssertionError(f"{day} {row['bar_end']} avg_price {row['avg_price']} != {expected}")
            checked.append(str(day))
    return {"days_checked": checked, "legacy_compatibility": legacy,
            "formula": "cumulative amount / volume × day factor / anchor factor"}


def check_prev_close(views: dict[str, tuple[dict, ChartView, pd.DataFrame]],
                     snapshot: Snapshot) -> dict:
    checked, legacy = [], []
    for period, (_data, view, frame) in views.items():
        metadata = view.metadata
        prev = metadata.get("prev_close")
        if prev is None:
            legacy.append(period)
            continue
        if not isinstance(prev, dict):
            raise AssertionError(f"{period}.prev_close is not a mapping")
        daily, _ = snapshot.series(view.ts_code)
        factor_by_day = snapshot.factor_map(view.ts_code)
        close_by_day = dict(zip(daily.trade_date.astype(str), daily.close.astype(float)))
        anchor = float(metadata["factor_lineage"]["anchor_value"])
        days = sorted(set(iso_day(day) for day in frame.trade_date.tolist()))
        for day in days:
            earlier = [candidate for candidate in close_by_day if candidate < day]
            if not earlier:
                raise AssertionError(f"no prior daily close in SNAP for {day}")
            prior_day = earlier[-1]
            expected = close_by_day[prior_day] * factor_by_day[prior_day] / anchor
            if day not in prev or not finite_close(prev[day], expected, rtol=1e-12, atol=1e-10):
                raise AssertionError(f"{period} prev_close[{day}] differs from prior SNAP close {prior_day}")
            checked.append(f"{period}:{day}")
    return {"entries_checked": checked, "legacy_compatibility": legacy}


def load_target_views(target: dict) -> dict[str, tuple[dict, ChartView, pd.DataFrame]]:
    paths: dict[str, Path] = {}
    if isinstance(target.get("views"), dict):
        paths.update({key: Path(value) for key, value in target["views"].items()})
    elif target.get("view"):
        data = read_json(Path(target["view"]))
        period = data.get("period", "daily")
        paths[period] = Path(target["view"])
    result = {}
    for period, path in paths.items():
        data, view, frame = view_json(path)
        if view.period != period:
            raise AssertionError(f"view key {period} does not match schema period {view.period}")
        result[period] = (data, view, frame)
    return result


def check_geometry_and_pixels(target: dict, views: dict[str, tuple[dict, ChartView, pd.DataFrame]]) -> dict:
    geometry = read_json(Path(target["geometry"]))
    manifest = read_json(Path(target["manifest"]))
    png_path = Path(target["png"])
    with Image.open(png_path) as opened:
        image = opened.convert("RGB")
    width, height = image.size
    background = ImageColor.getrgb(geometry.get("background", "#ffffff"))
    panels = geometry.get("panels") or {}
    if not panels:
        raise AssertionError("geometry has no panels")
    checks = []
    for panel_name, panel in panels.items():
        bars = panel.get("bars") or []
        if panel_name not in views:
            if len(panels) == 1 and len(views) == 1:
                view_key = next(iter(views))
            else:
                raise AssertionError(f"geometry panel {panel_name} has no matching view")
        else:
            view_key = panel_name
        _data, view, frame = views[view_key]
        if view.period == "daily":
            view_keys = [iso_day(value) for value in frame.trade_date.tolist()]
        else:
            view_keys = [str(value) for value in frame.bar_end.tolist()]
        bar_keys = [str(bar["key"]) for bar in bars]
        if target.get("type") == "multi" and panel_name == "daily":
            daily_bars = target.get("daily_bars", 250)
            if type(daily_bars) is not int or daily_bars < 1:
                raise AssertionError("multi target daily_bars must be a positive integer")
            expected_keys = view_keys[-daily_bars:]
        else:
            expected_keys = view_keys
        if bar_keys != expected_keys:
            raise AssertionError(f"{panel_name} geometry keys do not exactly cover the expected displayed view rows")
        expected_count = len(expected_keys)
        panel_manifest = (manifest.get("panels") or {}).get(panel_name, {})
        displayed = panel_manifest.get("displayed_rows")
        if len(panels) == 1:
            manifest_counts = {
                "input_rows": manifest.get("input_rows"),
                "candle_count": manifest.get("candle_count"),
                "volume_bar_count": manifest.get("volume_bar_count"),
            }
            if any(value is None or int(value) != expected_count for value in manifest_counts.values()):
                raise AssertionError(f"single-chart manifest counts differ from all view rows: {manifest_counts}")
            displayed = expected_count
            manifest_start = manifest.get("actual_start")
            manifest_end = manifest.get("actual_end")
        else:
            if not panel_manifest:
                raise AssertionError(f"multi manifest lacks panel {panel_name}")
            required_counts = {"input_rows": panel_manifest.get("input_rows"),
                               "displayed_rows": displayed,
                               "volume_bar_count": panel_manifest.get("volume_bar_count")}
            if view.period == "intraday":
                required_counts["line_point_count"] = panel_manifest.get("line_point_count")
                if panel_manifest.get("candle_count") != 0:
                    raise AssertionError(f"intraday manifest candle_count must be 0: {panel_manifest.get('candle_count')}")
            else:
                required_counts["candle_count"] = panel_manifest.get("candle_count")
            if any(value is None or int(value) != expected_count for value in required_counts.values()):
                raise AssertionError(f"{panel_name} manifest counts differ from expected displayed rows: {required_counts}")
            manifest_start = panel_manifest.get("actual_start")
            manifest_end = panel_manifest.get("actual_end")
        if manifest_start != expected_keys[0] or manifest_end != expected_keys[-1]:
            raise AssertionError(f"{panel_name} manifest range differs from complete displayed keys")
        x_floors = [math.floor(float(bar["x_px"])) for bar in bars]
        if len(x_floors) != len(set(x_floors)):
            raise AssertionError(f"{panel_name} floor(x_px) collisions")
        baseline = float(panel["volume_baseline_y_px"])
        probes = 0
        for bar in bars:
            if float(bar.get("vol", 0.0)) <= 0:
                continue
            x = math.floor(float(bar["x_px"]))
            y = math.floor(baseline - 1.0)
            if not (0 <= x < width and 0 <= y < height):
                raise AssertionError(f"{panel_name} volume probe is outside PNG bounds")
            if image.getpixel((x, y)) == background:
                raise AssertionError(f"{panel_name} positive-volume probe is background at ({x},{y})")
            probes += 1
        axes = panel.get("price_axes_px")
        right = panel.get("last_bar_right_px")
        right_color_pixels = 0
        if axes and right is not None and bars:
            color_set = {tuple(bar.get("candle_rgb", ())) for bar in bars}
            color_set.discard(())
            x0 = max(0, math.ceil(float(right)) + 1)
            x1 = min(width, math.ceil(float(axes[2]) + 1))
            y0 = max(0, math.floor(float(axes[1])))
            y1 = min(height, math.ceil(float(axes[3]) + 1))
            for y in range(y0, y1):
                for x in range(x0, x1):
                    if image.getpixel((x, y)) in color_set:
                        right_color_pixels += 1
            if right_color_pixels:
                raise AssertionError(f"{panel_name} has {right_color_pixels} up/down-color pixels right of last bar")
        checks.append({"panel": panel_name, "view_period": view.period,
                       "geometry_bars": len(bars), "displayed_rows": displayed,
                       "unique_floor_x": len(x_floors), "positive_volume_probes": probes,
                       "right_of_last_bar_color_pixels": right_color_pixels})
    return {"png_size": [width, height], "panels": checks,
            "probe": "center column, one pixel above volume baseline; background-only test"}


def nested_find(obj: Any, key: str) -> list[Any]:
    found = []
    if isinstance(obj, dict):
        if key in obj:
            found.append(obj[key])
        for value in obj.values():
            found.extend(nested_find(value, key))
    elif isinstance(obj, list):
        for value in obj:
            found.extend(nested_find(value, key))
    return found


def check_subsequent_daily_source(rows: list[dict], snapshot: Snapshot, code: str,
                                 reference_day: str, observed_through: str) -> dict:
    daily, _factors = snapshot.series(code)
    daily_by_day = daily.set_index("trade_date", drop=False)
    observed = stamp(observed_through)
    if observed.time() >= time(15, 0):
        last_closed_day = observed.date().isoformat()
    else:
        before = [str(day) for day in daily.trade_date.tolist()
                  if str(day) < observed.date().isoformat()]
        last_closed_day = before[-1] if before else ""
    normalized = [(iso_day(row["trade_date"]), row) for row in rows]
    after_cutoff = [day for day, _row in normalized if day > last_closed_day]
    if after_cutoff:
        raise AssertionError(f"subsequent_daily contains rows after last observed closed day: {after_cutoff[:3]}")
    for day, row in normalized:
        if day not in daily_by_day.index:
            raise AssertionError(f"subsequent_daily date is absent from SNAP raw daily: {day}")
        source = daily_by_day.loc[day]
        for col in OHLC:
            raw_key = f"raw_{col}"
            if raw_key not in row or float(row[raw_key]) != float(source[col]):
                raise AssertionError(f"{day} {raw_key} differs from SNAP raw daily")
        expected_vol = float(source.vol) * 100.0
        expected_amount = float(source.amount) * 1000.0
        if float(row["vol"]) != expected_vol:
            raise AssertionError(f"{day} volume differs from SNAP raw lots × 100")
        if float(row["amount"]) != expected_amount:
            raise AssertionError(f"{day} amount differs from SNAP raw thousand-CNY × 1000")
    expected_days = [str(day) for day in daily.trade_date.tolist()
                     if reference_day <= str(day) <= last_closed_day]
    actual_days = [day for day, _row in normalized if reference_day <= day <= last_closed_day]
    if actual_days != expected_days:
        raise AssertionError("subsequent_daily does not contain the complete SNAP trading-day sequence from reference through observed cutoff")
    return {"raw_source_rows_checked": len(normalized), "reference_trade_date": reference_day,
            "last_observed_closed_day": last_closed_day,
            "complete_session_rows_checked": len(expected_days),
            "source": "SNAP raw OHLC, lots × 100, thousand-CNY × 1000"}


def validate_result_target(target: dict, snapshot: Snapshot) -> dict:
    result_path = Path(target["result"])
    result = read_json(result_path)
    store = Path(target["store"])
    case_id = str(target["case_id"])
    revision = int(target["revision"])
    frozen = load_restore_input(store, case_id, revision)
    all_results = load_results(store, case_id, revision)
    sequence = int(result["result_sequence"])
    if sequence < 1 or sequence > len(all_results) or all_results[sequence - 1] != result:
        raise AssertionError("target result differs from cases.load_results output")
    if "path_snapshot" not in result:
        return {"schema": "legacy result without Amendment 3 path_snapshot",
                "sequence": sequence, "result_count": len(all_results),
                "legacy_compatibility": True}
    path = result["path_snapshot"]
    recorded_policy = result.get("mention_day", {}).get("policy")
    daily_only_policies = {"non_trading_day", "preopen_full_day", "auction_window",
                           "excluded_no_minutes", "postclose_excluded"}
    if recorded_policy in {"split_by_minutes", "lunch_split"}:
        raise AssertionError("only daily-only reveal policies are supported; minute split paths are not verified")
    if recorded_policy not in daily_only_policies:
        raise AssertionError(f"reveal policy is outside the supported daily-only scope: {recorded_policy}")
    daily_file = read_json(Path(target["subsequent_daily"]))
    rows = daily_file.get("rows") if isinstance(daily_file, dict) else None
    if not isinstance(rows, list) or not rows:
        raise AssertionError("subsequent_daily.json has no rows")
    rows = sorted(rows, key=lambda row: iso_day(row["trade_date"]))
    dates = [iso_day(row["trade_date"]) for row in rows]
    if dates != sorted(set(dates)):
        raise AssertionError("subsequent_daily dates are not unique and ascending")
    source_daily, factors = snapshot.series(frozen["request"]["ts_code"])
    factor_by_day = dict(zip(factors.trade_date.astype(str), factors.adj_factor.astype(float)))

    frozen_lineages = nested_find(frozen.get("view_identities", {}), "factor_lineage")
    anchors = [(iso_day(item["anchor_trade_date"]), float(item["anchor_value"]))
               for item in frozen_lineages if isinstance(item, dict) and
               item.get("anchor_trade_date") and item.get("anchor_value") is not None]
    if anchors:
        anchor_days = {item[0] for item in anchors}
        anchor_values = {item[1] for item in anchors}
        if len(anchor_days) != 1 or len(anchor_values) != 1:
            raise AssertionError("frozen views do not share a single factor anchor")
        anchor_day, anchor_value = anchors[0]
        expected_anchor_source = "frozen_restore_view"
    else:
        effective = stamp(frozen["request"]["cutoff"])
        cutoff_day = effective.date().isoformat()
        daily_dates = sorted(set(source_daily.trade_date.astype(str)))
        closed_days = [day for day in daily_dates
                       if day < cutoff_day or (day == cutoff_day and effective.time() >= time(15, 0))]
        if not closed_days:
            raise AssertionError("no fallback factor is available before cutoff")
        anchor_day = closed_days[-1]
        if anchor_day not in factor_by_day:
            raise AssertionError("fallback anchor factor is absent from SNAP")
        anchor_value = factor_by_day[anchor_day]
        expected_anchor_source = "fallback_last_closed"
    if anchor_day not in factor_by_day or not finite_close(factor_by_day[anchor_day], anchor_value,
                                                            rtol=0.0, atol=1e-14):
        raise AssertionError("SNAP anchor factor differs from frozen anchor")
    actual_anchor = result["price_anchor"]
    if actual_anchor.get("anchor_trade_date") != anchor_day or not finite_close(
            actual_anchor.get("anchor_value"), anchor_value, rtol=0.0, atol=1e-14):
        raise AssertionError("result price_anchor differs from independently recovered anchor")
    if actual_anchor.get("source") != expected_anchor_source:
        raise AssertionError("result price_anchor source differs from frozen record")

    for row in rows:
        day = iso_day(row["trade_date"])
        if day not in factor_by_day:
            raise AssertionError(f"SNAP factor missing for subsequent daily row {day}")
        multiplier = factor_by_day[day] / anchor_value
        if not finite_close(row["multiplier"], multiplier, rtol=1e-12, atol=1e-12):
            raise AssertionError(f"{day} multiplier differs from SNAP factor / anchor")
        for col in OHLC:
            raw_key = f"raw_{col}"
            if raw_key not in row or col not in row:
                raise AssertionError(f"subsequent row {day} lacks raw/qfq {col}")
            expected = float(row[raw_key]) * multiplier
            if not finite_close(row[col], expected, rtol=1e-12, atol=1e-10):
                raise AssertionError(f"{day} {col} does not equal raw price × factor / anchor")

    request_cutoff = stamp(frozen["request"]["cutoff"])
    mention_day = request_cutoff.date().isoformat()
    mention_clock = request_cutoff.timetz().replace(tzinfo=None)
    trading_days = set(source_daily.trade_date.astype(str))
    has_minutes = False  # These restored cases have no accepted registry source.
    if mention_day not in trading_days:
        expected_policy = "non_trading_day"
    elif mention_clock < time(9, 25):
        expected_policy = "preopen_full_day"
    elif mention_clock < time(9, 30):
        expected_policy = "auction_window"
    elif mention_clock >= time(15, 0):
        expected_policy = "postclose_excluded"
    elif has_minutes:
        expected_policy = "split_by_minutes" if not (time(11, 30) <= mention_clock < time(13, 0)) else "lunch_split"
    else:
        expected_policy = "excluded_no_minutes"
    mention = result["mention_day"]
    if mention["trade_date"] != mention_day or mention["policy"] != expected_policy:
        raise AssertionError(f"mention policy/date {mention['policy']}/{mention['trade_date']} != {expected_policy}/{mention_day}")
    reference = path["reference"]
    if expected_policy in {"preopen_full_day", "auction_window"}:
        reference_day = mention_day
        expected_kind = "next_session_open" if expected_policy == "preopen_full_day" else "auction_price"
    else:
        later = [day for day in dates if day > mention_day]
        if expected_policy == "non_trading_day":
            later = [day for day in dates if day > mention_day]
        if not later:
            raise AssertionError("no subsequent daily session for reference")
        reference_day = later[0]
        expected_kind = "next_session_open"
    if reference["trade_date"] != reference_day or reference["kind"] != expected_kind:
        raise AssertionError("reference kind/date does not match mention cutoff policy")
    ref_row = next((row for row in rows if iso_day(row["trade_date"]) == reference_day), None)
    if ref_row is None:
        raise AssertionError("reference trade date is missing from subsequent_daily")
    raw_reference = float(ref_row["raw_open"])
    adjusted_reference = raw_reference * factor_by_day[reference_day] / anchor_value
    if not finite_close(reference["raw_price"], raw_reference, rtol=1e-12, atol=1e-10):
        raise AssertionError("reference raw_price differs from daily open")
    if not finite_close(reference["price"], adjusted_reference, rtol=1e-12, atol=1e-10):
        raise AssertionError("reference price differs from adjusted daily open")
    if reference["at"] != reference_day + "T09:25:00+08:00":
        raise AssertionError("open/auction reference timestamp is not 09:25 on the reference day")
    if expected_policy == "auction_window" and "竞价" not in str(mention.get("note") or ""):
        raise AssertionError("auction-window note does not state that daily high/low includes auction")

    source_sequence = check_subsequent_daily_source(
        rows, snapshot, frozen["request"]["ts_code"], reference_day, result["observed_through"])
    last_closed_day = source_sequence["last_observed_closed_day"]
    path_rows = [row for row in rows if reference_day <= iso_day(row["trade_date"]) <= last_closed_day]
    if not path_rows:
        raise AssertionError("no observation rows from reference through observed_through")
    horizons = path.get("horizons") or []
    if [item.get("k") for item in horizons] != list(HORIZONS):
        raise AssertionError("horizon order is not 1,3,5,10,20")
    for item, k in zip(horizons, HORIZONS):
        if len(path_rows) < k:
            if item.get("complete") or any(item.get(field) is not None for field in
                                             ("trade_date", "close", "close_return", "mfe", "mfe_trade_date", "mae", "mae_trade_date")):
                raise AssertionError(f"k={k} incomplete horizon is not all null")
            continue
        sample = path_rows[:k]
        close_row = sample[-1]
        high_row = max(sample, key=lambda row: float(row["high"]))
        low_row = min(sample, key=lambda row: float(row["low"]))
        expected_values = {
            "trade_date": iso_day(close_row["trade_date"]),
            "close": float(close_row["close"]),
            "close_return": float(close_row["close"]) / adjusted_reference - 1.0,
            "mfe": max(float(row["high"]) for row in sample) / adjusted_reference - 1.0,
            "mfe_trade_date": iso_day(high_row["trade_date"]),
            "mae": min(float(row["low"]) for row in sample) / adjusted_reference - 1.0,
            "mae_trade_date": iso_day(low_row["trade_date"]),
        }
        if not item.get("complete"):
            raise AssertionError(f"k={k} has enough rows but result marks it incomplete")
        for key, expected in expected_values.items():
            actual = item.get(key)
            if isinstance(expected, str):
                if actual != expected:
                    raise AssertionError(f"k={k} {key} {actual} != {expected}")
            elif not finite_close(actual, expected, rtol=1e-12, atol=1e-12):
                raise AssertionError(f"k={k} {key} {actual} != {expected}")
    if result.get("complete") != all(bool(item.get("complete")) for item in horizons):
        raise AssertionError("result complete does not equal all horizon completion flags")
    if DISCLAIMER not in path.get("notes", []):
        raise AssertionError("fixed_horizon_v1 disclaimer is missing")

    expected_event_rows = path_rows
    expected_nodes = {
        "first_available": expected_event_rows[0],
        "interval_high": max(expected_event_rows, key=lambda row: float(row["high"])),
        "interval_low": min(expected_event_rows, key=lambda row: float(row["low"])),
        "latest_available": expected_event_rows[-1],
    }
    if expected_policy in {"split_by_minutes", "lunch_split"}:
        raise AssertionError("minute-based mention-day event cannot be independently recomputed from daily-only target")
    events = result.get("events", [])
    kinds = [event.get("kind") for event in events]
    if len(kinds) != len(set(kinds)):
        raise AssertionError("event kinds are not unique")
    ranks = [EVENT_KINDS.index(kind) for kind in kinds]
    ordered = sorted(events, key=lambda event: (stamp(event["at"]), EVENT_KINDS.index(event["kind"])))
    if events != ordered or ranks != [EVENT_KINDS.index(event["kind"]) for event in events]:
        raise AssertionError("event ordering is incorrect")
    if set(kinds) != set(expected_nodes):
        raise AssertionError(f"event kinds {kinds} != expected {list(expected_nodes)}")
    anchor_factor_text = f"因子{anchor_value:g}"
    for kind, row in expected_nodes.items():
        event = next(event for event in events if event["kind"] == kind)
        day = iso_day(row["trade_date"])
        if event["at"] != day + "T15:00:00+08:00":
            raise AssertionError(f"{kind} event date/time differs from independent node date {day}")
        description = event["description"]
        expected_fragments = [f"基准日{anchor_day}", anchor_factor_text]
        for prefix, field in (("O", "open"), ("H", "high"), ("L", "low"), ("C", "close")):
            expected_fragments.append(f"{prefix}={float(row[field]):g}")
            expected_fragments.append(f"{prefix}={float(row['raw_' + field]):g}")
        expected_fragments.extend((f"真实量={float(row['vol']):g}股", "日线节点日期不等于极值的准确时刻", "区间节点不表示实际买卖"))
        missing = [part for part in expected_fragments if part not in description]
        if missing:
            raise AssertionError(f"{kind} event description differs from recomputed node: {missing[:3]}")
    return {"anchor": {"date": anchor_day, "value": anchor_value, "source": expected_anchor_source},
            "policy": expected_policy, "reference": {"date": reference_day, "kind": expected_kind,
            "raw_price": raw_reference, "adjusted_price": adjusted_reference},
            "daily_only_policy_scope": sorted(daily_only_policies),
            "subsequent_source_sequence": source_sequence,
            "subsequent_rows": len(rows), "horizons_recomputed": list(HORIZONS),
            "events_recomputed": list(expected_nodes)}


def run(targets_path: Path, snapshot_dir: Path, output_path: Path) -> dict:
    snapshot = Snapshot(snapshot_dir.expanduser().resolve())
    targets = read_json(targets_path.expanduser().resolve())
    if not isinstance(targets, list):
        raise ValueError("targets file must contain a JSON array")
    checks: list[dict] = []
    for index, target in enumerate(targets):
        name = target_name(target, index)
        kind = target.get("type")
        if kind == "reveal_result":
            append_check(checks, name, "reveal_anchor_reference_horizons_events", lambda t=target: validate_result_target(t, snapshot))
            continue
        views: dict[str, tuple[dict, ChartView, pd.DataFrame]] = {}
        try:
            views = load_target_views(target)
        except Exception as exc:
            checks.append({"target": name, "check": "view_schema", "status": "failed",
                           "detail": {"error": type(exc).__name__, "message": str(exc)}})
            continue
        append_check(checks, name, "view_schema", lambda v=views: {
            "periods": {period: {"code": triplet[1].ts_code, "rows": len(triplet[2])}
                        for period, triplet in v.items()}})
        for period, (data, view, frame) in views.items():
            append_check(checks, name, f"cutoff_{period}",
                         lambda d=data, v=view, f=frame: check_cutoffs(d, v, f))
            if period == "daily":
                append_check(checks, name, "daily_price_and_volume", lambda v=view, f=frame, t=target, all_views=views:
                             check_daily_formula(v, f, snapshot, legacy_profile=bool(t.get("legacy_profile")),
                                                 views=all_views))
        if kind == "multi":
            append_check(checks, name, "fifteen_minute_reaggregation", lambda v=views: check_15m_rebuild(v))
            append_check(checks, name, "intraday_average_price", lambda v=views: check_avg_price(v, snapshot))
            append_check(checks, name, "previous_close", lambda v=views: check_prev_close(v, snapshot))
        if target.get("geometry") and target.get("manifest") and target.get("png"):
            append_check(checks, name, "geometry_and_independent_pixel_probe",
                         lambda t=target, v=views: check_geometry_and_pixels(t, v))
        else:
            checks.append({"target": name, "check": "geometry_and_independent_pixel_probe", "status": "failed",
                           "detail": "target is missing PNG/geometry/manifest paths"})
    failed = [item for item in checks if item["status"] != "passed"]
    result = {"task_id": "V2-L4", "contract_version": "chart.v1", "status": "failed" if failed else "passed",
              "targets_path": str(targets_path), "snapshot_dir": str(snapshot_dir),
              "limitations": ["reveal_result checks support daily-only mention policies; minute-split and lunch-split paths are rejected as unverified"],
              "target_count": len(targets), "check_count": len(checks), "checks": checks}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    return result


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Independently verify V2 chart integration artifacts")
    p.add_argument("--targets", type=Path, required=True)
    p.add_argument("--snapshot-dir", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True, help="JSON output path")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        result = run(args.targets, args.snapshot_dir, args.out)
    except Exception as exc:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        result = {"task_id": "V2-L4", "status": "failed", "target_count": 0,
                  "check_count": 0, "checks": [],
                  "fatal_error": {"type": type(exc).__name__, "message": str(exc)}}
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": result.get("status"), "target_count": result.get("target_count"),
                      "check_count": result.get("check_count"), "failed": sum(c.get("status") != "passed" for c in result.get("checks", [])),
                      "out": str(args.out)}, ensure_ascii=False))
    return 0 if result.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
