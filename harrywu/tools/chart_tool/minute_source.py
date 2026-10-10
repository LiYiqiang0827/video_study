"""Registry-only minute reader; daily overlap checks are privileged source gates.

An empty priority intentionally disables every source, including old caches.
Registry priority is an explicit authorization boundary, not a discovery hint.
Raw 241-row days merge 09:30 into 09:31; 240-row start grids shift by one
minute. These mechanical rules never claim a separately identifiable auction.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, time, timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import re
from string import Formatter
from typing import Callable
import uuid

import pandas as pd

from .contracts import (ChartError, ChartRequest, MINUTE_COLUMNS, MinuteDay,
                        RawBundle, parse_date, parse_timestamp, validate_calendar)

MinuteProvider = Callable[[RawBundle, str], MinuteDay | None]
_REGISTRY_KEYS = {"registry_version", "generated_at", "sources", "priority",
                  "thresholds", "threshold_evidence"}
_SOURCE_KEYS = {"source_id", "family", "kind", "layout", "root", "path_template",
                "row_filter", "coverage", "encoding", "columns",
                "timestamp_semantics", "has_auction_row", "auction_row_time",
                "vol_unit", "amount_unit", "price_basis", "evidence_refs"}
_COLUMN_KEYS = {"datetime", "open", "high", "low", "close", "vol", "amount",
                "code", "name"}
_THRESHOLD_KEYS = {"vol_rel", "amount_rel", "price_abs", "auction_open_abs"}
_TEMPLATE_KEYS = {"ts_code", "code", "market", "yyyy", "mm", "dd", "yyyymmdd"}
_CACHE_KEY = b"chart.minute.v1"
_AUTO_CHECK = "auto_check:minute-vs-daily:v1"


def _fail(code, message, **details):
    raise ChartError(code, message, details=details)


def _exact(value, keys, name):
    if not isinstance(value, dict) or set(value) != keys:
        _fail("INVALID_SCHEMA", f"Invalid {name} keys", expected=sorted(keys))


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def load_registry(path) -> dict:
    """Validate the exact minute-src-v1 schema; legal empty priority stays empty."""
    try:
        registry = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ChartError("SOURCE_UNAVAILABLE", "Cannot read minute registry") from exc
    _exact(registry, _REGISTRY_KEYS, "registry")
    if registry["registry_version"] != "minute-src-v1":
        _fail("INVALID_SCHEMA", "Unsupported minute registry version")
    parse_timestamp(registry["generated_at"])
    if not isinstance(registry["sources"], list) or not isinstance(registry["priority"], list):
        _fail("INVALID_SCHEMA", "Sources and priority must be lists")
    identifiers = []
    for source in registry["sources"]:
        _exact(source, _SOURCE_KEYS, "minute source")
        for key in ("source_id", "family", "root", "path_template"):
            if not _text(source[key]):
                _fail("INVALID_SCHEMA", f"Source {key} must be nonempty")
        identifiers.append(source["source_id"])
        if source["kind"] not in {"csv", "parquet"} or source["layout"] not in {
                "per_stock_day", "per_stock_year", "per_day_all_stocks", "per_stock_all"}:
            _fail("INVALID_SCHEMA", "Unsupported source kind/layout")
        if not Path(source["root"]).is_absolute():
            _fail("INVALID_SCHEMA", "Source root must be absolute")
        template = source["path_template"]
        if Path(template).is_absolute() or ".." in Path(template).parts:
            _fail("INVALID_SCHEMA", "Source template must be relative to root")
        try:
            for _, field, spec, conversion in Formatter().parse(template):
                if field is not None and (field not in _TEMPLATE_KEYS or spec or conversion):
                    _fail("INVALID_SCHEMA", "Unsupported path template field")
        except ValueError as exc:
            raise ChartError("INVALID_SCHEMA", "Malformed path template") from exc
        if (source["row_filter"] is not None and not _text(source["row_filter"])) or (
                source["layout"] != "per_stock_day" and not _text(source["row_filter"])):
            _fail("INVALID_SCHEMA", "Multi-stock/date layouts need a row filter description")
        _exact(source["coverage"], {"first_date", "last_date", "gaps_note"}, "coverage")
        first = parse_date(source["coverage"]["first_date"])
        last = parse_date(source["coverage"]["last_date"])
        if first > last or not isinstance(source["coverage"]["gaps_note"], str):
            _fail("INVALID_SCHEMA", "Invalid source coverage")
        if source["encoding"] not in {None, "utf-8", "gbk"}:
            _fail("INVALID_SCHEMA", "Unsupported source encoding")
        _exact(source["columns"], _COLUMN_KEYS, "source column map")
        for key, value in source["columns"].items():
            if not _text(value) and not (key in {"code", "name"} and value is None):
                _fail("INVALID_SCHEMA", "Invalid column name", column=key)
        used = [v for v in source["columns"].values() if v is not None]
        if len(used) != len(set(used)):
            _fail("INVALID_SCHEMA", "Column mappings must be distinct")
        if source["layout"] == "per_day_all_stocks" and source["columns"]["code"] is None:
            _fail("INVALID_SCHEMA", "All-stock sources need a code column")
        if (source["timestamp_semantics"] != "bar_end"
                or type(source["has_auction_row"]) is not bool
                or not isinstance(source["auction_row_time"], str)
                or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", source["auction_row_time"])
                or source["vol_unit"] not in {"shares", "lots"}
                or source["amount_unit"] not in {"CNY", "thousand_CNY"}
                or source["price_basis"] != "unadjusted"):
            _fail("INVALID_SCHEMA", "Unsupported source time/units/price basis")
        if not isinstance(source["evidence_refs"], list) or not source["evidence_refs"] or not all(
                _text(v) for v in source["evidence_refs"]):
            _fail("INVALID_SCHEMA", "Source evidence required")
    priority = registry["priority"]
    if (len(identifiers) != len(set(identifiers)) or not all(_text(v) for v in priority)
            or len(priority) != len(set(priority)) or not set(priority) <= set(identifiers)):
        _fail("INVALID_SCHEMA", "Duplicate or unknown source priority")
    _exact(registry["thresholds"], _THRESHOLD_KEYS, "thresholds")
    for key, value in registry["thresholds"].items():
        if type(value) not in {float, int} or not math.isfinite(value) or value < 0:
            _fail("INVALID_SCHEMA", "Threshold must be finite and nonnegative", threshold=key)
    if not _text(registry["threshold_evidence"]):
        _fail("INVALID_SCHEMA", "Threshold evidence required")
    return registry


def _source_path(source, ts_code, day):
    value = parse_date(day)
    code, market = ts_code.split(".")
    params = dict(ts_code=ts_code, code=code, market=market, yyyy=f"{value.year:04d}",
                  mm=f"{value.month:02d}", dd=f"{value.day:02d}",
                  yyyymmdd=value.strftime("%Y%m%d"))
    return Path(source["root"]) / source["path_template"].format(**params)


def _hash_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _timestamps(values, day):
    values = values.map(lambda v: f"{day} {v}" if isinstance(v, str) and re.fullmatch(
        r"\d{2}:\d{2}(?::\d{2})?", v) else v)
    parsed = pd.to_datetime(values, errors="raise")
    if parsed.dt.tz is None:
        return parsed.dt.tz_localize("Asia/Shanghai")
    if not all(v.utcoffset() == timedelta(hours=8) for v in parsed):
        _fail("SOURCE_UNVERIFIED", "Source timestamps must use China trading time", check="timezone")
    return parsed


def _select_rows(frame, source, ts_code, day):
    columns = source["columns"]
    if columns["code"] is not None:
        codes = frame[columns["code"]].astype(str)
        frame = frame.loc[codes.isin({ts_code, ts_code.split(".")[0]})].copy()
    if frame.empty:
        return frame
    timestamps = _timestamps(frame[columns["datetime"]], day)
    frame = frame.loc[timestamps.dt.strftime("%Y-%m-%d").eq(day)].copy()
    if frame.empty:
        return frame
    # These source-date columns are corroboration, never a substitute for time.
    for key in ("trade_date", "交易日期"):
        if key in frame:
            dates = frame[key].astype(str).str.replace("-", "", regex=False)
            if not dates.eq(day.replace("-", "")).all():
                _fail("SOURCE_UNVERIFIED", "Source date disagrees with timestamp", check="trade_date")
    return frame


def _read_source_rows(path, source, ts_code, day):
    columns = source["columns"]
    try:
        if source["kind"] == "csv":
            code_dtype = {columns["code"]: str} if columns["code"] else None
            chunks = pd.read_csv(path, encoding=source["encoding"] or "utf-8",
                                 dtype=code_dtype, chunksize=100000)
            selected = [_select_rows(chunk, source, ts_code, day) for chunk in chunks]
            selected = [frame for frame in selected if not frame.empty]
            return pd.concat(selected, ignore_index=True) if selected else pd.DataFrame()
        frame = pd.read_parquet(path)
        return _select_rows(frame, source, ts_code, day).reset_index(drop=True)
    except ChartError:
        raise
    except Exception as exc:
        raise ChartError("SOURCE_UNVERIFIED", "Cannot read declared minute source",
                         details={"check": "source_schema", "source_id": source["source_id"]}) from exc


def _expected_grid(day):
    base = datetime.combine(parse_date(day), time())
    return [(base.replace(hour=h, minute=m) + timedelta(minutes=i)).strftime(
        "%Y-%m-%dT%H:%M:%S+08:00") for h, m in ((9, 30), (13, 0)) for i in range(1, 121)]


def _normalize_rows(rows, source, day):
    columns = source["columns"]
    ends = _timestamps(rows[columns["datetime"]], day)
    values = rows[[columns[k] for k in ("open", "high", "low", "close", "vol", "amount")]].copy()
    values.columns = ["open", "high", "low", "close", "vol", "amount"]
    for key in values:
        values[key] = pd.to_numeric(values[key], errors="raise").astype(float)
    values["vol"] *= 100.0 if source["vol_unit"] == "lots" else 1.0
    values["amount"] *= 1000.0 if source["amount_unit"] == "thousand_CNY" else 1.0
    expected = _expected_grid(day)
    actual = ends.dt.strftime("%Y-%m-%dT%H:%M:%S+08:00").tolist()
    start_grid = [(datetime.fromisoformat(v) - timedelta(minutes=1)).isoformat() for v in expected]
    merge_first = actual == [day + "T09:30:00+08:00"] + expected
    shift_all = actual == start_grid
    if not (merge_first or shift_all):
        _fail("MISSING_MINUTE", "Raw day must match the ordered 241-row or 240-row start grid",
              check="grid_ok", actual_count=len(values), expected_counts=[241, 240])
    # Check source rows before merging so a bad first row cannot be hidden.
    prices = values[["open", "high", "low", "close"]]
    if (not all(math.isfinite(v) for v in values.to_numpy().flat)
            or (prices <= 0).any().any() or (values[["vol", "amount"]] < 0).any().any()
            or (values.high < prices.max(axis=1)).any()
            or (values.low > prices.min(axis=1)).any()):
        _fail("SOURCE_UNVERIFIED", "Raw minute values violate the contract", check="minute_values")
    if merge_first:
        values = values.reset_index(drop=True)
        first, second = values.iloc[0].copy(), values.iloc[1].copy()
        values.loc[1, ["open", "high", "low", "close", "vol", "amount"]] = [
            first.open, max(first.high, second.high), min(first.low, second.low),
            second.close, first.vol + second.vol, first.amount + second.amount]
        values = values.iloc[1:].reset_index(drop=True)
        ends = ends.iloc[1:].reset_index(drop=True)
    else:
        values = values.reset_index(drop=True)
        ends = (ends + pd.Timedelta(minutes=1)).reset_index(drop=True)
    values.insert(0, "trade_date", day)
    values["bar_start"] = (ends - pd.Timedelta(minutes=1)).dt.strftime("%Y-%m-%dT%H:%M:%S+08:00")
    values["bar_end"] = ends.dt.strftime("%Y-%m-%dT%H:%M:%S+08:00")
    values["is_partial"] = False
    return values.loc[:, list(MINUTE_COLUMNS)].reset_index(drop=True), None, None


def _cache_identity(source, ts_code, day, sha):
    return {"cache_version": 2, "source": source, "ts_code": ts_code,
            "trade_date": day, "source_sha256": sha}


def _read_cache(path, identity):
    import pyarrow.parquet as pq
    table = pq.read_table(path)
    payload = json.loads(table.schema.metadata[_CACHE_KEY])
    if payload["identity"] != identity:
        return None
    return table.to_pandas(), payload["auction"], payload["auction_ohlc"]


def _write_cache(path, identity, frame, auction, auction_ohlc):
    import pyarrow as pa
    import pyarrow.parquet as pq
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    table = pa.Table.from_pandas(frame, preserve_index=False)
    metadata = dict(table.schema.metadata or {})
    metadata[_CACHE_KEY] = json.dumps(dict(identity=identity, auction=auction,
        auction_ohlc=auction_ohlc), ensure_ascii=False, allow_nan=False).encode("utf-8")
    try:
        pq.write_table(table.replace_schema_metadata(metadata), temp)
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def _check_day(daily_raw, day, source, sha, frame, auction, auction_ohlc, thresholds):
    expected = _expected_grid(day)
    actual = frame.bar_end.tolist()
    if actual != expected or frame.trade_date.tolist() != [day] * 240:
        _fail("MISSING_MINUTE", "Minute source needs the exact ordered 240-minute grid",
              check="grid_ok", actual_count=len(frame), expected_count=240,
              missing=sorted(set(expected) - set(actual)),
              extra=sorted(set(actual) - set(expected)), ordered=actual == sorted(actual))
    try:
        candidate = MinuteDay(daily_raw.ts_code, day, frame, auction,
                              source["source_id"], source["family"], sha, {})
        candidate.validate()
    except ChartError as exc:
        raise ChartError("SOURCE_UNVERIFIED", "Minute source values violate the contract",
                         details={"check": "minute_values", "reason": exc.code}) from exc
    row = daily_raw.daily.loc[daily_raw.daily.trade_date.eq(day)].iloc[0]
    auction_vol = auction["vol"] if auction else 0.0
    auction_amount = auction["amount"] if auction else 0.0
    vol = float(frame.vol.sum()) + auction_vol
    amount = float(frame.amount.sum()) + auction_amount
    # Zero totals use an exact comparison; no arbitrary denominator is invented.
    def relative(actual_value, reference, name, threshold):
        if reference == 0 and actual_value != 0:
            _fail("SOURCE_UNVERIFIED", "Nonzero minute total against zero daily total",
                  check=name, actual=actual_value, daily=reference, threshold=threshold,
                  relative_diff=None)
        return abs(actual_value - reference) / abs(reference) if reference else 0.0
    checks = dict(grid_ok=True,
        auction_open_diff=abs(auction["price"] - float(row.open)) if auction else None,
        open_diff=abs(float(frame.open.iloc[0]) - float(row.open)),
        vol_rel_diff=relative(vol, float(row.vol), "vol_rel_diff", thresholds["vol_rel"]),
        amount_rel_diff=relative(amount, float(row.amount), "amount_rel_diff", thresholds["amount_rel"]),
        close_diff=abs(float(frame.close.iloc[-1]) - float(row.close)),
        high_diff=abs(max(float(frame.high.max()), auction["price"] if auction else float(frame.high.max())) - float(row.high)),
        low_diff=abs(min(float(frame.low.min()), auction["price"] if auction else float(frame.low.min())) - float(row.low)),
        thresholds=deepcopy(thresholds))
    # Totals and prices remain privileged per-day gates after normalization.
    for name, threshold in (("vol_rel_diff", "vol_rel"), ("amount_rel_diff", "amount_rel")):
        if checks[name] > thresholds[threshold]:
            _fail("SOURCE_UNVERIFIED", "Minute total disagrees with daily total",
                  check=name, actual=checks[name], threshold=thresholds[threshold])
    for name, threshold in (("auction_open_diff", "auction_open_abs"),
                            ("open_diff", "price_abs"),
                            ("close_diff", "price_abs"), ("high_diff", "price_abs"),
                            ("low_diff", "price_abs")):
        if checks[name] is not None and checks[name] > thresholds[threshold]:
            _fail("SOURCE_UNVERIFIED", "Minute price disagrees with daily price",
                  check=name, actual=checks[name], threshold=thresholds[threshold])
    return MinuteDay(daily_raw.ts_code, day, frame, auction, source["source_id"],
                     source["family"], sha, checks)


def _load_minute_day(daily_raw, trade_date, registry, cache_dir):
    daily_raw.validate()
    parse_date(trade_date)
    if not registry["priority"]:
        return None
    if not daily_raw.daily.trade_date.eq(trade_date).any():
        _fail("SOURCE_UNVERIFIED", "Minute overlap needs the stock's exact daily row",
              check="daily_row", trade_date=trade_date)
    by_id = {source["source_id"]: source for source in registry["sources"]}
    for source_id in registry["priority"]:
        source = by_id[source_id]
        if not source["coverage"]["first_date"] <= trade_date <= source["coverage"]["last_date"]:
            continue
        path = _source_path(source, daily_raw.ts_code, trade_date)
        if not path.is_file():
            continue
        sha = _hash_file(path)
        cache = Path(cache_dir) / "minutes" / daily_raw.ts_code / f"{trade_date}.{sha[:12]}.parquet"
        identity = _cache_identity(source, daily_raw.ts_code, trade_date, sha)
        cached = None
        if cache.is_file():
            try:
                cached = _read_cache(cache, identity)
            except Exception:
                # A malformed derived cache is rebuilt from the authoritative bytes.
                cached = None
        cache_hit = cached is not None
        if cached is None:
            rows = _read_source_rows(path, source, daily_raw.ts_code, trade_date)
            if rows.empty:
                continue
            try:
                cached = _normalize_rows(rows, source, trade_date)
            except ChartError:
                raise
            except Exception as exc:
                raise ChartError("SOURCE_UNVERIFIED", "Minute normalization failed",
                                 details={"check": "source_values"}) from exc
        frame, auction, auction_ohlc = cached
        result = _check_day(daily_raw, trade_date, source, sha, frame, auction,
                            auction_ohlc, registry["thresholds"])
        if not cache_hit:
            _write_cache(cache, identity, frame, auction, auction_ohlc)
        return result
    return None


def load_minute_day(daily_raw: RawBundle, trade_date: str, *, registry_path, cache_dir) -> MinuteDay | None:
    """Try enabled sources in order; absence permits fallback, invalid data does not."""
    return _load_minute_day(daily_raw, trade_date, load_registry(registry_path), cache_dir)


def make_minute_provider(*, registry_path, cache_dir) -> MinuteProvider:
    """Make the frozen two-argument callable used by reveal/continuation code."""
    def provider(daily_raw: RawBundle, trade_date: str) -> MinuteDay | None:
        return load_minute_day(daily_raw, trade_date, registry_path=registry_path,
                               cache_dir=cache_dir)
    return provider


def _calendar(raw, cutoff):
    if raw.calendar is None:
        _fail("CALENDAR_UNAVAILABLE", "Minute requests need an evidenced exchange calendar")
    validate_calendar(raw.calendar, raw.metadata)
    day = cutoff.date().isoformat()
    if not raw.calendar.trade_date.iloc[0] <= day <= raw.calendar.trade_date.iloc[-1]:
        _fail("CALENDAR_UNAVAILABLE", "Cutoff lies outside calendar coverage")
    return raw.calendar


def minute_start_for_sessions(raw: RawBundle, cutoff: str, sessions: int) -> str:
    if type(sessions) is not int or sessions <= 0:
        _fail("INVALID_REQUEST", "sessions must be a positive integer")
    instant = parse_timestamp(cutoff)
    calendar = _calendar(raw, instant)
    day = instant.date().isoformat()
    current_open = bool(calendar.loc[calendar.trade_date.eq(day), "is_open"].iloc[0])
    eligible = calendar.loc[calendar.is_open & (calendar.trade_date <= day if
        current_open and instant.time() >= time(9, 25) else calendar.trade_date < day), "trade_date"]
    if len(eligible) < sessions:
        _fail("CALENDAR_UNAVAILABLE", "Insufficient evidenced open sessions",
              requested=sessions, available=len(eligible))
    return str(eligible.iloc[-sessions])


def attach_minutes(daily_raw: RawBundle, request: ChartRequest, *, registry_path, cache_dir) -> RawBundle:
    request.validate()
    daily_raw.validate()
    if request.ts_code != daily_raw.ts_code:
        _fail("INVALID_REQUEST", "Request and raw stock differ")
    cutoff = parse_timestamp(request.cutoff)
    calendar = _calendar(daily_raw, cutoff)
    last = cutoff.date().isoformat()
    start = request.minute_start or last
    if start < calendar.trade_date.iloc[0]:
        _fail("CALENDAR_UNAVAILABLE", "Minute start lies outside calendar coverage")
    dates = calendar.loc[calendar.is_open & calendar.trade_date.between(start, last), "trade_date"].tolist()
    if cutoff.time() < time(9, 25):
        dates = [day for day in dates if day != last]
    dates = [day for day in dates if day in set(daily_raw.daily.trade_date)]
    registry = load_registry(registry_path)
    days = []
    for day in dates:
        value = _load_minute_day(daily_raw, day, registry, cache_dir)
        if value is None:
            _fail("SOURCE_UNAVAILABLE", "No enabled minute source for stock-day",
                  ts_code=daily_raw.ts_code, trade_date=day)
        days.append(value)
    result = deepcopy(daily_raw)
    result.one_min = pd.concat([day.one_min for day in days], ignore_index=True) if days else pd.DataFrame(columns=MINUTE_COLUMNS)
    result.auctions = {day.trade_date: dict(day.auction, source_id=day.source_id)
                       for day in days if day.auction is not None}
    # The adapter always derives 15m from the new canonical 1m set.
    result.fifteen_min = None
    result.metadata["series"].pop("fifteen_min", None)
    result.metadata["compatibility"].pop("fifteen_min", None)
    selected_ids = list(dict.fromkeys(day.source_id for day in days))
    sources = [source for source in registry["sources"] if source["source_id"] in selected_ids]
    single = sources[0] if len(sources) == 1 else None
    source_id = single["source_id"] if single else ("minute-registry:" + "+".join(selected_ids) if selected_ids else "minute-registry:empty")
    version = hashlib.sha256(json.dumps([(d.trade_date, d.source_id, d.source_sha256)
                                        for d in days], sort_keys=True).encode()).hexdigest()
    refs = list(dict.fromkeys(ref for source in sources for ref in source["evidence_refs"]))
    factor_id = result.metadata["series"]["factors"]["source_id"]
    result.metadata["series"]["one_min"] = dict(source_id=source_id,
        source_family=single["family"] if single else "minute_registry",
        source_version=version, price_basis="unadjusted", volume_unit="shares",
        amount_unit="CNY", original_volume_unit=single["vol_unit"] if single else "mixed",
        original_amount_unit=single["amount_unit"] if single else "mixed",
        volume_scale=100.0 if single and single["vol_unit"] == "lots" else 1.0,
        amount_scale=1000.0 if single and single["amount_unit"] == "thousand_CNY" else 1.0,
        unit_evidence_refs=refs + [_AUTO_CHECK], factor_source_id=factor_id,
        source_by_trade_date={day.trade_date: day.source_id for day in days})
    result.metadata["compatibility"]["one_min"] = dict(status="verified_overlap",
        daily_source_id=result.metadata["series"]["daily"]["source_id"],
        minute_source_id=source_id, factor_source_id=factor_id,
        checked_trade_dates=[day.trade_date for day in days], evidence_refs=refs + [_AUTO_CHECK],
        units_verified=True, unadjusted_price_basis_verified=True,
        factor_provenance_verified=True,
        auction_policy="same" if days and all(day.auction is not None for day in days) else "verified_difference")
    result.validate(require_minutes=True)
    return result
