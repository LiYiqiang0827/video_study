#!/usr/bin/env python3
"""Run the Posts page collector on the authenticated Mac mini and sync verified output."""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import shlex
import shutil
import subprocess
import sys
import uuid
from pathlib import Path, PurePosixPath


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ARCHIVE_ROOT = Path.home() / "云聪帖子全集"
COLLECTOR = Path(__file__).resolve().with_name("collect_posts.py")
REMOTE_ROOT = "/Users/nickzhang/YuncongVideoAnalysis/posts_tools"
REMOTE_SCRIPT = REMOTE_ROOT + "/project_scripts/collect_posts.py"
REMOTE_PYTHON = "/Users/nickzhang/YuncongVideoAnalysis/download_tools/venv/bin/python"
REMOTE_COOKIE = "/Users/nickzhang/YuncongVideoAnalysis/local_auth/www.youtube.com_cookies.txt"
REMOTE_PATH = "/opt/homebrew/bin:/Users/nickzhang/.local/bin:/usr/bin:/bin"
STATE_NAME = "monitor_state.json"


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


def archived_post_ids(archive_root):
    ids = set()
    for posts_path in archive_root.glob("*/posts.json"):
        if posts_path.is_symlink():
            raise CollectionError("archive_posts_json_must_not_be_symlink")
        data = json.loads(posts_path.read_text(encoding="utf-8"))
        for post in data.get("posts", []):
            if post.get("post_id"):
                ids.add(str(post["post_id"]))
    state_path = archive_root / STATE_NAME
    if state_path.is_symlink():
        raise CollectionError("monitor_state_must_not_be_symlink")
    if state_path.is_file():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        ids.update(str(value) for value in state.get("known_post_ids", []) if value)
    return ids


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel-url", required=True)
    parser.add_argument("--archive-root", type=Path, default=ARCHIVE_ROOT, help="Monthly archive root on this Mac")
    parser.add_argument("--max-posts", type=int)
    parser.add_argument("--download-images", action="store_true")
    parser.add_argument("--incremental", action="store_true", help="Stop at the first already archived post ID")
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
    archive_root = args.archive_root.expanduser().resolve()
    if archive_root != ARCHIVE_ROOT.resolve():
        parser.error(f"Archive root must be {ARCHIVE_ROOT}")

    job_id = uuid.uuid4().hex
    remote_job = f"{REMOTE_ROOT}/jobs/{job_id}"
    remote_output = remote_job + "/snapshot"
    local_stage = PROJECT_ROOT / "tmp" / "youtube_posts" / job_id
    remote_script_tmp = REMOTE_SCRIPT + "." + job_id + ".tmp"
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", args.ssh_host]
    scp = ["scp", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    remote_cookie = Path(args.remote_cookie_file)
    try:
        known_ids = archived_post_ids(archive_root) if args.incremental else set()
        if args.incremental and not known_ids:
            raise CollectionError("incremental_archive_has_no_known_post_ids_refusing_unbounded_collection")
        local_stage.mkdir(parents=True, mode=0o700)
        setup = "mkdir -p " + shlex.quote(str(PurePosixPath(REMOTE_SCRIPT).parent)) + " " + shlex.quote(str(PurePosixPath(REMOTE_ROOT + "/jobs"))) + " " + shlex.quote(remote_job)
        run(ssh + [setup], timeout=30)
        run(scp + [str(COLLECTOR), f"{args.ssh_host}:{remote_script_tmp}"], timeout=60)
        promote = f"mv {shlex.quote(remote_script_tmp)} {shlex.quote(REMOTE_SCRIPT)}"
        run(ssh + [promote], timeout=30)

        remote_args = ["env", f"PATH={REMOTE_PATH}", REMOTE_PYTHON, REMOTE_SCRIPT,
                       "--channel-url", args.channel_url, "--output-dir", remote_output,
                       "--cookie-file", str(remote_cookie), "--proxy", args.remote_proxy, "--impersonate-chrome"]
        if args.incremental:
            local_ids = local_stage.with_name(job_id + ".known_post_ids.json")
            local_ids.write_text(json.dumps(sorted(known_ids), ensure_ascii=False), encoding="utf-8")
            remote_ids = remote_job + "/known_post_ids.json"
            run(scp + [str(local_ids), f"{args.ssh_host}:{remote_ids}"], timeout=60)
            remote_args += ["--known-post-ids-file", remote_ids]
        if args.max_posts is not None:
            remote_args += ["--max-posts", str(args.max_posts)]
        if args.download_images:
            remote_args.append("--download-images")
        remote_command = shlex.join(remote_args)
        run(ssh + [remote_command], timeout=args.timeout)
        if args.incremental:
            local_ids.unlink(missing_ok=True)
        run(scp + ["-r", f"{args.ssh_host}:{remote_output}/.", str(local_stage) + "/"], timeout=args.timeout)

        manifest = verify_manifest(local_stage)
        if not (local_stage / "posts.json").is_file():
            raise CollectionError("posts_snapshot_missing")
        fetched = json.loads((local_stage / "posts.json").read_text(encoding="utf-8"))
        new_posts = fetched.get("posts", [])
        if args.incremental and not new_posts:
            print(json.dumps({"state": "no_new_posts", "post_count": 0,
                              "known_post_count": len(known_ids), "archive_root": str(archive_root)}, ensure_ascii=False))
            return 0
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from archive_posts_by_month import archive_snapshot
        result = archive_snapshot(local_stage, archive_root)
        if args.incremental:
            known_ids.update(str(post["post_id"]) for post in new_posts if post.get("post_id"))
            state = {"schema_version": 1, "channel_url": args.channel_url,
                     "known_post_ids": sorted(known_ids),
                     "last_successful_run_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                     "last_latest_post_id": new_posts[0].get("post_id"),
                     "last_new_post_ids": [post.get("post_id") for post in new_posts],
                     "pending_post_ids": [post.get("post_id") for post in new_posts]}
            atomic_json(archive_root / STATE_NAME, state)
        print(json.dumps({"state": "collected_and_archived_by_month", "post_count": result["post_count"],
                          "image_download_failures": result["image_download_failures"],
                          "archive_root": str(archive_root), "months": result["months"],
                          "new_posts": new_posts if args.incremental else [],
                          "known_post_count": len(known_ids) if args.incremental else None,
                          "source_manifest_files": len(manifest["files"])}, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"state": "failed", "error": type(exc).__name__,
                          "details": str(exc) if isinstance(exc, CollectionError) else "see local SSH/SCP diagnostics",
                          "local_temporary_path": str(local_stage) if local_stage.exists() else None}, ensure_ascii=False), file=sys.stderr)
        return 1
    finally:
        if local_stage.exists():
            shutil.rmtree(local_stage, ignore_errors=True)
        if "local_ids" in locals():
            local_ids.unlink(missing_ok=True)
        cleanup = "rm -rf " + shlex.quote(str(PurePosixPath(remote_job))) + "; rm -f " + shlex.quote(remote_script_tmp)
        try:
            subprocess.run(ssh + [cleanup], capture_output=True, timeout=30)
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
