from pathlib import Path
import shutil

legacy = Path(r'C:\Users\HarryWu_IOKI\Documents\推し活\media_platform')
root = Path(r'C:\Users\HarryWu_IOKI\Documents\SharedTools\media-platform')
src = root / 'src/media_platform'
src.mkdir(parents=True, exist_ok=True)
for name in ['ingest.py', 'evidence.py', 'frame_review.py']:
    shutil.copy2(legacy / name, src / name)
(root / 'tests').mkdir(exist_ok=True)
shutil.copy2(legacy / 'test_frame_review.py', root / 'tests/test_frame_review.py')
(src / '__init__.py').write_text('"""Project-independent local media evidence core."""\nSCHEMA_VERSION = "1.0"\n__version__ = "2.1.0"\n', encoding='utf-8')
(src / '__main__.py').write_text('from .cli import main\nmain()\n', encoding='utf-8')
(src / 'paths.py').write_text('''"""Runtime configuration belongs to the user, never to a project/package."""
import os
from pathlib import Path

def platform_home():
    override = os.environ.get("MEDIA_PLATFORM_HOME")
    if override:
        return Path(override).expanduser().resolve()
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local/share"))
    return base / "media-platform"

def profile_path():
    override = os.environ.get("MEDIA_PLATFORM_PROFILE")
    return Path(override).expanduser() if override else platform_home() / "device_profile.json"

def model_cache():
    override = os.environ.get("MEDIA_PLATFORM_MODEL_CACHE")
    return Path(override).expanduser() if override else platform_home() / "models"
''', encoding='utf-8')
p = src / 'ingest.py'
s = p.read_text(encoding='utf-8')
s = s.replace('from pathlib import Path\n', 'from pathlib import Path\nfrom media_platform.paths import model_cache, profile_path as shared_profile_path\n', 1)
s = s.replace('    local = Path.home() / ".cache" / "annx" / "models" / "faster-whisper-medium" / MEDIUM_REVISION', '''    local = model_cache() / "faster-whisper-medium" / MEDIUM_REVISION
    legacy_cache = Path.home() / ".cache" / "annx" / "models" / "faster-whisper-medium" / MEDIUM_REVISION
    if not local.exists() and legacy_cache.is_dir():
        local = legacy_cache''')
s = s.replace('allow_patterns=["*.json", "*.txt", "model.bin", "vocabulary.*"]))', 'allow_patterns=["*.json", "*.txt", "model.bin", "vocabulary.*"],\n                                      cache_dir=str(model_cache() / "huggingface")))')
s = s.replace('profile_path = args.profile or default_profile or Path(__file__).with_name("device_profile.json")', 'profile_path = args.profile or default_profile or shared_profile_path()')
# Preserve extraction schema/version and the explicit Japanese legacy default.
p.write_text(s, encoding='utf-8')
(src / 'cli.py').write_text('''"""One portable entry point; heavyweight model imports stay opt-in."""
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
''', encoding='utf-8')
(root / 'pyproject.toml').write_text('''[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "local-media-platform"
version = "2.1.0"
description = "Project-independent local speech and frame evidence with authenticated resume"
requires-python = ">=3.10"
dependencies = ["Pillow>=11,<13", "psutil>=7,<8"]

[project.optional-dependencies]
asr = ["faster-whisper>=1.2,<2"]
ocr = ["rapidocr-onnxruntime>=1.4,<2", "numpy>=1.26,<3"]
gpu = ["nvidia-cublas-cu12==12.6.4.1; sys_platform == 'win32'", "nvidia-cudnn-cu12==9.10.2.21; sys_platform == 'win32'"]

[project.scripts]
media-platform = "media_platform.cli:main"

[tool.setuptools.packages.find]
where = ["src"]
''', encoding='utf-8')
(root / '.gitignore').write_text('__pycache__/\n*.egg-info/\nbuild/\ndist/\n.venv/\n', encoding='utf-8')
runtime = Path.home() / 'AppData/Local/media-platform'
runtime.mkdir(parents=True, exist_ok=True)
if not (runtime / 'device_profile.json').exists():
    shutil.copy2(legacy / 'device_profile.json', runtime / 'device_profile.json')
print(root)
