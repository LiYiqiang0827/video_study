#!/usr/bin/env python3
"""Local-video queue for offline Qwen ASR, alignment and subtitle export.

Use --manifest LIST.json --job-root DIR, then --resume to continue queued items.
--retry-failed permits one additional attempt per invocation, two lifetime total.
STOP_REQUESTED in DIR stops the active child and preserves immutable attempts.
No authentication, model downloading, text repair, or media URLs are supported.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid

sys.dont_write_bytecode = True
from tool_paths import data_root, external_path
from extract_audio import extract_audio, executable

TOOLS = Path(__file__).resolve().parent
TERMINAL = {"processed_pending_review", "partial_needs_review", "failed"}
MIN_DISK = 8 * 1024**3
PROMPT = "抵扣价 均线 多头 空头 金叉 死叉 回踩 缩量 放量 涨停 MA20 MA30 MA60 MA120 MA250"


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024**2), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic(path, value):
    path = Path(path)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def local(value):
    if not isinstance(value, str) or "://" in value or "\0" in value or not Path(value).is_absolute():
        raise ValueError("Only absolute local paths are accepted")
    return Path(value).expanduser().resolve()


def load_manifest(path):
    raw = local(str(path)).read_bytes()
    data = json.loads(raw)
    if data.get("schema_version") != 1 or not isinstance(data.get("items"), list) or not data["items"]:
        raise ValueError("Expected schema_version=1 and a nonempty items list")
    ids = set()
    required = {"video_id", "video_path", "video_sha256", "bytes", "duration_seconds", "title", "upload_date", "upload_time_beijing"}
    for item in data["items"]:
        if not isinstance(item, dict) or not required <= item.keys():
            raise ValueError("Manifest item is missing required fields")
        ident = item["video_id"]
        if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", ident) or ident in ids:
            raise ValueError("Video IDs must be unique, safe, 11-character IDs")
        ids.add(ident)
        local(item["video_path"])
        if not re.fullmatch(r"[a-f0-9]{64}", str(item["video_sha256"])):
            raise ValueError("Invalid source SHA-256")
        if isinstance(item["bytes"], bool) or not isinstance(item["bytes"], int) or item["bytes"] <= 0:
            raise ValueError("Source bytes must be positive")
        duration = item["duration_seconds"]
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
            raise ValueError("Source duration must be finite and positive")
        if any(not isinstance(item[key], str) for key in ("title", "upload_date", "upload_time_beijing")):
            raise ValueError("Title/date fields must be strings")
    return data, raw, hashlib.sha256(raw).hexdigest()


@contextlib.contextmanager
def batch_lock(root):
    with (root / ".batch_asr.lock").open("a") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another batch worker holds this queue lock") from None
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def inventory(directory):
    return {str(path.relative_to(directory)): sha(path) for path in sorted(directory.rglob("*")) if path.is_file()}


def verify_artifacts(item):
    directory = item.get("attempt_directory")
    hashes = item.get("artifact_hashes")
    if not directory or not hashes:
        return False
    root = Path(directory)
    for name, expected in hashes.items():
        path = (root / name).resolve()
        if not path.is_relative_to(root) or not path.is_file() or sha(path) != expected:
            return False
    # Added files may contain edits: do not silently skip or replace them.
    return set(hashes) == {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


def classify(result, report=None, child_exit=0):
    if not isinstance(result, dict) or not result.get("raw_text"):
        return "failed"
    codes = [str(w.get("code", "")) for w in result.get("warnings", []) if isinstance(w, dict)]
    codes += [str(w.get("code", "")) for w in (report or {}).get("pending", {}).get("qwen", []) if isinstance(w, dict)]
    truncation = result.get("truncation") or {}
    bad = ("chunk_failed", "token_limit", "token_count_unavailable", "truncat", "missing_alignment", "missing_word_alignment", "unit_text_unmatched", "invalid_time", "negative_time", "reversed_time", "exceeds_duration")
    chunks = result.get("chunks", [])
    incomplete = not chunks or any(c.get("state") in {"failed", "running"} or not str(c.get("raw_text", "")).strip() or c.get("alignment_state") == "not_needed_empty_text" or (c.get("truncation") or {}).get("limit_reached") for c in chunks)
    if child_exit not in (0, 2) or result.get("state") == "failed" or not result.get("units") or incomplete or truncation.get("limit_reached") or truncation.get("truncated") or any(any(term in code for term in bad) for code in codes):
        return "partial_needs_review"
    # Zero-duration units retain strict and provisional files. No human accuracy claim.
    return "processed_pending_review"


def terminate_group(child):
    if child.poll() is None:
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait(timeout=10)


class Worker:
    def __init__(self, manifest, root, *, resume=False, retry_failed=False, limit=None,
                 poll=5, chunk_max_seconds=180, max_tokens=16384, prompt=PROMPT,
                 runtime_dir=None, python=None, models_dir=None, qwen_model=None,
                 aligner_model=None, ffmpeg="ffmpeg", ffprobe="ffprobe"):
        self.runtime_dir = external_path(runtime_dir or data_root() / "runtime" / "asr")
        self.python = executable(python or self.runtime_dir / "venv" / "bin" / "python")
        external_path(self.python)
        self.models_dir = external_path(models_dir or self.runtime_dir / "models")
        self.qwen_model = external_path(qwen_model or self.models_dir / "Qwen3-ASR-1.7B-8bit")
        self.aligner_model = external_path(aligner_model or self.models_dir / "Qwen3-ForcedAligner-0.6B-8bit")
        self.ffmpeg, self.ffprobe = executable(ffmpeg), executable(ffprobe)
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0:
            raise ValueError("max-tokens must be positive")
        self.max_tokens, self.prompt = max_tokens, prompt
        if isinstance(chunk_max_seconds, bool) or not isinstance(chunk_max_seconds, (int, float)) or not math.isfinite(chunk_max_seconds) or not 0 < chunk_max_seconds <= 180:
            raise ValueError("chunk-max-seconds must be finite and in (0, 180]")
        self.chunk_max_seconds = float(chunk_max_seconds)
        self.root = external_path(local(str(root)))
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest, self.raw_manifest, self.digest = load_manifest(manifest)
        self.resume, self.retry_failed, self.limit, self.poll = resume, retry_failed, limit, poll
        self.state = None
        from asr_engine import configure_runtime
        configure_runtime(self.runtime_dir)
        self.stop = False
        self.active = None

    def save(self, event=None, **details):
        state = self.state
        values = list(state["items"].values())
        state["updated_at"] = now()
        state["counts"] = {name: sum(v["state"] == name for v in values) for name in ("queued", "running", *sorted(TERMINAL))}
        state["processed_seconds"] = sum(v["duration_seconds"] for v in values if v["state"] == "processed_pending_review")
        state["total_media_seconds"] = sum(v["duration_seconds"] for v in values)
        recent = [v for v in values if v.get("processing_seconds") and v["state"] == "processed_pending_review"][-5:]
        rate = sum(v["duration_seconds"] for v in recent) / sum(v["processing_seconds"] for v in recent) if recent else None
        remaining = state["total_media_seconds"] - state["processed_seconds"]
        state["recent_audio_seconds_per_second"] = rate
        state["eta_seconds"] = remaining / rate if rate else None
        state["eta_note"] = "Recent successful processing speed; unresolved partials/failures and manual review are excluded from accuracy claims."
        state["total_wall_seconds"] = state.get("previous_wall_seconds", 0) + time.monotonic() - self.started
        atomic(self.root / "state.json", state)
        if event:
            with (self.root / "events.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"at": now(), "event": event, **details}, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())

    def initialize(self):
        old = read(self.root / "state.json")
        if old:
            if not self.resume:
                raise ValueError("Existing queue requires --resume")
            if old["manifest_sha256"] != self.digest or (self.root / "manifest.frozen.json").read_bytes() != self.raw_manifest:
                raise ValueError("Frozen manifest changed; use a different job-root for a different list")
            expected = {v["video_id"]: v for v in self.manifest["items"]}
            if set(old.get("items", {})) != set(expected) or any(any(old["items"][ident].get(k) != v for k, v in original.items()) for ident, original in expected.items()):
                raise ValueError("Queue source metadata differs from the frozen manifest")
            self.state = old
            self.state["previous_wall_seconds"] = old.get("total_wall_seconds", 0)
            for item in old["items"].values():
                if item["state"] == "running":
                    if item.get("active_pid"):
                        try:
                            os.kill(item["active_pid"], 0)
                        except ProcessLookupError:
                            pass
                        else:
                            raise RuntimeError("Prior child may still be running; do not start another GPU worker")
                    directory = Path(item["attempt_directory"])
                    item.update(state="partial_needs_review", active_pid=None, attempt_finished_at=now(), error_code="previous_worker_interrupted", artifact_hashes=inventory(directory))
        else:
            with (self.root / "manifest.frozen.json").open("xb") as handle:
                handle.write(self.raw_manifest)
                handle.flush()
                os.fsync(handle.fileno())
            self.state = {"schema_version": 1, "manifest_sha256": self.digest, "created_at": now(), "items": {v["video_id"]: {**v, "state": "queued", "attempts": 0, "attempt_history": []} for v in self.manifest["items"]}}
        self.started = time.monotonic()
        self.state.update(state="running", worker_pid=os.getpid(), current_video_id=None)
        self.save("worker_started")

    def child(self, argv, directory, timeout, item, status=None):
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1", "TOKENIZERS_PARALLELISM": "false"}
        began, last_status = time.monotonic(), None
        with (directory / f"{item['stage']}.log").open("xb") as log:
            child = subprocess.Popen([str(v) for v in argv], stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env, start_new_session=True)
            self.active = child
            item["active_pid"] = child.pid
            try:
                self.save("child_started", video_id=item["video_id"], stage=item["stage"], pid=child.pid)
                while child.poll() is None:
                    if self.stop or (self.root / "STOP_REQUESTED").exists():
                        self.stop = True
                        raise InterruptedError("Stop requested; active process terminated and partial files retained")
                    if time.monotonic() - began > timeout:
                        raise TimeoutError("Child processing timeout; partial files retained")
                    if shutil.disk_usage(self.root).free < MIN_DISK:
                        self.state["state"] = "paused_low_disk"
                        raise OSError("Minimum free disk threshold reached")
                    progress = read(status) if status else None
                    item["child_status"] = progress
                    self.save("child_progress" if progress != last_status else None, video_id=item["video_id"], child_status=progress)
                    last_status = progress
                    time.sleep(self.poll)
                if status:
                    item["child_status"] = read(status)
                return child.returncode
            finally:
                terminate_group(child)
                item["active_pid"] = None
                self.active = None

    def extract(self, item, directory):
        def supervised(stage, argv, timeout):
            item["stage"] = stage
            if self.child(argv, directory, timeout, item):
                raise RuntimeError(f"Audio extraction failed at {stage}")
        wav, original, _ = extract_audio(
            item["video_path"], directory, ffmpeg=self.ffmpeg, ffprobe=self.ffprobe,
            expected_sha256=item["video_sha256"], expected_bytes=item["bytes"],
            expected_duration=item["duration_seconds"], command_runner=supervised)
        return wav, original

    def process(self, item):
        if shutil.disk_usage(self.root).free < MIN_DISK:
            self.state["state"] = "paused_low_disk"
            self.save("disk_space_insufficient")
            return False
        if item.get("attempt_finished_at") and not verify_artifacts(item):
            item.update(state="partial_needs_review", error_code="artifact_hash_conflict")
            self.save("artifact_conflict", video_id=item["video_id"])
            return True
        if item.get("attempt_finished_at"):
            item["attempt_history"].append({k: item.get(k) for k in ("attempt_directory", "review_directory", "artifact_hashes", "attempt_finished_at", "state", "error_code", "parameters")})
        item["attempts"] += 1
        directory = self.root / item["video_id"] / f"attempt_{item['attempts']:03d}"
        directory.mkdir(parents=True, exist_ok=False)
        began = time.monotonic()
        parameters = {"backend": "qwen", "chunk_max_seconds": self.chunk_max_seconds,
                      "max_tokens_per_chunk": self.max_tokens, "prompt": self.prompt,
                      "qwen_model": str(self.qwen_model), "aligner_model": str(self.aligner_model),
                      "python": self.python, "runtime_dir": str(self.runtime_dir),
                      "ffmpeg": self.ffmpeg, "ffprobe": self.ffprobe,
                      "batch_runner_sha256": sha(Path(__file__)),
                      "asr_engine_sha256": sha(TOOLS / "asr_engine.py"),
                      "extract_audio_sha256": sha(TOOLS / "extract_audio.py")}
        atomic(directory / "PROVENANCE.json", {"video_id": item["video_id"], "source_sha256": item["video_sha256"], "parameters": parameters, "created_at": now()})
        item.update(state="running", attempt_directory=str(directory), review_directory=None, attempt_finished_at=None, artifact_hashes={}, active_pid=None, stage="starting", error_code=None, parameters=parameters)
        self.state["current_video_id"] = item["video_id"]
        self.save("item_started", video_id=item["video_id"], attempt=item["attempts"])
        try:
            wav, original = self.extract(item, directory)
            run = directory / "raw" / "qwen_run"
            item["stage"] = "qwen_asr_align"
            rc = self.child([self.python, TOOLS / "asr_engine.py", "--audio", wav,
                "--source-video", item["video_path"], "--output-root", run.parent,
                "--run-name", run.name, "--qwen-model", self.qwen_model,
                "--aligner-model", self.aligner_model, "--runtime-dir", self.runtime_dir,
                "--chunk-max-seconds", str(self.chunk_max_seconds), "--max-tokens", str(self.max_tokens),
                "--prompt", self.prompt, "--ffmpeg", self.ffmpeg], directory,
                max(900, item["duration_seconds"] * 2 + 600), item, run / "STATUS.json")
            result = read(run / "qwen/result.json")
            if result and result.get("raw_text"):
                review = directory / "review"
                item["stage"] = "subtitle_export"
                export_rc = self.child([self.python, TOOLS / "export_subtitles.py",
                    "--run-dir", run, "--output-dir", review, "--video-id", item["video_id"]],
                    directory, max(180, item["duration_seconds"] * .1), item)
                item["review_directory"] = str(review) if (review / "AUTO_EXPORT_PROVENANCE.json").is_file() else None
                item["state"] = classify(result, child_exit=rc) if export_rc == 0 else "partial_needs_review"
            else:
                item["state"] = classify(result, child_exit=rc)
        except (Exception, KeyboardInterrupt) as error:
            item["state"] = "partial_needs_review" if any(directory.rglob("raw_text.txt")) else "failed"
            item["error_code"] = type(error).__name__
            # Safe descriptions only: never serialize environment, arguments, or credentials.
            atomic(directory / "ERROR.json", {"type": type(error).__name__, "message": "Local processing did not finish; retained files require review", "at": now()})
            if isinstance(error, KeyboardInterrupt):
                self.stop = True
        finally:
            if self.active:
                terminate_group(self.active)
                self.active = None
            item.update(active_pid=None, attempt_finished_at=now(), processing_seconds=time.monotonic() - began, stage="finished")
            atomic(directory / "ATTEMPT.json", {"video_id": item["video_id"], "state": item["state"], "finished_at": item["attempt_finished_at"], "error_code": item["error_code"], "human_verified": False, "parameters": parameters})
            item["artifact_hashes"] = inventory(directory)
            self.save("item_finished", video_id=item["video_id"], state=item["state"])
        return not self.stop

    def run(self):
        with batch_lock(self.root):
            self.initialize()
            count = 0
            try:
                for item in self.state["items"].values():
                    if self.stop or (self.root / "STOP_REQUESTED").exists():
                        self.stop = True
                        break
                    if item["state"] in TERMINAL and not verify_artifacts(item):
                        item.update(state="partial_needs_review", error_code="artifact_hash_conflict")
                        self.save("artifact_conflict", video_id=item["video_id"])
                        continue
                    if item["state"] == "processed_pending_review" or item["state"] in TERMINAL and (not self.retry_failed or item["attempts"] >= 2):
                        continue
                    if self.limit is not None and count >= self.limit:
                        break
                    count += 1
                    if not self.process(item):
                        break
            finally:
                if self.active:
                    terminate_group(self.active)
                self.state.update(worker_pid=None, current_video_id=None)
                if self.state["state"] != "paused_low_disk":
                    self.state["state"] = "paused" if self.stop else "queued_remaining" if any(v["state"] == "queued" for v in self.state["items"].values()) else "finished_needs_review"
                self.save("worker_finished", state=self.state["state"])
            return self.state


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--job-root", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--chunk-max-seconds", type=float, default=180, help="New-attempt chunk maximum in (0, 180]; existing attempt files stay unchanged")
    parser.add_argument("--max-tokens", type=int, default=16384)
    parser.add_argument("--prompt", default=PROMPT, help="Exact native prompt; use an empty string for no hint")
    parser.add_argument("--runtime-dir", type=Path, help="Default YUNCONG_DATA_ROOT/runtime/asr")
    parser.add_argument("--python", help="Inference Python; default runtime-dir/venv/bin/python")
    parser.add_argument("--models-dir", type=Path, help="Default runtime-dir/models")
    parser.add_argument("--qwen-model", type=Path)
    parser.add_argument("--aligner-model", type=Path)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args(argv)
    if args.status:
        state = read(external_path(local(str(args.job_root))) / "state.json")
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0 if state else 1
    if not args.manifest or args.limit is not None and args.limit < 1:
        parser.error("--manifest is required and --limit must be positive")
    try:
        worker = Worker(args.manifest, args.job_root, resume=args.resume, retry_failed=args.retry_failed,
                        limit=args.limit, chunk_max_seconds=args.chunk_max_seconds,
                        max_tokens=args.max_tokens, prompt=args.prompt, runtime_dir=args.runtime_dir,
                        python=args.python, models_dir=args.models_dir, qwen_model=args.qwen_model,
                        aligner_model=args.aligner_model, ffmpeg=args.ffmpeg, ffprobe=args.ffprobe)
        previous = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
        for s in previous:
            signal.signal(s, lambda *_: setattr(worker, "stop", True))
        try:
            state = worker.run()
        finally:
            for s, handler in previous.items():
                signal.signal(s, handler)
        print(json.dumps({k: state[k] for k in ("state", "counts", "processed_seconds", "eta_seconds")}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        print(f"Batch setup failed: {type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
