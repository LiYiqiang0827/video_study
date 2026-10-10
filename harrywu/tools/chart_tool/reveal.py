"""Frozen-anchor revelation with minute boundaries and isolated case failures."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
import re
from typing import Callable

from . import cases
from .contracts import ChartError, ChartRequest, ChartView, MinuteDay, RawBundle, daily_safe_date, frame_digest, parse_timestamp
from .data import build_daily_view, load_raw
from .io import write_json
from .paths import writable_path

MinuteProvider = Callable[[RawBundle, str], MinuteDay | None]
HORIZONS = (1, 3, 5, 10, 20)
EVENT_KINDS = ("first_available", "mention_day_after_cutoff", "interval_high", "interval_low", "latest_available")
DISCLAIMER = "本快照不判定交易成败、不代表云聪的实际买卖价、不计算胜率。"


def _factor(raw, day):
    rows = raw.factors.loc[raw.factors.trade_date == day]
    if len(rows) != 1:
        raise ChartError("MISSING_FACTOR", "Reveal prices require an exact factor")
    return float(rows.iloc[0].adj_factor)


def restore_anchor(frozen_record: dict, raw: RawBundle) -> dict:
    identities = frozen_record.get("view_identities", [])
    anchors = []
    for identity in identities:
        lineage = identity.get("metadata", {}).get("factor_lineage")
        if lineage is None:
            raise ChartError("ANCHOR_MISMATCH", "Frozen view has no restoration anchor")
        anchors.append((lineage["anchor_trade_date"], float(lineage["anchor_value"])))
    if anchors:
        if any(anchor != anchors[0] for anchor in anchors):
            raise ChartError("ANCHOR_MISMATCH", "Frozen views use different restoration anchors")
        day, value = anchors[0]
        try:
            current = _factor(raw, day)
        except ChartError as exc:
            raise ChartError("ANCHOR_MISMATCH", "Frozen anchor date is absent from current factors") from exc
        if current != value:
            raise ChartError("ANCHOR_MISMATCH", "Current factor differs from frozen restoration anchor")
        source = "frozen_restore_view"
    else:
        safe = daily_safe_date(frozen_record["request"]["cutoff"]).isoformat()
        eligible = raw.factors.loc[raw.factors.trade_date <= safe]
        cutoff = parse_timestamp(frozen_record["request"]["cutoff"])
        eligible = eligible.loc[eligible.available_at.map(lambda x: x is None or parse_timestamp(x) <= cutoff)]
        if eligible.empty:
            raise ChartError("MISSING_FACTOR", "No completed restoration factor available")
        row = eligible.iloc[-1]
        day, value, source = row.trade_date, float(row.adj_factor), "fallback_last_closed"
    return {"anchor_trade_date": day, "anchor_value": value, "source": source}


def _request(record, observed, window="all"):
    return ChartRequest(record["request"]["ts_code"], observed,
                        {"kind": "study_end", "confidence": "high", "evidence_refs": ["Programmatic revelation after verified freeze"],
                         "candidates": [], "calibration_segment": None}, stage="reveal", window=window)


def build_reveal_daily_view(raw: RawBundle, frozen_record: dict, *, observed_through: str, window: str = "10y") -> ChartView:
    anchor = restore_anchor(frozen_record, raw)
    view = build_daily_view(raw, _request(frozen_record, observed_through, window))
    if view.bars.empty:
        raise ChartError("REVEAL_NO_DATA", "No completed daily bars available for reveal view")
    m = view.metadata
    lineage = m["factor_lineage"]
    ratio = lineage["anchor_value"] / anchor["anchor_value"]
    for column in ("open", "high", "low", "close"):
        view.bars[column] = view.bars[column] * ratio
    effective_day = view.bars.trade_date.iloc[-1]
    m["effective_cutoff"] = effective_day + "T15:00:00+08:00"
    used = raw.factors.loc[raw.factors.trade_date.isin(set(view.bars.trade_date) | {anchor["anchor_trade_date"]})].reset_index(drop=True)
    anchor_row = raw.factors.loc[raw.factors.trade_date == anchor["anchor_trade_date"]].iloc[0]
    lineage.update(anchor_trade_date=anchor["anchor_trade_date"], anchor_value=anchor["anchor_value"],
                   anchor_available_at=anchor_row.available_at, selection="restore_anchor",
                   visible_factor_digest=frame_digest(used), visible_factor_count=len(used))
    m["labels"] = {k: v for k, v in m["labels"].items() if v["effective_date"] <= effective_day and parse_timestamp(v["available_at"]) <= parse_timestamp(m["effective_cutoff"])}
    m["labels_missing"] = [k for k in ("name", "st_status", "themes", "boards") if k not in m["labels"]]
    m["gap_dates"] = [d for d in m["gap_dates"] if d <= effective_day]
    m.update(auctions={}, prev_close={})
    m["warnings"].append("RESTORE_ANCHOR_PRICES")
    view.validate()
    return view


def compute_mention_day(raw, frozen_record, minute_provider) -> dict:
    anchor = restore_anchor(frozen_record, raw)
    cutoff = parse_timestamp(frozen_record["request"]["cutoff"])
    day, clock = cutoff.date().isoformat(), cutoff.time()
    day_rows = raw.daily.loc[raw.daily.trade_date == day]
    if raw.calendar is not None:
        calendar_day = raw.calendar.loc[raw.calendar.trade_date == day]
        if len(calendar_day) and not bool(calendar_day.iloc[0].is_open):
            day_rows = day_rows.iloc[:0]
    session_open = day_rows.empty or clock >= time(15)
    policy, kind, note, minute = "non_trading_day", "next_session_open", None, None
    post = None
    if not day_rows.empty:
        if clock < time(9, 25):
            policy = "preopen_full_day"
        elif clock < time(9, 30):
            policy, kind = "auction_window", "auction_price"
        elif clock >= time(15):
            policy = "postclose_excluded"
        else:
            policy = "lunch_split" if time(11, 30) <= clock < time(13) else "split_by_minutes"
            kind = "lunch_close" if policy == "lunch_split" else "last_minute_close"
        if policy in {"auction_window", "lunch_split", "split_by_minutes"} and minute_provider is not None:
            minute = minute_provider(raw, day)
            if minute is not None:
                minute.validate()
                if minute.ts_code != raw.ts_code or minute.trade_date != day:
                    raise ChartError("SOURCE_UNVERIFIED", "Minute provider returned a different stock or date")
        if policy in {"lunch_split", "split_by_minutes"} and minute is None:
            policy, kind, note, session_open = "excluded_no_minutes", "next_session_open", "REF_FALLBACK_NEXT_SESSION_OPEN", True
    if session_open:
        following = raw.daily.loc[raw.daily.trade_date > day]
        if following.empty:
            raise ChartError("REFERENCE_UNAVAILABLE", "No next daily session available for reference")
        row = following.iloc[0]
    else:
        row = day_rows.iloc[0]
    ref_day = row.trade_date
    raw_price, at = float(row.open), ref_day + "T09:25:00+08:00"
    multiplier = _factor(raw, ref_day) / anchor["anchor_value"]
    high, low = float(row.high) * multiplier, float(row.low) * multiplier
    if minute is not None:
        ends = minute.one_min.bar_end.map(parse_timestamp)
        if policy == "auction_window":
            post = minute.one_min.copy()
        elif policy in {"split_by_minutes", "lunch_split"}:
            post = minute.one_min.loc[ends > cutoff].copy()
            before = minute.one_min.loc[ends <= cutoff]
            if before.empty:
                kind = "auction_price"
            else:
                last = before.iloc[-1]
                raw_price, at = float(last.close), last.bar_end
            if policy == "split_by_minutes":
                note = "分钟按 bar_end > C 纳入，会多含 C 所在那一分钟中提及前的成交。"
        if post is not None and not post.empty:
            high, low = float(post.high.max()) * multiplier, float(post.low.min()) * multiplier
    elif policy == "auction_window":
        note = "没有分钟数据，第1日采用全天高低点，含竞价。"
    result = {"trade_date": day, "policy": policy, "reference_session_date": ref_day,
              "post_mention_high": high if post is not None else None,
              "post_mention_low": low if post is not None else None,
              "minute_source_id": minute.source_id if minute else None, "note": note,
              "reference": {"kind": kind, "trade_date": ref_day, "at": at, "price": raw_price * multiplier, "raw_price": raw_price},
              "_first_high": high, "_first_low": low, "_post_minutes": post}
    return result


def _observation(raw, anchor, mention, observed_through):
    safe = daily_safe_date(observed_through).isoformat()
    rows = raw.daily.loc[(raw.daily.trade_date >= mention["reference_session_date"]) & (raw.daily.trade_date <= safe)].copy().reset_index(drop=True)
    for column in ("open", "high", "low", "close"):
        rows["raw_" + column] = rows[column]
    rows["multiplier"] = rows.trade_date.map(lambda day: _factor(raw, day) / anchor["anchor_value"])
    for column in ("open", "high", "low", "close"):
        rows[column] *= rows.multiplier
    return rows


def _stat_rows(rows, mention):
    stats = rows.copy()
    if len(stats):
        stats.loc[0, "high"], stats.loc[0, "low"] = mention["_first_high"], mention["_first_low"]
        post = mention["_post_minutes"]
        if post is not None and not post.empty:
            stats.loc[0, "raw_open"] = float(post.iloc[0].open)
            stats.loc[0, "open"] = float(post.iloc[0].open) * float(stats.iloc[0].multiplier)
            stats.loc[0, "vol"] = float(post.vol.sum())
            stats.loc[0, "amount"] = float(post.amount.sum())
    return stats


def compute_path_snapshot(raw, frozen_record, anchor, mention, observed_through) -> dict:
    rows = _stat_rows(_observation(raw, anchor, mention, observed_through), mention)
    reference = mention["reference"]
    fields = ("trade_date", "close", "close_return", "mfe", "mfe_trade_date", "mae", "mae_trade_date")
    horizons = []
    for k in HORIZONS:
        horizon = {"k": k, **dict.fromkeys(fields), "complete": False}
        if len(rows) >= k:
            interval = rows.iloc[:k]
            last = interval.iloc[-1]
            high = interval.loc[interval.high.idxmax()]
            low = interval.loc[interval.low.idxmin()]
            horizon.update(trade_date=last.trade_date, close=float(last.close), close_return=float(last.close) / reference["price"] - 1,
                           mfe=float(high.high) / reference["price"] - 1, mfe_trade_date=high.trade_date,
                           mae=float(low.low) / reference["price"] - 1, mae_trade_date=low.trade_date, complete=True)
        horizons.append(horizon)
    notes = [DISCLAIMER]
    if mention["note"]:
        notes.append(mention["note"])
    return {"method": "fixed_horizon_v1", "price_basis": "restore_anchor_qfq", "reference": deepcopy(reference), "horizons": horizons, "notes": notes}


def compute_events(rows, anchor, mention, evidence_refs):
    if rows.empty:
        return []
    stats = _stat_rows(rows, mention)
    nodes = [("first_available", 0), ("interval_high", int(stats.high.idxmax())),
             ("interval_low", int(stats.low.idxmin())), ("latest_available", len(stats) - 1)]
    if mention["policy"] in {"split_by_minutes", "lunch_split"}:
        nodes.append(("mention_day_after_cutoff", 0))
    events = []
    for kind, index in nodes:
        row = stats.iloc[index]
        raw_high, raw_low = float(row.high) / row.multiplier, float(row.low) / row.multiplier
        description = (f"还原锚点复权（基准日{anchor['anchor_trade_date']}，因子{anchor['anchor_value']:g}）："
                       f"O={row.open:g}, H={row.high:g}, L={row.low:g}, C={row.close:g}；"
                       f"未复权 O={row.raw_open:g}, H={raw_high:g}, L={raw_low:g}, C={row.raw_close:g}；真实量={row.vol:g}股。"
                       "日线节点日期不等于极值的准确时刻；区间节点不表示实际买卖。")
        events.append({"at": row.trade_date + "T15:00:00+08:00", "kind": kind, "description": description, "evidence_refs": evidence_refs})
    return sorted(events, key=lambda event: (event["at"], EVENT_KINDS.index(event["kind"])))


def _pending_refs(root):
    """Use the existing namespace checks, but isolate record-validation errors."""
    directory = cases._bounded(root, str(root / "restore"))
    if not directory.exists():
        return []
    pending = []
    for case_dir in directory.iterdir():
        cases._bounded(root, str(case_dir))
        cases._case(case_dir.name, 1)
        cases._need(case_dir.is_dir(), "CASE_PATH", "Case entry must be a directory")
        for revision_dir in case_dir.iterdir():
            cases._bounded(root, str(revision_dir))
            match = re.fullmatch(r"revision-([1-9][0-9]*)", revision_dir.name)
            cases._need(match is not None and revision_dir.is_dir(), "CASE_PATH", "Invalid revision directory")
            revision = int(match[1])
            if not cases._revision_path(root, case_dir.name, revision, "frozen.json").exists():
                continue
            ref = {"case_id": case_dir.name, "revision": revision}
            try:
                wrapper, cutoff = cases._load_frozen(root, case_dir.name, revision)
                if cases._results(root, case_dir.name, revision, wrapper, cutoff):
                    continue
                ref["restore_sha256"] = wrapper["restore_sha256"]
            except (ChartError, OSError, KeyError, ValueError, TypeError):
                # The case loop will emit the error; other freezes remain usable.
                pass
            pending.append(ref)
    return sorted(pending, key=lambda ref: (ref["case_id"], ref["revision"]))


def run_reveal(store, *, observed_through: str, cache_dir, snapshot_dir=None, refresh: bool = False,
               case_id: str | None = None, revision: int = 1, minute_provider: MinuteProvider | None = None,
               minute_registry=None) -> tuple[dict, int]:
    report = {"status": "failed", "results": [], "pending_after": []}
    try:
        observed = parse_timestamp(observed_through)
        root = cases._root(store)
        pending = _pending_refs(root)
        refs = [{"case_id": case_id, "revision": revision}] if case_id else pending
    except (ChartError, OSError) as exc:
        report["error"] = {"code": exc.code if isinstance(exc, ChartError) else "CASE_IO", "message": str(exc)}
        return report, 2
    if not refs:
        report["status"] = "nothing_pending"
        return report, 0
    for ref in refs:
        item = {"case_id": ref["case_id"], "revision": ref["revision"], "status": "failed"}
        created_files, result_dir, directory_created = [], None, False
        try:
            wrapper, cutoff = cases._load_frozen(root, ref["case_id"], ref["revision"])
            record = wrapper["record"]
            if observed < cutoff:
                raise ChartError("CUTOFF_LEAK", "Revelation observation ends before restoration cutoff")
            raw = load_raw(_request(record, observed_through), cache_dir=cache_dir, snapshot_dir=snapshot_dir, refresh=refresh)
            anchor = restore_anchor(record, raw)
            if minute_provider is None and minute_registry is not None:
                from .minute_source import make_minute_provider
                minute_provider = make_minute_provider(registry_path=minute_registry, cache_dir=cache_dir)
            mention = compute_mention_day(raw, record, minute_provider)
            if parse_timestamp(mention["reference"]["at"]) > observed:
                raise ChartError("REFERENCE_UNAVAILABLE", "Reference session is beyond observed coverage")
            post = mention["_post_minutes"]
            if post is not None:
                post = post.loc[post.bar_end.map(parse_timestamp) <= observed].copy()
                mention["_post_minutes"] = post
                multiplier = _factor(raw, mention["trade_date"]) / anchor["anchor_value"]
                mention["post_mention_high"] = float(post.high.max()) * multiplier if len(post) else None
                mention["post_mention_low"] = float(post.low.min()) * multiplier if len(post) else None
                mention["_first_high"], mention["_first_low"] = mention["post_mention_high"], mention["post_mention_low"]
            path = compute_path_snapshot(raw, record, anchor, mention, observed_through)
            rows = _observation(raw, anchor, mention, observed_through)
            sequence = len(cases.load_results(root, ref["case_id"], ref["revision"])) + 1
            result_dir = cases._bounded(root, str(root / "result_artifacts" / ref["case_id"] / f"revision-{ref['revision']}" / f"observation-{sequence}"))
            writable_path(result_dir, protected_roots=[snapshot_dir])
            if result_dir.exists():
                raise ChartError("CASE_CONFLICT", "Observation artifact directory already exists")
            result_dir.mkdir(parents=True)
            directory_created = True
            bars_path = result_dir / "subsequent_daily.json"
            write_json(bars_path, {"source_version": raw.metadata["source_version"], "price_basis": "restore_anchor_qfq", "price_anchor": anchor,
                                   "volume_unit": "shares", "amount_unit": "CNY", "bars": rows.to_dict(orient="records")})
            created_files.append(bars_path)
            evidence = [bars_path.relative_to(root).as_posix()]
            post = mention["_post_minutes"]
            if post is not None:
                post = post.copy()
                multiplier = _factor(raw, mention["trade_date"]) / anchor["anchor_value"]
                for column in ("open", "high", "low", "close"):
                    post["raw_" + column] = post[column]
                    post[column] *= multiplier
                post["multiplier"] = multiplier
                minutes_path = result_dir / "mention_day_minutes.json"
                write_json(minutes_path, {"source_id": mention["minute_source_id"], "bars": post.to_dict(orient="records")})
                created_files.append(minutes_path)
                evidence.append(minutes_path.relative_to(root).as_posix())
            complete = all(h["complete"] for h in path["horizons"])
            result = {"contract_version": "chart.v1", **ref, "restore_sha256": wrapper["restore_sha256"], "result_sequence": sequence,
                      "observed_through": observed_through, "complete": complete,
                      "coverage_warning": None if complete else "不足20个已收盘参考交易日，未完成步长以null记录。",
                      "events": compute_events(rows, anchor, mention, evidence), "price_anchor": anchor,
                      "mention_day": {k: v for k, v in mention.items() if not k.startswith("_") and k != "reference"},
                      "path_snapshot": path, "created_at": datetime.now(timezone(timedelta(hours=8))).isoformat()}
            result_path = cases.append_result(root, result)
            item.update(status="revealed", result_path=str(result_path), rows=len(rows), complete=complete)
        except Exception as exc:
            for file in reversed(created_files):
                file.unlink(missing_ok=True)
            if directory_created and result_dir.exists() and not any(result_dir.iterdir()):
                result_dir.rmdir()
            item["error"] = {"code": exc.code if isinstance(exc, ChartError) else "REVEAL_FAILED", "message": str(exc)}
        report["results"].append(item)
    report["pending_after"] = _pending_refs(root)
    failed = any(item["status"] == "failed" for item in report["results"])
    report["status"] = "partial" if failed else "completed"
    return report, 3 if failed else 0
