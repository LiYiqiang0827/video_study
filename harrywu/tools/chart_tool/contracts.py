"""chart.v1 contracts only: no database, cache, transformation or rendering work.

DataFrames are caller-owned. Validators inspect them without sorting, filling,
dropping columns or mutating values. CONTRACT.md defines producer behaviour.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
import hashlib
import json
import math
import re
from typing import Any, Literal, TypedDict

import pandas as pd

CONTRACT_VERSION = "chart.v1"
TIMEZONE = "Asia/Shanghai"
Stage = Literal["restore", "reveal", "study"]
Period = Literal["daily", "15m", "1m", "intraday"]
Window = Literal["10y", "5y", "all"]
DAILY_COLUMNS = ("trade_date", "open", "high", "low", "close", "vol", "amount")
MINUTE_COLUMNS = DAILY_COLUMNS + ("bar_start", "bar_end", "is_partial")
INTRADAY_COLUMNS = MINUTE_COLUMNS + ("avg_price",)
FACTOR_COLUMNS = ("trade_date", "adj_factor", "available_at")
CALENDAR_COLUMNS = ("trade_date", "is_open")
LABEL_KEYS = ("name", "st_status", "themes", "boards")
PRICE_RTOL = 1e-12
PRICE_ATOL = 1e-10
VOLUME_ATOL = 0.0
AMOUNT_ATOL = 0.0


class ChartError(ValueError):
    """Stable machine code plus concise context; never silently repair input."""

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        _json(self.details)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


class CutoffCandidate(TypedDict):
    cutoff: str
    evidence_ref: str


class CutoffSource(TypedDict):
    kind: Literal["live_offset", "screen_clock", "last_formed_bar", "manual", "study_end"]
    confidence: Literal["high", "medium", "low"]
    evidence_refs: list[str]
    candidates: list[CutoffCandidate]
    calibration_segment: str | None


class SeriesMetadata(TypedDict):
    source_id: str
    source_family: str
    source_version: str
    price_basis: Literal["unadjusted"]
    volume_unit: Literal["shares"]
    amount_unit: Literal["CNY"]
    original_volume_unit: str
    original_amount_unit: str
    volume_scale: float
    amount_scale: float
    unit_evidence_refs: list[str]
    factor_source_id: str
    # Mandatory for minute series: every returned stock-day has verified lineage.
    source_by_trade_date: dict[str, str]


class SourceCompatibility(TypedDict):
    status: Literal["same_source", "verified_overlap", "unverified"]
    daily_source_id: str
    minute_source_id: str
    factor_source_id: str
    checked_trade_dates: list[str]
    evidence_refs: list[str]
    units_verified: bool
    unadjusted_price_basis_verified: bool
    factor_provenance_verified: bool
    auction_policy: Literal["same", "verified_difference", "unknown"]


class RawMetadata(TypedDict):
    contract_version: str
    source_version: str
    series: dict[str, SeriesMetadata]
    compatibility: dict[str, SourceCompatibility]
    calendar_source: str | None
    calendar_version: str | None
    calendar_coverage: list[str] | None
    warnings: list[str]


class HistoricalLabel(TypedDict):
    value: str | list[str]
    effective_date: str
    available_at: str
    source_id: str
    evidence_ref: str


class FactorLineage(TypedDict):
    source_id: str
    source_version: str
    anchor_trade_date: str
    anchor_value: float
    anchor_available_at: str | None
    selection: Literal["last_closed_date", "available_at", "restore_anchor"]
    visible_factor_digest: str
    visible_factor_count: int
    formula: Literal["raw_price * row_factor / anchor_factor"]


class ViewMetadata(TypedDict):
    contract_version: str
    stage: Stage
    window: Window
    requested_cutoff: str
    effective_cutoff: str
    cutoff_source: CutoffSource
    cutoff_policy: Literal["daily_closed_v0.1", "minute_end_v1"]
    timezone: Literal["Asia/Shanghai"]
    price_unit: Literal["CNY/share"]
    volume_unit: Literal["shares"]
    amount_unit: Literal["CNY"]
    price_basis: Literal["fixed_anchor_qfq"]
    source_version: str
    source_ids: list[str]
    factor_lineage: FactorLineage | None
    labels: dict[str, HistoricalLabel]
    labels_missing: list[str]
    incomplete: bool
    warnings: list[str]
    gap_dates: list[str]
    auctions: dict[str, dict]
    prev_close: dict[str, float]


@dataclass(frozen=True)
class ChartRequest:
    ts_code: str
    cutoff: str
    cutoff_source: CutoffSource
    stage: Stage = "study"
    window: Window = "10y"
    periods: tuple[Period, ...] = ("daily",)
    minute_start: str | None = None
    include_partial: bool = True
    contract_version: str = CONTRACT_VERSION

    def validate(self) -> None:
        _require(bool(re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", self.ts_code)), "INVALID_REQUEST", "Use a six-digit exchange-qualified ts_code")
        cutoff = parse_timestamp(self.cutoff)
        _require(self.stage in {"restore", "reveal", "study"}, "INVALID_REQUEST", "Invalid stage")
        _require(self.window in {"10y", "5y", "all"}, "INVALID_REQUEST", "Invalid daily window")
        _require(self.contract_version == CONTRACT_VERSION, "CONTRACT_VERSION", "Unsupported contract")
        _require(bool(self.periods) and len(set(self.periods)) == len(self.periods) and set(self.periods) <= {"daily", "15m", "1m", "intraday"}, "INVALID_REQUEST", "Invalid or duplicate periods")
        _require(type(self.include_partial) is bool, "INVALID_REQUEST", "include_partial must be boolean")
        if self.minute_start is not None:
            _require(parse_date(self.minute_start) <= cutoff.date(), "INVALID_REQUEST", "minute_start exceeds cutoff date")
        validate_cutoff_source(self.cutoff_source, self.cutoff)

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        result = asdict(self)
        result["periods"] = list(self.periods)
        return result


@dataclass
class RawBundle:
    ts_code: str
    daily: pd.DataFrame
    factors: pd.DataFrame
    metadata: RawMetadata
    one_min: pd.DataFrame | None = None
    fifteen_min: pd.DataFrame | None = None
    calendar: pd.DataFrame | None = None
    labels: dict[str, HistoricalLabel] = field(default_factory=dict)
    auctions: dict = field(default_factory=dict)

    def validate(self, *, require_minutes: bool = False) -> None:
        _require(bool(re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", self.ts_code)), "INVALID_REQUEST", "Invalid RawBundle ts_code")
        validate_bars(self.daily, "daily", raw=True)
        validate_factors(self.factors)
        _exact_keys(self.metadata, RawMetadata.__required_keys__, "raw metadata")
        _require(self.metadata["contract_version"] == CONTRACT_VERSION, "CONTRACT_VERSION", "Unsupported raw contract")
        _nonempty(self.metadata["source_version"], "source_version")
        _json(self.metadata)
        series = self.metadata["series"]
        _require({"daily", "factors"} <= set(series), "SOURCE_UNVERIFIED", "daily and factors metadata required")
        for key, item in series.items():
            _require(key in {"daily", "factors", "one_min", "fifteen_min"}, "INVALID_SCHEMA", "Unknown raw series")
            _exact_keys(item, SeriesMetadata.__required_keys__, "series metadata")
            for name in ("source_id", "source_family", "source_version", "factor_source_id"):
                _nonempty(item[name], name)
            _require(item["price_basis"] == "unadjusted" and item["volume_unit"] == "shares" and item["amount_unit"] == "CNY", "UNIT_UNVERIFIED", "RawBundle must already use normalized unadjusted units")
            _require(bool(item["unit_evidence_refs"]), "UNIT_UNVERIFIED", "Unit evidence required")
            for name in ("volume_scale", "amount_scale"):
                _positive(item[name], name)
        _require(series["daily"]["source_family"] == series["factors"]["source_family"] and series["daily"]["factor_source_id"] == series["factors"]["source_id"], "SOURCE_UNVERIFIED", "Daily prices and factors must share their original source family")
        if not self.daily.empty:
            _require(set(self.daily.trade_date) <= set(self.factors.trade_date), "MISSING_FACTOR", "Every daily date needs an exact factor; no implicit 1 or forward fill")
        if self.calendar is not None:
            validate_calendar(self.calendar, self.metadata)
        for key, frame, period in (("one_min", self.one_min, "1m"), ("fifteen_min", self.fifteen_min, "15m")):
            if frame is not None:
                validate_bars(frame, period, raw=True)
                _require(key in series, "SOURCE_UNVERIFIED", f"Missing {key} metadata")
                if require_minutes and not frame.empty:
                    validate_source_compatibility(self.metadata, key, sorted(set(frame.trade_date)))
        if require_minutes:
            _require(self.calendar is not None, "CALENDAR_UNAVAILABLE", "Minute stage requires an evidenced exchange calendar")
        for key, label in self.labels.items():
            validate_label(key, label)
        _require(isinstance(self.auctions, dict), "INVALID_SCHEMA", "auctions must be a dictionary")
        minute_dates = set(self.one_min.trade_date) if self.one_min is not None else set()
        for day, auction in self.auctions.items():
            parse_date(day)
            _require(day in minute_dates, "INVALID_SCHEMA", "Auction needs a matching one_min date")
            _exact_keys(auction, {"price", "vol", "amount", "source_id"}, "raw auction")
            _validate_auction_values(auction)
            _nonempty(auction["source_id"], "auction source_id")


@dataclass(frozen=True)
class MinuteDay:
    ts_code: str
    trade_date: str
    one_min: pd.DataFrame
    auction: dict | None
    source_id: str
    source_family: str
    source_sha256: str
    checks: dict

    def validate(self) -> None:
        _require(isinstance(self.ts_code, str) and bool(re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", self.ts_code)), "INVALID_REQUEST", "Invalid MinuteDay ts_code")
        day = parse_date(self.trade_date)
        validate_bars(self.one_min, "1m", raw=True)
        expected = []
        for hour, minute in ((9, 30), (13, 0)):
            start = datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute)
            expected.extend((start + timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%S+08:00") for i in range(1, 121))
        _require(len(self.one_min) == 240 and self.one_min.trade_date.tolist() == [self.trade_date] * 240 and self.one_min.bar_end.tolist() == expected, "INVALID_INTERVAL", "MinuteDay requires the complete 240-minute session grid on trade_date")
        if self.auction is not None:
            _exact_keys(self.auction, {"price", "vol", "amount"}, "MinuteDay auction")
            _validate_auction_values(self.auction)
        for name in ("source_id", "source_family"):
            _nonempty(getattr(self, name), name)
        _require(isinstance(self.source_sha256, str) and re.fullmatch(r"[0-9a-f]{64}", self.source_sha256) is not None, "INVALID_SCHEMA", "MinuteDay source_sha256 must be lowercase SHA256")
        _require(isinstance(self.checks, dict), "INVALID_SCHEMA", "MinuteDay checks must be a dictionary")
        _json(self.checks)


@dataclass
class ChartView:
    ts_code: str
    period: Period
    bars: pd.DataFrame
    metadata: ViewMetadata

    def validate(self) -> None:
        _require(bool(re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", self.ts_code)), "INVALID_REQUEST", "Invalid ChartView ts_code")
        validate_bars(self.bars, self.period)
        legacy_keys = ViewMetadata.__required_keys__ - {"auctions", "prev_close"}
        _require(isinstance(self.metadata, dict) and set(self.metadata) in (legacy_keys, ViewMetadata.__required_keys__), "INVALID_SCHEMA", "view metadata must have exactly the legacy or Amendment 3 fields")
        m = self.metadata
        _require(m["contract_version"] == CONTRACT_VERSION, "CONTRACT_VERSION", "Unsupported view contract")
        _require(m["stage"] in {"restore", "reveal", "study"} and m["window"] in {"10y", "5y", "all"}, "INVALID_SCHEMA", "Invalid stage/window")
        requested = parse_timestamp(m["requested_cutoff"])
        effective = parse_timestamp(m["effective_cutoff"])
        _require(effective <= requested, "CUTOFF_LEAK", "Effective cutoff exceeds requested cutoff")
        if "auctions" in m:
            _require(isinstance(m["auctions"], dict) and isinstance(m["prev_close"], dict), "INVALID_SCHEMA", "auctions and prev_close must be dictionaries")
            for day, auction in m["auctions"].items():
                parse_date(day)
                _exact_keys(auction, {"price", "vol", "amount", "available_at", "source_id"}, "view auction")
                _validate_auction_values(auction)
                _nonempty(auction["source_id"], "auction source_id")
                _require(auction["available_at"] == day + "T09:25:00+08:00" and parse_timestamp(auction["available_at"]) <= requested, "CUTOFF_LEAK", "Auction availability must be 09:25 on its date and no later than requested cutoff")
            for day, price in m["prev_close"].items():
                _require(parse_date(day) <= requested.date(), "CUTOFF_LEAK", "Future prev_close date")
                _positive(price, "prev_close")
        validate_cutoff_source(m["cutoff_source"], m["requested_cutoff"])
        _require(m["timezone"] == TIMEZONE and m["price_unit"] == "CNY/share" and m["volume_unit"] == "shares" and m["amount_unit"] == "CNY" and m["price_basis"] == "fixed_anchor_qfq", "UNIT_UNVERIFIED", "Invalid normalized view units/basis")
        _nonempty(m["source_version"], "source_version")
        _require(type(m["incomplete"]) is bool and isinstance(m["warnings"], list) and all(isinstance(w, str) for w in m["warnings"]), "INVALID_SCHEMA", "Invalid incomplete/warnings fields")
        _require(bool(m["source_ids"]) and all(isinstance(v, str) and bool(v) for v in m["source_ids"]), "SOURCE_UNVERIFIED", "View source IDs required")
        if m["cutoff_policy"] == "daily_closed_v0.1":
            _require(self.period == "daily", "INVALID_SCHEMA", "Daily precision cannot produce minute panels")
            latest_allowed = daily_safe_date(m["requested_cutoff"])
            _require(effective.date() <= latest_allowed and effective.hour == 15 and effective.minute == 0 and effective.second == 0 and effective.microsecond == 0, "CUTOFF_LEAK", "Daily effective cutoff must be a permitted completed date at 15:00")
            _require(not m["incomplete"], "INVALID_SCHEMA", "Daily v0.1 has no partial current day")
        else:
            _require(m["cutoff_policy"] == "minute_end_v1", "INVALID_SCHEMA", "Unknown cutoff policy")
        if not self.bars.empty:
            if self.period == "daily":
                _require(all(parse_date(d) <= effective.date() for d in self.bars.trade_date), "CUTOFF_LEAK", "Daily row exceeds effective cutoff")
                if m["cutoff_policy"] == "daily_closed_v0.1":
                    _require(all(parse_date(d) <= daily_safe_date(m["requested_cutoff"]) for d in self.bars.trade_date), "CUTOFF_LEAK", "Unclosed daily row included")
                elif effective.time().hour < 15 or (effective.time().hour == 15 and effective.time().minute == 0 and effective.time().second == 0 and effective.time().microsecond == 0):
                    # A current-day synthetic daily bar needs explicit interval columns.
                    if any(parse_date(d) == effective.date() for d in self.bars.trade_date) and effective.hour < 15:
                        _require(set(MINUTE_COLUMNS) <= set(self.bars.columns), "CUTOFF_LEAK", "Intraday daily bar requires explicit interval and partial flag")
            if "bar_end" in self.bars:
                _require(all(parse_timestamp(t) <= effective for t in self.bars.bar_end), "CUTOFF_LEAK", "A bar crosses the effective cutoff")
                _require(m["incomplete"] == bool(self.bars.is_partial.any()), "INVALID_SCHEMA", "incomplete must match partial bars")
        lineage = m["factor_lineage"]
        _require(lineage is not None or self.bars.empty, "MISSING_FACTOR", "Nonempty view needs factor lineage")
        if lineage is not None:
            _exact_keys(lineage, FactorLineage.__required_keys__, "factor lineage")
            _positive(lineage["anchor_value"], "anchor_value")
            for name in ("source_id", "source_version"):
                _nonempty(lineage[name], name)
            anchor_date = parse_date(lineage["anchor_trade_date"])
            _require(anchor_date <= effective.date(), "CUTOFF_LEAK", "Future factor anchor")
            if lineage["anchor_available_at"] is not None:
                _require(parse_timestamp(lineage["anchor_available_at"]) <= effective, "CUTOFF_LEAK", "Anchor availability lies after cutoff")
            if lineage["selection"] == "last_closed_date":
                _require(anchor_date <= daily_safe_date(m["requested_cutoff"]), "CUTOFF_LEAK", "Date-only intraday anchor is not from a completed date")
            elif lineage["selection"] == "restore_anchor":
                _require(m["stage"] == "reveal", "CUTOFF_LEAK", "restore_anchor selection is only permitted during reveal")
            else:
                _require(lineage["selection"] == "available_at" and lineage["anchor_available_at"] is not None and parse_timestamp(lineage["anchor_available_at"]) <= effective, "CUTOFF_LEAK", "Intraday factor availability not established")
            _require(lineage["formula"] == "raw_price * row_factor / anchor_factor" and type(lineage["visible_factor_count"]) is int and lineage["visible_factor_count"] > 0, "INVALID_SCHEMA", "Invalid factor lineage")
            _nonempty(lineage["visible_factor_digest"], "visible_factor_digest")
        for key, label in m["labels"].items():
            validate_label(key, label, cutoff=effective)
        _require(len(m["labels_missing"]) == len(set(m["labels_missing"])) and (set(m["labels"]) | set(m["labels_missing"])) == set(LABEL_KEYS) and not (set(m["labels"]) & set(m["labels_missing"])), "INVALID_SCHEMA", "Every unavailable label must be explicitly listed once")
        for value in m["gap_dates"]:
            _require(parse_date(value) <= effective.date(), "CUTOFF_LEAK", "Future gap date")
        _json(m)

    def identity(self) -> dict[str, Any]:
        """Processed cache identity includes sanitized data content and all policies."""
        self.validate()
        return {"contract_version": CONTRACT_VERSION, "ts_code": self.ts_code,
                "period": self.period, "metadata": self.metadata,
                "bars_sha256": frame_digest(self.bars)}

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        result = {"ts_code": self.ts_code, "period": self.period,
                  "bars": {"columns": self.bars.columns.tolist(),
                           "records": self.bars.to_dict(orient="records")},
                  "metadata": self.metadata}
        _json(result)
        return result

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ChartView:
        _exact_keys(payload, {"ts_code", "period", "bars", "metadata"}, "serialized view")
        _exact_keys(payload["bars"], {"columns", "records"}, "serialized bars")
        expected = payload["bars"]["columns"]
        for record in payload["bars"]["records"]:
            _exact_keys(record, expected, "serialized bar")
        result = cls(payload["ts_code"], payload["period"],
                     pd.DataFrame(payload["bars"]["records"], columns=expected),
                     payload["metadata"])
        result.validate()
        return result


@dataclass(frozen=True)
class RenderOptions:
    width_px: int = 3840
    height_px: int = 1600
    dpi: int = 160
    font_family: str = "Microsoft YaHei"
    up_color: str = "#d62728"
    down_color: str = "#2ca02c"
    ma_periods: tuple[int, ...] = ()
    price_scale: str = "linear"
    auto_width: bool = True
    min_slot_px: float = 3.0
    max_width_px: int = 9600
    annotations: tuple = ()

    def validate(self) -> None:
        _require(all(type(x) is int and x > 0 for x in (self.width_px, self.height_px, self.dpi)), "INVALID_REQUEST", "Positive integer canvas dimensions required")
        _require(self.width_px >= 640 and self.height_px >= 400, "INVALID_REQUEST", "Canvas too small")
        _nonempty(self.font_family, "font_family")
        _require(all(re.fullmatch(r"#[0-9a-fA-F]{6}", c) for c in (self.up_color, self.down_color)), "INVALID_REQUEST", "Colors must be hex RGB")
        _require(len(set(self.ma_periods)) == len(self.ma_periods) and all(type(n) is int and n > 0 for n in self.ma_periods), "INVALID_REQUEST", "MA periods must be unique positive integers")
        _require(isinstance(self.price_scale, str) and self.price_scale in {"linear", "log"}, "INVALID_REQUEST", "Invalid price scale")
        _require(type(self.auto_width) is bool, "INVALID_REQUEST", "auto_width must be boolean")
        _require(isinstance(self.min_slot_px, (int, float)) and not isinstance(self.min_slot_px, bool) and 1.0 <= self.min_slot_px <= 20.0, "INVALID_REQUEST", "min_slot_px must be between 1 and 20")
        _require(type(self.max_width_px) is int and self.max_width_px >= 640, "INVALID_REQUEST", "max_width_px must be an integer >= 640")
        _require(isinstance(self.annotations, tuple), "INVALID_REQUEST", "annotations must be a tuple")
        for annotation in self.annotations:
            validate_annotation(annotation)


ANNOTATION_KEYS = {"kind", "at", "price", "text", "style"}


def validate_annotation(annotation: dict) -> None:
    _require(isinstance(annotation, dict) and set(annotation) == ANNOTATION_KEYS, "INVALID_REQUEST", "Invalid annotation keys")
    kind, at, price = annotation["kind"], annotation["at"], annotation["price"]
    _require(isinstance(kind, str) and kind in {"vline", "hline", "marker"}, "INVALID_REQUEST", "Invalid annotation kind")
    _require(isinstance(annotation["style"], str) and annotation["style"] in {"mention", "reference", "horizon", "note"}, "INVALID_REQUEST", "Invalid annotation style")
    _require(isinstance(annotation["text"], str) and 1 <= len(annotation["text"]) <= 40, "INVALID_REQUEST", "Annotation text must have 1 to 40 characters")
    if at is None:
        _require(kind == "hline", "INVALID_REQUEST", "Only hline may omit at")
    else:
        try:
            if isinstance(at, str) and len(at) == 10:
                parse_date(at)
            else:
                parse_timestamp(at)
                _require(at.endswith("+08:00"), "INVALID_REQUEST", "Annotation timestamp requires +08:00")
        except (ChartError, TypeError, AttributeError) as exc:
            raise ChartError("INVALID_REQUEST", "Invalid annotation position") from exc
    _require((price is None and kind == "vline") or (isinstance(price, (int, float)) and not isinstance(price, bool) and math.isfinite(price) and price > 0), "INVALID_REQUEST", "Annotation price must be positive and finite")


@dataclass(frozen=True)
class RenderManifest:
    ts_code: str
    period: Period
    status: Literal["ok", "empty"]
    output_path: str | None
    input_rows: int
    candle_count: int
    volume_bar_count: int
    actual_start: str | None
    actual_end: str | None
    view_identity: dict[str, Any]
    timings: dict[str, float] = field(default_factory=lambda: {"drawing_seconds": 0.0, "encoding_seconds": 0.0})
    warnings: tuple[str, ...] = ()
    contract_version: str = CONTRACT_VERSION

    def to_dict(self) -> dict[str, Any]:
        _require(self.contract_version == CONTRACT_VERSION, "CONTRACT_VERSION", "Unsupported manifest")
        _require(self.status in {"ok", "empty"}, "INVALID_SCHEMA", "Invalid render status")
        _require(self.input_rows == self.candle_count == self.volume_bar_count and self.input_rows >= 0, "RENDER_MISMATCH", "Every input bar needs a candle and a matching volume bar")
        _exact_keys(self.timings, {"drawing_seconds", "encoding_seconds"}, "render timings")
        _require(all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v)) and v >= 0 for v in self.timings.values()), "INVALID_SCHEMA", "Render timings must be finite nonnegative seconds")
        if self.status == "empty":
            _require(self.input_rows == 0 and self.output_path is None and self.actual_start is None and self.actual_end is None, "RENDER_MISMATCH", "Empty input produces no PNG and no invented coverage")
            _require(all(v == 0 for v in self.timings.values()), "RENDER_MISMATCH", "Empty render timings must be zero")
        else:
            _require(self.input_rows > 0 and bool(self.output_path) and self.actual_start is not None and self.actual_end is not None, "RENDER_MISMATCH", "Successful render requires path and actual coverage")
        result = asdict(self)
        result["warnings"] = list(self.warnings)
        _json(result)
        return result


def parse_date(value: str) -> date:
    _require(isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is not None, "INVALID_SCHEMA", "Dates must be ISO YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ChartError("INVALID_SCHEMA", "Invalid civil date") from exc


def parse_timestamp(value: str) -> datetime:
    _require(isinstance(value, str) and "T" in value, "INVALID_SCHEMA", "Timestamp must be timezone-aware ISO with T")
    try:
        result = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ChartError("INVALID_SCHEMA", "Invalid ISO timestamp") from exc
    _require(result.utcoffset() == timedelta(hours=8), "INVALID_SCHEMA", "Timestamp must use +08:00 China trading time")
    return result


def daily_safe_date(cutoff: str) -> date:
    """Civil-date upper bound only, never a claim that this is an exchange day."""
    instant = parse_timestamp(cutoff)
    return instant.date() if (instant.hour, instant.minute, instant.second, instant.microsecond) >= (15, 0, 0, 0) else instant.date() - timedelta(days=1)


def validate_cutoff_source(source: CutoffSource, cutoff: str) -> None:
    _exact_keys(source, CutoffSource.__required_keys__, "cutoff source")
    _require(source["kind"] in {"live_offset", "screen_clock", "last_formed_bar", "manual", "study_end"} and source["confidence"] in {"high", "medium", "low"}, "INVALID_SCHEMA", "Invalid cutoff source/confidence")
    _require(bool(source["evidence_refs"]), "CUTOFF_UNRESOLVED", "Cutoff evidence required")
    selected = parse_timestamp(cutoff)
    for candidate in source["candidates"]:
        _exact_keys(candidate, CutoffCandidate.__required_keys__, "cutoff candidate")
        _require(selected <= parse_timestamp(candidate["cutoff"]), "CUTOFF_LEAK", "Selected cutoff must not exceed any conflicting source")
        _nonempty(candidate["evidence_ref"], "evidence_ref")
    _json(source)


def validate_bars(frame: pd.DataFrame, period: Period, *, raw: bool = False) -> None:
    _require(isinstance(frame, pd.DataFrame), "INVALID_SCHEMA", "bars must be a pandas DataFrame")
    _require(period in {"daily", "15m", "1m", "intraday"}, "INVALID_SCHEMA", "Unknown period")
    columns = MINUTE_COLUMNS if period != "daily" or "bar_end" in frame.columns else DAILY_COLUMNS
    if period == "intraday" and tuple(frame.columns) == INTRADAY_COLUMNS:
        columns = INTRADAY_COLUMNS
    _require(tuple(frame.columns) == columns, "INVALID_SCHEMA", "Bars need exact canonical columns and order", expected=list(columns))
    if frame.empty:
        return
    dates = [parse_date(v) for v in frame.trade_date]
    for name in ("open", "high", "low", "close", "vol", "amount") + (("avg_price",) if columns == INTRADAY_COLUMNS else ()):
        _require(pd.api.types.is_numeric_dtype(frame[name]) and not pd.api.types.is_bool_dtype(frame[name]), "INVALID_SCHEMA", f"{name} must be numeric")
        values = frame[name].tolist()
        _require(all(pd.notna(v) and math.isfinite(float(v)) for v in values), "INVALID_VALUE", f"{name} must be finite and non-null")
        _require(all(float(v) > 0 if name in {"open", "high", "low", "close", "avg_price"} else float(v) >= 0 for v in values), "INVALID_VALUE", f"{name} has invalid sign")
    _require(bool(((frame.high >= frame[["open", "low", "close"]].max(axis=1)) & (frame.low <= frame[["open", "high", "close"]].min(axis=1))).all()), "INVALID_OHLC", "OHLC ordering is inconsistent")
    if columns == DAILY_COLUMNS:
        keys = frame.trade_date.tolist()
    else:
        starts = [parse_timestamp(v) for v in frame.bar_start]
        ends = [parse_timestamp(v) for v in frame.bar_end]
        _require(all(type(v) is bool for v in frame.is_partial.tolist()), "INVALID_SCHEMA", "is_partial must be boolean")
        _require(all(s < e and s.date() == d == e.date() for s, e, d in zip(starts, ends, dates)), "INVALID_INTERVAL", "Bar dates/intervals are inconsistent")
        _require(all(e.second == e.microsecond == 0 for e in ends), "INVALID_INTERVAL", "Minute evidence cannot claim seconds within a minute")
        if period == "daily":
            _require(all((s.hour, s.minute) == (9, 30) and ((9, 30) < (e.hour, e.minute) <= (11, 30) or (13, 0) < (e.hour, e.minute) <= (15, 0)) for s, e in zip(starts, ends)), "INVALID_INTERVAL", "Daily aggregate must start at 09:30 and end at a session minute")
        else:
            _require(all(((9, 30) <= (s.hour, s.minute) < (11, 30) and (e.hour, e.minute) <= (11, 30)) or ((13, 0) <= (s.hour, s.minute) < (15, 0) and (e.hour, e.minute) <= (15, 0)) for s, e in zip(starts, ends)), "INVALID_INTERVAL", "Interval crosses lunch or lies outside session")
        _require(all(s.second == s.microsecond == 0 for s in starts), "INVALID_INTERVAL", "Interval start must have minute precision")
        if period in {"1m", "intraday"}:
            _require(all(e - s == timedelta(minutes=1) for s, e in zip(starts, ends)) and not any(frame.is_partial), "INVALID_INTERVAL", "One-minute source bars are completed whole minutes")
        if raw:
            _require(not any(frame.is_partial), "INVALID_INTERVAL", "Raw source cannot contain partial bars")
        if period == "15m":
            _require(all(s.minute % 15 == 0 and timedelta(0) < e - s <= timedelta(minutes=15) and (partial or e - s == timedelta(minutes=15)) for s, e, partial in zip(starts, ends, frame.is_partial)), "INVALID_INTERVAL", "15-minute bins must align to sessions; shortened bins must be marked partial")
        if any(frame.is_partial):
            _require(frame.is_partial.tolist() == [False] * (len(frame) - 1) + [True], "INVALID_INTERVAL", "Only the final displayed bar may be partial")
        keys = frame.bar_end.tolist()
        _require(all(ends[i] <= starts[i + 1] for i in range(len(ends) - 1)), "INVALID_INTERVAL", "Overlapping bars")
    _require(len(keys) == len(set(keys)), "DUPLICATE_BARS", "Duplicate bar key")
    _require(keys == sorted(keys), "UNSORTED_BARS", "Bars must be strictly ascending; producer must not silently sort")


def validate_factors(frame: pd.DataFrame) -> None:
    _require(isinstance(frame, pd.DataFrame) and tuple(frame.columns) == FACTOR_COLUMNS, "INVALID_SCHEMA", "Factors need trade_date/adj_factor/available_at")
    keys = frame.trade_date.tolist()
    for value in keys:
        parse_date(value)
    _require(len(keys) == len(set(keys)), "DUPLICATE_FACTORS", "Duplicate factor date")
    _require(keys == sorted(keys), "UNSORTED_FACTORS", "Factors must be strictly ascending")
    for value in frame.adj_factor:
        _positive(value, "adj_factor")
    for value in frame.available_at:
        if value is not None:
            parse_timestamp(value)


def validate_calendar(frame: pd.DataFrame, metadata: RawMetadata) -> None:
    _require(tuple(frame.columns) == CALENDAR_COLUMNS and not frame.empty, "CALENDAR_UNAVAILABLE", "Calendar needs nonempty trade_date/is_open")
    coverage = metadata["calendar_coverage"]
    _require(metadata["calendar_source"] is not None and metadata["calendar_version"] is not None and coverage is not None and len(coverage) == 2, "CALENDAR_UNAVAILABLE", "Calendar source/version/coverage required")
    start, end = [parse_date(d) for d in coverage]
    expected = [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]
    _require(frame.trade_date.tolist() == expected and all(type(v) is bool for v in frame.is_open.tolist()), "CALENDAR_UNAVAILABLE", "Calendar must explicitly cover every civil date without weekday guesses")


def validate_source_compatibility(metadata: RawMetadata, key: str, trade_dates: list[str]) -> None:
    series = metadata["series"]
    item = series[key]
    lineage = item["source_by_trade_date"]
    _require(all(d in lineage and lineage[d] not in {"", "unknown"} for d in trade_dates), "SOURCE_UNVERIFIED", "Every minute stock-day needs verified source lineage")
    evidence = metadata["compatibility"].get(key)
    _require(evidence is not None, "SOURCE_UNVERIFIED", "No minute/daily source compatibility evidence")
    _exact_keys(evidence, SourceCompatibility.__required_keys__, "source compatibility")
    _require(evidence["status"] in {"same_source", "verified_overlap"} and bool(evidence["evidence_refs"]) and all(evidence[n] is True for n in ("units_verified", "unadjusted_price_basis_verified", "factor_provenance_verified")) and evidence["auction_policy"] in {"same", "verified_difference"}, "SOURCE_UNVERIFIED", "Unverified minute/daily/factor compatibility")
    _require(evidence["daily_source_id"] == series["daily"]["source_id"] and evidence["minute_source_id"] == item["source_id"] and evidence["factor_source_id"] == series["factors"]["source_id"] == item["factor_source_id"], "SOURCE_UNVERIFIED", "Compatibility evidence identifies different sources")
    if evidence["status"] == "same_source":
        _require(item["source_family"] == series["daily"]["source_family"], "SOURCE_UNVERIFIED", "Different source families cannot be declared same-source")
    else:
        _require(bool(evidence["checked_trade_dates"]), "SOURCE_UNVERIFIED", "Cross-source compatibility requires checked overlap dates")
        for value in evidence["checked_trade_dates"]:
            parse_date(value)


def validate_label(key: str, label: HistoricalLabel, *, cutoff: datetime | None = None) -> None:
    _require(key in LABEL_KEYS, "INVALID_SCHEMA", "Unknown historical label")
    _exact_keys(label, HistoricalLabel.__required_keys__, "historical label")
    effective = parse_date(label["effective_date"])
    available = parse_timestamp(label["available_at"])
    _require(isinstance(label["value"], str) or (isinstance(label["value"], list) and all(isinstance(v, str) for v in label["value"])), "INVALID_SCHEMA", "Invalid label value")
    _nonempty(label["source_id"], "source_id")
    _nonempty(label["evidence_ref"], "evidence_ref")
    if cutoff is not None:
        _require(effective <= cutoff.date() and available <= cutoff, "CUTOFF_LEAK", "Future or unavailable historical label")


def frame_digest(frame: pd.DataFrame) -> str:
    """Stable canonical digest of JSON-safe canonical bar/factor values."""
    content = {"columns": frame.columns.tolist(), "records": frame.to_dict(orient="records")}
    return hashlib.sha256(_json(content).encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ChartError("INVALID_SCHEMA", "Contract metadata must be finite JSON-serializable values") from exc


def _exact_keys(value: dict[str, Any], expected: Any, label: str) -> None:
    _require(isinstance(value, dict) and set(value) == set(expected), "INVALID_SCHEMA", f"{label} has missing or unknown fields")


def _positive(value: Any, label: str) -> None:
    _require(not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(float(value)) and float(value) > 0, "INVALID_VALUE", f"{label} must be positive finite numeric")


def _validate_auction_values(auction: dict) -> None:
    _positive(auction["price"], "auction price")
    for name in ("vol", "amount"):
        value = auction[name]
        _require(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)) and value >= 0, "INVALID_VALUE", f"auction {name} must be finite nonnegative numeric")


def _nonempty(value: Any, label: str) -> None:
    _require(isinstance(value, str) and bool(value.strip()), "INVALID_SCHEMA", f"{label} must be a nonempty string")


def _require(condition: bool, code: str, message: str, **details: Any) -> None:
    if not condition:
        raise ChartError(code, message, details=details)
