#!/usr/bin/env python3
"""Inspect audio/ASR/PPT dependencies without creating outputs or calling models."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

sys.dont_write_bytecode = True
from tool_paths import MODULE_ROOT, PROJECT_ROOT, data_root, external_path


def inspect(args: argparse.Namespace) -> dict:
    checks = []

    def record(name, ok, detail):
        checks.append({"name": name, "passed": bool(ok), "detail": str(detail)})

    def executable(name, value):
        location = shutil.which(value) if value else None
        if not location:
            record(name, False, f"Executable unavailable: {value or 'unset'}")
            return None
        try:
            option = "-version" if name in ("ffmpeg", "ffprobe") else "--version"
            result = subprocess.run([location, option], capture_output=True, text=True, timeout=15)
            record(name, result.returncode == 0, (result.stdout or result.stderr).splitlines()[0])
            return location if result.returncode == 0 else None
        except (OSError, subprocess.SubprocessError, IndexError) as error:
            record(name, False, type(error).__name__)
            return None

    record("python", sys.version_info >= (3, 10), sys.version.split()[0])
    try:
        root = data_root()
        record("data_root", True, root)
    except ValueError as error:
        record("data_root", False, error)
        root = None
    executable("ffmpeg", args.ffmpeg)
    executable("ffprobe", args.ffprobe)

    if args.mode == "asr":
        python = executable("asr_python", args.asr_python or sys.executable)
        if python:
            try:
                result = subprocess.run(
                    [python, "-B", str(MODULE_ROOT / "scripts/asr_engine.py"),
                     "--check-dependencies", "--ffmpeg", args.ffmpeg],
                    capture_output=True, text=True, timeout=30,
                )
                value = json.loads(result.stdout)
                record("asr_packages", result.returncode == 0 and value.get("ready"),
                       json.dumps(value, ensure_ascii=False))
            except (OSError, subprocess.SubprocessError, ValueError) as error:
                record("asr_packages", False, type(error).__name__)
        models = args.models_dir or (root / "runtime/asr/models" if root else None)
        try:
            models = external_path(models) if models else None
        except ValueError as error:
            record("model_directory", False, error)
            models = None
        for name in ("Qwen3-ASR-1.7B-8bit", "Qwen3-ForcedAligner-0.6B-8bit"):
            path = models / name if models else None
            present = path and (path / "config.json").is_file() and any(path.glob("*.safetensors"))
            record(name, present, f"{path or 'unset'}; presence only, no weight loading/hash verification")

    return {"schema_version": "yuncong_environment_v1", "mode": args.mode,
            "project_root": str(PROJECT_ROOT), "module_root": str(MODULE_ROOT), "data_root": str(root) if root else None,
            "passed": all(c["passed"] for c in checks), "checks": checks,
            "model_calls": 0, "network_calls": 0,
            "scope": "Dependency preflight only; does not verify transcription accuracy or PPT playback"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("basic", "asr"), default="basic")
    parser.add_argument("--asr-python", help="Existing external inference interpreter; may point to a Mac mini checkout when run there")
    parser.add_argument("--models-dir", type=Path, help="Existing model parent directory; default: data root/runtime/asr/models")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--json", action="store_true", help="Print a machine-readable report to stdout")
    args = parser.parse_args()
    report = inspect(args)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"Project: {report['project_root']}")
        for check in report["checks"]:
            print(f"{'OK' if check['passed'] else 'MISSING'} {check['name']}: {check['detail']}")
        print("Environment preflight passed" if report["passed"] else "Environment preflight incomplete")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
