#!/usr/bin/env python3
"""Run the Posts page collector on the authenticated Mac mini and sync verified output."""
from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import shutil
import subprocess
import sys
import uuid
from pathlib import Path, PurePosixPath


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ARCHIVE_ROOT = Path.home() / "云聪视频全集" / "youtube_posts"
COLLECTOR = Path(__file__).resolve().with_name("collect_posts.py")
REMOTE_ROOT = "/Users/nickzhang/YuncongVideoAnalysis/posts_tools"
REMOTE_SCRIPT = REMOTE_ROOT + "/project_scripts/collect_posts.py"
REMOTE_PYTHON = "/Users/nickzhang/YuncongVideoAnalysis/download_tools/venv/bin/python"
REMOTE_COOKIE = "/Users/nickzhang/YuncongVideoAnalysis/local_auth/www.youtube.com_cookies.txt"
REMOTE_PATH = "/opt/homebrew/bin:/Users/nickzhang/.local/bin:/usr/bin:/bin"


class CollectionError(RuntimeError):
    pass


def run(command, *, input_data=None, timeout=1200):
    result = subprocess.run(command, input=input_data, capture_output=True, timeout=timeout)
    if result.returncode:
        raise CollectionError("remote_collection_or_sync_failed")
    return result.stdout


def verify_manifest(folder):
    if any(path.is_symlink() for path in folder.rglob("*")):
        raise CollectionError("symlinks_not_allowed_in_synced_output")
    manifest_path = folder / "MANIFEST.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise CollectionError("manifest_missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1 or not isinstance(manifest.get("files"), list):
        raise CollectionError("manifest_invalid")
    seen = set()
    for entry in manifest["files"]:
        rel = PurePosixPath(entry.get("path", ""))
        if rel.is_absolute() or ".." in rel.parts or not rel.parts or str(rel) in seen or str(rel) == "MANIFEST.json":
            raise CollectionError("manifest_path_invalid")
        seen.add(str(rel))
        path = folder.joinpath(*rel.parts)
        if not path.is_file() or path.is_symlink():
            raise CollectionError("manifest_file_missing")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if path.stat().st_size != entry.get("bytes") or digest != entry.get("sha256"):
            raise CollectionError("manifest_hash_mismatch")
    actual = {str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file() and p.name != "MANIFEST.json"}
    if actual != seen:
        raise CollectionError("unmanifested_files_present")
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel-url", required=True)
    parser.add_argument("--output-dir", required=True, type=Path, help="New formal archive directory on this Mac")
    parser.add_argument("--max-posts", type=int)
    parser.add_argument("--download-images", action="store_true")
    parser.add_argument("--ssh-host", default="macmini")
    parser.add_argument("--remote-cookie-file", default=REMOTE_COOKIE, help=argparse.SUPPRESS)
    parser.add_argument("--remote-proxy", default="socks5h://127.0.0.1:7890")
    parser.add_argument("--timeout", type=int, default=1200)
    args = parser.parse_args(argv)
    if not args.ssh_host or args.ssh_host.startswith("-") or not all(c.isalnum() or c in "_.@-" for c in args.ssh_host):
        parser.error("Invalid SSH host alias")
    if args.max_posts is not None and args.max_posts < 1:
        parser.error("--max-posts must be positive")
    if args.timeout < 1:
        parser.error("--timeout must be positive")
    output = args.output_dir.expanduser().resolve()
    try:
        relative_output = output.relative_to(ARCHIVE_ROOT.resolve())
    except ValueError:
        parser.error(f"Output must be inside {ARCHIVE_ROOT}")
    if not relative_output.parts:
        parser.error("Output must be a new per-channel/per-run subdirectory")
    if output.exists():
        parser.error("Output directory already exists; choose a fresh archive path")

    job_id = uuid.uuid4().hex
    remote_job = f"{REMOTE_ROOT}/jobs/{job_id}"
    local_stage = PROJECT_ROOT / "tmp" / "youtube_posts" / job_id
    remote_script_tmp = REMOTE_SCRIPT + "." + job_id + ".tmp"
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", args.ssh_host]
    scp = ["scp", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    remote_cookie = Path(args.remote_cookie_file)
    output_created = False
    try:
        local_stage.mkdir(parents=True, mode=0o700)
        setup = "mkdir -p " + shlex.quote(str(PurePosixPath(REMOTE_SCRIPT).parent)) + " " + shlex.quote(str(PurePosixPath(REMOTE_ROOT + "/jobs")))
        run(ssh + [setup], timeout=30)
        run(scp + [str(COLLECTOR), f"{args.ssh_host}:{remote_script_tmp}"], timeout=60)
        promote = f"mv {shlex.quote(remote_script_tmp)} {shlex.quote(REMOTE_SCRIPT)}"
        run(ssh + [promote], timeout=30)

        remote_args = ["env", f"PATH={REMOTE_PATH}", REMOTE_PYTHON, REMOTE_SCRIPT,
                       "--channel-url", args.channel_url, "--output-dir", remote_job,
                       "--cookie-file", str(remote_cookie), "--proxy", args.remote_proxy, "--impersonate-chrome"]
        if args.max_posts is not None:
            remote_args += ["--max-posts", str(args.max_posts)]
        if args.download_images:
            remote_args.append("--download-images")
        remote_command = shlex.join(remote_args)
        run(ssh + [remote_command], timeout=args.timeout)
        run(scp + ["-r", f"{args.ssh_host}:{remote_job}/.", str(local_stage) + "/"], timeout=args.timeout)

        manifest = verify_manifest(local_stage)
        if not (local_stage / "posts.json").is_file():
            raise CollectionError("posts_snapshot_missing")
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(local_stage, output)
        output_created = True
        copied = verify_manifest(output)
        if copied != manifest:
            raise CollectionError("archive_copy_manifest_changed")
        posts = json.loads((output / "posts.json").read_text(encoding="utf-8"))
        print(json.dumps({"state": "collected_and_synced", "post_count": posts.get("post_count"),
                          "image_download_failures": posts.get("image_download_failures", 0),
                          "output_dir": str(output), "manifest_files": len(manifest["files"]),
                          "manifest_sha256": hashlib.sha256((output / "MANIFEST.json").read_bytes()).hexdigest()}, ensure_ascii=False))
        return 0
    except Exception as exc:
        if output_created and output.exists():
            shutil.rmtree(output, ignore_errors=True)
        print(json.dumps({"state": "failed", "error": type(exc).__name__,
                          "details": str(exc) if isinstance(exc, CollectionError) else "see local SSH/SCP diagnostics",
                          "local_temporary_path": str(local_stage) if local_stage.exists() else None}, ensure_ascii=False), file=sys.stderr)
        return 1
    finally:
        if local_stage.exists():
            shutil.rmtree(local_stage, ignore_errors=True)
        cleanup = "rm -rf " + shlex.quote(str(PurePosixPath(remote_job))) + "; rm -f " + shlex.quote(remote_script_tmp)
        try:
            subprocess.run(ssh + [cleanup], capture_output=True, timeout=30)
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
