"""Read-only daily adapter, immutable per-stock raw cache, and safe daily views.

Snapshot inputs are the original provider's separate daily/factor Parquets.
Database revision checks use size/mtime and sidecar state, not a whole-file hash:
an external edit preserving that metadata requires ``refresh=True``. Refresh
always re-reads and hashes canonical components. No source files are written.
``get_last_timings`` reports measured seconds for the latest call in this thread;
it is an operational sidecar, never historical ChartView metadata.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, time, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import threading
from time import perf_counter
import uuid

import pandas as pd

from .contracts import (
    CALENDAR_COLUMNS, CONTRACT_VERSION, DAILY_COLUMNS, FACTOR_COLUMNS,
    LABEL_KEYS, ChartError, ChartRequest, ChartView, RawBundle, daily_safe_date,
    frame_digest, parse_date, parse_timestamp,
)
from .paths import writable_path

_LOCAL = threading.local()
_QUERY_TIMEOUT = 120.0
_CACHE_SCHEMA = 1


def get_last_timings() -> dict:
    """Copy latest thread-local load timings and daily conversion time."""
    return dict(getattr(_LOCAL, "timings", {}))


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)


def _digest(value) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _db_path() -> Path:
    default = Path(__file__).resolve().parents[1] / "offlineDataManager" / "data"
    return Path(os.environ.get("TRADING_AGENT_DATA_DIR", str(default))).expanduser().resolve() / "db_cn_basic.db"


def _snapshot_paths(directory: Path, ts_code: str) -> dict[str, Path]:
    return {"daily": directory / f"{ts_code}.daily.parquet",
            "factors": directory / f"{ts_code}.factors.parquet",
            "calendar": directory / "calendar.parquet"}


def source_fingerprint(ts_code: str, *, snapshot_dir: str | Path | None = None) -> dict:
    """Content hashes for snapshots; quiescent stat/sidecar identity for SQLite.

    This privileged operational result may contain full-source coverage/path
    information; it must not be supplied to a restoration analyst or renderer.
    """
    if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", ts_code):
        raise ChartError("INVALID_REQUEST", "Invalid stock identifier")
    try:
        if snapshot_dir is not None:
            directory = Path(snapshot_dir).expanduser().resolve()
            paths = _snapshot_paths(directory, ts_code)
            files = {}
            for key, path in paths.items():
                if not path.is_file():
                    if key == "calendar":
                        files[key] = None
                        continue
                    raise ChartError("SOURCE_UNAVAILABLE", f"Missing {key} snapshot")
                files[key] = {"sha256": _hash_file(path), "size": path.stat().st_size}
            return {"kind": "provider_parquet", "path": str(directory), "files": files}
        path = _db_path()
        if not path.is_file():
            raise ChartError("SOURCE_UNAVAILABLE", "Daily source database not found")
        sidecars = {}
        # SQLite readers update shared-memory lock coordination. Its timestamp
        # is not a data revision; including it invalidates our own read.
        for suffix in ("-wal", "-journal"):
            sidecar = Path(str(path) + suffix)
            if sidecar.exists():
                stat = sidecar.stat()
                if suffix in {"-wal", "-journal"} and stat.st_size:
                    raise ChartError("SOURCE_ACTIVE", "Source has an active WAL or journal")
                sidecars[suffix] = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
            else:
                sidecars[suffix] = None
        stat = path.stat()
        return {"kind": "sqlite_ro", "path": str(path), "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns, "sidecars": sidecars}
    except ChartError:
        raise
    except OSError as exc:
        raise ChartError("SOURCE_UNAVAILABLE", "Unable to inspect daily source") from exc


def _read_source(ts_code: str, snapshot_dir) -> dict[str, pd.DataFrame | None]:
    if snapshot_dir is not None:
        paths = _snapshot_paths(Path(snapshot_dir).expanduser().resolve(), ts_code)
        try:
            return {key: pd.read_parquet(path) if path.is_file() else None
                    for key, path in paths.items()}
        except Exception as exc:
            raise ChartError("SOURCE_READ", "Unable to read source Parquet") from exc
    # Lazy import: standalone snapshot use neither imports settings nor opens DBs.
    try:
        original_path = sys.path[:]
        try:
            # The existing reader imports ``config`` from its scripts directory.
            # Its own historical path insertion points one level too high.
            sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "offlineDataManager" / "scripts"))
            from offlineDataManager.scripts.core.offline_db_client import (
                get_adj_factor, get_day, get_tradecal,
            )
        finally:
            sys.path[:] = original_path
        path = _db_path()
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2.0)
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA busy_timeout=2000")
            connection.execute("BEGIN")
            def query(function, **kwargs):
                deadline = perf_counter() + _QUERY_TIMEOUT
                connection.set_progress_handler(lambda: int(perf_counter() >= deadline), 1000)
                try:
                    return function(conn=connection, **kwargs)
                finally:
                    connection.set_progress_handler(None, 0)
            daily = query(get_day, ts_code=ts_code, qfq=False)
            factors = query(get_adj_factor, ts_code=ts_code)
            calendar = query(get_tradecal, market="SSE", is_open=None)
            connection.rollback()
            return {"daily": daily, "factors": factors, "calendar": calendar}
        finally:
            connection.close()
    except Exception as exc:
        raise ChartError("SOURCE_READ", "Read-only source query failed or timed out") from exc


def _dates(values) -> list[str]:
    result = []
    for value in values:
        text = str(value)
        if re.fullmatch(r"\d{8}", text):
            text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
        parse_date(text)
        result.append(text)
    return result


def _stock_check(frame, ts_code):
    if "ts_code" not in frame or not frame.ts_code.eq(ts_code).all():
        raise ChartError("SOURCE_UNVERIFIED", "Source snapshot must contain only the requested stock")


def _normalize(source: dict, ts_code: str) -> RawBundle:
    daily_source, factor_source, calendar_source = (source[k] for k in ("daily", "factors", "calendar"))
    if daily_source is None or factor_source is None:
        raise ChartError("SOURCE_UNAVAILABLE", "Daily and factor sources required")
    try:
        daily = pd.DataFrame(columns=DAILY_COLUMNS)
        if not daily_source.empty:
            _stock_check(daily_source, ts_code)
            daily = daily_source.loc[:, list(DAILY_COLUMNS)].copy()
            daily["trade_date"] = _dates(daily.trade_date)
            daily["vol"] = daily.vol * 100.0
            daily["amount"] = daily.amount * 1000.0
        factors = pd.DataFrame(columns=FACTOR_COLUMNS)
        if not factor_source.empty:
            _stock_check(factor_source, ts_code)
            factors = factor_source.loc[:, ["trade_date", "adj_factor"]].copy()
            factors["trade_date"] = _dates(factors.trade_date)
            # snap_ts means ingestion time, never historical availability.
            factors["available_at"] = pd.Series([None] * len(factors), index=factors.index, dtype=object)
        calendar = None
        if calendar_source is not None and not calendar_source.empty:
            if "exchange" not in calendar_source or not calendar_source.exchange.eq("SSE").all():
                raise ChartError("SOURCE_UNVERIFIED", "Calendar must identify the verified SSE source")
            flags = calendar_source.is_open.tolist()
            if not all(v in (0, 1) and pd.notna(v) for v in flags):
                raise ChartError("CALENDAR_UNAVAILABLE", "Calendar is_open must be 0 or 1")
            calendar = pd.DataFrame({"trade_date": _dates(calendar_source.cal_date),
                                     "is_open": [bool(v) for v in flags]}, columns=CALENDAR_COLUMNS)
        daily = daily.reset_index(drop=True)
        factors = factors.reset_index(drop=True)
        revisions = {"daily": frame_digest(daily), "factors": frame_digest(factors),
                     "calendar": frame_digest(calendar) if calendar is not None else None}
        factor_id = "tushare:tbl_cn_adj_factor"
        def series(source_id, key, volume_unit, amount_unit, volume_scale, amount_scale):
            return {"source_id": source_id, "source_family": "tushare",
                    "source_version": revisions[key], "price_basis": "unadjusted",
                    "volume_unit": "shares", "amount_unit": "CNY",
                    "original_volume_unit": volume_unit, "original_amount_unit": amount_unit,
                    "volume_scale": volume_scale, "amount_scale": amount_scale,
                    "unit_evidence_refs": ["coreClient/docs/data_provider.md#daily-field-units", "chart_tool/CONTRACT.md#canonical-frames-and-metadata"],
                    "factor_source_id": factor_id, "source_by_trade_date": {}}
        metadata = {"contract_version": CONTRACT_VERSION, "source_version": _digest(revisions),
                    "series": {"daily": series("tushare:tbl_cn_day", "daily", "lots", "thousand_CNY", 100.0, 1000.0),
                               "factors": series(factor_id, "factors", "not_applicable", "not_applicable", 1.0, 1.0)},
                    "compatibility": {}, "calendar_source": "tushare:tbl_cn_tradecal:SSE" if calendar is not None else None,
                    "calendar_version": revisions["calendar"],
                    "calendar_coverage": [calendar.trade_date.iloc[0], calendar.trade_date.iloc[-1]] if calendar is not None else None,
                    "warnings": [] if calendar is not None else ["CALENDAR_UNAVAILABLE"]}
        result = RawBundle(ts_code, daily, factors, metadata, calendar=calendar)
        result.validate()
        return result
    except ChartError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise ChartError("INVALID_SCHEMA", "Source daily/factor/calendar schema is invalid") from exc


@contextmanager
def _stock_lock(partition: Path):
    partition.mkdir(parents=True, exist_ok=True)
    path = partition / "LOCK"
    token = uuid.uuid4().hex
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(_json({"pid": os.getpid(), "token": token}))
    except FileExistsError as exc:
        raise ChartError("CACHE_LOCKED", "Stock cache has another writer; stale locks require explicit inspection") from exc
    try:
        yield
    finally:
        # Never remove another owner's lock, even if externally replaced.
        try:
            if json.loads(path.read_text(encoding="utf-8")).get("token") == token:
                path.unlink()
        except (OSError, ValueError):
            pass


def _coverage(frame) -> dict:
    return {"rows": len(frame), "first": frame.trade_date.iloc[0] if len(frame) else None,
            "last": frame.trade_date.iloc[-1] if len(frame) else None}


def _read_generation(partition: Path, generation: str, fingerprint: dict) -> RawBundle:
    if not re.fullmatch(r"gen-[a-f0-9]{32}", generation):
        raise ChartError("CACHE_CORRUPT", "Invalid generation pointer")
    directory = partition / generation
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        expected_keys = {"cache_schema", "contract_version", "ts_code", "source_fingerprint", "metadata", "files", "coverage", "manifest_sha256"}
        if set(manifest) != expected_keys or manifest["cache_schema"] != _CACHE_SCHEMA or manifest["contract_version"] != CONTRACT_VERSION or manifest["ts_code"] != partition.name:
            raise ChartError("CACHE_CORRUPT", "Cache manifest schema mismatch")
        content = {k: v for k, v in manifest.items() if k != "manifest_sha256"}
        if manifest["manifest_sha256"] != _digest(content):
            raise ChartError("CACHE_CORRUPT", "Cache manifest digest mismatch")
        if manifest["source_fingerprint"] != fingerprint:
            raise ChartError("CACHE_STALE", "Source revision changed")
        keys = {"daily", "factors"} | ({"calendar"} if manifest["metadata"]["calendar_source"] is not None else set())
        if set(manifest["files"]) != keys or set(manifest["coverage"]) != keys:
            raise ChartError("CACHE_CORRUPT", "Missing cache components")
        frames = {}
        for key in keys:
            path = directory / f"{key}.parquet"
            if manifest["files"][key] != {"sha256": _hash_file(path)}:
                raise ChartError("CACHE_CORRUPT", "Cache file digest mismatch")
            frames[key] = pd.read_parquet(path)
            if manifest["coverage"][key] != _coverage(frames[key]):
                raise ChartError("CACHE_CORRUPT", "Cache coverage mismatch")
        result = RawBundle(partition.name, frames["daily"], frames["factors"], manifest["metadata"], calendar=frames.get("calendar"))
        result.validate()
        revisions = {"daily": frame_digest(result.daily), "factors": frame_digest(result.factors),
                     "calendar": frame_digest(result.calendar) if result.calendar is not None else None}
        if result.metadata["source_version"] != _digest(revisions) or any(result.metadata["series"][k]["source_version"] != revisions[k] for k in ("daily", "factors")) or result.metadata["calendar_version"] != revisions["calendar"]:
            raise ChartError("CACHE_CORRUPT", "Cache canonical content digest mismatch")
        return result
    except ChartError:
        raise
    except Exception as exc:
        raise ChartError("CACHE_CORRUPT", "Unable to validate cache generation") from exc


def _write_cache(partition: Path, raw: RawBundle, fingerprint: dict):
    generation = "gen-" + uuid.uuid4().hex
    directory = partition / generation
    pointer_temp = partition / ("CURRENT-" + uuid.uuid4().hex + ".tmp")
    directory.mkdir()
    published = False
    try:
        frames = {"daily": raw.daily, "factors": raw.factors}
        if raw.calendar is not None:
            frames["calendar"] = raw.calendar
        for key, frame in frames.items():
            frame.to_parquet(directory / f"{key}.parquet", index=False)
        manifest = {"cache_schema": _CACHE_SCHEMA, "contract_version": CONTRACT_VERSION,
                    "ts_code": raw.ts_code, "source_fingerprint": fingerprint,
                    "metadata": raw.metadata,
                    "files": {key: {"sha256": _hash_file(directory / f"{key}.parquet")} for key in frames},
                    "coverage": {key: _coverage(frame) for key, frame in frames.items()}}
        manifest["manifest_sha256"] = _digest(manifest)
        (directory / "manifest.json").write_text(_json(manifest), encoding="utf-8")
        reread = _read_generation(partition, generation, fingerprint)
        for key, frame in frames.items():
            pd.testing.assert_frame_equal(frame, getattr(reread, key), check_exact=True)
        if raw.metadata != reread.metadata:
            raise ChartError("CACHE_CORRUPT", "Cache metadata roundtrip differs")
        with pointer_temp.open("x", encoding="utf-8") as stream:
            stream.write(generation)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pointer_temp, partition / "CURRENT")
        published = True
    finally:
        if pointer_temp.exists():
            pointer_temp.unlink()
        if not published:
            shutil.rmtree(directory)


def _log(root: Path, ts_code: str, action: str, **values):
    root.mkdir(parents=True, exist_ok=True)
    record = {"purpose": "看图工具", "ts_code": ts_code, "action": action, **values}
    # One append write; no provider/global logger or credential config.
    with writable_path(root / "access.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(_json(record) + "\n")


def load_raw(request: ChartRequest, *, cache_dir: str | Path,
             snapshot_dir: str | Path | None = None, use_cache: bool = True,
             refresh: bool = False) -> RawBundle:
    request.validate()
    if type(use_cache) is not bool or type(refresh) is not bool:
        raise ChartError("INVALID_REQUEST", "Cache flags must be boolean")
    started = perf_counter()
    timings = {k: 0.0 for k in ("source_fingerprint_seconds", "cache_load_seconds", "source_read_seconds", "normalize_seconds", "cache_write_seconds", "load_total_seconds", "convert_seconds")}
    timings.update(cache_hit=False, cache_rebuilt=False)
    _LOCAL.timings = timings
    root = Path(cache_dir).expanduser().resolve()
    source_root = (Path(snapshot_dir).expanduser().resolve()
                   if snapshot_dir is not None else _db_path().parent)
    if root == source_root or root.is_relative_to(source_root):
        raise ChartError("SOURCE_WRITE_SCOPE", "Cache must be outside the read-only source directory")
    try:
        root = writable_path(cache_dir, protected_roots=[source_root])
    except ChartError as exc:
        raise ChartError("SOURCE_WRITE_SCOPE", "Cache overlaps a protected source or filesystem redirect") from exc
    partition = root / request.ts_code
    if partition.resolve().parent != root:
        raise ChartError("SOURCE_WRITE_SCOPE", "Stock cache partition escapes cache root")
    _log(root, request.ts_code, "load_requested", refresh=refresh, use_cache=use_cache)
    try:
        with _stock_lock(partition):
            stamp = perf_counter()
            fingerprint = source_fingerprint(request.ts_code, snapshot_dir=snapshot_dir)
            timings["source_fingerprint_seconds"] += perf_counter() - stamp
            pointer = partition / "CURRENT"
            if use_cache and not refresh and pointer.exists():
                stamp = perf_counter()
                try:
                    result = _read_generation(partition, pointer.read_text(encoding="utf-8"), fingerprint)
                    # Catch source changes during cache validation as well.
                    if source_fingerprint(request.ts_code, snapshot_dir=snapshot_dir) != fingerprint:
                        raise ChartError("SOURCE_CHANGED", "Source changed during cache load")
                    timings["cache_hit"] = True
                    _log(root, request.ts_code, "cache_hit")
                    return result
                except ChartError as exc:
                    if exc.code not in {"CACHE_STALE", "CACHE_CORRUPT"}:
                        raise
                    timings["cache_rebuilt"] = True
                    _log(root, request.ts_code, "cache_rejected", reason=exc.code)
                except (OSError, UnicodeError) as exc:
                    timings["cache_rebuilt"] = True
                    _log(root, request.ts_code, "cache_rejected", reason="CACHE_CORRUPT")
                finally:
                    timings["cache_load_seconds"] = perf_counter() - stamp
            stamp = perf_counter()
            source = _read_source(request.ts_code, snapshot_dir)
            timings["source_read_seconds"] = perf_counter() - stamp
            stamp = perf_counter()
            result = _normalize(source, request.ts_code)
            timings["normalize_seconds"] = perf_counter() - stamp
            stamp = perf_counter()
            if source_fingerprint(request.ts_code, snapshot_dir=snapshot_dir) != fingerprint:
                raise ChartError("SOURCE_CHANGED", "Source changed during raw import")
            timings["source_fingerprint_seconds"] += perf_counter() - stamp
            if use_cache:
                stamp = perf_counter()
                _write_cache(partition, result, fingerprint)
                timings["cache_write_seconds"] = perf_counter() - stamp
            _log(root, request.ts_code, "source_imported", source_kind=fingerprint["kind"], source_version=result.metadata["source_version"])
            return result
    except ChartError as exc:
        _log(root, request.ts_code, "error", code=exc.code)
        raise
    except Exception as exc:
        _log(root, request.ts_code, "error", code="CACHE_IO")
        raise ChartError("CACHE_IO", "Unable to write or load raw cache") from exc
    finally:
        timings["load_total_seconds"] = perf_counter() - started


def build_daily_view(raw: RawBundle, request: ChartRequest) -> ChartView:
    started = perf_counter()
    try:
        request.validate()
        raw.validate()
        if raw.ts_code != request.ts_code:
            raise ChartError("INVALID_REQUEST", "Raw stock does not match request")
        cutoff = parse_timestamp(request.cutoff)
        safe_date = daily_safe_date(request.cutoff)
        warnings = ["INTRADAY_DAY_OMITTED"] if cutoff.time() < time(15) else []
        effective_date = safe_date
        calendar_available = False
        if raw.calendar is not None:
            # Slice calendar before inspecting coverage or deriving gaps.
            visible_calendar = raw.calendar.loc[raw.calendar.trade_date <= safe_date.isoformat()]
            if not visible_calendar.empty and visible_calendar.trade_date.iloc[-1] == safe_date.isoformat():
                open_dates = visible_calendar.loc[visible_calendar.is_open, "trade_date"]
                if not open_dates.empty:
                    effective_date = parse_date(open_dates.iloc[-1])
                    calendar_available = True
        if not calendar_available:
            warnings.append("CALENDAR_UNAVAILABLE")
        effective = datetime.combine(effective_date, time(15), tzinfo=cutoff.tzinfo)
        if request.window == "all":
            start_date = None
        else:
            years = 10 if request.window == "10y" else 5
            try:
                start_date = cutoff.date().replace(year=cutoff.year - years)
            except ValueError:
                start_date = cutoff.date().replace(year=cutoff.year - years, day=28)
        bars = raw.daily.loc[raw.daily.trade_date <= effective_date.isoformat()].copy()
        if start_date is not None:
            bars = bars.loc[bars.trade_date >= start_date.isoformat()].copy()
        bars = bars.reset_index(drop=True)
        factors = raw.factors.loc[raw.factors.trade_date <= effective_date.isoformat()].copy()
        factors = factors.loc[factors.available_at.map(lambda value: value is None or parse_timestamp(value) <= effective)].copy()
        lineage = None
        if not bars.empty:
            if factors.empty or not set(bars.trade_date) <= set(factors.trade_date):
                raise ChartError("MISSING_FACTOR", "Visible prices need exact eligible factors and an anchor")
            anchor = factors.iloc[-1]
            row_factors = factors.set_index("trade_date").adj_factor.reindex(bars.trade_date).to_numpy()
            for column in ("open", "high", "low", "close"):
                bars[column] = bars[column].to_numpy() * row_factors / float(anchor.adj_factor)
            used = factors.loc[factors.trade_date.isin(set(bars.trade_date) | {anchor.trade_date})].reset_index(drop=True)
            item = raw.metadata["series"]["factors"]
            lineage = {"source_id": item["source_id"], "source_version": item["source_version"],
                       "anchor_trade_date": anchor.trade_date, "anchor_value": float(anchor.adj_factor),
                       "anchor_available_at": anchor.available_at, "selection": "last_closed_date",
                       "visible_factor_digest": frame_digest(used), "visible_factor_count": len(used),
                       "formula": "raw_price * row_factor / anchor_factor"}
        labels = {key: deepcopy(value) for key, value in raw.labels.items()
                  if parse_date(value["effective_date"]) <= effective_date and parse_timestamp(value["available_at"]) <= effective}
        gaps = []
        if not bars.empty:
            first = bars.trade_date.iloc[0]
            if raw.calendar is not None:
                visible_calendar = raw.calendar.loc[(raw.calendar.trade_date >= first) & (raw.calendar.trade_date <= effective_date.isoformat())]
                gaps = sorted(set(visible_calendar.loc[visible_calendar.is_open, "trade_date"]) - set(bars.trade_date))
                if visible_calendar.empty or visible_calendar.trade_date.iloc[0] != first or visible_calendar.trade_date.iloc[-1] != effective_date.isoformat():
                    if "CALENDAR_UNAVAILABLE" not in warnings:
                        warnings.append("CALENDAR_UNAVAILABLE")
            if gaps:
                warnings.append("DAILY_GAPS_UNEXPLAINED")
        metadata = {"contract_version": CONTRACT_VERSION, "stage": request.stage, "window": request.window,
                    "requested_cutoff": request.cutoff, "effective_cutoff": effective.isoformat(),
                    "cutoff_source": deepcopy(request.cutoff_source), "cutoff_policy": "daily_closed_v0.1",
                    "timezone": "Asia/Shanghai", "price_unit": "CNY/share", "volume_unit": "shares",
                    "amount_unit": "CNY", "price_basis": "fixed_anchor_qfq",
                    "source_version": raw.metadata["source_version"],
                    "source_ids": [raw.metadata["series"]["daily"]["source_id"], raw.metadata["series"]["factors"]["source_id"]],
                    "factor_lineage": lineage, "labels": labels,
                    "labels_missing": [key for key in LABEL_KEYS if key not in labels],
                    "incomplete": False, "warnings": warnings, "gap_dates": gaps}
        view = ChartView(request.ts_code, "daily", bars, metadata)
        view.validate()
        return view
    finally:
        if not hasattr(_LOCAL, "timings"):
            _LOCAL.timings = {}
        _LOCAL.timings["convert_seconds"] = perf_counter() - started
