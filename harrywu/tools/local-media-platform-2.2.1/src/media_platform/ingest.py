"""Turn a local audio or video asset into time-linked, reviewable evidence.

Requires ffmpeg/ffprobe, faster-whisper, Pillow, and (for OCR)
rapidocr-onnxruntime. The original media is never modified.
"""

from __future__ import annotations

import argparse
import atexit
import difflib
import gc
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from media_platform.paths import model_cache, profile_path as shared_profile_path

VERSION = "2.0"
MEDIUM_REVISION = "08e178d48790749d25932bbc082711ddcfdfbc4f"
MEDIUM_MODEL_SHA256 = "9b45e1009dcc4ab601eff815b61d80e60ce3fd8c74c1a14f4a282258286b51ae"
EXTENSIONS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma",
              ".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
_DLL_HANDLES = []


def acquire_single_run_lock() -> None:
    """Keep two expensive ingestion processes from running at the same time."""
    lock_path = Path(tempfile.gettempdir()) / "media_platform_ingest.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        if sys.platform == "win32":
            import msvcrt
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(descriptor)
        raise RuntimeError("Another media ingestion is running; wait or stop it before starting this one") from exc
    atexit.register(os.close, descriptor)


def enable_cuda_libraries() -> None:
    """Expose optional NVIDIA runtime wheels to CTranslate2 on Windows."""
    if sys.platform != "win32":
        return
    import site
    for root in site.getsitepackages():
        for package in ("cublas", "cudnn"):
            directory = Path(root) / "nvidia" / package / "bin"
            if directory.is_dir():
                _DLL_HANDLES.append(os.add_dll_directory(str(directory)))
                os.environ["PATH"] = str(directory) + os.pathsep + os.environ["PATH"]


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def atomic_write(path: Path, content: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def resource_snapshot() -> dict:
    import psutil
    snapshot = {"ram_free_gib": round(psutil.virtual_memory().available / 2**30, 2),
                "cpu_percent": psutil.cpu_percent(interval=0.1)}
    if shutil.which("nvidia-smi"):
        try:
            fields = run(["nvidia-smi", "--query-gpu=memory.free,memory.total,memory.used",
                          "--format=csv,noheader,nounits"]).stdout.splitlines()[0].split(",")
            snapshot.update({"vram_free_gib": round(int(fields[0]) / 1024, 2),
                             "vram_total_gib": round(int(fields[1]) / 1024, 2),
                             "vram_used_gib": round(int(fields[2]) / 1024, 2)})
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            pass
    return snapshot


def choose_device(args, profile=None):
    snapshot = resource_snapshot()
    if args.device != "auto":
        return args.device, args.compute_type if args.compute_type != "auto" else ("float16" if args.device == "cuda" else "int8"), snapshot
    if args.compute_type in {"float16", "int8_float16"}:
        if "vram_free_gib" not in snapshot:
            raise RuntimeError("GPU precision requested but NVIDIA GPU is unavailable")
        return "cuda", args.compute_type, snapshot
    if args.compute_type == "float32":
        return "cpu", "float32", snapshot
    candidates = []
    blocked = set((profile or {}).get("excluded_candidates", []))
    if snapshot["ram_free_gib"] >= args.ram_reserve_gib + 3:
        if "cpu/int8" not in blocked:
            candidates.append(("cpu", "int8"))
    for precision, requirement in (("float16", 5.5), ("int8_float16", 4.0)):
        if args.compute_type not in {"auto", precision}:
            continue
        if f"cuda/{precision}" in blocked:
            continue
        requirement = (profile or {}).get("peak_vram_delta_gib", {}).get(f"cuda/{precision}", requirement-0.5) + 0.5
        if snapshot.get("vram_free_gib", 0) >= requirement + args.vram_reserve_gib:
            candidates.append(("cuda", precision))
    if not candidates:
        raise RuntimeError(f"Insufficient free resources for reserve: {snapshot}; try --device cpu")
    scores = (profile or {}).get("seconds_per_audio_second", {})
    candidates.sort(key=lambda pair: scores.get("/".join(pair),
                    {("cuda", "float16"): 10, ("cuda", "int8_float16"): 20, ("cpu", "int8"): 30}.get(pair, 100)))
    return *candidates[0], snapshot


def model_identity(name: str) -> dict:
    if name != "medium":
        return {"id": name, "revision": "not pinned"}
    local = model_cache() / "faster-whisper-medium" / MEDIUM_REVISION
    legacy_cache = Path.home() / ".cache" / "annx" / "models" / "faster-whisper-medium" / MEDIUM_REVISION
    if not local.exists() and legacy_cache.is_dir():
        local = legacy_cache
    if not all((local / filename).is_file() for filename in ("config.json", "tokenizer.json", "model.bin")):
        from huggingface_hub import snapshot_download
        local = Path(snapshot_download("Systran/faster-whisper-medium", revision=MEDIUM_REVISION,
                                      allow_patterns=["*.json", "*.txt", "model.bin", "vocabulary.*"],
                                      cache_dir=str(model_cache() / "huggingface")))
    model_bin = local / "model.bin"
    actual_sha256 = digest(model_bin)
    if actual_sha256 != MEDIUM_MODEL_SHA256:
        raise RuntimeError(f"Pinned medium model checksum mismatch: {actual_sha256}")
    return {"id": "Systran/faster-whisper-medium", "revision": MEDIUM_REVISION,
            "cache": str(local), "model_bin_bytes": model_bin.stat().st_size,
            "model_bin_sha256": actual_sha256}


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, text=True, capture_output=True,
                          encoding="utf-8", errors="replace", timeout=1800)


