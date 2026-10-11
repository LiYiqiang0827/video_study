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

    if args.mode == "ppt":
        executable("ppt_node", os.environ.get("RUNTIME_NODE") or shutil.which("node"))
        requirements = {
            "RUNTIME_NODE_MODULES": [
                "@oai/artifact-tool/dist/artifact_tool.mjs",
                "@oai/artifact-tool/node_modules/skia-canvas/lib/index.mjs",
            ],
            "PRESENTATIONS_SKILL_DIR": ["container_tools/artifact_tool_utils.mjs"],
        }
        for name, files in requirements.items():
            value = os.environ.get(name)
            paths = [Path(value).expanduser() / f for f in files] if value else []
            record(name, bool(paths) and all(p.is_file() for p in paths),
                   value or "Environment variable unset; see references/runtime.md")
        python = executable("ppt_python", os.environ.get("RUNTIME_PYTHON"))
        if python:
            probe = "import importlib.util,json; print(json.dumps({m:importlib.util.find_spec(m) is not None for m in ('lxml','PIL')}))"
            try:
                result = subprocess.run([python, "-B", "-c", probe], capture_output=True, text=True, timeout=15)
                value = json.loads(result.stdout)
                record("ppt_python_packages", result.returncode == 0 and all(value.values()), value)
            except (OSError, subprocess.SubprocessError, ValueError) as error:
                record("ppt_python_packages", False, type(error).__name__)

    return {"schema_version": "yuncong_environment_v1", "mode": args.mode,
            "project_root": str(PROJECT_ROOT), "module_root": str(MODULE_ROOT), "data_root": str(root) if root else None,
            "passed": all(c["passed"] for c in checks), "checks": checks,
            "model_calls": 0, "network_calls": 0,
            "scope": "Dependency preflight only; does not verify transcription accuracy or PPT playback"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("basic", "ppt"), default="basic")
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
