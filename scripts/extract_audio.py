#!/usr/bin/env python3
"""Extract a complete local AAC track and a mono 16-kHz PCM WAV for ASR.

The original track is copied into M4A without transcoding. Both outputs retain
the full audio timeline. No authentication, downloads, VAD or silence deletion.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid
import wave

sys.dont_write_bytecode = True
from tool_paths import external_path


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024**2), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path, value):
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


def local_file(value):
    if "://" in str(value) or "\0" in str(value):
        raise ValueError("Only local filesystem paths are accepted")
    path = Path(value).expanduser().resolve(strict=True)
    if not path.is_file():
        raise ValueError("Source must be a local file")
    return path


def executable(value):
    if "://" in str(value) or "\0" in str(value):
        raise ValueError("Expected a local executable")
    resolved = shutil.which(os.path.expanduser(str(value)))
    if not resolved:
        raise ValueError(f"Executable unavailable: {value}")
    # Preserve the invoked virtualenv path: resolving its Python symlink to the
    # base interpreter would silently discard that environment's dependencies.
    return os.path.abspath(resolved)


def audio_stream(profile):
    stream = next((item for item in profile.get("streams", [])
                   if item.get("codec_type") == "audio"), None)
    if stream is None:
        raise ValueError("Source has no audio stream")
    return stream


def duration_seconds(profile, stream):
    value = float(stream.get("duration") or profile.get("format", {}).get("duration", "nan"))
    if not math.isfinite(value) or value <= 0:
        raise ValueError("Audio duration must be finite and positive")
    return value


def extract_audio(source, output_dir, *, ffmpeg="ffmpeg", ffprobe="ffprobe",
                  expected_sha256=None, expected_bytes=None,
                  expected_duration=None, command_runner=None):
    """Extract and verify audio; optional runner(stage, argv, timeout) supervises children.

    A supplied runner must write combined stdout/stderr to output_dir/stage.log
    and raise on failure. This lets batch workers keep stop/timeout supervision
    while sharing exactly the same extraction and validation implementation.
    Existing artifacts are never overwritten, including failed partial output.
    """
    source = local_file(source)
    if "://" in str(output_dir):
        raise ValueError("Output must be a local directory")
    directory = external_path(output_dir)
    ffmpeg, ffprobe = executable(ffmpeg), executable(ffprobe)
    targets = ["audio_original.m4a", "audio_asr_16k.wav", "audio_profile.json",
               "AUDIO_PROVENANCE.json", "audio_commands.json", "AUDIO_ERROR.json",
               "audio_probe.log", "audio_original.log", "audio_16k.log", "audio_original_probe.log"]
    directory.mkdir(parents=True, exist_ok=True)
    if any((directory / name).exists() for name in targets):
        raise FileExistsError("Audio extraction artifacts already exist; choose a new output directory")
    lock = directory / ".EXTRACT_RUNNING.lock"
    with lock.open("x", encoding="utf-8") as handle:
        json.dump({"pid": os.getpid(), "source": str(source)}, handle)
    commands = []
    began = time.monotonic()

    def run(stage, argv, timeout):
        commands.append({"stage": stage, "argv": [str(v) for v in argv],
                         "log": str(directory / f"{stage}.log")})
        atomic_json(directory / "audio_commands.json", commands)
        if command_runner is not None:
            command_runner(stage, argv, timeout)
        else:
            with (directory / f"{stage}.log").open("xb") as log:
                subprocess.run([str(v) for v in argv], stdin=subprocess.DEVNULL,
                               stdout=log, stderr=log, timeout=timeout, check=True)

    try:
        source_bytes, source_hash = source.stat().st_size, sha256_file(source)
        if expected_bytes is not None and source_bytes != expected_bytes:
            raise ValueError("Source bytes differ from frozen metadata")
        if expected_sha256 is not None and source_hash != expected_sha256:
            raise ValueError("Source SHA-256 differs from frozen metadata")
        run("audio_probe", [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", source], 120)
        profile = json.loads((directory / "audio_probe.log").read_text(encoding="utf-8"))
        audio = audio_stream(profile)
        if audio.get("codec_name") != "aac":
            raise ValueError("Original M4A stream-copy requires an AAC source track")
        origin = float(audio.get("start_time", "nan"))
        if not math.isfinite(origin) or abs(origin) > .001:
            raise ValueError("Audio origin must be verified at zero; other origins require review")
        measured = duration_seconds(profile, audio)
        if expected_duration is not None and (not math.isfinite(expected_duration) or expected_duration <= 0
                or abs(measured - expected_duration) > max(2, expected_duration * .001)):
            raise ValueError("Source duration differs from frozen metadata")
        original, wav = directory / "audio_original.m4a", directory / "audio_asr_16k.wav"
        common = [ffmpeg, "-hide_banner", "-v", "error", "-nostdin", "-n", "-xerror",
                  "-i", source, "-map", "0:a:0", "-vn"]
        run("audio_original", common + ["-c:a", "copy", original], max(180, measured))
        run("audio_16k", common + ["-c:a", "pcm_s16le", "-ar", "16000", "-ac", "1", wav], max(180, measured))
        run("audio_original_probe", [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", original], 120)
        original_profile = json.loads((directory / "audio_original_probe.log").read_text(encoding="utf-8"))
        original_stream = audio_stream(original_profile)
        if any(original_stream.get(key) != audio.get(key) for key in ("codec_name", "sample_rate", "channels")):
            raise ValueError("Original audio track profile changed")
        original_duration = duration_seconds(original_profile, original_stream)
        if abs(original_duration - measured) > .2:
            raise ValueError("Extracted original audio duration differs from source")
        with wave.open(str(wav), "rb") as handle:
            count, rate, channels, width = handle.getnframes(), handle.getframerate(), handle.getnchannels(), handle.getsampwidth()
        if (rate, channels, width) != (16000, 1, 2) or abs(count / rate - measured) > .2:
            raise ValueError("Derived WAV profile/duration differs from source")
        if source.stat().st_size != source_bytes or sha256_file(source) != source_hash:
            raise ValueError("Source changed while extracting audio")
        hashes = {path.name: sha256_file(path) for path in (original, wav)}
        record = {"schema_version": 1, "source_video": str(source), "source_sha256": source_hash,
                  "source_bytes": source_bytes, "source_profile": profile,
                  "source_audio_start_seconds": origin, "duration_seconds": measured,
                  "original_audio": original.name, "original_audio_profile": original_profile,
                  "original_audio_duration_seconds": original_duration,
                  "audio_sha256": hashes, "audio_bytes": {p.name: p.stat().st_size for p in (original, wav)},
                  "wav": {"sample_rate": rate, "channels": channels, "sample_width_bytes": width,
                          "sample_count": count, "duration_seconds": count / rate},
                  "silence_removed": False, "timeline": "full source audio from verified time zero; no concatenation or silence deletion",
                  "original_audio_processing": "AAC packet stream copy to M4A; no audio transcoding",
                  "asr_audio_processing": "complete audio decode to 16-kHz mono PCM16 WAV",
                  "commands": commands, "tool_paths": {"ffmpeg": ffmpeg, "ffprobe": ffprobe},
                  "extractor_sha256": sha256_file(Path(__file__).resolve()),
                  "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                  "processing_seconds": time.monotonic() - began, "state": "complete"}
        atomic_json(directory / "audio_profile.json", record)
        atomic_json(directory / "AUDIO_PROVENANCE.json", record)
        return wav, original, record
    except BaseException as error:
        atomic_json(directory / "AUDIO_ERROR.json", {"state": "failed", "type": type(error).__name__,
                                                     "message": str(error), "source_video": str(source)})
        raise
    finally:
        lock.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", type=Path, required=True, help="Local video with an AAC track")
    parser.add_argument("--output-dir", type=Path, required=True, help="New artifact directory outside the code project")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    args = parser.parse_args(argv)
    try:
        wav, original, record = extract_audio(args.video, args.output_dir, ffmpeg=args.ffmpeg, ffprobe=args.ffprobe)
        print(json.dumps({"state": "complete", "audio_original": str(original), "audio_asr": str(wav),
                          "duration_seconds": record["wav"]["duration_seconds"]}, ensure_ascii=False))
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
