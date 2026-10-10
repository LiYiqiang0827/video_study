"""One portable entry point; heavyweight model imports stay opt-in."""
import argparse
import importlib.metadata
import json
import shutil
import subprocess
import sys
from pathlib import Path
from . import __version__
from .paths import platform_home, profile_path, model_cache

def doctor():
    packages = {}
    for name in ("psutil", "Pillow", "faster-whisper", "ctranslate2", "rapidocr-onnxruntime"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    gpu = None
    if shutil.which("nvidia-smi"):
        result = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,memory.free", "--format=csv,noheader"], capture_output=True, text=True, timeout=15)
        gpu = result.stdout.strip() if result.returncode == 0 else "query failed"
    result = {"version": __version__, "python": sys.executable,
        "package": str(Path(__file__).parent), "home": str(platform_home()),
        "profile": str(profile_path()), "profile_exists": profile_path().is_file(),
        "model_cache": str(model_cache()),
        "tools": {x: shutil.which(x) for x in ("ffmpeg", "ffprobe")},
        "dependencies": packages, "nvidia": gpu,
        "note": "Read-only inventory; no model loading, download or CUDA inference test."}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if all(result["tools"].values()) and packages["psutil"] and packages["Pillow"] else 1

def main():
    # Keep redirected Chinese/Japanese output readable for other projects.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Shared local media evidence: ingest, frames, verify, doctor")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("command", choices=["ingest", "frames", "verify", "doctor"])
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "--version"):
        parser.parse_args()
        return
    command = sys.argv[1]
    if command in ("ingest", "frames"):
        sys.argv = [sys.argv[0] + " " + command] + sys.argv[2:]
        if command == "ingest":
            from .ingest import main as run
        else:
            from .frame_review import main as run
        run()
    elif command == "doctor":
        parser.parse_args()
        raise SystemExit(doctor())
    elif command == "verify":
        check = argparse.ArgumentParser(description="Validate a completed extraction, its source and recorded outputs")
        check.add_argument("extraction", type=Path)
        args = check.parse_args(sys.argv[2:])
        from .evidence import load_evidence
        bundle = load_evidence(args.extraction)
        print(json.dumps({"valid": True, "schema_version": bundle["schema_version"],
            "source_sha256": bundle["media_asset"]["source_sha256"],
            "speech_segments": len(bundle["speech_segments"])}, ensure_ascii=False))
    else:
        parser.parse_args()