def parse_time(value: str) -> float:
    parts = value.split(":")
    if len(parts) > 3:
        raise argparse.ArgumentTypeError("Use seconds, MM:SS, or HH:MM:SS")
    try:
        seconds = sum(float(part) * 60 ** index for index, part in enumerate(reversed(parts)))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Invalid time") from exc
    if not math.isfinite(seconds) or seconds < 0:
        raise argparse.ArgumentTypeError("Time must be nonnegative")
    return seconds


def stamp(seconds: float) -> str:
    whole = int(seconds)
    return f"{whole // 3600:02d}:{whole // 60 % 60:02d}:{whole % 60:02d}"


def rectify_screen(frame: Path, target: Path) -> dict | None:
    """Conservatively rectify a large four-corner screen in a derived frame."""
    import cv2
    import numpy as np
    image = cv2.imdecode(np.fromfile(frame, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return None
    height, width = image.shape[:2]
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(cv2.GaussianBlur(gray, (5, 5), 0), 40, 130)
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    candidates = []
    for contour in contours:
        polygon = cv2.approxPolyDP(contour, 0.025 * cv2.arcLength(contour, True), True)
        if len(polygon) == 4 and cv2.isContourConvex(polygon):
            area = cv2.contourArea(polygon)
            if 0.5 * width * height <= area <= 0.98 * width * height:
                candidates.append((area, polygon.reshape(4, 2).astype("float32")))
    if not candidates:
        return None
    corners = max(candidates, key=lambda pair: pair[0])[1]
    sums, diffs = corners.sum(axis=1), np.diff(corners, axis=1).ravel()
    source = np.array([corners[np.argmin(sums)], corners[np.argmin(diffs)],
                       corners[np.argmax(sums)], corners[np.argmax(diffs)]], dtype="float32")
    out_width = int(max(np.linalg.norm(source[1]-source[0]), np.linalg.norm(source[2]-source[3])))
    out_height = int(max(np.linalg.norm(source[3]-source[0]), np.linalg.norm(source[2]-source[1])))
    if out_width < 100 or out_height < 100:
        return None
    destination = np.array([[0, 0], [out_width-1, 0], [out_width-1, out_height-1], [0, out_height-1]], dtype="float32")
    matrix = cv2.getPerspectiveTransform(source, destination)
    target.parent.mkdir(exist_ok=True)
    ok, encoded = cv2.imencode(".jpg", cv2.warpPerspective(image, matrix, (out_width, out_height)))
    if not ok:
        return None
    encoded.tofile(target)
    return {"corners": source.tolist(), "matrix": matrix.tolist(), "output_size": [out_width, out_height]}


def probe(path: Path) -> dict:
    return json.loads(run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)]).stdout)


