"""One bounded ASR timing pilot, using the installed platform and local hints."""
from __future__ import annotations

import csv
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(r"D:\YuncongVideoAnalysis\ledger_2026_v0.1")
TRIAL = ROOT / "text_pilot_20261004_0804"
OUT = TRIAL / "asr"
VOCAB = ROOT / "planning/stock_vocabulary_v0.1_20261004"
STREAM_ID = "-9HY0rxruC0"

def now():
    return datetime.now(timezone.utc).isoformat()

def save(path, obj):
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)

def child():
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["PYTHONIOENCODING"] = "utf-8"
    import faster_whisper
    from huggingface_hub import snapshot_download
    import media_platform.ingest as ingest

    spec = json.loads((TRIAL / "task.json").read_text(encoding="utf-8"))
    hints = (VOCAB / "asr_hotwords_short.txt").read_text(encoding="utf-8").strip()
    if hashlib.sha256((VOCAB / "asr_hotwords_short.txt").read_bytes()).hexdigest() != spec["hotwords_sha256"]:
        raise RuntimeError("Hint file changed after task was frozen")
    runtime = json.loads((ROOT / "runtime_manifest.json").read_text(encoding="utf-8"))
    cache = snapshot_download("Systran/faster-whisper-large-v3", revision=runtime["snapshot"], local_files_only=True,
        allow_patterns=["config.json", "model.bin", "tokenizer.json", "preprocessor_config.json", "vocabulary.*"])
    identity = {"id": "Systran/faster-whisper-large-v3", "revision": runtime["snapshot"],
                "cache": cache, "hotwords_sha256": spec["hotwords_sha256"]}
    ingest.model_identity = lambda name: identity if name == "large-v3" else (_ for _ in ()).throw(ValueError(name))
    original_model = faster_whisper.WhisperModel
    state_path = TRIAL / "progress.json"
    progress = {"status": "model_loading", "started_utc": now(), "pid": os.getpid(),
                "model": identity, "processed_media_seconds": 0, "completed_calls": 0}
    save(state_path, progress)

    class HintedWhisperModel(original_model):
        def __init__(self, *args, **kwargs):
            t = time.monotonic()
            super().__init__(*args, **kwargs)
            progress.update(status="transcribing", model_load_seconds=round(time.monotonic() - t, 3),
                            updated_utc=now())
            save(state_path, progress)

        def transcribe(self, *args, **kwargs):
            if (TRIAL / "STOP").exists():
                raise RuntimeError("User stop control was requested")
            kwargs["hotwords"] = hints
            started = time.monotonic()
            segments, info = super().transcribe(*args, **kwargs)
            def timed_segments():
                count = 0
                for segment in segments:
                    count += 1
                    yield segment
                progress["completed_calls"] += 1
                progress.update(status="transcribing", updated_utc=now(),
                    processed_media_seconds=min(progress["completed_calls"] * 300, spec["duration_seconds"]),
                    last_call_seconds=round(time.monotonic() - started, 3), last_call_segments=count)
                save(state_path, progress)
                print(json.dumps(progress, ensure_ascii=False), flush=True)
            return timed_segments(), info

    faster_whisper.WhisperModel = HintedWhisperModel
    sys.argv = ["local_single_video_pilot", spec["source_path"], "--output", str(OUT),
                "--model", "large-v3", "--device", "cuda", "--compute-type", "float16",
                "--language", "zh", "--chunk-seconds", "300", "--vad", "conservative", "--no-video", "--no-ocr"]
    ingest.main()
    progress.update(status="asr_complete", updated_utc=now())
    save(state_path, progress)

