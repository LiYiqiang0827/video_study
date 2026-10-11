#!/usr/bin/env python3
"""Offline Qwen3 ASR and forced alignment with durable intermediate outputs.

Verified interfaces: mlx-audio 0.5.8 Qwen3 ASR/ForcedAligner source. No model IDs or media URLs are accepted. The runner
never downloads models and never turns Qwen ASR chunk boundaries into timings.
All times are seconds. Normalized units use the selected clip's time origin;
source_start/source_end also retain positions in the original source audio.
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime as dt
import hashlib
import importlib.metadata
import inspect
import json
import math
import os
from pathlib import Path
import platform
import re
import socket
import subprocess
import sys
import time
import traceback
import uuid
from typing import Any, Callable

sys.dont_write_bytecode = True
from tool_paths import data_root, external_path
from extract_audio import executable

SAMPLE_RATE = 16000
MODEL_RECOMMENDATIONS = {
    "qwen": {"repo": "mlx-community/Qwen3-ASR-1.7B-8bit",
             "revision": "a8379a2e2f9e313c9292cdf1af4055ab56d50d55"},
    "aligner": {"repo": "mlx-community/Qwen3-ForcedAligner-0.6B-8bit",
                "revision": "0e1a68e91d815300c7c9754b2a7639378b23db15"},
}
API_REFERENCES = [
    "https://github.com/Blaizzy/mlx-audio/blob/v0.5.8/mlx_audio/stt/models/qwen3_asr/qwen3_asr.py",
    "https://github.com/Blaizzy/mlx-audio/blob/v0.5.8/mlx_audio/stt/models/qwen3_asr/qwen3_forced_aligner.py",
]


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def to_jsonable(value: Any) -> Any:
    """Preserve dataclass/raw backend fields without inventing alignment."""
    if dataclasses.is_dataclass(value):
        return {f.name: to_jsonable(getattr(value, f.name))
                for f in dataclasses.fields(value)}
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return {"nonfinite_float": repr(value)}
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if hasattr(value, "tolist"):
        return to_jsonable(value.tolist())
    if hasattr(value, "__dict__"):
        return {k: to_jsonable(v) for k, v in vars(value).items()
                if not k.startswith("_")}
    return {"python_type": type(value).__qualname__, "repr": repr(value)}


def atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_json(path: Path, value: Any) -> None:
    atomic_text(path, json.dumps(to_jsonable(value), ensure_ascii=False,
                               indent=2, allow_nan=False) + "\n")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def local_file(value: str | Path, suffix: str | None = None) -> Path:
    if "://" in str(value):
        raise ValueError("Only local filesystem paths are accepted")
    path = Path(value).expanduser().resolve(strict=True)
    if not path.is_file() or (suffix and path.suffix.lower() != suffix):
        raise ValueError(f"Expected a local {suffix or ''} file: {path}")
    return path


def local_model(value: str | Path | None) -> Path:
    if value is None or "://" in str(value):
        raise ValueError("A pre-downloaded local model directory is required")
    path = Path(value).expanduser().resolve(strict=True)
    if not path.is_dir() or not (path / "config.json").is_file():
        raise ValueError(f"Model directory must contain config.json: {path}")
    if not any(path.glob("*.safetensors")) and not any(path.glob("*.npz")):
        raise ValueError(f"No local model weights found: {path}")
    return path


@dataclasses.dataclass
class ASRConfig:
    audio: Path
    output_root: Path
    qwen_model: Path | None = None
    aligner_model: Path | None = None
    source_video: Path | None = None
    start: float = 0.0
    duration: float | None = None
    chunk_max_seconds: float = 180.0
    cut_search_seconds: float = 5.0
    energy_window_seconds: float = 0.1
    max_tokens: int = 16384
    prompt: str = ""
    run_name: str | None = None
    ffmpeg: str = "ffmpeg"
    runtime_dir: Path | None = None
    qwen_revision: str | None = None
    aligner_revision: str | None = None

    def validate(self) -> None:
        self.audio = local_file(self.audio, ".wav")
        if self.source_video is not None:
            self.source_video = local_file(self.source_video)
        if not math.isfinite(self.start) or self.start < 0:
            raise ValueError("start must be finite and nonnegative")
        if self.duration is not None and (not math.isfinite(self.duration) or self.duration <= 0):
            raise ValueError("duration must be finite and positive")
        if not math.isfinite(self.chunk_max_seconds) or not 0 < self.chunk_max_seconds <= 180:
            raise ValueError("chunk-max-seconds must be in (0, 180]")
        if not math.isfinite(self.cut_search_seconds) or self.cut_search_seconds < 0:
            raise ValueError("cut-search-seconds must be finite and nonnegative")
        if not math.isfinite(self.energy_window_seconds) or self.energy_window_seconds <= 0:
            raise ValueError("energy-window-seconds must be finite and positive")
        if self.max_tokens <= 0:
            raise ValueError("max-tokens must be positive")
        if "://" in str(self.output_root):
            raise ValueError("Output must be a local directory")
        self.output_root = external_path(self.output_root)
        self.runtime_dir = external_path(self.runtime_dir or data_root() / "runtime" / "asr")
        self.qwen_model = local_model(self.qwen_model or self.runtime_dir / "models" / "Qwen3-ASR-1.7B-8bit")
        self.aligner_model = local_model(self.aligner_model or self.runtime_dir / "models" / "Qwen3-ForcedAligner-0.6B-8bit")
        external_path(self.qwen_model)
        external_path(self.aligner_model)
        self.ffmpeg = executable(self.ffmpeg)
        if self.run_name and not re.fullmatch(r"[A-Za-z0-9_.-]+", self.run_name):
            raise ValueError("run-name must contain only letters, digits, _, - or .")
        if self.run_name in {".", ".."}:
            raise ValueError("Invalid run-name")


class RunDirectory:
    """Never reuse an output directory; atomic STATUS.json enables supervision."""
    def __init__(self, root: Path, name: str | None = None):
        root = external_path(root)
        root.mkdir(parents=True, exist_ok=True)
        name = name or (dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
                        + f"_{os.getpid()}_{uuid.uuid4().hex[:8]}")
        self.path = root / name
        self.path.mkdir(exist_ok=False)
        self.lock_path = self.path / ".RUNNING.lock"
        with self.lock_path.open("x", encoding="utf-8") as handle:
            json.dump({"pid": os.getpid(), "host": socket.gethostname(),
                       "created_at": utc_now()}, handle)
        self.status("created")

    def status(self, stage: str, state: str = "running", **details: Any) -> None:
        event = {"updated_at": utc_now(), "state": state, "stage": stage,
                 "pid": os.getpid(), **details}
        atomic_json(self.path / "STATUS.json", event)
        with (self.path / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(to_jsonable(event), ensure_ascii=False) + "\n")
            handle.flush()

    def finish(self, state: str, **details: Any) -> None:
        self.status("finished", state=state, **details)
        self.lock_path.unlink(missing_ok=True)


@dataclasses.dataclass
class AudioSample:
    waveform: Any
    sample_rate: int
    metadata: dict[str, Any]

    @property
    def duration(self) -> float:
        return len(self.waveform) / self.sample_rate

    @property
    def source_offset(self) -> float:
        return self.metadata["source_offset_seconds"]


def select_sample_bounds(total_samples: int, sample_rate: int, start: float,
                         duration: float | None) -> tuple[int, int]:
    first = round(start * sample_rate)
    if first >= total_samples:
        raise ValueError("Requested start is outside the decoded audio")
    last = total_samples if duration is None else min(total_samples, first + round(duration * sample_rate))
    if last <= first:
        raise ValueError("Requested clip contains no audio samples")
    return first, last


def write_wav(path: Path, waveform: Any, sample_rate: int) -> None:
    import soundfile as sf
    sf.write(str(path), waveform, sample_rate, subtype="PCM_16", format="WAV")


def prepare_audio(config: ASRConfig, run: RunDirectory) -> AudioSample:
    """Decode the entire local source first; only then select the requested clip."""
    import soundfile as sf
    audio_dir = run.path / "audio"
    audio_dir.mkdir()
    full_path = audio_dir / "source_full_16k_mono.wav"
    command = [config.ffmpeg, "-nostdin", "-n", "-v", "error", "-xerror",
               "-i", str(config.audio), "-map", "0:a:0", "-ac", "1", "-ar",
               str(SAMPLE_RATE), "-c:a", "pcm_s16le", str(full_path)]
    atomic_json(audio_dir / "decode_command.json", command)
    run.status("audio_full_decode")
    began = time.perf_counter()
    with (audio_dir / "ffmpeg.stdout.txt").open("w", encoding="utf-8") as out, \
            (audio_dir / "ffmpeg.stderr.txt").open("w", encoding="utf-8") as err:
        subprocess.run(command, stdout=out, stderr=err, check=True)
    decoded_seconds = time.perf_counter() - began
    waveform, sr = sf.read(str(full_path), dtype="float32", always_2d=False)
    if sr != SAMPLE_RATE or waveform.ndim != 1:
        raise ValueError("Full decode did not produce mono 16 kHz audio")
    first, last = select_sample_bounds(len(waveform), sr, config.start, config.duration)
    selected = waveform[first:last]
    sample_path = audio_dir / "sample.wav"
    write_wav(sample_path, selected, sr)
    source_info = sf.info(str(config.audio))
    metadata = {
        "source_audio": str(config.audio), "source_audio_sha256": sha256_file(config.audio),
        "source_audio_info": {"sample_rate": source_info.samplerate, "channels": source_info.channels,
                              "frames": source_info.frames, "duration_seconds": source_info.duration,
                              "format": source_info.format, "subtype": source_info.subtype},
        "source_video": str(config.source_video) if config.source_video else None,
        "source_video_sha256": sha256_file(config.source_video) if config.source_video else None,
        "full_decoded_audio": str(full_path), "full_decoded_sha256": sha256_file(full_path),
        "full_decode_seconds": decoded_seconds, "full_decoded_samples": len(waveform),
        "full_decoded_duration_seconds": len(waveform) / sr,
        "sample_audio": str(sample_path), "sample_audio_sha256": sha256_file(sample_path),
        "sample_rate": sr, "sample_count": len(selected), "duration_seconds": len(selected) / sr,
        "requested_start_seconds": config.start, "requested_duration_seconds": config.duration,
        "start_sample_in_decoded_source": first, "end_sample_in_decoded_source": last,
        "source_offset_seconds": first / sr, "source_end_seconds": last / sr,
        "duration_clamped_to_source": config.duration is not None and last < first + round(config.duration * sr),
        "timeline": "units.start/end are selected-clip-relative seconds; source_start/end are source-relative seconds",
        "preprocessing": "Full ffmpeg decode, mono downmix and 16k resampling; contiguous slice; no VAD or pause removal",
    }
    atomic_json(run.path / "audio_metadata.json", metadata)
    return AudioSample(selected, sr, metadata)


def split_audio_indices(waveform: Any, sr: int, max_seconds: float = 180,
                        search_seconds: float = 5, energy_seconds: float = 0.1) -> list[tuple[int, int]]:
    """Strict contiguous sample coverage, with cuts never beyond the maximum.

    Search only BEFORE the hard boundary, unlike MLX Audio's symmetric search
    which can exceed its nominal maximum. No overlap, dropped silence or padding.
    """
    if sr <= 0 or not math.isfinite(max_seconds) or not 0 < max_seconds <= 180:
        raise ValueError("Invalid sample rate or chunk maximum")
    if not math.isfinite(search_seconds) or search_seconds < 0 or not math.isfinite(energy_seconds) or energy_seconds <= 0:
        raise ValueError("Invalid energy search configuration")
    maximum = math.floor(max_seconds * sr)
    if maximum < 1:
        raise ValueError("Chunk maximum is shorter than one sample")
    search = max(0, math.floor(search_seconds * sr))
    window = max(1, round(energy_seconds * sr))
    chunks, start, total = [], 0, len(waveform)
    while start < total:
        end = min(start + maximum, total)
        if end < total and search:
            low = max(start + 1, end - search)
            region = waveform[low:end]
            if len(region):
                width = min(window, len(region))
                try:
                    import numpy as np
                except ModuleNotFoundError:
                    # Standard-library fallback keeps mock tests package-free.
                    energy = sum(float(v) ** 2 for v in region[:width])
                    best_energy, best_index = energy, 0
                    for offset in range(1, len(region) - width + 1):
                        energy += float(region[offset + width - 1]) ** 2 - float(region[offset - 1]) ** 2
                        if energy < best_energy:
                            best_energy, best_index = energy, offset
                else:
                    region = np.asarray(region, dtype=np.float64)
                    cumulative = np.concatenate(([0.0], np.cumsum(region * region)))
                    energies = cumulative[width:] - cumulative[:-width]
                    best_index = int(np.argmin(energies))
                cut = low + best_index + width // 2
                end = max(start + 1, min(end, cut))
        chunks.append((start, end))
        start = end
    return chunks


def package_versions() -> dict[str, Any]:
    packages = ["mlx", "mlx-audio", "mlx-lm", "transformers",
                "huggingface-hub", "numpy", "soundfile", "tiktoken"]
    versions: dict[str, Any] = {"python": sys.version, "platform": platform.platform(),
                                "machine": platform.machine(), "executable": sys.executable}
    for package in packages:
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    return versions


def describe_model(path: Path | None, revision: str | None) -> Any:
    if path is None:
        return None
    files = []
    for file in sorted(path.iterdir()):
        if file.is_file():
            entry = {"name": file.name, "size_bytes": file.stat().st_size}
            if file.suffix in {".json", ".txt"}:
                entry["sha256"] = sha256_file(file)
            files.append(entry)
    return {"path": str(path), "declared_revision": revision,
            "revision_verified_by_runner": False, "files": files,
            "config": json.loads((path / "config.json").read_text(encoding="utf-8"))}


def method_identity(method: Any) -> dict[str, Any]:
    """Record installed code identity without assuming package versions match."""
    record = {"qualname": getattr(method, "__qualname__", None)}
    try:
        source = inspect.getsource(method)
        record.update(source_file=inspect.getsourcefile(method),
                      source_sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(),
                      source_mentions_fix_timestamp="self.fix_timestamp(" in source)
    except (TypeError, OSError) as error:
        record["source_inspection_unavailable"] = str(error)
    return record


def pair_before_repair_units(words: Any, timestamps_ms: Any) -> list[dict] | None:
    """Pair actual parser input positions; never repair or fabricate missing ones."""
    if not isinstance(words, list) or not isinstance(timestamps_ms, list) or len(timestamps_ms) != len(words) * 2:
        return None
    if any(not isinstance(v, (int, float)) or isinstance(v, bool) for v in timestamps_ms):
        return None
    return [{"text": word, "start_time": timestamps_ms[index * 2] / 1000,
             "end_time": timestamps_ms[index * 2 + 1] / 1000}
            for index, word in enumerate(words)]


@contextlib.contextmanager
def capture_alignment_parse(processor: Any):
    """Observe ONE owned processor instance; always call/return its real method.

    MLX Audio 0.5.8 parse_timestamp receives predicted class IDs multiplied by
    timestamp_segment_time, in milliseconds, then calls fix_timestamp. These
    are pre-repair predictions, not logits. The wrapper does no alignment or
    repair itself and restores the original instance attribute in finally.
    """
    try:
        version = importlib.metadata.version("mlx-audio")
    except importlib.metadata.PackageNotFoundError:
        version = None
    audit: dict[str, Any] = {
        "schema_version": 1, "mlx_audio_version": version, "capture_state": "unavailable",
        "scope": "one runner-owned aligner_processor instance, one serial generate call",
        "input_stage": "real parse_timestamp input, before library timestamp repair",
        "input_time_unit": "milliseconds", "derived_before_units_time_unit": "seconds",
        "parse_output_time_unit": "milliseconds", "returned_units_time_unit": "seconds",
        "prediction_scope": "argmax timestamp-class predictions scaled by timestamp_segment_time; not logits",
        "known_0_5_8_behavior": "parse_timestamp calls fix_timestamp (longest nondecreasing subsequence repair); generate divides by 1000 and rounds to three decimals",
        "algorithm_changed_by_runner": False, "calls": [],
    }
    original = getattr(processor, "parse_timestamp", None) if processor is not None else None
    if not callable(original):
        audit["unavailable_reason"] = "aligner_processor.parse_timestamp is unavailable"
        yield audit
        return
    audit["parse_method"] = method_identity(original)
    fix_method = getattr(processor, "fix_timestamp", None)
    audit["fix_method"] = method_identity(fix_method) if callable(fix_method) else None
    own_attributes = getattr(processor, "__dict__", {})
    had_own_attribute = "parse_timestamp" in own_attributes
    own_original = own_attributes.get("parse_timestamp")

    def observed_parse(word_list, timestamp, *args, **kwargs):
        call: dict[str, Any] = {"index": len(audit["calls"])}
        audit["calls"].append(call)
        try:
            # Snapshot BEFORE the real method can mutate its arguments.
            call["input_word_list"] = to_jsonable(word_list)
            call["input_timestamp_ms"] = to_jsonable(timestamp)
            call["input_timestamp_shape"] = to_jsonable(getattr(timestamp, "shape", None))
            call["input_timestamp_dtype"] = str(getattr(timestamp, "dtype", "unknown"))
            call["units_before_library_repair_seconds"] = pair_before_repair_units(
                call["input_word_list"], call["input_timestamp_ms"])
        except Exception as error:
            call["input_capture_exception"] = exception_record(error)
        try:
            output = original(word_list, timestamp, *args, **kwargs)
        except BaseException as error:
            call["parse_exception"] = exception_record(error)
            raise
        try:
            call["parse_output_after_library_repair_ms"] = to_jsonable(output)
            before = call.get("input_timestamp_ms")
            after = call["parse_output_after_library_repair_ms"]
            after_flat = ([v for item in after for v in [item.get("start_time"), item.get("end_time")]]
                          if isinstance(after, list) and all(isinstance(item, dict) for item in after) else None)
            if isinstance(before, list) and isinstance(after_flat, list) and len(before) == len(after_flat):
                call["changed_endpoints"] = [
                    {"endpoint_index": index, "word_index": index // 2,
                     "boundary": "start" if index % 2 == 0 else "end",
                     "before_ms": old, "after_ms": new, "delta_ms": new - old}
                    for index, (old, new) in enumerate(zip(before, after_flat))
                    if isinstance(old, (int, float)) and isinstance(new, (int, float)) and old != new]
                call["changed_endpoint_count"] = len(call["changed_endpoints"])
        except Exception as error:
            call["output_capture_exception"] = exception_record(error)
        return output  # Exact native return value, including object identity.

    installed = False
    try:
        setattr(processor, "parse_timestamp", observed_parse)
        installed = True
    except (AttributeError, TypeError) as error:
        audit["unavailable_reason"] = f"Cannot observe this processor instance: {error}"
    try:
        yield audit
    finally:
        if installed:
            if had_own_attribute:
                setattr(processor, "parse_timestamp", own_original)
            else:
                delattr(processor, "parse_timestamp")
        calls = audit["calls"]
        if calls:
            audit["capture_state"] = ("captured" if all(
                "input_timestamp_ms" in call and "parse_output_after_library_repair_ms" in call
                for call in calls) else "partial")
            audit["changed_endpoint_count"] = sum(call.get("changed_endpoint_count", 0) for call in calls)
        elif installed:
            audit["unavailable_reason"] = "The native generate call did not invoke parse_timestamp"


class QwenBackend:
    def load(self, config: ASRConfig) -> dict[str, float]:
        import mlx.core as mx
        from mlx_audio.stt import load
        self.mx = mx
        began = time.perf_counter()
        self.asr = load(config.qwen_model, lazy=False)
        mx.synchronize()
        asr_seconds = time.perf_counter() - began
        began = time.perf_counter()
        self.aligner = load(config.aligner_model, lazy=False)
        mx.synchronize()
        return {"asr_model_load_seconds": asr_seconds,
                "aligner_model_load_seconds": time.perf_counter() - began}

    def transcribe(self, audio: Any, config: ASRConfig) -> Any:
        result = self.asr.generate(audio, language="Chinese", system_prompt=config.prompt or None,
                                   max_tokens=config.max_tokens, temperature=0.0, batch_size=1,
                                   stream=False, verbose=False,
                                   chunk_duration=config.chunk_max_seconds + 1.0,
                                   min_chunk_duration=0.0)
        self.mx.synchronize()
        return result

    def align(self, audio: Any, text: str, config: ASRConfig) -> Any:
        processor = getattr(self.aligner, "aligner_processor", None)
        with capture_alignment_parse(processor) as audit:
            self.last_alignment_audit = audit
            audit["timestamp_segment_time_ms"] = getattr(getattr(self.aligner, "config", None),
                                                         "timestamp_segment_time", None)
            result = self.aligner.generate(audio=audio, text=text, language="Chinese")
            self.mx.synchronize()
            returned = to_jsonable(result)
            audit["returned_units_after_library_repair_seconds"] = (returned.get("items")
                                                                   if isinstance(returned, dict) else returned)
            return result

    def close(self) -> None:
        self.asr = None
        self.aligner = None
        self.mx.clear_cache()


def normalize_units(raw_units: list[Any], *, chunk_offset: float, source_offset: float,
                    chunk_duration: float, chunk_index: int, kind: str) -> tuple[list[dict], list[dict]]:
    units, problems, previous_start = [], [], -math.inf
    for index, original in enumerate(raw_units):
        item = to_jsonable(original)
        if not isinstance(item, dict):
            problems.append({"code": "invalid_alignment_item", "index": index})
            continue
        text = item.get("word", item.get("text"))
        start = item.get("start_time", item.get("start"))
        end = item.get("end_time", item.get("end"))
        if not isinstance(text, str) or isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            problems.append({"code": "missing_alignment_fields", "index": index})
            continue
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start or end > chunk_duration + 0.1:
            problems.append({"code": "invalid_alignment_span", "index": index,
                             "start": start, "end": end, "chunk_duration": chunk_duration})
            continue
        if start < previous_start:
            problems.append({"code": "nonmonotonic_alignment", "index": index})
        previous_start = start
        normalized = {"text": text, "start": start + chunk_offset, "end": end + chunk_offset,
                      "source_start": start + chunk_offset + source_offset,
                      "source_end": end + chunk_offset + source_offset,
                      "chunk_index": chunk_index, "alignment_kind": kind,
                      "raw_unit_index": index}
        if "probability" in item:
            normalized["probability"] = item["probability"]
        units.append(normalized)
    return units, problems


def exception_record(error: BaseException) -> dict[str, str]:
    return {"type": type(error).__name__, "message": str(error),
            "traceback": "".join(traceback.format_exception(type(error), error, error.__traceback__)),
            "recorded_at": utc_now()}


def run_qwen(config: ASRConfig, sample: AudioSample, directory: Path,
             run: RunDirectory, backend: Any) -> dict[str, Any]:
    bounds = split_audio_indices(sample.waveform, sample.sample_rate, config.chunk_max_seconds,
                                config.cut_search_seconds, config.energy_window_seconds)
    manifest = [{"index": index, "start_sample": first, "end_sample": last,
                 "start_seconds": first / sample.sample_rate, "end_seconds": last / sample.sample_rate,
                 "source_start_seconds": first / sample.sample_rate + sample.source_offset,
                 "source_end_seconds": last / sample.sample_rate + sample.source_offset}
                for index, (first, last) in enumerate(bounds)]
    atomic_json(directory / "chunk_manifest.json", {"chunks": manifest,
                "coverage_samples": sum(last - first for first, last in bounds),
                "source_sample_count": len(sample.waveform), "padding_samples": 0,
                "overlap_samples": 0, "removed_samples": 0})
    all_units, texts, warnings, records = [], [], [], []
    asr_seconds = alignment_seconds = 0.0
    any_failed = any_limit = False
    for index, (first, last) in enumerate(bounds):
        chunk_dir = directory / f"chunk_{index:03d}"
        chunk_dir.mkdir()
        audio = sample.waveform[first:last]
        write_wav(chunk_dir / "audio.wav", audio, sample.sample_rate)
        record: dict[str, Any] = {**manifest[index], "state": "running"}
        warning_count_before = len(warnings)
        try:
            run.status("qwen_transcribe", backend="qwen", chunk_index=index, total_chunks=len(bounds))
            began = time.perf_counter()
            try:
                raw = to_jsonable(backend.transcribe(audio, config))
            finally:
                record["asr_inference_seconds"] = time.perf_counter() - began
                asr_seconds += record["asr_inference_seconds"]
            atomic_json(chunk_dir / "raw_asr.json", raw)
            text = raw.get("text", "")
            texts.append(text)
            record["raw_text"] = text
            atomic_text(chunk_dir / "raw_text.txt", text)
            generation_tokens = raw.get("generation_tokens")
            limit_reached = (isinstance(generation_tokens, int) and generation_tokens >= config.max_tokens)
            record["truncation"] = {"generation_tokens": generation_tokens, "max_tokens": config.max_tokens,
                                    "limit_reached": limit_reached,
                                    "truncated": None if limit_reached else False}
            if limit_reached:
                any_limit = True
                warnings.append({"code": "token_limit_reached", "chunk_index": index,
                                 "message": "Possible truncation; retry with a larger budget or shorter clip"})
            if generation_tokens is None:
                warnings.append({"code": "token_count_unavailable", "chunk_index": index})
            if text.strip():
                run.status("qwen_align", backend="qwen", chunk_index=index, total_chunks=len(bounds))
                began = time.perf_counter()
                try:
                    alignment = to_jsonable(backend.align(audio, text, config))
                finally:
                    record["alignment_inference_seconds"] = time.perf_counter() - began
                    alignment_seconds += record["alignment_inference_seconds"]
                    audit = getattr(backend, "last_alignment_audit", None)
                    if audit is None:
                        audit = {"capture_state": "unavailable", "calls": [],
                                 "unavailable_reason": "Backend did not provide intermediate alignment audit"}
                    atomic_json(chunk_dir / "alignment_audit.json", audit)
                    before_units = [unit for call in audit.get("calls", [])
                                    for unit in (call.get("units_before_library_repair_seconds") or [])]
                    atomic_json(chunk_dir / "alignment_before_library_repair_units.json", {
                        "time_unit": "seconds", "time_origin": "chunk-relative",
                        "timing_stage": "before_library_timestamp_repair",
                        "capture_state": audit.get("capture_state"),
                        "derived_from": "actual parse_timestamp word_list and millisecond timestamps",
                        "units": before_units,
                    })
                    record["alignment_audit"] = {
                        "capture_state": audit.get("capture_state"),
                        "changed_endpoint_count": audit.get("changed_endpoint_count"),
                        "path": str(chunk_dir / "alignment_audit.json"),
                        "before_units_path": str(chunk_dir / "alignment_before_library_repair_units.json"),
                    }
                    if audit.get("capture_state") != "captured":
                        warnings.append({"code": "alignment_intermediate_audit_unavailable", "chunk_index": index,
                                         "message": audit.get("unavailable_reason", "Only partial parser capture")})
                atomic_json(chunk_dir / "raw_alignment.json", alignment)
                # Official ForcedAlignResult is a dataclass with items. Mock tests
                # may use a list. ASR segments are NEVER an alignment fallback.
                raw_units = alignment.get("items", []) if isinstance(alignment, dict) else alignment
                if not isinstance(raw_units, list):
                    raw_units = []
                atomic_json(chunk_dir / "raw_alignment_units.json", raw_units)
                atomic_json(chunk_dir / "alignment_after_library_repair_units.json", {
                    "time_unit": "seconds", "time_origin": "chunk-relative",
                    "timing_stage": "native generate return, after library timestamp repair and rounding",
                    "units": raw_units,
                })
                record["alignment_unit_semantics"] = "raw_alignment_units is the raw library RETURN VALUE, after internal timestamp repair; it is not pre-repair model predictions"
                units, issues = normalize_units(raw_units, chunk_offset=first / sample.sample_rate,
                                               source_offset=sample.source_offset,
                                               chunk_duration=len(audio) / sample.sample_rate,
                                               chunk_index=index, kind="qwen3_forced_alignment_after_library_repair")
                all_units.extend(units)
                warnings.extend({**issue, "chunk_index": index} for issue in issues)
                if not units:
                    warnings.append({"code": "missing_alignment", "chunk_index": index,
                                     "message": "ASR text exists but forced aligner produced no valid units"})
                record["aligned_unit_count"] = len(units)
            else:
                record["alignment_state"] = "not_needed_empty_text"
                atomic_json(chunk_dir / "raw_alignment_units.json", [])
            record["state"] = "pending_review" if len(warnings) > warning_count_before else "complete"
        except Exception as error:
            any_failed = True
            record["state"] = "failed"
            record["exception"] = exception_record(error)
            atomic_json(chunk_dir / "exception.json", record["exception"])
            warnings.append({"code": "chunk_failed", "chunk_index": index, "message": str(error)})
        records.append(record)
        atomic_json(chunk_dir / "result.json", record)
        # Preserve all completed partial work after every chunk.
        atomic_json(directory / "partial_result.json", {"raw_text": "".join(texts), "units": all_units,
                                                         "chunks": records, "warnings": warnings})
    text = "".join(texts)  # Verbatim chunk text, no normalization or invented punctuation.
    atomic_text(directory / "raw_text.txt", text)
    return {"backend": "qwen", "raw_text": text, "raw_text_chunks": texts, "units": all_units,
            "raw_text_joiner": "", "state": "failed" if any_failed else "pending_review" if warnings else "complete",
            "warnings": warnings, "chunks": records,
            "timings": {"asr_inference_seconds": asr_seconds, "alignment_inference_seconds": alignment_seconds},
            "truncation": {"limit_reached": any_limit, "max_tokens_per_chunk": config.max_tokens,
                           "truncated": None if any_limit else False},
            "audio": sample.metadata,
            "timing_note": "ASR segments are chunk extents only; normalized units come from native forced-aligner RETURN VALUES after library timestamp repair; pre-repair predictions are separately captured in alignment_audit.json"}


def configure_runtime(runtime_dir: Path) -> None:
    """Keep package caches and temporary files outside the versioned project."""
    runtime_dir = external_path(runtime_dir)
    locations = {"HF_HOME": runtime_dir / "cache" / "huggingface",
                 "HF_HUB_CACHE": runtime_dir / "cache" / "huggingface" / "hub",
                 "TRANSFORMERS_CACHE": runtime_dir / "cache" / "transformers",
                 "XDG_CACHE_HOME": runtime_dir / "cache",
                 "TMPDIR": runtime_dir / "tmp"}
    for key, directory in locations.items():
        directory.mkdir(parents=True, exist_ok=True)
        os.environ[key] = str(directory)
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                      HF_DATASETS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1",
                      TOKENIZERS_PARALLELISM="false", PYTHONDONTWRITEBYTECODE="1")


def run_asr(config: ASRConfig, *, backend_factory: Callable = QwenBackend,
            audio_preparer: Callable = prepare_audio) -> tuple[Path, int]:
    config.validate()
    configure_runtime(config.runtime_dir)
    run = RunDirectory(config.output_root, config.run_name)
    print(str(run.path), flush=True)
    results: dict[str, Any] = {}
    began_run = time.perf_counter()
    try:
        atomic_json(run.path / "config.json", {**dataclasses.asdict(config), "backend": "qwen",
                    "sample_rate": SAMPLE_RATE, "task": "transcribe", "qwen_language": "Chinese",
                    "actual_prompt": config.prompt, "runner_sha256": sha256_file(Path(__file__).resolve()),
                    "offline": True, "api_references": API_REFERENCES,
                    "model_recommendations": MODEL_RECOMMENDATIONS})
        atomic_json(run.path / "versions.json", package_versions())
        models = {name: describe_model(getattr(config, f"{name}_model"), getattr(config, f"{name}_revision"))
                  for name in ["qwen", "aligner"]}
        atomic_json(run.path / "models.json", models)
        sample = audio_preparer(config, run)
        directory = run.path / "qwen"
        directory.mkdir()
        backend = backend_factory()
        began_backend = time.perf_counter()
        timing: dict[str, Any] = {}
        with (directory / "stdout.txt").open("w", encoding="utf-8", buffering=1) as stdout, \
                (directory / "stderr.txt").open("w", encoding="utf-8", buffering=1) as stderr, \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                run.status("qwen_model_load", backend="qwen")
                began = time.perf_counter()
                try:
                    timing.update(backend.load(config))
                finally:
                    timing["load_call_seconds_including_imports"] = time.perf_counter() - began
                    atomic_json(directory / "load_timings.json", timing)
                result = run_qwen(config, sample, directory, run, backend)
                result["timings"].update(timing)
            except Exception as error:
                result = {"backend": "qwen", "state": "failed", "exception": exception_record(error),
                          "timings": timing}
                atomic_json(directory / "exception.json", result["exception"])
                traceback.print_exc()
            finally:
                try:
                    backend.close()
                except Exception as error:
                    atomic_json(directory / "cleanup_exception.json", exception_record(error))
        result["timings"]["backend_total_seconds"] = time.perf_counter() - began_backend
        results["qwen"] = result
        atomic_json(directory / "result.json", result)
        atomic_json(run.path / "results.json", results)
        state = result["state"]
        atomic_json(run.path / "summary.json", {"state": state, "backends": results,
                    "run_seconds": time.perf_counter() - began_run,
                    "quality_note": "No human ground truth: native ASR and alignment require review"})
        run.finish(state)
        return run.path, {"complete": 0, "failed": 1, "pending_review": 2}[state]
    except BaseException as error:
        atomic_json(run.path / "exception.json", exception_record(error))
        run.finish("interrupted" if isinstance(error, KeyboardInterrupt) else "failed",
                   message=str(error), partial_backends=list(results))
        raise


def check_dependencies(ffmpeg: str) -> tuple[dict[str, Any], int]:
    versions = package_versions()
    errors = []
    for package in ("mlx", "mlx-audio", "numpy", "soundfile"):
        if not versions.get(package):
            errors.append(f"missing package: {package}")
    if versions.get("mlx-audio") not in (None, "0.5.8"):
        errors.append("Expected verified mlx-audio version 0.5.8")
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        errors.append("MLX inference requires an Apple Silicon macOS runtime")
    try:
        versions["ffmpeg"] = executable(ffmpeg)
    except ValueError as error:
        errors.append(str(error))
    return {"versions": versions, "errors": errors, "ready": not errors}, 1 if errors else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", type=Path, help="Local source WAV; decoded completely before clipping")
    parser.add_argument("--output-root", type=Path, help="New results root outside the code project")
    parser.add_argument("--qwen-model", type=Path, help="Local model; default runtime-dir/models/Qwen3-ASR-1.7B-8bit")
    parser.add_argument("--aligner-model", type=Path, help="Local model; default runtime-dir/models/Qwen3-ForcedAligner-0.6B-8bit")
    parser.add_argument("--runtime-dir", type=Path, help="External runtime/cache root; default YUNCONG_DATA_ROOT/runtime/asr")
    parser.add_argument("--source-video", type=Path, help="Optional local video for hash metadata only")
    parser.add_argument("--start", type=float, default=0.0)
    parser.add_argument("--duration", type=float)
    parser.add_argument("--chunk-max-seconds", type=float, default=180.0)
    parser.add_argument("--cut-search-seconds", type=float, default=5.0)
    parser.add_argument("--energy-window-seconds", type=float, default=0.1)
    parser.add_argument("--max-tokens", type=int, default=16384, help="Generation budget per external chunk")
    parser.add_argument("--prompt", default="", help="Exact native prompt, recorded without text rewriting")
    parser.add_argument("--run-name", help="Optional unique directory name; existing runs are never overwritten")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--check-dependencies", action="store_true", help="Inspect installed packages/platform without loading models")
    for name in ["qwen", "aligner"]:
        parser.add_argument(f"--{name}-revision", help="Caller-declared model revision, retained as metadata")
    args = vars(parser.parse_args(argv))
    check = args.pop("check_dependencies")
    if check:
        report, code = check_dependencies(args["ffmpeg"])
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return code
    if args["audio"] is None or args["output_root"] is None:
        parser.error("--audio and --output-root are required for inference")
    config = ASRConfig(**args)
    try:
        _, code = run_asr(config)
        return code
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
