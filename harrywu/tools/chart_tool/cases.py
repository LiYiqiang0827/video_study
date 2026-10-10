"""Immutable chart.v1 restoration storage and separate append-only revelations.

Transcript/frame timestamps are declared provenance, not semantic certification.
Locks coordinate cooperating local writers; this is not an OS access sandbox.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import wraps
import hashlib
import json
import math
import os
from pathlib import Path, PureWindowsPath
import re
import stat
import uuid

from .contracts import (CONTRACT_VERSION, ChartError, ChartRequest, ChartView,
                        _json, parse_date, parse_timestamp)

_RESTORE_KEYS = {"contract_version", "case_id", "revision", "prior_revision",
                 "revision_reason", "request", "observations", "input_refs",
                 "view_identities", "created_at"}
_REQUEST_KEYS = {"ts_code", "cutoff", "cutoff_source", "stage", "window",
                 "periods", "minute_start", "include_partial", "contract_version"}
_RESULT_KEYS = {"contract_version", "case_id", "revision", "restore_sha256",
                "result_sequence", "observed_through", "complete",
                "coverage_warning", "events", "created_at"}
_OBSERVATIONS = {"seen", "reason", "buy_point", "expected_path", "invalidation"}
_RESTORE_A3_KEYS = {"mention_type", "narrated_actions", "frame_facts", "frame_window"}
_RESULT_A3_KEYS = {"price_anchor", "mention_day", "path_snapshot"}
_EVENT_KINDS = ("first_available", "mention_day_after_cutoff", "interval_high", "interval_low", "latest_available")


def _boundary(function):
    """Malformed caller JSON and filesystem failures retain the public error type."""
    @wraps(function)
    def checked(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except ChartError:
            raise
        except (TypeError, KeyError, AttributeError, IndexError, ValueError) as exc:
            raise ChartError("INVALID_SCHEMA", "Malformed case JSON schema") from exc
        except OSError as exc:
            raise ChartError("CASE_IO", "Case store filesystem operation failed") from exc
    return checked


def _need(condition, code, message):
    if not condition:
        raise ChartError(code, message)


def _keys(value, keys, label):
    _need(isinstance(value, dict) and set(value) == keys, "INVALID_SCHEMA",
          f"{label} has missing or unknown fields")


def _text(value, label):
    _need(isinstance(value, str) and bool(value.strip()), "INVALID_SCHEMA",
          f"{label} must be a nonempty string")


def _versioned_keys(value, legacy, added, label):
    _need(isinstance(value, dict) and set(value) in (legacy, legacy | added),
          "INVALID_SCHEMA", f"{label} must have exactly legacy or Amendment 3 fields")


def _short_strings(value):
    if isinstance(value, str):
        _need(len(value) <= 200, "INVALID_SCHEMA", "Amendment 3 strings must not exceed 200 characters")
    elif isinstance(value, dict):
        for item in value.values():
            _short_strings(item)
    elif isinstance(value, list):
        for item in value:
            _short_strings(item)


def _optional_text(value, label):
    _need(value is None or isinstance(value, str), "INVALID_SCHEMA", f"{label} must be string or null")


def _numeric(value, label, *, positive=False):
    _need(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
          and (not positive or value > 0), "INVALID_SCHEMA", f"{label} must be finite numeric" + (" and positive" if positive else ""))


def _digest(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _sha(value):
    _need(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None,
          "INVALID_SCHEMA", "SHA256 must be lowercase hexadecimal")


def _case(case_id, revision):
    _need(isinstance(case_id, str) and re.fullmatch(r"[A-Za-z0-9_-]+", case_id) is not None,
          "CASE_PATH", "case_id must use ASCII letters, digits, underscore or hyphen")
    _need(type(revision) is int and revision >= 1, "INVALID_SCHEMA", "revision must be a positive integer")


def _root(store_dir):
    try:
        from .paths import writable_path
        writable_path(store_dir)
        root = Path(store_dir).absolute()
        # Reject a supplied root or existing ancestor that redirects via a link.
        for part in [root, *root.parents]:
            _not_link(part)
        return root.resolve()
    except (OSError, ValueError, TypeError) as exc:
        raise ChartError("CASE_PATH", "Invalid store root") from exc


def _not_link(path):
    if path.exists() or path.is_symlink():
        info = path.lstat()
        _need(not stat.S_ISLNK(info.st_mode) and not (
            getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)),
            "CASE_PATH", "Links and reparse points are forbidden in the case store")
        _need(not stat.S_ISREG(info.st_mode) or info.st_nlink == 1,
              "CASE_PATH", "Hard-linked case files are forbidden")


def _bounded(root, value):
    _text(value, "path")
    # Check Windows syntax on every OS, including drive-relative paths and ADS.
    win = PureWindowsPath(value)
    _need(".." not in win.parts and ".." not in Path(value).parts,
          "CASE_PATH", "Parent traversal is forbidden")
    _need(not (win.drive and not win.is_absolute()), "CASE_PATH", "Drive-relative path is forbidden")
    path = Path(value)
    if not path.is_absolute():
        _need(not win.is_absolute(), "CASE_PATH", "Foreign absolute path is forbidden")
        path = root / path
    try:
        relative = path.absolute().relative_to(root)
        _need(all(":" not in part for part in relative.parts), "CASE_PATH", "Alternate streams are forbidden")
        current = root
        for part in relative.parts:
            current /= part
            _not_link(current)
        resolved = path.resolve()
        resolved.relative_to(root)
        return resolved
    except (ValueError, OSError) as exc:
        raise ChartError("CASE_PATH", "Path escapes the store") from exc


def _read_bytes(root, path):
    path = _bounded(root, str(path))
    _need(path.is_file(), "CASE_NOT_FOUND", "Required case/input file does not exist")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ChartError("CASE_IO", "Unable to read case/input file") from exc


def _decode(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            _need(key not in result, "INVALID_SCHEMA", "Duplicate JSON object key")
            result[key] = value
        return result
    try:
        result = json.loads(data.decode("utf-8-sig"), object_pairs_hook=unique,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
        _json(result)
        return result
    except (UnicodeError, ValueError, TypeError) as exc:
        if isinstance(exc, ChartError):
            raise
        raise ChartError("INVALID_SCHEMA", "Invalid finite UTF-8 JSON") from exc


def _revision_path(root, case_id, revision, name):
    _case(case_id, revision)
    return _bounded(root, str(root / "restore" / case_id / f"revision-{revision}" / name))


@contextmanager
def _writer(root, case_id):
    lock_dir = _bounded(root, str(root / ".locks"))
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock = _bounded(root, str(lock_dir / f"{case_id}.lock"))
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ChartError("CASE_LOCKED", "Case already has an active writer") from exc
    except OSError as exc:
        raise ChartError("CASE_IO", "Unable to acquire case writer lock") from exc
    try:
        os.write(fd, str(os.getpid()).encode("ascii"))
        yield
    finally:
        os.close(fd)
        lock.unlink()


def _publish(root, path, value):
    """Flush a unique temporary file, then atomically link without overwriting."""
    path = _bounded(root, str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = _bounded(root, str(path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"))
    try:
        with temporary.open("xb") as stream:
            stream.write(_json(value).encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        # Same-directory hard-link publication is atomic and exclusive, including Windows.
        os.link(temporary, path)
    except FileExistsError as exc:
        raise ChartError("CASE_CONFLICT", "Append-only record already exists") from exc
    except OSError as exc:
        raise ChartError("CASE_IO", "Unable to publish exclusive case record") from exc
    finally:
        if temporary.exists():
            temporary.unlink()
    return path


def _validate_restore(root, record):
    _versioned_keys(record, _RESTORE_KEYS, _RESTORE_A3_KEYS, "restoration record")
    _json(record)
    _need(record["contract_version"] == CONTRACT_VERSION, "CONTRACT_VERSION", "Unsupported case contract")
    case_id, revision = record["case_id"], record["revision"]
    _case(case_id, revision)
    parse_timestamp(record["created_at"])
    if revision == 1:
        _need(record["prior_revision"] is None and record["revision_reason"] is None,
              "INVALID_SCHEMA", "First revision has no predecessor or revision reason")
    else:
        _need(type(record["prior_revision"]) is int and record["prior_revision"] == revision - 1,
              "CASE_REVISION", "Revision must point to immediately preceding frozen revision")
        _text(record["revision_reason"], "revision_reason")
        _load_frozen(root, case_id, revision - 1)
    data = record["request"]
    _keys(data, _REQUEST_KEYS, "case request")
    _need(isinstance(data["periods"], list) and all(isinstance(p, str) for p in data["periods"]),
          "INVALID_SCHEMA", "request periods must be a JSON list of strings")
    _need(isinstance(data["ts_code"], str), "INVALID_SCHEMA", "ts_code must be a string")
    request = ChartRequest(**{**data, "periods": tuple(data["periods"])})
    request.validate()
    _need(request.stage == "restore", "CASE_STAGE", "Case request stage must be restore")
    refs = record["input_refs"]
    identities = record["view_identities"]
    _need(isinstance(refs, list) and bool(refs), "INVALID_SCHEMA", "Nonempty input_refs required")
    _need(isinstance(identities, list) and bool(identities), "INVALID_SCHEMA", "Nonempty view_identities required")
    paths, evidence_paths, actual_identities = set(), set(), []
    for ref in refs:
        _keys(ref, {"kind", "path", "sha256", "visible_through"}, "input ref")
        _need(ref["kind"] in {"transcript", "frame", "view"}, "INVALID_SCHEMA", "Invalid input kind")
        path = _bounded(root, ref["path"])
        # Dedicated input namespace also prevents export metadata collisions.
        # Absolute refs cannot be relocated into a fresh analyst context safely.
        relative = path.relative_to(root)
        _need(not Path(ref["path"]).is_absolute() and not PureWindowsPath(ref["path"]).is_absolute()
              and len(relative.parts) >= 2 and relative.parts[0].casefold() == "inputs",
              "CASE_PATH", "Restoration refs must be relative files under inputs/")
        _need(path not in paths, "INVALID_SCHEMA", "Duplicate input path")
        paths.add(path)
        _sha(ref["sha256"])
        raw = _read_bytes(root, path)
        _need(bool(raw), "INVALID_SCHEMA", "Empty bounded input is forbidden")
        _need(hashlib.sha256(raw).hexdigest() == ref["sha256"], "CASE_HASH", "Input hash mismatch")
        visible_through = parse_timestamp(ref["visible_through"])
        if ref["kind"] == "view":
            try:
                view = ChartView.from_dict(_decode(raw))
                identity = view.identity()
            except (TypeError, KeyError, AttributeError, IndexError) as exc:
                raise ChartError("INVALID_SCHEMA", "Malformed typed view input") from exc
            m = view.metadata
            _need(visible_through <= parse_timestamp(m["effective_cutoff"]),
                  "CUTOFF_LEAK", "View ref exceeds its effective market-data cutoff")
            _need(view.ts_code == request.ts_code and view.period in request.periods and
                  m["stage"] == "restore" and m["requested_cutoff"] == request.cutoff and
                  m["cutoff_source"] == request.cutoff_source and m["window"] == request.window,
                  "CASE_VIEW", "Typed view does not match restoration request")
            if not request.include_partial:
                _need(not m["incomplete"], "CASE_VIEW", "Request excludes partial bars")
            if request.minute_start is not None and view.period != "daily":
                _need(all(d >= request.minute_start for d in view.bars.trade_date),
                      "CASE_VIEW", "Minute rows precede requested minute_start")
            actual_identities.append(identity)
        else:
            _need(visible_through <= parse_timestamp(request.cutoff),
                  "CUTOFF_LEAK", "Transcript/frame exceeds actual requested mention cutoff")
            evidence_paths.add(path)
    _need(len(actual_identities) == len(request.periods) and
          {v["period"] for v in actual_identities} == set(request.periods),
          "CASE_VIEW", "Exactly one actual typed view per requested period required")
    cutoffs = {v["metadata"]["effective_cutoff"] for v in actual_identities}
    _need(len(cutoffs) == 1, "CASE_VIEW", "Views must share effective cutoff")
    effective = parse_timestamp(next(iter(cutoffs)))
    _need(sorted(_json(v) for v in actual_identities) == sorted(_json(v) for v in identities),
          "CASE_VIEW", "Claimed identities do not match actual typed views")
    observations = record["observations"]
    _keys(observations, _OBSERVATIONS, "observations")
    for observation in observations.values():
        _keys(observation, {"status", "text", "evidence_refs"}, "observation")
        cited = observation["evidence_refs"]
        _need(isinstance(cited, list), "INVALID_SCHEMA", "Observation evidence_refs must be a list")
        if observation["status"] == "not_mentioned":
            _need(observation["text"] is None and cited == [], "INVALID_SCHEMA", "Unmentioned observations cannot contain inferred content")
        else:
            _need(observation["status"] == "mentioned", "INVALID_SCHEMA", "Invalid observation status")
            _text(observation["text"], "observation text")
            _need(bool(cited) and all(_bounded(root, p) in evidence_paths for p in cited),
                  "CASE_EVIDENCE", "Mentioned observations require bounded transcript/frame evidence")
    if "mention_type" in record:
        daily_cutoffs = [parse_timestamp(v["metadata"]["effective_cutoff"]) for v in actual_identities if v["period"] == "daily"]
        _validate_restore_a3(root, record, effective, daily_cutoffs[0] if daily_cutoffs else effective)
    return effective


def _validate_restore_a3(root, record, effective, daily_effective):
    for name in _RESTORE_A3_KEYS:
        _short_strings(record[name])
    _need(record["mention_type"] in {"new_pick", "live_operation", "retrospective", "mixed", "unclear"}, "INVALID_SCHEMA", "Invalid mention_type")
    evidence = {_bounded(root, ref["path"]): ref["kind"] for ref in record["input_refs"] if ref["kind"] in {"transcript", "frame"}}

    def cited_path(path, kinds):
        resolved = _bounded(root, path)
        _need(evidence.get(resolved) in kinds, "CASE_EVIDENCE", "Reference must cite an input with the required evidence kind")
        return resolved

    _need(isinstance(record["narrated_actions"], list), "INVALID_SCHEMA", "narrated_actions must be a list")
    for action in record["narrated_actions"]:
        _keys(action, {"action", "tense", "bar_ref", "price_text", "stated_reason", "stated_expectation", "stated_outcome", "evidence_refs", "segment_ids", "verification"}, "narrated action")
        _need(action["action"] in {"buy", "sell", "add", "reduce", "hold", "watch", "plan_buy", "plan_sell", "plan_hold", "other"}, "INVALID_SCHEMA", "Invalid narrated action")
        _need(action["tense"] in {"past", "present", "plan"}, "INVALID_SCHEMA", "Invalid action tense")
        _need(action["verification"] in {"transcript_unverified", "audio_verified"}, "INVALID_SCHEMA", "Invalid verification")
        bar = action["bar_ref"]
        _keys(bar, {"trade_date", "period", "basis", "note"}, "bar_ref")
        _need(bar["period"] in {"daily", "15m", "1m", None} and bar["basis"] in {"explicit", "inferred", "unknown"}, "INVALID_SCHEMA", "Invalid bar reference period/basis")
        _optional_text(bar["note"], "bar_ref note")
        if bar["basis"] in {"inferred", "unknown"}:
            _text(bar["note"], "Inferred or unknown bar reference note")
        _need((bar["trade_date"] is None) == (bar["basis"] == "unknown" or action["tense"] == "plan"), "INVALID_SCHEMA", "Null trade_date is required exactly for unknown references or planned actions")
        if bar["trade_date"] is not None:
            _need(parse_date(bar["trade_date"]) <= effective.date(), "CUTOFF_LEAK", "Narrated action refers to a future bar")
        for name in ("price_text", "stated_reason", "stated_expectation", "stated_outcome"):
            _optional_text(action[name], name)
        refs = action["evidence_refs"]
        segments = action["segment_ids"]
        _need(isinstance(refs, list) and bool(refs) and isinstance(segments, list), "INVALID_SCHEMA", "Nonempty action evidence and list of segments required")
        paths = [cited_path(p, {"transcript", "frame"}) for p in refs]
        _need(bool(segments) or any(evidence[p] == "frame" for p in paths), "CASE_EVIDENCE", "Actions without segments require frame evidence")
        texts = []
        for path in paths:
            if evidence[path] == "transcript":
                try:
                    texts.append(_read_bytes(root, path).decode("utf-8-sig"))
                except UnicodeError as exc:
                    raise ChartError("CASE_EVIDENCE", "Referenced transcript must be UTF-8 text") from exc
        for segment in segments:
            _text(segment, "segment id")
            pattern = r"(?<!\w)" + re.escape(segment) + r"(?!\w)"
            _need(any(re.search(pattern, text) for text in texts), "CASE_EVIDENCE", "Segment id absent from cited transcripts")

    _need(isinstance(record["frame_facts"], list), "INVALID_SCHEMA", "frame_facts must be a list")
    for fact in record["frame_facts"]:
        _keys(fact, {"field", "value_text", "frame_ref", "region", "read_by", "verified"}, "frame fact")
        _need(fact["field"] in {"chart_period", "chart_adjustment", "chart_window", "price_label", "drawn_object", "indicator", "order_book", "auction_indicative", "quote_panel", "news_panel", "other"}, "INVALID_SCHEMA", "Invalid frame fact field")
        for name in ("value_text", "region"):
            _text(fact[name], name)
        cited_path(fact["frame_ref"], {"frame"})
        _need(fact["read_by"] in {"human", "ai_visual", "ocr"} and type(fact["verified"]) is bool and (not fact["verified"] or fact["read_by"] == "human"), "INVALID_SCHEMA", "Only human-read frame facts may be verified")
    window = record["frame_window"]
    if window is not None:
        _keys(window, {"period", "first_visible_trade_date", "last_visible_trade_date", "bar_count_estimate", "tolerance_bars", "basis", "evidence_ref", "frame_chart_region_px", "note"}, "frame_window")
        first, last = parse_date(window["first_visible_trade_date"]), parse_date(window["last_visible_trade_date"])
        _need(first <= last, "INVALID_SCHEMA", "Frame window dates must be ordered")
        _need(last <= daily_effective.date(), "CUTOFF_LEAK", "Frame window exceeds daily effective cutoff")
        _need(window["period"] == "daily" and window["basis"] in {"visual_estimate", "ocr"}, "INVALID_SCHEMA", "Invalid frame window period/basis")
        _need(type(window["bar_count_estimate"]) is int and window["bar_count_estimate"] >= 1 and type(window["tolerance_bars"]) is int and window["tolerance_bars"] >= 0, "INVALID_SCHEMA", "Invalid frame window bar counts")
        cited_path(window["evidence_ref"], {"frame"})
        _optional_text(window["note"], "frame window note")
        region = window["frame_chart_region_px"]
        if region is not None:
            _need(isinstance(region, list) and len(region) == 4, "INVALID_SCHEMA", "Frame chart region must have four coordinates")
            for coord in region:
                _numeric(coord, "frame coordinate")
            _need(0 <= region[0] < region[2] and 0 <= region[1] < region[3], "INVALID_SCHEMA", "Invalid frame chart pixel rectangle")


def _load_frozen(root, case_id, revision):
    path = _revision_path(root, case_id, revision, "frozen.json")
    wrapper = _decode(_read_bytes(root, path))
    _keys(wrapper, {"record", "restore_sha256", "frozen_at"}, "frozen wrapper")
    _sha(wrapper["restore_sha256"])
    parse_timestamp(wrapper["frozen_at"])
    _need(_digest(wrapper["record"]) == wrapper["restore_sha256"], "CASE_HASH", "Frozen record hash mismatch")
    _validate_restore(root, wrapper["record"])
    _need(wrapper["record"]["case_id"] == case_id and wrapper["record"]["revision"] == revision,
          "CASE_CONFLICT", "Frozen record identity does not match its path")
    # Outcome time is measured from the actual mention, which may be later
    # than the last completed daily market-data cutoff.
    return wrapper, parse_timestamp(wrapper["record"]["request"]["cutoff"])


def _save(store_dir, record, *, subsequent):
    root = _root(store_dir)
    _versioned_keys(record, _RESTORE_KEYS, _RESTORE_A3_KEYS, "restoration record")
    _case(record["case_id"], record["revision"])
    _need((record["revision"] > 1) == subsequent, "CASE_REVISION",
          "Use save_draft for revision 1 and append_revision for subsequent revisions")
    with _writer(root, record["case_id"]):
        _validate_restore(root, record)
        return _publish(root, _revision_path(root, record["case_id"], record["revision"], "draft.json"), record)


@_boundary
def save_draft(store_dir: str | Path, record: dict) -> Path:
    return _save(store_dir, record, subsequent=False)


@_boundary
def append_revision(store_dir: str | Path, record: dict) -> Path:
    return _save(store_dir, record, subsequent=True)


@_boundary
def freeze_restore(store_dir: str | Path, case_id: str, revision: int, *, expected_sha256: str) -> dict:
    root = _root(store_dir)
    _case(case_id, revision)
    _sha(expected_sha256)
    with _writer(root, case_id):
        raw = _read_bytes(root, _revision_path(root, case_id, revision, "draft.json"))
        _need(hashlib.sha256(raw).hexdigest() == expected_sha256, "CASE_HASH", "Draft bytes differ from caller's expected SHA256")
        record = _decode(raw)
        _validate_restore(root, record)
        _need(record["case_id"] == case_id and record["revision"] == revision,
              "CASE_CONFLICT", "Draft identity differs from path")
        path = _revision_path(root, case_id, revision, "frozen.json")
        if path.exists():
            wrapper, _ = _load_frozen(root, case_id, revision)
            _need(wrapper["record"] == record, "CASE_CONFLICT", "Conflicting immutable freeze retry")
        else:
            wrapper = {"record": record, "restore_sha256": _digest(record),
                       "frozen_at": datetime.now(timezone(timedelta(hours=8))).isoformat()}
            _publish(root, path, wrapper)
        return {"contract_version": CONTRACT_VERSION, "case_id": case_id,
                "revision": revision, "restore_sha256": wrapper["restore_sha256"],
                "frozen_at": wrapper["frozen_at"], "restore_path": str(path)}


@_boundary
def load_restore_input(store_dir: str | Path, case_id: str, revision: int) -> dict:
    # This code path never consults the results namespace, even for existence.
    return _load_frozen(_root(store_dir), case_id, revision)[0]["record"]


def _validate_result(root, result, wrapper, effective):
    _versioned_keys(result, _RESULT_KEYS, _RESULT_A3_KEYS, "reveal record")
    _json(result)
    frozen = wrapper["record"]
    _need(result["contract_version"] == CONTRACT_VERSION, "CONTRACT_VERSION", "Unsupported result contract")
    _case(result["case_id"], result["revision"])
    _sha(result["restore_sha256"])
    _need(result["case_id"] == frozen["case_id"] and result["revision"] == frozen["revision"] and
          result["restore_sha256"] == wrapper["restore_sha256"], "CASE_HASH", "Reveal must reference exact frozen revision/hash")
    _need(type(result["result_sequence"]) is int and result["result_sequence"] >= 1,
          "INVALID_SCHEMA", "result_sequence must be a positive integer")
    observed = parse_timestamp(result["observed_through"])
    parse_timestamp(result["created_at"])
    _need(type(result["complete"]) is bool, "INVALID_SCHEMA", "complete must be boolean")
    _need(observed >= effective and (observed > effective or not result["complete"]),
          "CUTOFF_LEAK", "Coverage cannot precede actual mention; no future coverage requires incomplete status")
    if result["complete"]:
        _need(result["coverage_warning"] is None or isinstance(result["coverage_warning"], str),
              "INVALID_SCHEMA", "coverage_warning must be string or null")
    else:
        _text(result["coverage_warning"], "coverage_warning for incomplete result")
    _need(isinstance(result["events"], list), "INVALID_SCHEMA", "events must be a list")
    amendment3 = "path_snapshot" in result
    if amendment3:
        _validate_result_a3(result)
    previous = effective
    seen_kinds, previous_rank = set(), -1
    for event in result["events"]:
        _keys(event, {"at", "kind", "description", "evidence_refs"}, "result event")
        at = parse_timestamp(event["at"])
        _need(effective < at <= observed and at >= previous, "CUTOFF_LEAK", "Result events must be ordered and strictly later than actual mention cutoff within coverage")
        if amendment3:
            kind = event["kind"]
            _need(kind in _EVENT_KINDS and kind not in seen_kinds, "INVALID_SCHEMA", "Event kinds must be allowed and unique")
            rank = _EVENT_KINDS.index(kind)
            _need(at != previous or rank > previous_rank, "INVALID_SCHEMA", "Events at equal timestamps must follow kind order")
            seen_kinds.add(kind)
            previous_rank = rank
        previous = at
        _text(event["kind"], "event kind")
        _text(event["description"], "event description")
        refs = event["evidence_refs"]
        _need(isinstance(refs, list) and bool(refs), "CASE_EVIDENCE", "Result event evidence required")
        for ref in refs:
            _need(bool(_read_bytes(root, _bounded(root, ref))), "CASE_EVIDENCE", "Result evidence is empty")


def _validate_result_a3(result):
    anchor = result["price_anchor"]
    _keys(anchor, {"anchor_trade_date", "anchor_value", "source"}, "price_anchor")
    parse_date(anchor["anchor_trade_date"])
    _numeric(anchor["anchor_value"], "anchor_value", positive=True)
    _need(anchor["source"] in {"frozen_restore_view", "fallback_last_closed"}, "INVALID_SCHEMA", "Invalid price anchor source")
    mention = result["mention_day"]
    _keys(mention, {"trade_date", "policy", "reference_session_date", "post_mention_high", "post_mention_low", "minute_source_id", "note"}, "mention_day")
    parse_date(mention["trade_date"])
    parse_date(mention["reference_session_date"])
    _need(mention["policy"] in {"non_trading_day", "preopen_full_day", "auction_window", "split_by_minutes", "lunch_split", "excluded_no_minutes", "postclose_excluded"}, "INVALID_SCHEMA", "Invalid mention day policy")
    for name in ("post_mention_high", "post_mention_low"):
        if mention[name] is not None:
            _numeric(mention[name], name, positive=True)
    for name in ("minute_source_id", "note"):
        _optional_text(mention[name], name)
    path = result["path_snapshot"]
    _keys(path, {"method", "price_basis", "reference", "horizons", "notes"}, "path_snapshot")
    _need(path["method"] == "fixed_horizon_v1" and path["price_basis"] == "restore_anchor_qfq", "INVALID_SCHEMA", "Invalid path method/price basis")
    reference = path["reference"]
    _keys(reference, {"kind", "trade_date", "at", "price", "raw_price"}, "path reference")
    _need(reference["kind"] in {"next_session_open", "auction_price", "last_minute_close", "lunch_close"}, "INVALID_SCHEMA", "Invalid reference kind")
    _need(parse_timestamp(reference["at"]).date() == parse_date(reference["trade_date"]), "INVALID_SCHEMA", "Reference timestamp and date differ")
    for name in ("price", "raw_price"):
        _numeric(reference[name], name, positive=True)
    _need(isinstance(path["notes"], list) and all(isinstance(n, str) for n in path["notes"]), "INVALID_SCHEMA", "Path notes must be a string list")
    horizons = path["horizons"]
    _need(isinstance(horizons, list) and len(horizons) == 5, "INVALID_SCHEMA", "Five fixed horizons required")
    fields = {"trade_date", "close", "close_return", "mfe", "mfe_trade_date", "mae", "mae_trade_date"}
    previous = None
    for horizon, k in zip(horizons, (1, 3, 5, 10, 20)):
        _keys(horizon, fields | {"k", "complete"}, "horizon")
        _need(type(horizon["k"]) is int and horizon["k"] == k and type(horizon["complete"]) is bool, "INVALID_SCHEMA", "Invalid horizon k/order/complete")
        if not horizon["complete"]:
            _need(all(horizon[name] is None for name in fields), "INVALID_SCHEMA", "Incomplete horizon must have only null values")
            continue
        _need(all(horizon[name] is not None for name in fields), "INVALID_SCHEMA", "Completed horizon must have all values")
        day = parse_date(horizon["trade_date"])
        _need(previous is None or day > previous, "INVALID_SCHEMA", "Completed horizon dates must strictly increase")
        previous = day
        for name in ("mfe_trade_date", "mae_trade_date"):
            parse_date(horizon[name])
        _numeric(horizon["close"], "horizon close", positive=True)
        for name in ("close_return", "mfe", "mae"):
            _numeric(horizon[name], name)
        _need(horizon["mae"] - 1e-12 <= horizon["close_return"] <= horizon["mfe"] + 1e-12, "INVALID_SCHEMA", "Horizon return lies outside mae/mfe")
    _need(result["complete"] == all(h["complete"] for h in horizons), "INVALID_SCHEMA", "Result complete must equal completion of all five horizons")


def _results(root, case_id, revision, wrapper, effective):
    directory = _bounded(root, str(root / "results" / case_id / f"revision-{revision}"))
    if not directory.exists():
        return []
    _need(directory.is_dir(), "CASE_PATH", "Results namespace must be a directory")
    found = []
    for path in directory.iterdir():
        _bounded(root, str(path))
        if path.name.startswith(".") and path.name.endswith(".tmp"):
            continue  # An unpublished temporary file is not an accepted result.
        match = re.fullmatch(r"result-([1-9][0-9]*)\.json", path.name)
        _need(match is not None and path.is_file(), "INVALID_SCHEMA", "Unknown entry in result revision directory")
        found.append((int(match[1]), path))
    found.sort()
    _need([n for n, _ in found] == list(range(1, len(found) + 1)), "CASE_SEQUENCE", "Result sequence has a gap")
    results = []
    for sequence, path in found:
        result = _decode(_read_bytes(root, path))
        _validate_result(root, result, wrapper, effective)
        _need(result["result_sequence"] == sequence, "CASE_SEQUENCE", "Result sequence differs from its filename")
        results.append(result)
    return results


@_boundary
def append_result(store_dir: str | Path, result: dict) -> Path:
    root = _root(store_dir)
    _versioned_keys(result, _RESULT_KEYS, _RESULT_A3_KEYS, "reveal record")
    case_id, revision = result["case_id"], result["revision"]
    _case(case_id, revision)
    with _writer(root, case_id):
        wrapper, effective = _load_frozen(root, case_id, revision)
        _validate_result(root, result, wrapper, effective)
        existing = _results(root, case_id, revision, wrapper, effective)
        _need(result["result_sequence"] == len(existing) + 1, "CASE_SEQUENCE", "Result sequence must be the next contiguous append")
        path = _bounded(root, str(root / "results" / case_id / f"revision-{revision}" / f"result-{result['result_sequence']}.json"))
        return _publish(root, path, result)


@_boundary
def load_results(store_dir: str | Path, case_id: str, revision: int) -> list[dict]:
    root = _root(store_dir)
    wrapper, effective = _load_frozen(root, case_id, revision)
    return _results(root, case_id, revision, wrapper, effective)


@_boundary
def list_pending_reveals(store_dir: str | Path) -> list[dict]:
    root = _root(store_dir)
    directory = _bounded(root, str(root / "restore"))
    if not directory.exists():
        return []
    pending = []
    for case_dir in directory.iterdir():
        _bounded(root, str(case_dir))
        _case(case_dir.name, 1)
        _need(case_dir.is_dir(), "CASE_PATH", "Case entry must be a directory")
        for revision_dir in case_dir.iterdir():
            _bounded(root, str(revision_dir))
            match = re.fullmatch(r"revision-([1-9][0-9]*)", revision_dir.name)
            _need(match is not None and revision_dir.is_dir(), "CASE_PATH", "Invalid revision directory")
            revision = int(match[1])
            if not _revision_path(root, case_dir.name, revision, "frozen.json").exists():
                continue
            wrapper, effective = _load_frozen(root, case_dir.name, revision)
            if not _results(root, case_dir.name, revision, wrapper, effective):
                pending.append({"case_id": case_dir.name, "revision": revision,
                                "restore_sha256": wrapper["restore_sha256"]})
    return sorted(pending, key=lambda item: (item["case_id"], item["revision"]))
