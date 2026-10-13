#!/usr/bin/env python3
"""Incrementally archive new Yuncong YouTube Posts, rebuild decks, and emit a Feishu notice."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


PROJECT = Path(__file__).resolve().parents[2]
SCRIPTS = Path(__file__).resolve().parent
ARCHIVE = Path.home() / "云聪帖子全集"
CHANNEL = "https://www.youtube.com/channel/UCg9zI64pEB_3xwJPur6isiQ"
NODE = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node"
RUNTIME_PYTHON = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
NODE_MODULES = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules"


class MonitorError(RuntimeError):
    pass


def presentation_skill_dir():
    base = Path.home() / ".codex/plugins/cache/openai-primary-runtime/presentations"
    candidates = sorted(base.glob("*/skills/presentations"), reverse=True)
    for candidate in candidates:
        if (candidate / "container_tools/artifact_tool_utils.mjs").is_file():
            return candidate
    raise MonitorError("presentation_runtime_skill_not_found")


def run_json(command, *, env=None, timeout=3600):
    result = subprocess.run(command, capture_output=True, text=True, env=env, timeout=timeout)
    if result.returncode:
        raise MonitorError(f"command_failed:{Path(command[0]).name}:{result.stderr[-1600:]}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        start = result.stdout.find("{")
        if start >= 0:
            try:
                value, end = json.JSONDecoder().raw_decode(result.stdout, start)
                if not result.stdout[end:].strip():
                    return value
            except json.JSONDecodeError:
                pass
        raise MonitorError(f"command_returned_invalid_json:{Path(command[0]).name}") from exc


def stable_deck_name(month, month_json):
    if month != "未识别日期":
        return f"云聪帖子_{month}.pptx"
    reference = month_json.get("month_assignment_reference")
    try:
        anchor = datetime.fromisoformat(reference.replace("Z", "+00:00")).astimezone(ZoneInfo("Asia/Shanghai"))
        return f"云聪帖子_约{anchor.year - 1}年{anchor.month}月以前（推断）.pptx"
    except (AttributeError, ValueError, TypeError):
        return "云聪帖子_较早年份（推断；具体范围未确认）.pptx"


def build_deck(month):
    month_dir = ARCHIVE / month
    data = json.loads((month_dir / "posts.json").read_text(encoding="utf-8"))
    skill_dir = presentation_skill_dir()
    env = os.environ.copy()
    env["RUNTIME_NODE_MODULES"] = str(NODE_MODULES)
    result = run_json([
        str(NODE), str(SCRIPTS / "generate_monthly_posts_ppt.mjs"), str(month_dir),
        str(PROJECT), str(skill_dir), str(RUNTIME_PYTHON),
    ], env=env)
    final_path = Path(result.get("finalPath", ""))
    receipt_path = Path(result.get("receiptPath", ""))
    if not final_path.is_file() or not receipt_path.is_file():
        raise MonitorError(f"deck_artifact_missing:{month}")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    digest = hashlib.sha256(final_path.read_bytes()).hexdigest()
    if receipt.get("finalSha256") != digest or receipt.get("byteCount") != final_path.stat().st_size:
        raise MonitorError(f"deck_validation_receipt_mismatch:{month}")
    for section in (receipt.get("packageIntegrity", {}), receipt.get("presentationLayout", {})):
        if section.get("passed") is False or section.get("status") == "failed":
            raise MonitorError(f"deck_validation_failed:{month}")
    target = ARCHIVE / stable_deck_name(month, data)
    temporary = target.with_name(target.name + ".tmp")
    shutil.copyfile(final_path, temporary)
    temporary.replace(target)
    if month == "未识别日期":
        for old in ARCHIVE.glob("云聪帖子_约*年*月以前（推断）*.pptx"):
            if old != target:
                old.unlink()
    temp_root = PROJECT / "tmp/youtube_posts/monthly_ppt"
    for generated in (final_path.parent, receipt_path.parent):
        if generated.is_relative_to(temp_root) and generated.is_dir():
            shutil.rmtree(generated)
    return {"month": month, "path": str(target), "slide_count": result.get("slideCount")}


def format_notice(posts, decks):
    lines = [f"云聪 YouTube 新帖子：{len(posts)} 条"]
    for index, post in enumerate(posts, 1):
        raw_time = post.get("published_time_raw") or "发布时间未标注"
        link = post.get("url") or f"https://www.youtube.com/post/{post.get('post_id', '')}"
        body = " ".join(str(post.get("text") or "（无文字正文）").split())
        if len(body) > 520:
            body = body[:520].rstrip() + "…"
        lines.extend([f"{index}. {raw_time} · {post.get('post_id', 'ID未知')}", body, link])
    if decks:
        lines.append("已归档并更新 PPT：" + "、".join(Path(item["path"]).name for item in decks))
    return "\n".join(lines)


def retry_posts(post_ids):
    wanted = set(str(value) for value in post_ids)
    found = []
    months = []
    for posts_path in ARCHIVE.glob("*/posts.json"):
        data = json.loads(posts_path.read_text(encoding="utf-8"))
        matched = [post for post in data.get("posts", []) if str(post.get("post_id")) in wanted]
        if matched:
            found.extend(matched)
            months.append(posts_path.parent.name)
    if {str(post.get("post_id")) for post in found} != wanted:
        raise MonitorError("pending_post_records_missing_from_archive")
    found.sort(key=lambda post: int(post.get("published_timestamp") or 0), reverse=True)
    return found, sorted(set(months))


def write_state(path, value):
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def main():
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    lock_path = ARCHIVE / ".posts_monitor.lock"
    with lock_path.open("a", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("帖子监控上一轮仍在运行；本轮跳过。", file=sys.stderr)
            return 0
        state_path = ARCHIVE / "monitor_state.json"
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        pending = state.get("pending_post_ids", [])
        if pending:
            posts, months = retry_posts(pending)
        else:
            collection = run_json([
                sys.executable, str(SCRIPTS / "collect_posts_via_macmini.py"),
                "--channel-url", CHANNEL, "--archive-root", str(ARCHIVE),
                "--incremental", "--download-images",
            ], timeout=2400)
            if collection.get("state") == "no_new_posts":
                return 0
            posts = collection.get("new_posts") or []
            if not posts:
                raise MonitorError("collector_did_not_return_new_post_records")
            months = sorted({item["month"] for item in collection.get("months", [])})
        decks = [build_deck(month) for month in months]
        refreshed = run_json([
            sys.executable, str(SCRIPTS / "archive_posts_by_month.py"),
            "--archive-root", str(ARCHIVE), "--refresh-manifests",
        ])
        if not refreshed.get("updated_months"):
            raise MonitorError("archive_manifests_not_refreshed")
        state["pending_post_ids"] = []
        state["last_archived_and_notified_at"] = datetime.now().astimezone().isoformat()
        write_state(state_path, state)
        print(format_notice(posts, decks), flush=True)
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"云聪帖子监控失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