def parent():
    previous = None
    if TRIAL.exists():
        previous = json.loads((TRIAL / "status.json").read_text(encoding="utf-8"))
        if previous.get("status") != "failed" or previous.get("attempt", 1) != 1 or OUT.exists():
            raise RuntimeError("Trial output already exists; no overwrite or second job")
        save(TRIAL / "preflight_attempt1.json", previous)
        (TRIAL / "asr.log").rename(TRIAL / "asr_attempt1.log")
    else:
        TRIAL.mkdir()
    with (ROOT / "streams_inventory.csv").open(encoding="utf-8-sig", newline="") as f:
        row = next(row for row in csv.DictReader(f) if row["stream_id"] == STREAM_ID)
    source = Path(row["source_path"]).resolve(strict=True)
    allowed = Path(r"D:\YuncongVideoAnalysis\2026-01-01_to_2026-10-03").resolve(strict=True)
    if source.parent != allowed:
        raise ValueError("Video outside authorized source directory")
    hint_file = VOCAB / "asr_hotwords_short.txt"
    spec = {"task_id": "yuncong_text_pilot_20261004_0804", "scope": "one_video_text_only",
            "stream_id": STREAM_ID, "title": row["title"], "source_path": str(source),
            "source_sha256": row["source_sha256"], "duration_seconds": float(row["local_duration_seconds"]),
            "actual_start_beijing": row["actual_start_beijing"], "model": "large-v3",
            "device": "cuda", "compute_type": "float16", "language": "zh", "vad": "conservative",
            "chunk_seconds": 300, "hint_file": str(hint_file),
            "hotwords_sha256": hashlib.sha256(hint_file.read_bytes()).hexdigest(),
            "asr_timeout_seconds": 3600, "created_utc": now(), "visual_processing": False,
            "other_videos_authorized": False, "adapter_path": str(Path(__file__).resolve())}
    if previous:
        frozen = json.loads((TRIAL / "task.json").read_text(encoding="utf-8"))
        if any(spec[key] != frozen[key] for key in ("stream_id", "source_sha256", "hotwords_sha256", "model", "device", "compute_type")):
            raise RuntimeError("Retry changed frozen inputs")
        spec = frozen
    else:
        save(TRIAL / "task.json", spec)
    started = time.monotonic()
    state = {"status": "asr_running", "started_utc": now(), "runner_pid": os.getpid(),
             "child_pid": None, "task_id": spec["task_id"], "attempt": 2 if previous else 1}
    save(TRIAL / "status.json", state)
    environment = {**os.environ, "HF_HUB_OFFLINE": "1", "PYTHONIOENCODING": "utf-8"}
    with (TRIAL / "asr.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--child"],
                                   stdout=log, stderr=log, env=environment,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        state["child_pid"] = process.pid
        save(TRIAL / "status.json", state)
        while process.poll() is None:
            if (TRIAL / "STOP").exists() or time.monotonic() - started > spec["asr_timeout_seconds"]:
                import psutil
                try:
                    scoped_process = psutil.Process(process.pid)
                    descendants = scoped_process.children(recursive=True)
                    for item in descendants:
                        item.terminate()
                    scoped_process.terminate()
                    _, alive = psutil.wait_procs(descendants + [scoped_process], timeout=15)
                    for item in alive:
                        item.kill()
                except psutil.NoSuchProcess:
                    pass
                process.wait(timeout=20)
                state.update(status="stopped_or_timeout", ended_utc=now(),
                             asr_wall_seconds=round(time.monotonic() - started, 3))
                save(TRIAL / "status.json", state)
                return 1
            time.sleep(2)
    elapsed = time.monotonic() - started
    if process.returncode:
        state.update(status="failed", return_code=process.returncode, ended_utc=now(), asr_wall_seconds=round(elapsed, 3))
        save(TRIAL / "status.json", state)
        print(json.dumps(state, ensure_ascii=False))
        return process.returncode
    verify_start = time.monotonic()
    verified = subprocess.run([sys.executable, "-m", "media_platform", "verify", str(OUT)],
                              capture_output=True, text=True, encoding="utf-8", timeout=300, env=environment)
    (TRIAL / "verification.json").write_text(verified.stdout + verified.stderr, encoding="utf-8")
    if verified.returncode:
        state.update(status="verification_failed", ended_utc=now())
        save(TRIAL / "status.json", state)
        return verified.returncode
    manifest = json.loads((OUT / "manifest.json").read_text(encoding="utf-8"))
    speech = json.loads((OUT / "speech.json").read_text(encoding="utf-8"))
    if not speech or manifest["source_sha256"] != spec["source_sha256"]:
        raise RuntimeError("Empty transcript or changed source video")
    if any(chunk["device"] != "cuda" or chunk["compute_type"] != "float16" for chunk in manifest["chunks"]):
        raise RuntimeError("Model device/precision changed")
    report = {"stream_id": STREAM_ID, "title": row["title"], "duration_seconds": spec["duration_seconds"],
        "asr_wall_seconds": round(elapsed, 3), "media_speed_multiple": round(spec["duration_seconds"] / elapsed, 3),
        "inference_seconds": round(sum(chunk["elapsed_seconds"] for chunk in manifest["chunks"]), 3),
        "validation_seconds": round(time.monotonic() - verify_start, 3), "segments": len(speech),
        "chunks": len(manifest["chunks"]), "hotwords_enabled": True, "video_processed": False,
        "source_hash_matches": True, "asr_large_model_token_usage": 0,
        "preflight_retry_seconds": previous.get("asr_wall_seconds", 0) if previous else 0,
        "semantic_segmentation": "pending", "asr_finished_utc": now()}
    save(TRIAL / "timing_report.json", report)
    state.update(status="asr_complete_ready_for_text_segmentation", ended_utc=now(),
                 asr_wall_seconds=report["asr_wall_seconds"], child_pid=None)
    save(TRIAL / "status.json", state)
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0

if __name__ == "__main__":
    if "--child" in sys.argv:
        child()
    else:
        raise SystemExit(parent())