def transcribe(path: Path, start: float, end: float, output: Path, args, manifest: dict, profile=None) -> dict:
    enable_cuda_libraries()
    from faster_whisper import WhisperModel
    identity = model_identity(args.model)
    manifest["model"] = identity
    atomic_write(output / "manifest.json", manifest)
    chunk_dir = output / "chunks"
    chunk_dir.mkdir(exist_ok=True)
    chunks = []
    model = None
    choice = None
    for index in range(math.ceil((end - start) / args.chunk_seconds)):
        begin = start + index * args.chunk_seconds
        finish = min(end, begin + args.chunk_seconds)
        saved = chunk_dir / f"{index:05d}.json"
        if args.resume and saved.exists():
            chunk = json.loads(saved.read_text(encoding="utf-8"))
            if chunk.get("begin") == begin and chunk.get("finish") == finish:
                chunks.append(chunk)
                continue
            raise RuntimeError(f"Resume chunk boundaries changed: {saved}")
        left, right = max(start, begin - 2), min(end, finish + 2)
        # A loaded GPU model consumes its own VRAM. Keep it while the *remaining*
        # free VRAM meets the browser reserve; otherwise auto mode would alternate
        # GPU/CPU at every chunk merely because it measured its own allocation.
        snapshot = resource_snapshot()
        if (args.device == "auto" and choice is not None and choice[0] == "cuda" and model is not None
                and snapshot.get("vram_free_gib", 0) >= args.vram_reserve_gib
                and snapshot["ram_free_gib"] >= args.ram_reserve_gib):
            device, precision = choice
        else:
            device, precision, snapshot = choose_device(args, profile)
        def recognize(active_model):
            with tempfile.TemporaryDirectory(prefix="annx_audio_") as temporary:
                wav = Path(temporary) / "audio.wav"
                run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-ss", str(left), "-i", str(path),
                     "-map", f"0:{args.selected_audio_stream}", "-t", str(right-left),
                     "-vn", "-ac", "1", "-ar", "16000", str(wav)])
                began = time.monotonic()
                segments, info = active_model.transcribe(str(wav), language=args.language, beam_size=5,
                    vad_filter=args.vad == "conservative",
                    vad_parameters={"min_silence_duration_ms": 1000, "speech_pad_ms": 500} if args.vad == "conservative" else None,
                    condition_on_previous_text=False)
                rows = []
                for segment in segments:
                    a, b = left + segment.start, left + segment.end
                    if begin <= (a+b)/2 < finish and segment.text.strip():
                        rows.append({"start": round(a, 3), "end": round(b, 3), "text": segment.text.strip(),
                                     "avg_logprob": round(segment.avg_logprob, 4),
                                     "no_speech_prob": round(segment.no_speech_prob, 4),
                                     "review": segment.avg_logprob < -0.8 or segment.no_speech_prob > 0.5})
                return {"begin": begin, "finish": finish, "segments": rows, "language": info.language,
                        "elapsed_seconds": round(time.monotonic()-began, 2)}
        try:
            if choice != (device, precision):
                model = None
                gc.collect()
                model = WhisperModel(identity.get("cache", args.model), device=device, compute_type=precision,
                                     cpu_threads=args.cpu_threads, num_workers=1)
                choice = (device, precision)
            chunk = recognize(model)
        except (RuntimeError, MemoryError) as exc:
            reason = str(exc).lower()
            if args.device != "auto" or device != "cuda" or not any(x in reason for x in
                    ("memory", "cublas", "cudnn", "cuda")):
                raise
            model = None
            gc.collect()
            manifest.setdefault("fallbacks", []).append({"chunk": index, "reason": str(exc)[:200]})
            for alternate in (("cuda", "int8_float16"), ("cpu", "int8")):
                if alternate == (device, precision):
                    continue
                if alternate[0] == "cuda":
                    needed = (profile or {}).get("peak_vram_delta_gib", {}).get("cuda/int8_float16", 3.5) + 0.5
                    if resource_snapshot().get("vram_free_gib", 0) < needed + args.vram_reserve_gib:
                        continue
                try:
                    model = WhisperModel(identity.get("cache", args.model), device=alternate[0],
                                         compute_type=alternate[1], cpu_threads=args.cpu_threads)
                    chunk = recognize(model)
                    choice = alternate
                    break
                except (RuntimeError, MemoryError):
                    model = None
                    gc.collect()
            else:
                raise RuntimeError("All same-model OOM fallbacks failed") from exc
        atomic_write(saved, chunk)
        chunks.append(chunk)
        manifest.setdefault("chunks", []).append({"index": index, "sha256": digest(saved),
                  "device": choice[0], "compute_type": choice[1], "resources_before": snapshot,
                  "elapsed_seconds": chunk["elapsed_seconds"]})
        atomic_write(output / "manifest.json", manifest)
    rows = sorted((row for chunk in chunks for row in chunk["segments"]), key=lambda row: row["start"])
    rows, dedupe_events = deduplicate_rows(rows)
    for number, row in enumerate(rows, 1):
        row["id"] = f"s{number:05d}"
    atomic_write(output / "speech.json", rows)
    atomic_write(output / "speech.md", "# 自动语音转写（待原声核对）\n\n" +
      "\n".join(f"- {row['id']} [{stamp(row['start'])}–{stamp(row['end'])}] {row['text']}" +
                (" ⚠ 待回听" if row["review"] else "") for row in rows) + "\n")
    gaps = [{"start": rows[i-1]["end"], "end": rows[i]["start"]} for i in range(1, len(rows))
            if rows[i]["start"]-rows[i-1]["end"] >= 5]
    return {"model": identity["id"], "segments": len(rows), "boundary_deduplication": dedupe_events,
            "possible_silence_intervals": gaps,
            "review_count": sum(row["review"] for row in rows),
            "elapsed_seconds": round(sum(chunk["elapsed_seconds"] for chunk in chunks), 2)}


