"""Local closed-run storage checks for production subtitle exports."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

ID = re.compile(r"[A-Za-z0-9_-]{11}")
SHA = re.compile(r"[0-9a-f]{64}")
COMPLETED = {"processed_pending_review", "processed_auto", "complete", "completed"}


class ExportError(Exception):
    def __init__(self, code, **details):
        super().__init__(code)
        self.code, self.details = code, details


def now():
    return datetime.now(timezone.utc).isoformat()


def read_json(path):
    if Path(path).is_symlink():
        raise ExportError("symlink_rejected", path=str(path))
    with Path(path).open(encoding="utf-8") as stream:
        return json.load(stream)


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def fingerprint(path):
    stat = Path(path).stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
            "ctime_ns": stat.st_ctime_ns, "inode": stat.st_ino}


def local_path(root, relative):
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ExportError("relative_path_invalid", path=str(relative))
    root = Path(root).resolve()
    path = root
    for part in relative.parts:
        path /= part
        if path.is_symlink():
            raise ExportError("symlink_rejected", path=str(path))
    if not path.resolve().is_relative_to(root):
        raise ExportError("path_escape", path=str(path))
    return path


def inventory(directory):
    """Fingerprint every existing regular file; reject symlinks throughout."""
    directory = Path(directory)
    result = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ExportError("symlink_rejected", path=str(path))
        if path.is_file():
            result[path.relative_to(directory).as_posix()] = fingerprint(path)
    return result


def hash_inventory(directory, fingerprints, prior=None):
    prior = prior or {}
    old_stats = prior.get("source_fingerprints", {})
    old_hashes = prior.get("input_hashes", {})
    hashes = {}
    for name, stat in fingerprints.items():
        path = local_path(directory, name)
        cached = old_hashes.get(name)
        hashes[name] = cached if old_stats.get(name) == stat and isinstance(cached, str) and SHA.fullmatch(cached) else sha256_file(path)
        if fingerprint(path) != stat:
            raise ExportError("source_changed_during_hash", path=name)
    return hashes


def verify_declared(directory, expected, cache=None):
    if not isinstance(expected, dict) or not expected:
        raise ExportError("source_hashes_missing")
    measured = {}
    for name, digest in expected.items():
        if not isinstance(name, str) or not isinstance(digest, str) or not SHA.fullmatch(digest):
            raise ExportError("source_hash_invalid")
        path = local_path(directory, name)
        if not path.is_file():
            raise ExportError("source_artifact_missing", path=name)
        measured[name] = fingerprint(path)
    hashes = hash_inventory(directory, measured, cache)
    if hashes != expected:
        raise ExportError("source_hash_mismatch")
    return measured


def atomic_json(path, value):
    path = Path(path)
    if path.is_symlink():
        raise ExportError("symlink_rejected", path=str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".subtitle-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def select_current_run(root, ident, item, mirror=None):
    """Select only the current finished attempt, never a previous history item."""
    if not ID.fullmatch(ident):
        raise ExportError("invalid_video_id")
    if item.get("state", item.get("status")) not in COMPLETED or not item.get("attempt_finished_at"):
        return None
    declared = item.get("artifact_hashes")
    if not isinstance(declared, dict) or not declared:
        raise ExportError("source_hashes_missing")
    name = item.get("attempt_directory")
    if not isinstance(name, str) or not name:
        raise ExportError("attempt_directory_missing")
    record = (mirror or {}).get("attempts", {}).get(name, {})
    if mirror is not None:
        if record.get("state") != "synced" or record.get("issues"):
            return None
        if record.get("verified_hashes") != declared:
            raise ExportError("mirror_hash_contract_mismatch")
        relative = record.get("relative_directory")
    else:
        path = Path(name)
        if path.is_absolute():
            try:
                relative = path.relative_to(Path(root).resolve())
            except ValueError:
                raise ExportError("attempt_outside_job_root") from None
        else:
            relative = path
    attempt = local_path(root, relative)
    if ident not in attempt.relative_to(Path(root).resolve()).parts[:-1] or not re.fullmatch(r"attempt_[0-9]{3,}", attempt.name):
        raise ExportError("attempt_path_invalid")
    run = local_path(attempt, "raw/qwen_run")
    if not run.is_dir():
        raise ExportError("source_run_missing")
    return {"attempt_directory": attempt, "run_directory": run, "attempt_name": attempt.name,
            "source_state": item["state"], "attempt_finished_at": item["attempt_finished_at"],
            "declared_hashes": declared}
