"""Minute-end safety boundary and explicitly scoped snapshot adapter.

Raw bundles/profiles are privileged engineering inputs. Only validated ChartViews
leave this module; native 15m bars are never spliced into the 1m aggregation.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, time, timedelta
import hashlib
import json
from pathlib import Path
import re

import pandas as pd

from .contracts import (
    CONTRACT_VERSION, DAILY_COLUMNS, FACTOR_COLUMNS, INTRADAY_COLUMNS, LABEL_KEYS, MINUTE_COLUMNS,
    ChartError, ChartRequest, ChartView, RawBundle, frame_digest, parse_date,
    parse_timestamp, validate_source_compatibility,
)

_UNCHANGED = "ROOT_VERIFIED_UNCHANGED_FACTOR:"


def _fail(code, message):
    raise ChartError(code, message)


def _stamp(day, clock, tz):
    return datetime.combine(parse_date(day), clock, tzinfo=tz)


def _effective(raw, cutoff):
    calendar = raw.calendar
    day = cutoff.date().isoformat()
    row = calendar.loc[calendar.trade_date == day]
    if row.empty:
        _fail("CALENDAR_UNAVAILABLE", "Requested civil date lacks calendar evidence")
    if not bool(row.is_open.iloc[0]) or cutoff.time() < time(9, 30):
        prior = calendar.loc[(calendar.trade_date < day) & calendar.is_open]
        if prior.empty:
            _fail("CALENDAR_UNAVAILABLE", "Previous open date lacks calendar evidence")
        return _stamp(prior.trade_date.iloc[-1], time(15), cutoff.tzinfo)
    if time(11, 30) <= cutoff.time() < time(13):
        return _stamp(day, time(11, 30), cutoff.tzinfo)
    return min(cutoff, _stamp(day, time(15), cutoff.tzinfo))


def _grid(day, effective):
    result = []
    for start in (time(9, 30), time(13)):
        base = _stamp(day, start, effective.tzinfo)
        result.extend((base + timedelta(minutes=i)).isoformat()
                      for i in range(1, 121)
                      if base + timedelta(minutes=i) <= effective)
    return result


def _check_minutes(raw, minutes, days, effective):
    for day in days:
        expected = _grid(day, effective)
        if not expected:
            continue
        actual = minutes.loc[minutes.trade_date == day, "bar_end"].tolist()
        if not actual:
            _fail("SOURCE_UNAVAILABLE", "Requested stock-day has no visible minute evidence")
        if actual != expected:
            _fail("MISSING_MINUTE", "Visible minute grid is incomplete; no bar was fabricated")
    dates = sorted(set(minutes.trade_date))
    if dates:
        validate_source_compatibility(raw.metadata, "one_min", dates)
        evidence = raw.metadata["compatibility"]["one_min"]
        if not set(dates) <= set(evidence["checked_trade_dates"]):
            _fail("SOURCE_UNVERIFIED", "Minute stock-day is outside compatibility evidence")
        open_dates = set(raw.calendar.loc[raw.calendar.is_open, "trade_date"])
        if not set(dates) <= open_dates:
            _fail("CALENDAR_UNAVAILABLE", "Minute evidence lies outside confirmed open dates")


def _aggregate(frame, start, *, partial):
    return {"trade_date": frame.trade_date.iloc[0],
            "open": float(frame.open.iloc[0]), "high": float(frame.high.max()),
            "low": float(frame.low.min()), "close": float(frame.close.iloc[-1]),
            "vol": float(frame.vol.sum()), "amount": float(frame.amount.sum()),
            "bar_start": start, "bar_end": frame.bar_end.iloc[-1],
            "is_partial": bool(partial)}


def _fold_auction(record, auction):
    if auction is not None:
        record["open"] = float(auction["price"])
        record["high"] = max(record["high"], float(auction["price"]))
        record["low"] = min(record["low"], float(auction["price"]))
        record["vol"] += float(auction["vol"])
        record["amount"] += float(auction["amount"])
    return record


def _fifteen(minutes, include_partial, auctions=None):
    records = []
    for day, frame in minutes.groupby("trade_date", sort=False):
        for clock in (time(9, 30), time(13)):
            base = parse_timestamp(f"{day}T{clock.isoformat()}+08:00")
            for i in range(8):
                start, end = base + timedelta(minutes=15*i), base + timedelta(minutes=15*(i+1))
                group = frame.loc[(frame.bar_end > start.isoformat()) & (frame.bar_end <= end.isoformat())]
                if group.empty:
                    continue
                partial = group.bar_end.iloc[-1] != end.isoformat()
                if not partial and len(group) != 15:
                    _fail("MISSING_MINUTE", "A complete 15m bin needs fifteen exact minute endpoints")
                if not partial or include_partial:
                    record = _aggregate(group, start.isoformat(), partial=partial)
                    if clock == time(9, 30) and i == 0:
                        _fold_auction(record, (auctions or {}).get(day))
                    records.append(record)
    return pd.DataFrame(records, columns=MINUTE_COLUMNS)


def _factor_plan(raw, request, effective, dates, *, automatic=False):
    closed_day = effective.date()
    if effective.date() == parse_timestamp(request.cutoff).date() and effective.time() < time(15):
        closed_day -= timedelta(days=1)
    last_factor_date = parse_timestamp(request.cutoff).date() if automatic else effective.date()
    factors = raw.factors.loc[raw.factors.trade_date <= last_factor_date.isoformat()].copy()
    eligible = factors.loc[factors.apply(
        lambda r: (parse_date(r.trade_date) <= effective.date()) and
        ((r.available_at is None and parse_date(r.trade_date) <= closed_day)
        or (r.available_at is not None and parse_timestamp(r.available_at) <= effective)), axis=1)].copy()
    if not dates:
        return None, {}, []
    if eligible.empty:
        _fail("MISSING_FACTOR", "Visible prices have no eligible factor anchor")
    anchor = eligible.iloc[-1]
    row_factors, used, warnings = {}, {anchor.trade_date}, []
    for day in sorted(dates):
        row = factors.loc[factors.trade_date == day]
        if row.empty:
            _fail("MISSING_FACTOR", "Visible prices require an exact dated factor")
        if day in set(eligible.trade_date):
            row_factors[day] = float(row.adj_factor.iloc[0])
            used.add(day)
            continue
        proof = _UNCHANGED + day
        refs = raw.metadata["compatibility"].get("one_min", {}).get("evidence_refs", [])
        prior = factors.loc[factors.trade_date < day] if automatic else eligible.loc[eligible.trade_date < day]
        if ((not automatic and proof not in refs) or prior.empty
                or prior.trade_date.iloc[-1] not in set(eligible.trade_date)
                or float(row.adj_factor.iloc[0]) != float(prior.adj_factor.iloc[-1])):
            _fail("FACTOR_UNAVAILABLE", "Current factor lacks cutoff-safe availability or unchanged-factor proof")
        row_factors[day] = float(prior.adj_factor.iloc[-1])
        used.add(prior.trade_date.iloc[-1])
        warnings.append("CURRENT_FACTOR_UNCHANGED_FROM_PREVIOUS" if automatic
                        else "CURRENT_FACTOR_VERIFIED_UNCHANGED_PRIOR_ANCHOR")
    visible = eligible.loc[eligible.trade_date.isin(used)].reset_index(drop=True)
    item = raw.metadata["series"]["factors"]
    lineage = {"source_id": item["source_id"], "source_version": item["source_version"],
               "anchor_trade_date": anchor.trade_date, "anchor_value": float(anchor.adj_factor),
               "anchor_available_at": anchor.available_at,
               "selection": "available_at" if anchor.available_at is not None else "last_closed_date",
               "visible_factor_digest": frame_digest(visible), "visible_factor_count": len(visible),
               "formula": "raw_price * row_factor / anchor_factor"}
    return lineage, row_factors, list(dict.fromkeys(warnings))


def _build_legacy_multiperiod_views(raw: RawBundle, request: ChartRequest) -> dict[str, ChartView]:
    """Return exactly requested periods, all sliced before aggregation/adjustment."""
    request.validate()
    raw.validate(require_minutes=True)
    if raw.ts_code != request.ts_code:
        _fail("INVALID_REQUEST", "Raw stock does not match request")
    cutoff = parse_timestamp(request.cutoff)
    effective = _effective(raw, cutoff)
    current_day = effective.date().isoformat()
    start = request.minute_start or cutoff.date().isoformat()
    if request.minute_start is not None and start < raw.calendar.trade_date.iloc[0]:
        _fail("CALENDAR_UNAVAILABLE", "Minute window starts outside calendar evidence")
    if raw.one_min is None:
        _fail("SOURCE_UNAVAILABLE", "Canonical one-minute evidence is required")
    # No native 15m values or later rows enter aggregation.
    visible = raw.one_min.loc[raw.one_min.bar_end.map(lambda t: parse_timestamp(t) <= effective)].copy()
    current_needed = effective.date() == cutoff.date()
    lower = min(start, current_day) if current_needed else start
    visible = visible.loc[visible.trade_date >= lower].reset_index(drop=True)
    days = raw.calendar.loc[(raw.calendar.trade_date >= start)
                            & (raw.calendar.trade_date <= current_day) & raw.calendar.is_open,
                            "trade_date"].tolist() if any(p != "daily" for p in request.periods) else []
    if current_needed and current_day not in days:
        days.append(current_day)
    _check_minutes(raw, visible, days, effective)
    minutes = visible.loc[visible.trade_date >= start].reset_index(drop=True)
    historical = raw.daily.loc[raw.daily.trade_date < current_day if current_needed
                               else raw.daily.trade_date <= current_day].copy()
    if request.window != "all":
        years = 10 if request.window == "10y" else 5
        try:
            beginning = cutoff.date().replace(year=cutoff.year-years)
        except ValueError:
            beginning = cutoff.date().replace(year=cutoff.year-years, day=28)
        historical = historical.loc[historical.trade_date >= beginning.isoformat()]
    historical = historical.reset_index(drop=True)
    historical["bar_start"] = historical.trade_date.map(lambda d: f"{d}T09:30:00+08:00")
    historical["bar_end"] = historical.trade_date.map(lambda d: f"{d}T15:00:00+08:00")
    historical["is_partial"] = False
    daily = historical.loc[:, list(MINUTE_COLUMNS)].copy()
    day_minutes = visible.loc[visible.trade_date == current_day]
    partial_day = effective.time() < time(15)
    if current_needed and not day_minutes.empty and (not partial_day or request.include_partial):
        record = _aggregate(day_minutes, f"{current_day}T09:30:00+08:00", partial=partial_day)
        daily = pd.concat([daily, pd.DataFrame([record], columns=MINUTE_COLUMNS)], ignore_index=True)
    frames = {"daily": daily, "15m": _fifteen(minutes, request.include_partial),
              "1m": minutes.copy(), "intraday": minutes.copy()}
    dates = set().union(*(set(frames[p].trade_date) for p in request.periods))
    lineage, factors, factor_warnings = _factor_plan(raw, request, effective, dates)
    labels = {key: deepcopy(value) for key, value in raw.labels.items()
              if parse_date(value["effective_date"]) <= effective.date()
              and parse_timestamp(value["available_at"]) <= effective}
    warnings = factor_warnings[:]
    auction_difference = raw.metadata["compatibility"].get("one_min", {}).get("auction_policy") == "verified_difference"
    if auction_difference:
        warnings.append("OPENING_AUCTION_0930_EXCLUDED")
    if current_needed and not day_minutes.empty:
        warnings.append("CURRENT_DAILY_FROM_OBSERVED_MINUTES")
        if auction_difference:
            warnings.append("CURRENT_DAILY_OBSERVED_SUBSET_NOT_OFFICIAL_DAY_OPEN")
    if "SYNTHETIC_ENGINEERING_ONLY" in raw.metadata["warnings"]:
        warnings.append("SYNTHETIC_ENGINEERING_ONLY")
    gaps = []
    if not daily.empty:
        first = daily.trade_date.iloc[0]
        calendar = raw.calendar.loc[(raw.calendar.trade_date >= first) & (raw.calendar.trade_date <= current_day)]
        gaps = sorted(set(calendar.loc[calendar.is_open, "trade_date"]) - set(daily.trade_date))
        if current_needed and (not request.include_partial or not _grid(current_day, effective)):
            gaps = [d for d in gaps if d != current_day]
        if first < raw.calendar.trade_date.iloc[0]:
            warnings.append("HISTORICAL_CALENDAR_COVERAGE_UNAVAILABLE")
        if gaps:
            warnings.append("DAILY_GAPS_UNEXPLAINED")
    result = {}
    for period in request.periods:
        bars = frames[period].copy()
        if lineage is not None and not bars.empty:
            multipliers = bars.trade_date.map(factors).to_numpy() / lineage["anchor_value"]
            for column in ("open", "high", "low", "close"):
                bars[column] = bars[column].to_numpy() * multipliers
        source_keys = ["daily", "factors", "one_min"] if period == "daily" else ["one_min", "factors"]
        panel_warnings = warnings + (["FIFTEEN_MINUTES_DERIVED_FROM_ONE_MINUTE"] if period == "15m" else [])
        metadata = {"contract_version": CONTRACT_VERSION, "stage": request.stage, "window": request.window,
                    "requested_cutoff": request.cutoff, "effective_cutoff": effective.isoformat(),
                    "cutoff_source": deepcopy(request.cutoff_source), "cutoff_policy": "minute_end_v1",
                    "timezone": "Asia/Shanghai", "price_unit": "CNY/share", "volume_unit": "shares",
                    "amount_unit": "CNY", "price_basis": "fixed_anchor_qfq",
                    "source_version": raw.metadata["source_version"],
                    "source_ids": [raw.metadata["series"][key]["source_id"] for key in source_keys],
                    "factor_lineage": deepcopy(lineage), "labels": deepcopy(labels),
                    "labels_missing": [key for key in LABEL_KEYS if key not in labels],
                    "incomplete": bool(bars.is_partial.any()), "warnings": panel_warnings,
                    "gap_dates": gaps[:] if period == "daily" else []}
        view = ChartView(request.ts_code, period, bars, metadata)
        view.validate()
        result[period] = view
    return result


def _display_session(raw, cutoff):
    """Match minute_start_for_sessions(..., 1), including its 09:25 boundary."""
    day = cutoff.date().isoformat()
    current_open = bool(raw.calendar.loc[raw.calendar.trade_date == day, "is_open"].iloc[0])
    eligible = raw.calendar.loc[raw.calendar.is_open &
        (raw.calendar.trade_date <= day if current_open and cutoff.time() >= time(9, 25)
         else raw.calendar.trade_date < day), "trade_date"]
    if eligible.empty:
        _fail("CALENDAR_UNAVAILABLE", "Display session lacks calendar evidence")
    return str(eligible.iloc[-1])


def _intraday(minutes, auction):
    result = minutes.copy()
    volume = result.vol.cumsum() + (float(auction["vol"]) if auction else 0.)
    amount = result.amount.cumsum() + (float(auction["amount"]) if auction else 0.)
    # Use close only when the cumulative denominator is zero.
    result["avg_price"] = (amount / volume.where(volume != 0)).where(volume != 0, result.close)
    return result.loc[:, list(INTRADAY_COLUMNS)]


def build_multiperiod_views(raw: RawBundle, request: ChartRequest) -> dict[str, ChartView]:
    """Slice canonical evidence first, then derive Amendment 3 panels.

    The explicitly scoped old profile keeps its original numerical interpretation;
    a registry source without auction rows never inherits that exception.
    """
    request.validate()
    raw.validate(require_minutes=True)
    refs = raw.metadata["compatibility"].get("one_min", {}).get("evidence_refs", [])
    minute_source = raw.metadata["series"].get("one_min", {})
    if (raw.ts_code == "002025.SZ"
            and minute_source.get("source_id") == "csv:original_20260918:002025.SZ"
            and minute_source.get("source_family") == "csv_verified"
            and "root-verified-csv-002025-20260918-v1" in refs):
        return _build_legacy_multiperiod_views(raw, request)
    if raw.ts_code != request.ts_code:
        _fail("INVALID_REQUEST", "Raw stock does not match request")
    cutoff = parse_timestamp(request.cutoff)
    effective = _effective(raw, cutoff)
    display_day = _display_session(raw, cutoff)
    current_day = effective.date().isoformat()
    start = request.minute_start or cutoff.date().isoformat()
    if start < raw.calendar.trade_date.iloc[0]:
        _fail("CALENDAR_UNAVAILABLE", "Minute window starts outside calendar evidence")
    if raw.one_min is None:
        _fail("SOURCE_UNAVAILABLE", "Canonical one-minute evidence is required")
    current_needed = effective.date() == cutoff.date()
    lower = min(start, current_day) if current_needed else start
    visible = raw.one_min.loc[(raw.one_min.trade_date >= lower) &
        raw.one_min.bar_end.map(lambda t: parse_timestamp(t) <= effective)].copy().reset_index(drop=True)
    days = raw.calendar.loc[raw.calendar.is_open & raw.calendar.trade_date.between(start, current_day)
        & raw.calendar.trade_date.isin(raw.daily.trade_date), "trade_date"].tolist() if any(
            p != "daily" for p in request.periods) else []
    if current_needed and current_day not in days:
        days.append(current_day)
    _check_minutes(raw, visible, days, effective)
    minutes = visible.loc[visible.trade_date >= start].reset_index(drop=True)
    display_minutes = minutes.loc[minutes.trade_date == display_day].reset_index(drop=True)
    day_minutes = visible.loc[visible.trade_date == current_day]
    # Auction metadata follows requested time, while bars follow effective time.
    session_dates = set(minutes.trade_date) | {display_day}
    auctions = {day: deepcopy(auction) for day, auction in raw.auctions.items()
                if day in session_dates and _stamp(day, time(9, 25), cutoff.tzinfo) <= cutoff}
    warnings = []
    requested_day = cutoff.date().isoformat()
    requested_open = bool(raw.calendar.loc[raw.calendar.trade_date == requested_day, "is_open"].iloc[0])
    if requested_open and cutoff.time() < time(9, 25):
        warnings.append("AUCTION_NOT_YET_REVEALED")
    if any(day not in auctions for day in session_dates):
        warnings.append("AUCTION_UNAVAILABLE")
    if display_day != requested_day:
        warnings.append("DISPLAY_SESSION_PREVIOUS")
    if set(minutes.trade_date) & set(auctions):
        warnings.append("OPENING_AUCTION_FOLDED")
    historical = raw.daily.loc[raw.daily.trade_date < current_day if current_needed
                               else raw.daily.trade_date <= current_day].copy()
    if request.window != "all":
        years = 10 if request.window == "10y" else 5
        try:
            beginning = cutoff.date().replace(year=cutoff.year-years)
        except ValueError:
            beginning = cutoff.date().replace(year=cutoff.year-years, day=28)
        historical = historical.loc[historical.trade_date >= beginning.isoformat()]
    historical = historical.reset_index(drop=True)
    historical["bar_start"] = historical.trade_date.map(lambda d: f"{d}T09:30:00+08:00")
    historical["bar_end"] = historical.trade_date.map(lambda d: f"{d}T15:00:00+08:00")
    historical["is_partial"] = False
    daily = historical.loc[:, list(MINUTE_COLUMNS)].copy()
    partial_day = effective.time() < time(15)
    if current_needed and not day_minutes.empty and (not partial_day or request.include_partial):
        record = _fold_auction(_aggregate(day_minutes, f"{current_day}T09:30:00+08:00",
                               partial=partial_day), auctions.get(current_day))
        daily = pd.concat([daily, pd.DataFrame([record], columns=MINUTE_COLUMNS)], ignore_index=True)
    if current_needed and not day_minutes.empty:
        warnings.append("CURRENT_DAILY_FROM_OBSERVED_MINUTES")
    frames = {"daily": daily, "15m": _fifteen(minutes, request.include_partial, auctions),
              "1m": display_minutes, "intraday": _intraday(display_minutes, auctions.get(display_day))}
    prev_dates = set(frames["15m"].trade_date) | {display_day}
    previous_rows = {}
    for day in sorted(prev_dates):
        prior = raw.daily.loc[raw.daily.trade_date < day]
        if prior.empty:
            warnings.append("PREV_CLOSE_UNAVAILABLE")
        else:
            previous_rows[day] = prior.iloc[-1]
    dates = set().union(*(set(frames[p].trade_date) for p in request.periods)) | set(auctions)
    dates.update(row.trade_date for row in previous_rows.values())
    lineage, factors, factor_warnings = _factor_plan(raw, request, effective, dates, automatic=True)
    warnings.extend(factor_warnings)
    if "SYNTHETIC_ENGINEERING_ONLY" in raw.metadata["warnings"]:
        warnings.append("SYNTHETIC_ENGINEERING_ONLY")
    adjusted_auctions = {day: dict(auction, price=float(auction["price"]) * factors[day] /
                                  lineage["anchor_value"], available_at=f"{day}T09:25:00+08:00")
                         for day, auction in auctions.items()}
    prev_close = {day: float(row.close) * factors[row.trade_date] / lineage["anchor_value"]
                  for day, row in previous_rows.items()}
    labels = {key: deepcopy(value) for key, value in raw.labels.items()
              if parse_date(value["effective_date"]) <= effective.date()
              and parse_timestamp(value["available_at"]) <= effective}
    gaps = []
    if not daily.empty:
        first = daily.trade_date.iloc[0]
        calendar = raw.calendar.loc[raw.calendar.trade_date.between(first, current_day)]
        gaps = sorted(set(calendar.loc[calendar.is_open, "trade_date"]) - set(daily.trade_date))
        if current_needed and (not request.include_partial or not _grid(current_day, effective)):
            gaps = [day for day in gaps if day != current_day]
        if first < raw.calendar.trade_date.iloc[0]:
            warnings.append("HISTORICAL_CALENDAR_COVERAGE_UNAVAILABLE")
        if gaps:
            warnings.append("DAILY_GAPS_UNEXPLAINED")
    result = {}
    for period in request.periods:
        bars = frames[period].copy()
        if lineage is not None and not bars.empty:
            multipliers = bars.trade_date.map(factors).to_numpy() / lineage["anchor_value"]
            for column in ("open", "high", "low", "close") + (("avg_price",) if period == "intraday" else ()):
                bars[column] = bars[column].to_numpy() * multipliers
        source_keys = ["daily", "factors", "one_min"] if period == "daily" else ["one_min", "factors"]
        panel_warnings = warnings + (["FIFTEEN_MINUTES_DERIVED_FROM_ONE_MINUTE"] if period == "15m" else [])
        metadata = {"contract_version": CONTRACT_VERSION, "stage": request.stage, "window": request.window,
                    "requested_cutoff": request.cutoff, "effective_cutoff": effective.isoformat(),
                    "cutoff_source": deepcopy(request.cutoff_source), "cutoff_policy": "minute_end_v1",
                    "timezone": "Asia/Shanghai", "price_unit": "CNY/share", "volume_unit": "shares",
                    "amount_unit": "CNY", "price_basis": "fixed_anchor_qfq",
                    "source_version": raw.metadata["source_version"],
                    "source_ids": [raw.metadata["series"][key]["source_id"] for key in source_keys],
                    "factor_lineage": deepcopy(lineage), "labels": deepcopy(labels),
                    "labels_missing": [key for key in LABEL_KEYS if key not in labels],
                    "incomplete": bool(bars.is_partial.any()), "warnings": list(dict.fromkeys(panel_warnings)),
                    "gap_dates": gaps[:] if period == "daily" else [],
                    "auctions": deepcopy(adjusted_auctions), "prev_close": deepcopy(prev_close)}
        view = ChartView(request.ts_code, period, bars, metadata)
        view.validate()
        result[period] = view
    return result


def attach_snapshot_minutes(raw: RawBundle, request: ChartRequest, *,
                            snapshot_dir: str | Path, profile_path: str | Path) -> RawBundle:
    """Attach only explicitly hashed files from the one accepted stock-day profile."""
    request.validate()
    raw.validate()
    if request.ts_code != raw.ts_code:
        _fail("INVALID_REQUEST", "Raw stock does not match request")
    try:
        profile = json.loads(Path(profile_path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ChartError("SOURCE_UNAVAILABLE", "Minute profile is unavailable") from exc
    if not isinstance(profile, dict):
        _fail("SOURCE_UNVERIFIED", "Minute profile must be an explicit mapping")
    scoped_day = "2026-09-18"
    files = profile.get("one_min_files")
    if not isinstance(files, dict) or not isinstance(files.get(scoped_day), dict):
        _fail("SOURCE_UNVERIFIED", "Minute profile must explicitly identify its file")
    if (profile.get("contract_version") != CONTRACT_VERSION
            or profile.get("profile_id") != "root-verified-csv-002025-20260918-v1"
            or profile.get("ts_code") != "002025.SZ" or raw.ts_code != profile["ts_code"]
            or profile.get("trade_dates") != [scoped_day]
            or parse_timestamp(request.cutoff).date().isoformat() != scoped_day
            or request.minute_start not in {None, scoped_day}
            or set(profile.get("one_min_files", {})) != {scoped_day}
            or profile.get("source_family") != "csv_verified"
            or profile.get("source_id") != "csv:original_20260918:002025.SZ"
            or profile.get("factor_source_id") != "tushare:tbl_cn_adj_factor"
            or raw.metadata["series"]["daily"]["source_id"] != "tushare:tbl_cn_day"
            or raw.metadata["series"]["factors"]["source_id"] != profile["factor_source_id"]
            or profile.get("factor_provenance_verified") is not True
            or profile.get("unadjusted_price_basis_verified") is not True
            or profile.get("auction_policy") != "verified_difference"
            or profile.get("unchanged_factor_dates") != [scoped_day]
            or profile.get("exclude_0930") is not True
            or profile.get("native_fifteen_policy") != "do_not_attach; derive all15m from same1m"
            or profile.get("calendar_exchange_acceptance") != "root accepts SSE calendar for this confirmed SZ stock-day only"
            or not profile.get("unit_evidence_refs")):
        _fail("SOURCE_UNVERIFIED", "Minute profile does not authorize this stock-day/source")
    if any(profile.get(k) != v for k, v in {"volume_unit": "shares", "amount_unit": "CNY",
                                          "volume_scale": 1.0, "amount_scale": 1.0,
                                          "price_basis": "unadjusted"}.items()):
        _fail("UNIT_UNVERIFIED", "Minute profile has unknown units or price basis")
    root = Path(snapshot_dir).expanduser().resolve()
    entry = profile["one_min_files"][scoped_day]
    name, expected = entry.get("file"), entry.get("sha256")
    if (not isinstance(name, str) or Path(name).name != name or "/" in name or "\\" in name
            or not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected)):
        _fail("SOURCE_UNVERIFIED", "Minute profile filename/hash is invalid")
    path = (root / name).resolve()
    if path.parent != root:
        _fail("SOURCE_UNVERIFIED", "Minute profile file escapes snapshot directory")
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise ChartError("SOURCE_UNAVAILABLE", "Profile minute file is missing") from exc
    if hashlib.sha256(content).hexdigest() != expected:
        _fail("SOURCE_UNVERIFIED", "Profile minute file hash differs")
    try:
        from io import BytesIO
        source = pd.read_parquet(BytesIO(content))
        required = {"ts_code", "trade_date", "datetime", "adj_factor", *DAILY_COLUMNS[1:]}
        if not required <= set(source.columns) or source.empty:
            _fail("INVALID_SCHEMA", "Minute snapshot lacks required fields")
        if not source.ts_code.eq(raw.ts_code).all() or not source.trade_date.astype(str).eq("20260918").all():
            _fail("SOURCE_UNVERIFIED", "Minute snapshot contains an unauthorized stock-day")
        factor = raw.factors.loc[raw.factors.trade_date == scoped_day]
        prior = raw.factors.loc[raw.factors.trade_date < scoped_day]
        if (factor.empty or prior.empty or float(factor.adj_factor.iloc[0]) != float(prior.adj_factor.iloc[-1])
                or not source.adj_factor.eq(float(factor.adj_factor.iloc[0])).all()):
            _fail("SOURCE_UNVERIFIED", "Minute factor values lack unchanged daily-factor lineage")
        ends = pd.to_datetime(source.datetime)
        if ends.dt.tz is None:
            ends = ends.dt.tz_localize("Asia/Shanghai")
        if not all(t.utcoffset() == timedelta(hours=8) and t.date().isoformat() == scoped_day for t in ends):
            _fail("INVALID_INTERVAL", "Minute timestamps do not identify the authorized China session")
        if not ((ends.dt.second == 0) & (ends.dt.microsecond == 0)).all():
            _fail("INVALID_INTERVAL", "Minute source cannot claim within-minute timestamps")
        keep = ~((ends.dt.hour == 9) & (ends.dt.minute == 30))
        source, ends = source.loc[keep].copy(), ends.loc[keep]
        frame = source.loc[:, list(DAILY_COLUMNS[1:])].copy()
        frame.insert(0, "trade_date", scoped_day)
        frame["bar_start"] = [(t.to_pydatetime() - timedelta(minutes=1)).isoformat() for t in ends]
        frame["bar_end"] = [t.to_pydatetime().isoformat() for t in ends]
        frame["is_partial"] = False
        frame = frame.reset_index(drop=True)
    except ChartError:
        raise
    except (ValueError, TypeError, KeyError) as exc:
        raise ChartError("INVALID_SCHEMA", "Minute snapshot schema cannot be normalized") from exc
    result = deepcopy(raw)
    result.one_min, result.fifteen_min = frame, None
    result.metadata["series"].pop("fifteen_min", None)
    result.metadata["compatibility"].pop("fifteen_min", None)
    result.metadata["series"]["one_min"] = {
        "source_id": profile["source_id"], "source_family": profile["source_family"],
        "source_version": expected, "price_basis": "unadjusted", "volume_unit": "shares", "amount_unit": "CNY",
        "original_volume_unit": "shares", "original_amount_unit": "CNY", "volume_scale": 1.0, "amount_scale": 1.0,
        "unit_evidence_refs": deepcopy(profile["unit_evidence_refs"]),
        "factor_source_id": profile["factor_source_id"], "source_by_trade_date": {scoped_day: profile["source_id"]}}
    result.metadata["compatibility"]["one_min"] = {
        "status": "verified_overlap", "daily_source_id": "tushare:tbl_cn_day", "minute_source_id": profile["source_id"],
        "factor_source_id": profile["factor_source_id"], "checked_trade_dates": [scoped_day],
        "evidence_refs": deepcopy(profile["unit_evidence_refs"]) + [profile["profile_id"], _UNCHANGED + scoped_day],
        "units_verified": True, "unadjusted_price_basis_verified": True, "factor_provenance_verified": True,
        "auction_policy": "verified_difference"}
    result.metadata["source_version"] = hashlib.sha256(
        (raw.metadata["source_version"] + expected + profile["profile_id"]).encode()).hexdigest()
    result.validate(require_minutes=True)
    return result