def deduplicate_rows(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Resolve only strong cross-window transcript overlap; retain original chunk files."""
    resolved, events = [], []
    for original in rows:
        row = dict(original)
        if resolved:
            prior = resolved[-1]
            overlap = min(prior["end"], row["end"]) - max(prior["start"], row["start"])
            if overlap >= 1:
                a = "".join(char for char in prior["text"] if char.isalnum())
                b = "".join(char for char in row["text"] if char.isalnum())
                ratio = difflib.SequenceMatcher(None, a, b).ratio()
                shorter = min(prior["end"]-prior["start"], row["end"]-row["start"])
                if shorter > 0 and overlap / shorter >= 0.7 and ratio >= 0.55:
                    if (len(row["text"]), row.get("avg_logprob", -1)) > (len(prior["text"]), prior.get("avg_logprob", -1)):
                        resolved[-1] = row
                        kept = "later"
                    else:
                        kept = "earlier"
                    events.append({"time": round(max(prior["start"], row["start"]), 3),
                                   "action": "overlapping_alternative", "kept": kept,
                                   "review": True})
                    resolved[-1]["review"] = True
                    continue
                for length in range(min(len(a), len(b)), 7, -1):
                    if a.endswith(b[:length]):
                        positions = [i for i, char in enumerate(row["text"]) if char.isalnum()]
                        cut = positions[length-1] + 1
                        remaining = row["text"][cut:].lstrip(" 、。,.!?！？")
                        if remaining:
                            row["text"] = remaining
                            row["start"] = round(max(row["start"], prior["end"]), 3)
                            row["review"] = True
                            events.append({"time": row["start"], "action": "trim_repeated_prefix",
                                           "characters": length, "review": True})
                        break
        resolved.append(row)
    return resolved, events


def scan_video(path: Path, start: float, end: float, output: Path, args) -> dict:
    from PIL import Image, ImageChops, ImageStat
    interval = args.frame_interval or (0.5 if end-start <= 40 else 2.0 if end-start <= 180 else 10.0)
    interval = max(interval, (end-start) / args.max_frames)
    frames_dir = output / "frames"
    frames_dir.mkdir(exist_ok=True)
    # Single bounded decode. ffmpeg honors rotation metadata and showinfo exposes filtered PTS.
    filter_spec = f"fps=fps=1/{interval}:round=near,showinfo,scale=w='min(1280,iw)':h=-2"
    began = time.monotonic()
    result = run(["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "info", "-ss", str(start),
                  "-i", str(path), "-t", str(end-start), "-an", "-vf", filter_spec,
                  "-frames:v", str(args.max_frames), "-q:v", "4", str(frames_dir / "%06d.jpg")])
    files = sorted(frames_dir.glob("*.jpg"))
    times = [float(x) for x in re.findall(r"showinfo[^\n]*?pts_time:([\d.]+)", result.stderr)]
    if len(times) != len(files):
        times = [i*interval for i in range(len(files))]
    ocr = None
    if not args.no_ocr:
        from rapidocr_onnxruntime import RapidOCR
        ocr = RapidOCR()
    rows, previous, previous_text = [], None, set()
    for index, target in enumerate(files):
        with Image.open(target) as image:
            thumbnail = image.convert("L").resize((320, 180))
        difference = ImageChops.difference(previous, thumbnail) if previous else None
        change = ImageStat.Stat(difference).mean[0] / 255 if difference else 1.0
        sharp_change = sum(difference.histogram()[30:]) / (thumbnail.width * thumbnail.height) if difference else 1.0
        previous = thumbnail
        detected = []
        short_video = end-start <= 180
        meaningful_change = sharp_change >= 0.002 or change >= max(0.015, args.change_threshold * 2)
        scan_text = index == 0 or meaningful_change or short_video or index % max(1, int(60 / interval)) == 0
        if ocr and scan_text:
            recognized, _ = ocr(str(target))
            detected = [{"text": item[1], "confidence": round(float(item[2]), 3)} for item in (recognized or [])]
        corrected_info, corrected_ocr = None, []
        if args.perspective and scan_text:
            corrected_target = output / "frames_corrected" / target.name
            corrected_info = rectify_screen(target, corrected_target)
            if corrected_info:
                corrected_info["frame"] = corrected_target.relative_to(output).as_posix()
                corrected_info["frame_sha256"] = digest(corrected_target)
                if ocr:
                    corrected_result, _ = ocr(str(corrected_target))
                    corrected_ocr = [{"text": item[1], "confidence": round(float(item[2]), 3)}
                                     for item in (corrected_result or [])]
        current_text = ({item["text"] for item in detected if item["confidence"] >= 0.5}
                        if scan_text else previous_text)
        novel_text = any(len(value) >= 5 and all(difflib.SequenceMatcher(None, value, old).ratio() < 0.6
                        for old in previous_text) for value in current_text)
        keep = index == 0 or meaningful_change or (short_video and current_text != previous_text) or (
            not short_video and scan_text and novel_text)
        previous_text = current_text
        if keep:
            rows.append({"time": round(start+times[index], 3), "frame": target.relative_to(output).as_posix(),
                         "frame_sha256": digest(target), "change": round(change, 4),
                         "sharp_change": round(sharp_change, 4),
                         "ocr_sampled": bool(ocr and scan_text), "ocr": detected,
                         "perspective": corrected_info, "ocr_corrected": corrected_ocr})
        else:
            target.unlink()
            if corrected_info:
                corrected_target.unlink(missing_ok=True)
    atomic_write(output / "visual.json", rows)
    lines, prior = ["# 视频抽样与 OCR（仅代表抽样时间点，待核对）", ""], set()
    for row in rows:
        lines.append(f"- [{stamp(row['time'])}] [画面]({row['frame']})")
        for item in row["ocr"]:
            if item["confidence"] >= 0.5 and item["text"] not in prior:
                lines.append(f"  - {item['text']}（{item['confidence']:.2f}）")
        for item in row["ocr_corrected"]:
            if item["confidence"] >= 0.5 and item["text"] not in prior:
                lines.append(f"  - [透视派生帧 OCR] {item['text']}（{item['confidence']:.2f}）")
        prior = {item["text"] for item in row["ocr"] if item["confidence"] >= 0.5}
    atomic_write(output / "screen_text.md", "\n".join(lines) + "\n")
    return {"sampled": len(files), "retained": len(rows), "interval_seconds": interval,
            "ocr": not args.no_ocr, "elapsed_seconds": round(time.monotonic()-began, 2)}


def validate_resume(output: Path, manifest: dict, source: Path, source_sha: str,
                    params: dict, param_sha: str, has_audio: bool, has_video: bool,
                    end: float) -> list[Path]:
    """Authenticate recorded work before any resume mutation; return orphan chunks to rebuild."""
    if manifest.get("schema") != 2 or manifest.get("version") != VERSION:
        raise RuntimeError("Resume manifest schema or version differs; use a new --output")
    if (manifest.get("source_sha256") != source_sha or manifest.get("source_bytes") != source.stat().st_size
            or manifest.get("params_sha256") != param_sha or manifest.get("params") != params
            or manifest.get("start") != params["start"] or manifest.get("end") != end):
        raise RuntimeError("Resume source or parameters differ; select a new --output")
    encoded = json.dumps(manifest["params"], sort_keys=True).encode()
    if hashlib.sha256(encoded).hexdigest() != manifest["params_sha256"]:
        raise RuntimeError("Resume manifest parameter hash mismatch")
    steps = manifest.get("steps", {})
    if steps.get("audio") not in {"pending", "running", "failed", "complete", "skipped"} or \
            steps.get("video") not in {"pending", "running", "failed", "complete", "skipped"}:
        raise RuntimeError("Resume manifest has invalid step state")
    if (steps["audio"] == "skipped") == has_audio or (steps["video"] == "skipped") == has_video:
        raise RuntimeError("Resume media streams differ")
    outputs = manifest.get("outputs", {})
    if not isinstance(outputs, dict):
        raise RuntimeError("Resume output hash records are invalid")
    expected_outputs = (("speech.json", "speech.md") if steps["audio"] == "complete" else ()) + \
                       (("visual.json", "screen_text.md") if steps["video"] == "complete" else ())
    for name in expected_outputs:
        if name not in outputs:
            raise RuntimeError(f"Resume completed output lacks recorded hash: {name}")
    for name, expected in outputs.items():
        if name not in {"speech.json", "speech.md", "visual.json", "screen_text.md"} or \
                not isinstance(expected, str) or len(expected) != 64:
            raise RuntimeError(f"Resume output hash record is invalid: {name}")
        path = output / name
        if not path.is_file() or digest(path) != expected:
            raise RuntimeError(f"Resume output hash mismatch: {name}")
    if steps.get("evidence") == "complete":
        evidence_file = output / "evidence.json"
        if not manifest.get("evidence_sha256") or not evidence_file.is_file() or \
                digest(evidence_file) != manifest["evidence_sha256"]:
            raise RuntimeError("Resume evidence hash mismatch")
    records = manifest.get("chunks", [])
    if not isinstance(records, list):
        raise RuntimeError("Resume chunk hash records are invalid")
    expected_count = math.ceil((end - params["start"]) / params["chunk_seconds"]) if has_audio else 0
    indices = set()
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("index"), int) or \
                not 0 <= record["index"] < expected_count or record["index"] in indices:
            raise RuntimeError("Resume chunk index record is invalid")
        index = record["index"]
        indices.add(index)
        path = output / "chunks" / f"{index:05d}.json"
        if not path.is_file() or digest(path) != record.get("sha256"):
            raise RuntimeError(f"Resume chunk hash mismatch: {path.name}")
        chunk = json.loads(path.read_text(encoding="utf-8"))
        if (chunk.get("begin") != params["start"] + index * params["chunk_seconds"] or
                chunk.get("finish") != min(end, params["start"] + (index + 1) * params["chunk_seconds"])):
            raise RuntimeError(f"Resume chunk boundaries differ: {path.name}")
    if steps["audio"] == "complete" and len(indices) != expected_count:
        raise RuntimeError("Resume completed audio lacks chunk hash records")
    chunk_dir = output / "chunks"
    orphans = [path for path in chunk_dir.glob("[0-9][0-9][0-9][0-9][0-9].json")
               if int(path.stem) not in indices] if chunk_dir.exists() else []
    if orphans and steps["audio"] == "complete":
        raise RuntimeError("Resume completed audio has unrecorded chunks")
    return orphans


def handle(source: Path, output: Path, args, profile=None) -> None:
    metadata = probe(source)
    streams = metadata.get("streams", [])
    has_audio = any(stream.get("codec_type") == "audio" for stream in streams) and not args.no_audio
    has_video = any(stream.get("codec_type") == "video" for stream in streams) and not args.no_video
    if not has_audio and not has_video:
        raise RuntimeError("No enabled audio or video stream")
    audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
    preferred = next((stream for stream in audio_streams if stream.get("codec_name") in
                      {"aac", "mp3", "flac", "opus", "vorbis", "pcm_s16le", "pcm_s24le", "alac"}),
                     audio_streams[0] if audio_streams else None)
    args.selected_audio_stream = (args.audio_stream_index if args.audio_stream_index is not None
                                  else preferred["index"] if preferred else None)
    if has_audio and args.selected_audio_stream not in {stream["index"] for stream in audio_streams}:
        raise ValueError("Selected audio stream index does not exist")
    duration = float(metadata["format"]["duration"])
    end = min(args.end if args.end is not None else duration, duration)
    if args.start >= end:
        raise ValueError("Start must precede end within source")
    source_sha = digest(source)
    params = {key: getattr(args, key) for key in ("model", "language", "device", "compute_type", "start", "end",
        "vad", "chunk_seconds", "no_audio", "no_video", "no_ocr", "frame_interval", "change_threshold", "max_frames", "perspective", "selected_audio_stream")}
    params["profile_sha256"] = (hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()
                                if has_audio and profile is not None else None)
    param_sha = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()
    manifest_file = output / "manifest.json"
    if output.exists():
        if not args.resume or not manifest_file.exists():
            raise FileExistsError(f"Output exists; use --resume for matching work: {output}")
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        orphans = validate_resume(output, manifest, source, source_sha, params, param_sha,
                                  has_audio, has_video, end)
        for orphan in orphans:
            orphan.rename(orphan.with_name(f"{orphan.stem}.unverified-{time.time_ns()}.json"))
    else:
        output.mkdir(parents=True)
        manifest = {"schema": 2, "version": VERSION, "source": str(source), "source_bytes": source.stat().st_size,
            "source_sha256": source_sha, "source_modified": datetime.fromtimestamp(source.stat().st_mtime, timezone.utc).isoformat(),
            "params": params, "params_sha256": param_sha, "created": datetime.now(timezone.utc).isoformat(),
            "start": args.start, "end": end, "duration": duration, "streams": streams,
            "steps": {"audio": "pending" if has_audio else "skipped", "video": "pending" if has_video else "skipped",
                      "evidence": "pending"}}
        atomic_write(manifest_file, manifest)
    try:
        if all(status in {"complete", "skipped"} for status in manifest["steps"].values()):
            print(output)
            return
        if has_audio and manifest["steps"]["audio"] != "complete":
            manifest["steps"]["audio"] = "running"
            atomic_write(manifest_file, manifest)
            manifest["audio"] = transcribe(source, args.start, end, output, args, manifest, profile)
            manifest["steps"]["audio"] = "complete"
            manifest.setdefault("outputs", {}).update({name: digest(output / name)
                for name in ("speech.json", "speech.md")})
            atomic_write(manifest_file, manifest)
        if has_video and manifest["steps"]["video"] != "complete":
            manifest["steps"]["video"] = "running"
            atomic_write(manifest_file, manifest)
            manifest["video"] = scan_video(source, args.start, end, output, args)
            manifest["steps"]["video"] = "complete"
            manifest.setdefault("outputs", {}).update({name: digest(output / name)
                for name in ("visual.json", "screen_text.md")})
        manifest["outputs"] = {name: digest(output / name) for name in
            ("speech.json", "speech.md", "visual.json", "screen_text.md") if (output / name).exists()}
        if manifest["steps"].get("evidence") != "complete":
            from media_platform.evidence import build_evidence
            manifest["steps"]["evidence"] = "running"
            atomic_write(manifest_file, manifest)
            bundle_manifest = {**manifest, "steps": {**manifest["steps"], "evidence": "complete"}}
            bundle = build_evidence(output, bundle_manifest)
            atomic_write(output / "evidence.json", bundle)
            manifest["evidence_sha256"] = digest(output / "evidence.json")
            manifest["steps"]["evidence"] = "complete"
        manifest.pop("last_error", None)
        manifest["updated"] = datetime.now(timezone.utc).isoformat()
        atomic_write(manifest_file, manifest)
    except BaseException as exc:
        for step, status in manifest["steps"].items():
            if status == "running":
                manifest["steps"][step] = "failed"
        manifest["last_error"] = {"type": type(exc).__name__, "message": str(exc)[:500],
                                  "time": datetime.now(timezone.utc).isoformat()}
        atomic_write(manifest_file, manifest)
        raise
    print(output)


def main(default_output_name: str = "media_outputs", default_profile: Path | None = None) -> None:
    parser = argparse.ArgumentParser(description="Local resumable speech transcription and video OCR")
    parser.add_argument("media", type=Path, help="Media file or directory")
    parser.add_argument("--output", type=Path, help="Output directory, or root for directory input")
    parser.add_argument("--start", type=parse_time, default=0.0)
    parser.add_argument("--end", type=parse_time)
    parser.add_argument("--model", default="medium")
    parser.add_argument("--device", choices=["auto", "cpu", "cuda"], default="auto")
    parser.add_argument("--compute-type", choices=["auto", "float32", "float16", "int8_float16", "int8"], default="auto")
    parser.add_argument("--language", default="ja")
    parser.add_argument("--audio-stream-index", type=int, help="ffprobe stream index; defaults to first common decodable audio codec")
    parser.add_argument("--vad", choices=["off", "conservative"], default="conservative")
    parser.add_argument("--chunk-seconds", type=int, default=300)
    parser.add_argument("--cpu-threads", type=int, default=4)
    parser.add_argument("--ram-reserve-gib", type=float, default=4.0)
    parser.add_argument("--vram-reserve-gib", type=float, default=1.5)
    parser.add_argument("--frame-interval", type=float)
    parser.add_argument("--max-frames", type=int, default=300)
    parser.add_argument("--change-threshold", type=float, default=0.004)
    parser.add_argument("--no-audio", action="store_true")
    parser.add_argument("--no-video", action="store_true")
    parser.add_argument("--no-ocr", action="store_true")
    parser.add_argument("--perspective", action="store_true", help="Try conservative screen rectification on derived frames")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--profile", type=Path, help="Measured candidate speed profile JSON")
    args = parser.parse_args()
    if args.chunk_seconds < 15 or args.cpu_threads < 1 or args.max_frames < 1 or (args.frame_interval is not None and args.frame_interval <= 0):
        parser.error("Invalid chunk seconds, threads, max frames or frame interval")
    if not 0 <= args.change_threshold <= 1:
        parser.error("Change threshold must be between 0 and 1")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        parser.error("ffmpeg and ffprobe must be available")
    acquire_single_run_lock()
    import psutil
    process = psutil.Process()
    process.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS if sys.platform == "win32" else 10)
    os.environ["OMP_NUM_THREADS"] = str(args.cpu_threads)
    source = args.media.expanduser().resolve(strict=True)
    profile_path = args.profile or default_profile or shared_profile_path()
    profile = json.loads(profile_path.read_text(encoding="utf-8")) if profile_path.exists() else None
    if source.is_file():
        inputs = [(source, Path())]
        root = args.output or source.parent / default_output_name
    elif source.is_dir():
        inputs = [(file, file.relative_to(source).parent) for file in sorted(source.rglob("*"))
                  if file.is_file() and file.suffix.lower() in EXTENSIONS and
                  default_output_name not in file.parts]
        root = args.output or source / default_output_name
    else:
        parser.error("Input must be a file or directory")
    seen = set()
    failures = 0
    settings = {key: getattr(args, key) for key in ("model", "device", "compute_type", "language", "start", "end",
        "vad", "chunk_seconds", "no_audio", "no_video", "no_ocr", "frame_interval", "change_threshold",
        "max_frames", "perspective", "audio_stream_index")}
    settings_tag = hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()[:8]
    for file, relative in inputs:
        identity = (file.stat().st_dev, file.stat().st_ino)
        if identity in seen:
            continue
        seen.add(identity)
        safe = re.sub(r'[<>:"/\\|?*]', "_", file.stem)[:80]
        destination = (args.output if source.is_file() and args.output else root / relative /
                       f"{safe}_{digest(file)[:10]}_{int(args.start)}s_{settings_tag}").expanduser().resolve()
        try:
            handle(file, destination, args, profile)
        except Exception as exc:
            failures += 1
            print(f"FAILED {file}: {type(exc).__name__}: {exc}", file=sys.stderr)
            if source.is_file():
                raise
    if failures:
        raise SystemExit(f"{failures} media file(s) failed")


if __name__ == "__main__":
    main()
