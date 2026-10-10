"""SYNTHETIC_ENGINEERING_ONLY case-store boundary checks; no source database IO."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import uuid

import pytest

from chart_tool import cases
from chart_tool.contracts import ChartError, ChartRequest, ChartView, DAILY_COLUMNS, _json

OUTPUT = Path(r"C:\Project Rocket\outputs\chart_tool_development_20261009\D")
SINGLE = Path(r"C:\Project Rocket\outputs\chart_tool_development_20261009\inputs\render\single.json")
CUTOFF = "2026-09-30T15:00:00+08:00"
LATER = "2026-10-09T10:00:00+08:00"


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json(value), encoding="utf-8")
    return path


def make_record(root, case_id="synthetic_case1", revision=1):
    payload = json.loads(SINGLE.read_text(encoding="utf-8-sig"))
    payload["bars"] = {"columns": list(DAILY_COLUMNS), "records": payload["bars"]}
    payload["metadata"]["stage"] = "restore"
    source = {"kind": "manual", "confidence": "high",
              "evidence_refs": ["inputs/transcript.txt"], "candidates": [], "calibration_segment": None}
    payload["metadata"]["cutoff_source"] = source
    view = ChartView.from_dict(payload)
    view_path = _write(root / "inputs" / "view.json", view.to_dict())
    transcript = root / "inputs" / "transcript.txt"
    transcript.write_text("SYNTHETIC_ENGINEERING_ONLY\n观察量价", encoding="utf-8")
    refs = [{"kind": kind, "path": path.relative_to(root).as_posix(),
             "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "visible_through": CUTOFF}
            for kind, path in [("view", view_path), ("transcript", transcript)]]
    observations = {key: {"status": "not_mentioned", "text": None, "evidence_refs": []}
                    for key in ["seen", "reason", "buy_point", "expected_path", "invalidation"]}
    observations["seen"] = {"status": "mentioned", "text": "观察量价", "evidence_refs": ["inputs/transcript.txt"]}
    request = ChartRequest(payload["ts_code"], CUTOFF, source, stage="restore").to_dict()
    return {"contract_version": "chart.v1", "case_id": case_id, "revision": revision,
            "prior_revision": revision - 1 if revision > 1 else None,
            "revision_reason": "SYNTHETIC_ENGINEERING_ONLY: evidence correction" if revision > 1 else None,
            "request": request, "observations": observations, "input_refs": refs,
            "view_identities": [view.identity()], "created_at": "2026-10-09T09:00:00+08:00"}


def freeze(root, record):
    operation = cases.save_draft if record["revision"] == 1 else cases.append_revision
    draft = operation(root, record)
    return cases.freeze_restore(root, record["case_id"], record["revision"],
                                expected_sha256=hashlib.sha256(draft.read_bytes()).hexdigest())


def result(root, frozen, sequence=1):
    evidence = _write(root / "later-evidence" / f"sample-{sequence}.json",
                      {"classification": "SYNTHETIC_ENGINEERING_ONLY", "observed_through": LATER,
                       "future_sentinel": "RESULT_PRIVATE_FUTURE_SENTINEL"})
    return {"contract_version": "chart.v1", "case_id": frozen["case_id"], "revision": frozen["revision"],
            "restore_sha256": frozen["restore_sha256"], "result_sequence": sequence,
            "observed_through": LATER, "complete": False,
            "coverage_warning": "SYNTHETIC_ENGINEERING_ONLY: sparse later observations; outcome incomplete",
            "events": [{"at": LATER, "kind": "engineering_sample", "description": "RESULT_PRIVATE_FUTURE_SENTINEL",
                        "evidence_refs": [evidence.relative_to(root).as_posix()]}], "created_at": LATER}


@pytest.fixture
def store():
    root = OUTPUT / "test-stores" / uuid.uuid4().hex
    root.mkdir(parents=True)
    return root


def test_freeze_bytes_hash_retry_and_immutable_draft(store):
    record = make_record(store)
    draft = cases.save_draft(store, record)
    raw = draft.read_bytes()
    expected = hashlib.sha256(raw).hexdigest()
    frozen = cases.freeze_restore(store, record["case_id"], 1, expected_sha256=expected)
    before = Path(frozen["restore_path"]).read_bytes()
    assert cases.freeze_restore(store, record["case_id"], 1, expected_sha256=expected) == frozen
    assert Path(frozen["restore_path"]).read_bytes() == before
    assert frozen["restore_sha256"] == hashlib.sha256(_json(record).encode("utf-8")).hexdigest()
    assert draft.read_bytes() == raw
    with pytest.raises(ChartError, match="already exists"):
        cases.save_draft(store, record)


def test_wrong_hash_and_failedfreeze_cannot_reveal(store):
    record = make_record(store)
    cases.save_draft(store, record)
    with pytest.raises(ChartError) as error:
        cases.freeze_restore(store, record["case_id"], 1, expected_sha256="0" * 64)
    assert error.value.code == "CASE_HASH"
    assert not (store / "restore" / record["case_id"] / "revision-1" / "frozen.json").exists()
    with pytest.raises(ChartError) as error:
        cases.append_result(store, result(store, {"case_id": record["case_id"], "revision": 1, "restore_sha256": "0" * 64}))
    assert error.value.code == "CASE_NOT_FOUND"
    assert cases.list_pending_reveals(store) == []


def test_result_separation_restore_never_reads_results(store, monkeypatch):
    record = make_record(store)
    frozen = freeze(store, record)
    before = cases.load_restore_input(store, record["case_id"], 1)
    frozen_bytes = Path(frozen["restore_path"]).read_bytes()
    path = cases.append_result(store, result(store, frozen))
    assert path.relative_to(store).parts[0] == "results"
    assert cases.load_results(store, record["case_id"], 1)[0]["complete"] is False
    original = Path.read_bytes
    def guard(path):
        assert "results" not in path.relative_to(store).parts
        return original(path)
    monkeypatch.setattr(Path, "read_bytes", guard)
    restored = cases.load_restore_input(store, record["case_id"], 1)
    assert restored == before == record
    assert "RESULT_PRIVATE_FUTURE_SENTINEL" not in _json(restored)
    assert Path(frozen["restore_path"]).read_bytes() == frozen_bytes


def test_never_mentioned_again_and_per_revision_pending(store):
    record = make_record(store)
    first = freeze(store, record)
    second_record = deepcopy(record)
    second_record["case_id"] = "synthetic_never_mentioned_again"
    second = freeze(store, second_record)
    third_record = deepcopy(record)
    third_record.update(revision=2, prior_revision=1, revision_reason="Synthetic evidence clarification")
    third = freeze(store, third_record)
    cases.append_result(store, result(store, first))
    pending = cases.list_pending_reveals(store)
    assert pending == sorted([{"case_id": x["case_id"], "revision": x["revision"],
                               "restore_sha256": x["restore_sha256"]} for x in [second, third]],
                             key=lambda x: (x["case_id"], x["revision"]))
    assert cases.load_restore_input(store, record["case_id"], 1) == record
    assert cases.load_results(store, record["case_id"], 2) == []


@pytest.mark.parametrize("change", ["missing_prior", "wrong_prior", "missing_reason"])
def test_revision_requires_frozen_prior_and_reason(store, change):
    record = make_record(store)
    if change != "missing_prior":
        freeze(store, record)
    record.update(revision=2, prior_revision=1, revision_reason="Synthetic revision")
    if change == "wrong_prior":
        record["prior_revision"] = 0
    if change == "missing_reason":
        record["revision_reason"] = " "
    with pytest.raises(ChartError):
        cases.append_revision(store, record)


@pytest.mark.parametrize("kind", ["extra_record", "extra_observation", "inferred", "no_evidence", "view_evidence", "not_mentioned_refs", "empty_view_identities"])
def test_no_smuggled_or_inferred_observations(store, kind):
    record = make_record(store)
    if kind == "extra_record":
        record["results"] = "future"
    elif kind == "extra_observation":
        record["observations"]["seen"]["result"] = "future"
    elif kind == "inferred":
        record["observations"]["reason"]["text"] = "inferred motive"
    elif kind == "no_evidence":
        record["observations"]["seen"]["evidence_refs"] = []
    elif kind == "view_evidence":
        record["observations"]["seen"]["evidence_refs"] = ["inputs/view.json"]
    elif kind == "not_mentioned_refs":
        record["observations"]["reason"]["evidence_refs"] = ["inputs/transcript.txt"]
    else:
        record["view_identities"] = []
    with pytest.raises(ChartError):
        cases.save_draft(store, record)


@pytest.mark.parametrize("kind", ["future_bar", "digest_lie", "stage", "stock", "future_ref", "mutated_ref"])
def test_validates_actual_typed_view_not_claimed_metadata(store, kind):
    record = make_record(store)
    path = store / "inputs" / "view.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    if kind == "future_bar":
        payload["bars"]["records"][0]["trade_date"] = "2026-10-09"
    elif kind == "digest_lie":
        payload["bars"]["records"][0]["vol"] += 1
    elif kind == "stage":
        payload["metadata"]["stage"] = "reveal"
    elif kind == "stock":
        payload["ts_code"] = "002025.SZ"
    elif kind == "future_ref":
        record["input_refs"][1]["visible_through"] = LATER
    else:
        (store / "inputs" / "transcript.txt").write_text("changed", encoding="utf-8")
    _write(path, payload)
    record["input_refs"][0]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ChartError):
        cases.save_draft(store, record)


def test_freeze_revalidates_refs_changed_after_draft(store):
    record = make_record(store)
    path = cases.save_draft(store, record)
    expected = hashlib.sha256(path.read_bytes()).hexdigest()
    (store / "inputs" / "transcript.txt").write_text("changed", encoding="utf-8")
    with pytest.raises(ChartError) as error:
        cases.freeze_restore(store, record["case_id"], 1, expected_sha256=expected)
    assert error.value.code == "CASE_HASH"
    assert not (path.parent / "frozen.json").exists()


def intraday_daily_record(store):
    record = make_record(store)
    mention = "2026-10-01T10:07:30+08:00"
    record["request"]["cutoff"] = mention
    path = store / "inputs" / "view.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["metadata"]["requested_cutoff"] = mention
    payload["metadata"]["warnings"].append("INTRADAY_DAY_OMITTED")
    view = ChartView.from_dict(payload)
    _write(path, view.to_dict())
    record["input_refs"][0]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    record["input_refs"][1]["visible_through"] = mention
    record["view_identities"] = [view.identity()]
    return record


def test_actual_mention_transcript_after_daily_effective_is_permitted(store):
    record = intraday_daily_record(store)
    frozen = freeze(store, record)
    assert cases.load_restore_input(store, record["case_id"], 1) == record
    cases.append_result(store, result(store, frozen))


def test_no_future_data_still_has_incomplete_result_at_actual_mention(store):
    record = intraday_daily_record(store)
    frozen = freeze(store, record)
    reveal = result(store, frozen)
    reveal["observed_through"] = record["request"]["cutoff"]
    reveal["events"] = []
    reveal["coverage_warning"] = "SYNTHETIC_ENGINEERING_ONLY: no future data available"
    path = cases.append_result(store, reveal)
    assert path.exists()
    assert cases.list_pending_reveals(store) == []
    assert cases.load_results(store, record["case_id"], 1) == [reveal]


def test_no_future_coverage_cannot_claim_complete(store):
    record = intraday_daily_record(store)
    frozen = freeze(store, record)
    reveal = result(store, frozen)
    reveal.update(observed_through=record["request"]["cutoff"], events=[], complete=True)
    with pytest.raises(ChartError) as error:
        cases.append_result(store, reveal)
    assert error.value.code == "CUTOFF_LEAK"


@pytest.mark.parametrize("kind", ["view_at_mention", "transcript_after_mention", "event_before_mention", "coverage_before_mention"])
def test_market_cutoff_and_actual_mention_are_distinct(store, kind):
    record = intraday_daily_record(store)
    if kind == "view_at_mention":
        record["input_refs"][0]["visible_through"] = record["request"]["cutoff"]
        with pytest.raises(ChartError) as error:
            cases.save_draft(store, record)
    elif kind == "transcript_after_mention":
        record["input_refs"][1]["visible_through"] = "2026-10-01T10:08:00+08:00"
        with pytest.raises(ChartError) as error:
            cases.save_draft(store, record)
    else:
        frozen = freeze(store, record)
        reveal = result(store, frozen)
        before_actual_mention = "2026-10-01T10:07:00+08:00"
        if kind == "event_before_mention":
            reveal["events"][0]["at"] = before_actual_mention
        else:
            reveal["observed_through"] = before_actual_mention
            reveal["events"] = []
        with pytest.raises(ChartError) as error:
            cases.append_result(store, reveal)
    assert error.value.code == "CUTOFF_LEAK"


@pytest.mark.parametrize("value", ["../escaped", "a/b", "汉字", "", "a.b"])
def test_case_id_path_rejected(store, value):
    record = make_record(store)
    record["case_id"] = value
    with pytest.raises(ChartError) as error:
        cases.save_draft(store, record)
    assert error.value.code == "CASE_PATH"


@pytest.mark.parametrize("path", ["../escape.json", "inputs/../view.json", "C:relative.json", "inputs/view.json:stream", "results/private.json"])
def test_input_path_confinement(store, path):
    record = make_record(store)
    record["input_refs"][0]["path"] = path
    with pytest.raises(ChartError) as error:
        cases.save_draft(store, record)
    assert error.value.code == "CASE_PATH"


def test_lock_exclusive_freeze_and_result(store):
    record = make_record(store)
    frozen = freeze(store, record)
    expected = hashlib.sha256((Path(frozen["restore_path"]).parent / "draft.json").read_bytes()).hexdigest()
    with cases._writer(cases._root(store), record["case_id"]):
        with pytest.raises(ChartError) as error:
            cases.freeze_restore(store, record["case_id"], 1, expected_sha256=expected)
        assert error.value.code == "CASE_LOCKED"
        with pytest.raises(ChartError) as error:
            cases.append_result(store, result(store, frozen))
        assert error.value.code == "CASE_LOCKED"
    assert not (store / ".locks" / f"{record['case_id']}.lock").exists()
    assert cases.append_result(store, result(store, frozen)).exists()


@pytest.mark.parametrize("kind", ["cutoff_event", "coverage_cutoff", "after_coverage", "descending", "missing_warning", "sequence_gap", "wrong_hash", "extra_key", "bool_sequence", "missing_evidence"])
def test_result_boundaries(store, kind):
    record = make_record(store)
    frozen = freeze(store, record)
    reveal = result(store, frozen)
    if kind == "cutoff_event":
        reveal["events"][0]["at"] = CUTOFF
    elif kind == "coverage_cutoff":
        reveal["observed_through"] = CUTOFF
    elif kind == "after_coverage":
        reveal["events"][0]["at"] = "2026-10-09T10:01:00+08:00"
    elif kind == "descending":
        event = deepcopy(reveal["events"][0])
        event["at"] = "2026-10-09T09:00:00+08:00"
        reveal["events"].append(event)
    elif kind == "missing_warning":
        reveal["coverage_warning"] = None
    elif kind == "sequence_gap":
        reveal["result_sequence"] = 2
    elif kind == "wrong_hash":
        reveal["restore_sha256"] = "0" * 64
    elif kind == "extra_key":
        reveal["win_rate"] = 1
    elif kind == "bool_sequence":
        reveal["result_sequence"] = True
    else:
        reveal["events"][0]["evidence_refs"] = []
    with pytest.raises(ChartError):
        cases.append_result(store, reveal)
    assert cases.list_pending_reveals(store) == [{"case_id": frozen["case_id"], "revision": 1,
                                                "restore_sha256": frozen["restore_sha256"]}]


def test_append_only_results_and_frozen_tamper_detection(store):
    record = make_record(store)
    frozen = freeze(store, record)
    first = result(store, frozen)
    path = cases.append_result(store, first)
    before = path.read_bytes()
    with pytest.raises(ChartError):
        cases.append_result(store, first)
    cases.append_result(store, result(store, frozen, sequence=2))
    assert path.read_bytes() == before
    assert len(cases.load_results(store, record["case_id"], 1)) == 2
    wrapper = json.loads(Path(frozen["restore_path"]).read_text(encoding="utf-8"))
    wrapper["record"]["created_at"] = LATER
    _write(Path(frozen["restore_path"]), wrapper)  # Deliberate engineering corruption.
    with pytest.raises(ChartError) as error:
        cases.load_restore_input(store, record["case_id"], 1)
    assert error.value.code == "CASE_HASH"


def test_corrupted_result_does_not_change_restore_loader(store):
    record = make_record(store)
    frozen = freeze(store, record)
    path = cases.append_result(store, result(store, frozen))
    path.write_text("not valid JSON RESULT_PRIVATE_FUTURE_SENTINEL", encoding="utf-8")
    assert cases.load_restore_input(store, record["case_id"], 1) == record
    with pytest.raises(ChartError):
        cases.load_results(store, record["case_id"], 1)


def test_symlink_escape(store):
    record = make_record(store)
    target = OUTPUT / "test-stores" / f"outside-{uuid.uuid4().hex}.json"
    _write(target, {"classification": "SYNTHETIC_ENGINEERING_ONLY"})
    linked = store / "inputs" / "linked.json"
    try:
        linked.symlink_to(target)
    except OSError:
        pytest.skip("Windows host does not grant symlink creation; deterministic reparse check covered separately")
    record["input_refs"][0]["path"] = "inputs/linked.json"
    with pytest.raises(ChartError) as error:
        cases.save_draft(store, record)
    assert error.value.code == "CASE_PATH"


def test_reparse_point_rejected_without_following(store, monkeypatch):
    record = make_record(store)
    original = Path.lstat
    class ReparseStat:
        st_mode = 0
        st_file_attributes = 1024
    def fake(path):
        if path == store / "inputs" / "view.json":
            return ReparseStat()
        return original(path)
    monkeypatch.setattr(Path, "lstat", fake)
    with pytest.raises(ChartError) as error:
        cases.save_draft(store, record)
    assert error.value.code == "CASE_PATH"


@pytest.mark.parametrize("field", ["ref_kind", "observation", "request_cutoff_source", "identity"])
def test_malformed_json_has_chart_error(store, field):
    record = make_record(store)
    if field == "ref_kind":
        record["input_refs"][0]["kind"] = []
    elif field == "observation":
        record["observations"]["seen"] = []
    elif field == "request_cutoff_source":
        record["request"]["cutoff_source"]["candidates"] = [None]
    else:
        record["view_identities"] = [None]
    with pytest.raises(ChartError):
        cases.save_draft(store, record)


def exercise_evidence():
    root = OUTPUT / f"synthetic-store-{uuid.uuid4().hex}"
    root.mkdir(parents=True)
    record = make_record(root)
    first = freeze(root, record)
    before = cases.load_restore_input(root, record["case_id"], 1)
    restore_bytes = Path(first["restore_path"]).read_bytes()
    result_path = cases.append_result(root, result(root, first))
    case2 = deepcopy(record)
    case2["case_id"] = "synthetic_never_mentioned_again"
    second = freeze(root, case2)
    revision2 = deepcopy(record)
    revision2.update(revision=2, prior_revision=1, revision_reason="SYNTHETIC_ENGINEERING_ONLY bounded correction")
    third = freeze(root, revision2)
    after = cases.load_restore_input(root, record["case_id"], 1)
    assert before == after and Path(first["restore_path"]).read_bytes() == restore_bytes
    assert "RESULT_PRIVATE_FUTURE_SENTINEL" not in _json(after)
    intraday_root = OUTPUT / f"synthetic-intraday-store-{uuid.uuid4().hex}"
    intraday_root.mkdir(parents=True)
    intraday_record = intraday_daily_record(intraday_root)
    intraday_frozen = freeze(intraday_root, intraday_record)
    no_future = result(intraday_root, intraday_frozen)
    no_future.update(observed_through=intraday_record["request"]["cutoff"], events=[],
                     coverage_warning="SYNTHETIC_ENGINEERING_ONLY: no future data available")
    no_future_path = cases.append_result(intraday_root, no_future)
    evidence = {"classification": "SYNTHETIC_ENGINEERING_ONLY", "store_dir": str(root),
                "frozen": [first, second, third], "result_path": str(result_path),
                "result_file_sha256": hashlib.sha256(result_path.read_bytes()).hexdigest(),
                "restore_before_after_equal": before == after, "frozen_bytes_unchanged": True,
                "future_sentinel_absent_from_restore": True, "pending": cases.list_pending_reveals(root),
                "incomplete_result": cases.load_results(root, record["case_id"], 1)[0],
                "intraday_actual_mention_case": {"store_dir": str(intraday_root), "frozen": intraday_frozen,
                    "actual_mention": intraday_record["request"]["cutoff"], "daily_effective_cutoff": CUTOFF,
                    "transcript_visible_through": intraday_record["input_refs"][1]["visible_through"],
                    "no_future_result_path": str(no_future_path), "no_future_result": no_future,
                    "pending": cases.list_pending_reveals(intraday_root)},
                "provenance_limit": "Transcript/frame declared timestamp and hashes do not certify semantic absence of future discussion.",
                "scope_limit": "Task-scoped IO and cooperating-writer locks are not OS access isolation."}
    path = _write(OUTPUT / "evidence.json", evidence)
    print(_json({"evidence": str(path), "store": str(root), "pending_count": len(evidence["pending"])}))


if __name__ == "__main__":
    if sys.argv[1:] != ["--exercise-evidence"]:
        raise SystemExit("Use --exercise-evidence")
    exercise_evidence()
