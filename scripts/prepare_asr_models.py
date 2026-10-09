#!/usr/bin/env python3
"""Prepare public, revision-pinned Qwen ASR/ForcedAligner models outside this project.

Downloads use public Hugging Face files without saved authentication. --check
validates existing model manifests and hashes without network access or loading.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid

sys.dont_write_bytecode = True
from tool_paths import data_root, external_path

MODELS = {
    "qwen": {"repo": "mlx-community/Qwen3-ASR-1.7B-8bit",
             "revision": "a8379a2e2f9e313c9292cdf1af4055ab56d50d55"},
    "aligner": {"repo": "mlx-community/Qwen3-ForcedAligner-0.6B-8bit",
                "revision": "0e1a68e91d815300c7c9754b2a7639378b23db15"},
}


def atomic_json(path, obj):
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(obj, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024**2), b""):
            result.update(block)
    return result.hexdigest()


def included(name):
    return Path(name).name == name and name not in {".", ".."} and (
        name.endswith((".json", ".safetensors", ".model", ".tiktoken", ".txt", ".jinja"))
        or name in {"LICENSE", "README.md"})


def metadata(repo, revision, proxy):
    handler = urllib.request.ProxyHandler({"https": proxy, "http": proxy} if proxy else {})
    opener = urllib.request.build_opener(handler)
    url = "https://huggingface.co/api/models/" + repo + "/revision/" + revision + "?blobs=true"
    for attempt in range(3):
        try:
            with opener.open(url, timeout=30) as response:
                result = json.load(response)
            if result.get("sha") != revision:
                raise RuntimeError("model_revision_mismatch")
            return result
        except Exception:
            if attempt == 2:
                raise RuntimeError("public_model_metadata_unavailable") from None
            time.sleep(2)


def validate_manifest(manifest, specification):
    if manifest.get("repo") != specification["repo"] or manifest.get("revision") != specification["revision"]:
        raise RuntimeError("model_identity_or_revision_mismatch")
    files = manifest.get("files")
    if not isinstance(files, list) or not files or not any(f.get("name", "").endswith(".safetensors") for f in files):
        raise RuntimeError("missing_safe_weights")
    names = [f.get("name") for f in files]
    if any(not isinstance(name, str) or not included(name) for name in names) or len(set(names)) != len(names):
        raise RuntimeError("invalid_model_filename")
    if "config.json" not in names:
        raise RuntimeError("missing_model_config")


def verify_file(path, item):
    if not path.is_file():
        raise RuntimeError("model_file_missing")
    if item.get("size") is not None and path.stat().st_size != item["size"]:
        raise RuntimeError("model_size_mismatch")
    actual = digest(path)
    if item.get("expected_sha256") and actual != item["expected_sha256"]:
        raise RuntimeError("model_hash_mismatch")
    if item.get("actual_sha256") and actual != item["actual_sha256"]:
        raise RuntimeError("previous_model_hash_mismatch")
    return actual


def check_models(models_dir, specifications):
    results = []
    for specification in specifications:
        local = models_dir / specification["repo"].split("/")[-1]
        entry = {**specification, "path": str(local)}
        try:
            manifest = json.loads((local / "MODEL_MANIFEST.json").read_text(encoding="utf-8"))
            validate_manifest(manifest, specification)
            for item in manifest["files"]:
                if not item.get("actual_sha256"):
                    raise RuntimeError("model_manifest_not_completed")
                verify_file(local / item["name"], item)
            entry.update(status="verified", file_count=len(manifest["files"]))
        except Exception as error:
            entry.update(status="failed", error_category=str(error) if isinstance(error, RuntimeError) else type(error).__name__)
        results.append(entry)
    return {"status": "verified" if all(r["status"] == "verified" for r in results) else "failed", "models": results}


def prepare(models_dir, cache_dir, status_path, specifications, *, proxy=None,
            curl="curl", limit_rate="8M", min_free_gb=15):
    models_dir, cache_dir, status_path = (external_path(path) for path in (models_dir, cache_dir, status_path))
    for path in (models_dir, cache_dir, status_path.parent):
        path.mkdir(parents=True, exist_ok=True)
    curl = shutil.which(curl)
    if not curl:
        raise RuntimeError("curl_unavailable")
    state = {"status": "running", "pid": os.getpid(), "started_at": time.time(),
             "models_dir": str(models_dir), "cache_dir": str(cache_dir), "models": []}
    with (models_dir / ".prepare_models.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another_model_preparation_is_running") from None
        try:
            atomic_json(status_path, state)
            if min(shutil.disk_usage(models_dir).free, shutil.disk_usage(cache_dir).free) < min_free_gb * 1024**3:
                raise RuntimeError("insufficient_free_disk")
            for specification in specifications:
                repo, revision = specification["repo"], specification["revision"]
                local = models_dir / repo.split("/")[-1]
                local.mkdir(parents=True, exist_ok=True)
                manifest_path = local / "MODEL_MANIFEST.json"
                if manifest_path.exists():
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                else:
                    info = metadata(repo, revision, proxy)
                    manifest = {"repo": repo, "revision": revision, "authentication": "none_public",
                                "files": [{"name": f["rfilename"], "size": f.get("size"),
                                           "expected_sha256": (f.get("lfs") or {}).get("sha256")}
                                          for f in info.get("siblings", []) if included(f["rfilename"])],
                                "created_at": time.time()}
                    validate_manifest(manifest, specification)
                    atomic_json(manifest_path, manifest)
                validate_manifest(manifest, specification)
                entry = {**specification, "path": str(local), "status": "running", "files_completed": 0}
                state["models"].append(entry)
                for item in manifest["files"]:
                    name, target = item["name"], local / item["name"]
                    entry["active_file"] = name
                    state["updated_at"] = time.time()
                    atomic_json(status_path, state)
                    if target.exists():
                        actual = verify_file(target, item)
                    else:
                        parts = cache_dir / repo.split("/")[-1] / revision
                        parts.mkdir(parents=True, exist_ok=True)
                        part = parts / (name + ".part")
                        url = "https://huggingface.co/" + repo + "/resolve/" + revision + "/" + urllib.parse.quote(name, safe="")
                        command = [curl, "--disable", "--fail", "--silent", "--location",
                                   "--connect-timeout", "20", "--max-time", "1200", "--retry", "2",
                                   "--retry-delay", "2", "--limit-rate", limit_rate,
                                   "--continue-at", "-", "--output", str(part)]
                        # Explicitly suppress inherited proxy settings unless one is configured.
                        command += ["--proxy", proxy or "", url]
                        rc = None
                        for attempt in range(3):
                            result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                                    stderr=subprocess.DEVNULL, timeout=1260)
                            rc = result.returncode
                            if rc == 0:
                                break
                            if rc in (51, 60, 77):
                                raise RuntimeError("model_certificate_verification_failed")
                            time.sleep(2)
                        if rc:
                            raise RuntimeError("model_transfer_failed_code_" + str(rc))
                        actual = verify_file(part, item)
                        # Copy to a destination-local temporary file, then expose without replacement.
                        temporary = local / ("." + name + ".verified." + uuid.uuid4().hex)
                        try:
                            with part.open("rb") as src, temporary.open("xb") as dst:
                                shutil.copyfileobj(src, dst, 4 * 1024**2)
                                dst.flush()
                                os.fsync(dst.fileno())
                            os.link(temporary, target)
                        finally:
                            temporary.unlink(missing_ok=True)
                        part.unlink()
                    item["actual_sha256"] = actual
                    entry["files_completed"] += 1
                    atomic_json(manifest_path, manifest)
                    atomic_json(status_path, state)
                entry["status"] = "complete"
                entry.pop("active_file", None)
                atomic_json(status_path, state)
            state.update(status="complete", updated_at=time.time())
            atomic_json(status_path, state)
            return state
        except BaseException as error:
            state.update(status="interrupted" if isinstance(error, KeyboardInterrupt) else "failed",
                         error_category=str(error) if isinstance(error, RuntimeError) else type(error).__name__,
                         updated_at=time.time())
            atomic_json(status_path, state)
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, help="Default YUNCONG_DATA_ROOT/runtime/asr")
    parser.add_argument("--models-dir", type=Path, help="Default runtime-dir/models")
    parser.add_argument("--cache-dir", type=Path, help="Default runtime-dir/cache/model_downloads")
    parser.add_argument("--status-file", type=Path, help="Default runtime-dir/model_preparation_status.json")
    parser.add_argument("--model", choices=["all", "qwen", "aligner"], default="all")
    parser.add_argument("--qwen-revision", default=MODELS["qwen"]["revision"])
    parser.add_argument("--aligner-revision", default=MODELS["aligner"]["revision"])
    parser.add_argument("--proxy", help="Optional HTTP(S) proxy; no saved account authentication")
    parser.add_argument("--curl", default="curl")
    parser.add_argument("--limit-rate", default="8M")
    parser.add_argument("--min-free-gb", type=float, default=15)
    parser.add_argument("--check", action="store_true", help="Hash-check existing models; no downloads")
    parser.add_argument("--status", action="store_true", help="Read existing status; no downloads")
    parser.add_argument("--plan", action="store_true", help="Print resolved paths/revisions; no downloads")
    args = parser.parse_args(argv)
    try:
        runtime = external_path(args.runtime_dir or data_root() / "runtime" / "asr")
        models = external_path(args.models_dir or runtime / "models")
        cache = external_path(args.cache_dir or runtime / "cache" / "model_downloads")
        status = external_path(args.status_file or runtime / "model_preparation_status.json")
        specifications = [{**MODELS[name], "revision": getattr(args, name + "_revision")}
                          for name in MODELS if args.model in {"all", name}]
        if any(not re.fullmatch(r"[a-f0-9]{40}", s["revision"]) for s in specifications):
            raise ValueError("Model revisions must be immutable 40-character commit hashes")
        if not math.isfinite(args.min_free_gb) or args.min_free_gb < 0:
            raise ValueError("min-free-gb must be nonnegative")
        if args.plan:
            report = {"models_dir": str(models), "cache_dir": str(cache), "status_file": str(status), "models": specifications}
        elif args.status:
            report = json.loads(status.read_text(encoding="utf-8"))
        elif args.check:
            report = check_models(models, specifications)
        else:
            report = prepare(models, cache, status, specifications, proxy=args.proxy, curl=args.curl,
                             limit_rate=args.limit_rate, min_free_gb=args.min_free_gb)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if report.get("status") == "failed" else 0
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
