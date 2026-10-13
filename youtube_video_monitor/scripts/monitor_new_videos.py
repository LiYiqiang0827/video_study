#!/usr/bin/env python3
"""Monitor the Yuncong channel and archive new member videos end to end.

The Mac mini owns authenticated discovery, resilient downloading, and MLX Qwen
ASR/alignment. This Mac runs grounded viewpoint analysis through llm-server,
builds the editable PPT, and sends the finished deck through Hermes Feishu.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "analysis_ppt/scripts"))
SCRIPTS = Path(__file__).resolve().parent
CHANNEL = "https://www.youtube.com/channel/UCg9zI64pEB_3xwJPur6isiQ/videos"
ARCHIVE = Path.home() / "云聪视频全集"
TMP = PROJECT / "tmp/youtube_video_monitor"
REMOTE = Path.home() / "YuncongVideoAnalysis/monitor_jobs"
HOST = "macmini"
REMOTE_TOOLS = Path.home() / "YuncongVideoAnalysis/monitor_tools"
DOWNLOAD_ROOT = Path.home() / "YuncongVideoAnalysis"
COOKIE = Path.home() / "YuncongVideoAnalysis/local_auth/www.youtube.com_cookies.txt"
DOWNLOAD_PY = Path.home() / "YuncongVideoAnalysis/download_tools/venv/bin/python"
ASR_PY = Path.home() / "YuncongVideoAnalysis/asr_tools/venv/bin/python"
ASR_MODELS = Path.home() / "YuncongVideoAnalysis/asr_tools/models"
PROXY = "socks5h://127.0.0.1:7890"
ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
NODE = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node"
PYTHON = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
NODE_MODULES = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules"


class MonitorError(RuntimeError):
    pass


def run(command, *, timeout=3600, input_bytes=None, capture=True, env=None):
    result = subprocess.run(command, input=input_bytes, stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.PIPE if capture else None, timeout=timeout, env=env)
    if result.returncode:
        detail = (result.stderr or b"")[-1200:].decode("utf-8", "replace") if capture else ""
        raise MonitorError(f"command_failed:{Path(command[0]).name}:{detail}")
    return result.stdout if capture else b""


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def archive_ids() -> set[str]:
    result = set()
    for path in ARCHIVE.glob("*/*来源记录.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            ident = value.get("video_id")
            if isinstance(ident, str) and ID_RE.fullmatch(ident):
                result.add(ident)
        except (OSError, ValueError):
            continue
    return result


def archived_video(video_id: str) -> tuple[dict, Path] | None:
    for path in ARCHIVE.glob("*/*来源记录.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if record.get("video_id") == video_id:
            folder = path.parent
            return ({"video_id": video_id, "title": record.get("title") or folder.name,
                     "url": record.get("source_url") or f"https://www.youtube.com/watch?v={video_id}"}, folder)
    return None


def remote(command: str, *, timeout=3600, stdin=None) -> bytes:
    return run(["/usr/bin/ssh", "-T", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=20",
                "-o", "ServerAliveCountMax=30", HOST, command], timeout=timeout, input_bytes=stdin)


def sync_tools() -> None:
    remote(f"mkdir -p {shlex.quote(str(REMOTE_TOOLS))}", timeout=60)
    for name in ("video_download", "audio_subtitles"):
        run(["/usr/bin/rsync", "-a", "--delete", str(PROJECT / name) + "/",
             f"{HOST}:{shlex.quote(str(REMOTE_TOOLS / name))}/"], timeout=300)


def discover() -> list[dict]:
    py = shlex.quote(str(DOWNLOAD_PY))
    cookie = shlex.quote(str(COOKIE))
    url = shlex.quote(CHANNEL)
    command = (f"env PATH=/opt/homebrew/bin:/Users/nickzhang/.local/bin:/usr/bin:/bin {py} -m yt_dlp "
               f"--quiet --no-warnings --flat-playlist --playlist-end 12 --extractor-retries 6 --retries 8 "
               f"--extractor-args youtubetab:skip=authcheck --print '%(id)s\\t%(title)s\\t%(duration)s' "
               f"--cookies {cookie} --proxy {shlex.quote(PROXY)} --impersonate chrome {url}")
    last_error = None
    for attempt in range(3):
        try:
            lines = remote(command, timeout=240).decode("utf-8").splitlines()
            break
        except MonitorError as error:
            last_error = error
            if attempt == 2:
                raise
            time.sleep(5 * (attempt + 1))
    else:
        raise last_error or MonitorError("channel_listing_failed")
    if not lines:
        raise MonitorError("channel_listing_empty_or_invalid")
    old = archive_ids()
    fresh = []
    boundary = False
    for line in lines:
        fields = line.split("\\t", 2)
        if len(fields) != 3:
            raise MonitorError("channel_listing_row_invalid")
        ident, title, raw_duration = fields
        if not isinstance(ident, str) or not ID_RE.fullmatch(ident):
            continue
        if ident in old:
            boundary = True
            break
        try:
            duration = float(raw_duration) if raw_duration != "NA" else None
        except ValueError:
            duration = None
        fresh.append({"video_id": ident, "title": title or ident,
                      "duration": duration, "url": f"https://www.youtube.com/watch?v={ident}"})
    if fresh and not boundary:
        raise MonitorError("archive_boundary_not_found_in_channel_listing")
    return fresh


def remote_json(path: str) -> dict:
    source = "import pathlib,sys; p=pathlib.Path(" + repr(path) + "); sys.stdout.write(p.read_text())"
    return json.loads(remote("python3 -c " + shlex.quote(source), timeout=60).decode("utf-8"))


def prepare_download(video: dict) -> tuple[str, Path, dict]:
    ident = video["video_id"]
    run_name = "video_monitor_" + ident
    remote_batch = DOWNLOAD_ROOT / "batches" / run_name
    listing = json.dumps([{"url": video["url"], "expected_duration_seconds": video.get("duration")}],
                         ensure_ascii=False).encode("utf-8")
    list_path = REMOTE / ident / "download_list.json"
    remote(f"mkdir -p {shlex.quote(str(list_path.parent))} && cat > {shlex.quote(str(list_path))}", stdin=listing)
    d = shlex.quote(str(REMOTE_TOOLS / "video_download/scripts/download_batch.py"))
    root = shlex.quote(str(DOWNLOAD_ROOT))
    batch_name = shlex.quote(run_name)
    exists = remote(f"if [ -d {shlex.quote(str(remote_batch))} ]; then printf yes; fi", timeout=30) == b"yes"
    if exists:
        frozen = remote_json(str(remote_batch / "manifest.json"))
        frozen_items = frozen.get("items", [])
        if (len(frozen_items) != 1 or frozen_items[0].get("video_id") != ident
                or frozen_items[0].get("url") != video["url"]):
            raise MonitorError(f"existing_download_batch_contract_mismatch:{ident}")
    else:
        remote(f"{shlex.quote(str(DOWNLOAD_PY))} {d} init --list {shlex.quote(str(list_path))} --root {root} --name {batch_name}", timeout=180)
    runner = shlex.quote(str(REMOTE_TOOLS / "video_download/scripts/run_resilient_batch.py"))
    cmd = (f"env PATH=/opt/homebrew/bin:/Users/nickzhang/.local/bin:/usr/bin:/bin "
           f"{shlex.quote(str(DOWNLOAD_PY))} {runner} --batch-dir {shlex.quote(str(remote_batch))} "
           f"--cookie-file {shlex.quote(str(COOKIE))} --proxy {shlex.quote(PROXY)} --max-attempts 8 --retry-failed "
           "--max-runtime-minutes 300")
    remote(cmd, timeout=5 * 3600)
    state = remote_json(str(remote_batch / "state.json"))
    saved = state.get("items", {}).get(ident, {})
    receipt = saved.get("receipt") or {}
    if saved.get("status") != "complete" or receipt.get("status") != "technical_checks_passed":
        raise MonitorError(f"download_not_verified:{ident}:{saved.get('status')}")
    result_path = remote_batch / "items" / ident / "result.json"
    result = remote_json(str(result_path))
    return ident, remote_batch, result


def run_asr(ident: str, remote_batch: Path, download_result: dict) -> tuple[Path, dict]:
    remote_job = REMOTE / ident / "asr"
    result_path = remote_batch / "items" / ident / "result.json"
    py = shlex.quote(str(ASR_PY))
    scripts = REMOTE_TOOLS / "audio_subtitles/scripts"
    manifest = remote_job / "manifest.json"
    build = (f"{py} {shlex.quote(str(scripts / 'build_manifest.py'))} --result {shlex.quote(str(result_path))} "
             f"--output {shlex.quote(str(manifest))}")
    remote(f"mkdir -p {shlex.quote(str(remote_job))} && {build}", timeout=120)
    root = remote_job / "batch"
    resume_check = remote(f"if [ -f {shlex.quote(str(root / 'state.json'))} ]; then printf yes; fi", timeout=30)
    resume = " --resume" if resume_check == b"yes" else ""
    batch = (f"env PATH=/opt/homebrew/bin:/Users/nickzhang/.local/bin:/usr/bin:/bin {py} "
             f"{shlex.quote(str(scripts / 'batch_asr.py'))} --manifest {shlex.quote(str(manifest))} "
             f"--job-root {shlex.quote(str(root))} --python {shlex.quote(str(ASR_PY))} "
             f"--models-dir {shlex.quote(str(ASR_MODELS))}{resume} --retry-failed")
    remote(batch, timeout=5 * 3600)
    state = remote_json(str(root / "state.json"))
    item = state.get("items", {}).get(ident, {})
    if item.get("state") != "processed_pending_review":
        raise MonitorError(f"qwen_asr_not_complete:{ident}:{item.get('state')}")
    attempt = Path(item["attempt_directory"])
    production = Path(item.get("review_directory") or "")
    if not production.is_absolute() or not str(production).startswith(str(attempt) + "/"):
        raise MonitorError(f"closed_subtitle_export_missing:{ident}")
    if remote_json(str(production / "AUTO_EXPORT_PROVENANCE.json")).get("state") != "exported_auto":
        raise MonitorError(f"subtitle_export_not_closed:{ident}")
    return production, item


def transfer(remote_path: Path, local_path: Path, *, directory=False) -> None:
    local_path.parent.mkdir(parents=True, exist_ok=True)
    source = f"{HOST}:{shlex.quote(str(remote_path))}" + ("/" if directory else "")
    command = ["/usr/bin/rsync", "-a", "--safe-links", source, str(local_path) + ("/" if directory else "")]
    run(command, timeout=3600)


def presentation_skill_dir() -> Path:
    root = Path.home() / ".codex/plugins/cache/openai-primary-runtime/presentations"
    for path in sorted(root.glob("*/skills/presentations"), reverse=True):
        if (path / "container_tools/artifact_tool_utils.mjs").is_file():
            return path
    raise MonitorError("presentation_skill_runtime_missing")


def verify_frames_with_ocr(analysis_path: Path, visual_path: Path) -> dict:
    analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
    frames = [frame for entity in analysis.get("entities", []) for segment in entity.get("segments", [])
              for frame in segment.get("frames", [])]
    if not frames:
        raise MonitorError("analysis_has_no_screenshots")
    image_paths = [str((analysis_path.parent / frame["path"]).resolve()) for frame in frames]
    swift = PROJECT / "youtube_video_monitor/scripts/ocr_frames.swift"
    raw = run(["/usr/bin/swift", str(swift), *image_paths], timeout=300)
    records = json.loads(raw.decode("utf-8"))
    visual_path.parent.mkdir(parents=True, exist_ok=True)
    visual_path.with_suffix(".ocr.json").write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    text_by_path = {item["path"]: " ".join(item.get("lines", [])) for item in records}

    def norm(value: str) -> str:
        return "".join(ch for ch in unicodedata.normalize("NFKC", value).casefold() if ch.isalnum())

    selected: dict[str, dict] = {}
    missing = []
    entities_selection = {}
    for entity in analysis.get("entities", []):
        name = str(entity.get("name") or "")
        needle = norm(name)
        matched = []
        for segment in entity.get("segments", []):
            frame_specs = []
            for frame in segment.get("frames", []):
                full_path = str((analysis_path.parent / frame["path"]).resolve())
                ocr_text = text_by_path.get(full_path, "")
                if len(needle) >= 2 and needle in norm(ocr_text):
                    spec = {"video_seconds": frame["video_seconds"], "caption": frame.get("caption", ""),
                            "visual_verified": True, "visible_entity_name": name,
                            "review_note": f"macOS Vision OCR 在同一画面识别到实体名称：{name}"}
                    if entity.get("kind") in {"index", "market"}:
                        spec["chart_verified"] = True
                    frame_specs.append(spec)
                    matched.append(frame["path"])
            if frame_specs:
                selected[str(segment["id"])] = {"frames": frame_specs}
        if matched:
            entities_selection[name] = {"display_name": name, "visual_verified": True,
                                        "basis": "macOS Vision OCR 在原始视频截图中识别到实体全名"}
        elif entity.get("kind") in {"stock", "other"}:
            missing.append(name)
    result = {"schema_version": 1, "source_video_sha256": analysis["metadata"]["source_video_sha256"],
              "segments": selected, "entities": entities_selection,
              "unverified_entities": missing,
              "verification_method": "macOS Vision OCR exact entity-name matching; this is machine verification, not human review."}
    visual_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def gpu_request(payload: dict) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    curl = ["curl", "--noproxy", "127.0.0.1,localhost,::1", "-sS", "--fail-with-body", "--max-time", "1200",
            "-H", "Content-Type:application/json", "--data-binary", "@-", "http://127.0.0.1:8002/v1/chat/completions"]
    raw = run(["ssh", "-T", "-o", "BatchMode=yes", "llm-server", shlex.join(curl)], timeout=1260, input_bytes=data)
    return json.loads(raw)


def build_plan(analysis_path: Path, out_path: Path, visual_selection: dict) -> None:
    analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
    model_info = json.loads(run(["ssh", "-T", "-o", "BatchMode=yes", "llm-server",
                                 "curl -sS --fail http://127.0.0.1:8002/v1/models"], timeout=30))
    models = model_info.get("data") or []
    if not models:
        raise MonitorError("gpu_qwen_model_missing")
    prompt = (PROJECT / "analysis_ppt/prompts/video_ppt_outline.md").read_text(encoding="utf-8")
    eligible = {entity["name"] for entity in analysis.get("entities", [])
                if entity["name"] in visual_selection.get("entities", {})}
    prompt += ("\n\n视觉验收补充：只为以下 OCR 明确确认的实体制作截图详情：" +
               json.dumps(sorted(eligible), ensure_ascii=False) +
               "。没有通过 OCR 身份确认的实体不要选入 stocks 或 market_items。分析摘要可保留原文提及，"+
               "但调用方会从详情中移除无核验截图的对象。\n")
    prompt += "分析文件 SHA256：" + sha(analysis_path) + "\n完整 analysis.json：\n" + json.dumps(analysis, ensure_ascii=False)
    payload = {"model": models[0]["id"], "messages": [{"role": "user", "content": prompt}],
               "temperature": 0.1, "max_tokens": 12000,
               "chat_template_kwargs": {"enable_thinking": False}}
    out_path.with_suffix(".prompt.txt").write_text(prompt, encoding="utf-8")
    out_path.with_suffix(".request.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    completion = gpu_request(payload)
    out_path.with_suffix(".response.json").write_text(json.dumps(completion, ensure_ascii=False), encoding="utf-8")
    from analysis_ppt.scripts.analyze_video_transcript import extract_json
    plan = extract_json(completion["choices"][0]["message"]["content"])
    finalize_plan(analysis, analysis_path, plan, visual_selection)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def finalize_plan(analysis: dict, analysis_path: Path, plan: dict, visual_selection: dict) -> None:
    plan["analysis_sha256"] = sha(analysis_path)
    eligible = {entity["name"] for entity in analysis.get("entities", [])
                if entity.get("name") in visual_selection.get("entities", {})}
    frames_by_entity = {}
    for entity in analysis.get("entities", []):
        if entity.get("name") not in eligible:
            continue
        frames_by_entity[entity["id"]] = {frame["path"] for segment in entity.get("segments", [])
                                           for frame in segment.get("frames", []) if frame.get("visual_verified") is True}
    def keep_detail(item):
        valid = frames_by_entity.get(item.get("entity_id"), set())
        item["details"] = [detail for detail in item.get("details", []) if detail.get("frame_path") in valid]
        entity = next((candidate for candidate in analysis.get("entities", [])
                       if candidate.get("id") == item.get("entity_id")), {})
        for detail in item["details"]:
            points = detail.get("points")
            if isinstance(points, list) and len(points) == 1:
                summary = item.get("outline_sentence") or entity.get("overall_view", {}).get("text", "")
                summary = summary.strip()
                if summary and summary != points[0].get("text"):
                    detail["points"].append({"heading": "观点概述", "text": summary[:58]})
        return bool(item["details"])
    plan["stocks"] = [item for item in plan.get("stocks", []) if keep_detail(item)]
    plan["market_items"] = [item for item in plan.get("market_items", []) if keep_detail(item)]
    if not plan["stocks"] and not plan["market_items"]:
        raise MonitorError("no_ocr_verified_entities_available_for_ppt")
    omitted = visual_selection.get("unverified_entities", [])
    if omitted:
        note = "画面标的需复核：" + "、".join(omitted)
        plan["overview"]["focus"] = note if len(note) <= 65 else note[:64] + "…"


def create_ppt_analysis(full_analysis_path: Path, output_path: Path, visual_selection: dict) -> Path:
    """Make a traceable PPT input containing only OCR-identity-verified stocks.

    The full transcript analysis remains alongside the archived video. This
    presentation projection keeps the PPT validator's stock-coverage contract
    aligned with the screenshots that can safely be shown.
    """
    full = json.loads(full_analysis_path.read_text(encoding="utf-8"))
    verified_names = set(visual_selection.get("entities", {}))
    included = [entity for entity in full.get("entities", [])
                if entity.get("kind") == "stock" and entity.get("name") in verified_names]
    if not included:
        raise MonitorError("no_ocr_verified_stocks_available_for_ppt")
    projected = dict(full)
    projected["entities"] = included
    output_path.write_text(json.dumps(projected, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    scope = {
        "schema_version": 1,
        "full_analysis_sha256": sha(full_analysis_path),
        "ppt_analysis_sha256": sha(output_path),
        "included_stock_names": [entity["name"] for entity in included],
        "omitted_for_visual_identity_review": visual_selection.get("unverified_entities", []),
        "reason": "PPT only includes stocks whose full name was found by macOS Vision OCR in original video frames.",
    }
    output_path.with_name("PPT_SCOPE.json").write_text(json.dumps(scope, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output_path


def validated_ppt_build(ppt: Path, build_dir: Path) -> bool:
    status_path = build_dir / "STATUS.json"
    if not ppt.is_file() or not status_path.is_file():
        return False
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
        validation_path = Path(status["validation_path"])
        validation = json.loads(validation_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return (status.get("state") == "complete"
            and validation.get("finalSha256") == sha(ppt)
            and validation.get("packageIntegrity", {}).get("status") == "pass"
            and validation.get("packageIntegrity", {}).get("finding_count", 1) == 0
            and validation.get("presentationLayout", {}).get("finding_count", 1) == 0
            and validation.get("presentationLayout", {}).get("warning_count", 1) == 0)


def repair_failed_model(ident: str, transcript_path: Path, model_root: Path) -> Path | None:
    for repair_dir in sorted((TMP / ident).glob("model_citation_repair_*"), reverse=True):
        validated = repair_dir / "validated_analysis"
        if (validated / "content_analysis.json").is_file() and (validated / "ANALYSIS_PROVENANCE.json").is_file():
            return validated / "content_analysis.json"
    candidates = [model_root, *sorted((TMP / ident).glob("model_retry_*"))]
    candidates = [path for path in candidates if (path / "validation_attempts.json").is_file()
                  and not (path / "ANALYSIS_PROVENANCE.json").is_file()]
    if not candidates:
        return None
    failed = max(candidates, key=lambda path: path.stat().st_mtime)
    history = json.loads((failed / "validation_attempts.json").read_text(encoding="utf-8"))
    if not history or not history[-1].get("errors"):
        return None
    raw_files = sorted(failed.glob("attempt_*.raw.txt"))
    if not raw_files:
        return None
    raw_path = raw_files[-1]
    transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
    input_packet = json.loads((failed / "input_packet.json").read_text(encoding="utf-8"))
    cue_ids = sorted({int(found) for error in history[-1]["errors"]
                      for found in re.findall(r"cue (\d+)", error)})
    cue_map = {cue["id"]: cue["text"] for cue in transcript.get("cues", [])}
    exact_cues = {str(cid): cue_map[cid] for cid in cue_ids if cid in cue_map}
    prompt = (
        "请修复下面的云聪视频分析 JSON，只处理机器校验错误。必须返回完整 JSON 对象，不要 Markdown。"
        "引用 text 必须是给定字幕 cue 的逐字连续子串；不能凭意思改写。若现有结论没有来源支持，就删除该条结论，"
        "并保留其它正确内容、字段和 ID。不要更改时间，也不要补外部事实。\n\n"
        "校验错误：\n" + json.dumps(history[-1]["errors"], ensure_ascii=False) +
        "\n\n出错 cue 的原文：\n" + json.dumps(exact_cues, ensure_ascii=False) +
        "\n\n输入证据包：\n" + json.dumps(input_packet, ensure_ascii=False) +
        "\n\n上次完整 JSON：\n" + raw_path.read_text(encoding="utf-8")
    )
    discovery = json.loads((failed / "model_discovery.json").read_text(encoding="utf-8"))
    models = discovery.get("data") or []
    if not models:
        raise MonitorError("gpu_qwen_model_missing_for_citation_repair")
    repair_no = 1
    while (TMP / ident / f"model_citation_repair_{repair_no:02d}").exists():
        repair_no += 1
    repair_dir = TMP / ident / f"model_citation_repair_{repair_no:02d}"
    repair_dir.mkdir(parents=True)
    (repair_dir / "repair_prompt.txt").write_text(prompt, encoding="utf-8")
    payload = {"model": models[0]["id"], "messages": [{"role": "user", "content": prompt}],
               "temperature": 0, "max_tokens": 24000,
               "chat_template_kwargs": {"enable_thinking": False}}
    (repair_dir / "repair_request.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    response = gpu_request(payload)
    text = response.get("choices", [{}])[0].get("message", {}).get("content")
    if not isinstance(text, str) or not text.strip():
        raise MonitorError("gpu_qwen_citation_repair_empty")
    (repair_dir / "repair_response.json").write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")
    raw_response = repair_dir / "repaired_analysis.txt"
    raw_response.write_text(text, encoding="utf-8")
    output_dir = repair_dir / "validated_analysis"
    run([str(PYTHON), str(PROJECT / "analysis_ppt/scripts/analyze_video_transcript.py"),
         "--input", str(transcript_path), "--output-dir", str(output_dir), "--raw-response", str(raw_response)],
        timeout=1800)
    return output_dir / "content_analysis.json"


def video_stem(result: dict, video: dict) -> str:
    name = result.get("canonical_filename") or ""
    if name.endswith(".mp4"):
        return name[:-4]
    raise MonitorError(f"exact_archive_filename_missing:{video['video_id']}")


def archive_and_analyze(video: dict, download_result: dict, download_batch: Path,
                        production: Path, asr_item: dict) -> Path:
    ident = video["video_id"]
    stem = video_stem(download_result, video)
    target = ARCHIVE / stem
    if target.exists():
        record = next(target.glob("*来源记录.json"), None)
        if record and json.loads(record.read_text(encoding="utf-8")).get("video_id") == ident:
            return target / f"{stem}.pptx"
        raise MonitorError(f"archive_folder_exists_for_different_video:{target.name}")
    stage = TMP / ident / "archive_stage" / stem
    stage.mkdir(parents=True, exist_ok=True)
    remote_mp4 = Path(download_result["file"])
    local_mp4 = stage / f"{stem}.mp4"
    transfer(remote_mp4, local_mp4)
    if sha(local_mp4) != download_result.get("sha256") or local_mp4.stat().st_size != download_result.get("bytes"):
        raise MonitorError(f"download_mirror_hash_mismatch:{ident}")
    remote_attempt = Path(asr_item["attempt_directory"])
    local_audio = stage / f"{stem}.m4a"
    transfer(remote_attempt / "audio_original.m4a", local_audio)
    audio_hashes = asr_item.get("artifact_hashes") or {}
    declared_audio_hash = next((digest for name, digest in audio_hashes.items()
                                if name.endswith("/audio_original.m4a") or name == "audio_original.m4a"), None)
    if declared_audio_hash and sha(local_audio) != declared_audio_hash:
        raise MonitorError(f"audio_mirror_hash_mismatch:{ident}")
    local_prod = stage / "subtitle_export"
    transfer(production, local_prod, directory=True)
    transcript_source = local_prod / "transcript.json"
    if not transcript_source.is_file():
        raise MonitorError(f"subtitle_transcript_missing:{ident}")
    transcript = json.loads(transcript_source.read_text(encoding="utf-8"))
    if not transcript.get("cues"):
        raise MonitorError(f"subtitle_cues_empty:{ident}")
    for suffix, key in ((".json", "transcript.json"), (".srt", "transcript.srt"),
                        (".vtt", "transcript.vtt"), (".md", "transcript.md")):
        shutil.copy2(local_prod / key, stage / (stem + suffix))
    subtitle_provenance = json.loads((local_prod / "AUTO_EXPORT_PROVENANCE.json").read_text(encoding="utf-8"))
    shutil.rmtree(local_prod)
    record = {"schema_version": 1, "video_id": ident, "source_video_sha256": sha(local_mp4),
              "title": video["title"], "source_url": video["url"],
              "upload_time_beijing": download_result.get("upload_time_local") or download_result.get("upload_time_beijing"),
              "upload_time_precision": download_result.get("upload_time_precision"),
              "download_result": "Mac mini resilient downloader; full decode and audio/video stream checks passed",
              "subtitle_backend": "Mac mini mlx-audio Qwen3-ASR-1.7B-8bit + Qwen3-ForcedAligner-0.6B-8bit",
              "subtitle_provenance": subtitle_provenance,
              "human_transcription_review": "pending"}
    (stage / f"{stem}_来源记录.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    download_record = {key: download_result.get(key) for key in (
        "video_id", "source_url", "title", "canonical_filename", "upload_time_local",
        "upload_time_precision", "timestamp", "expected_duration_seconds", "listed_duration_seconds",
        "local_duration_seconds", "bytes", "sha256", "streams", "decode_check", "duration_checks", "status")}
    download_record["source_url"] = video["url"]
    download_record["title"] = video["title"]
    download_record["verified_on"] = "Mac mini resilient downloader"
    (stage / f"{stem}_下载来源记录.json").write_text(
        json.dumps(download_record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    transcript_path = stage / f"{stem}.json"
    analysis_dir = stage / "解析_自动"
    model_root = TMP / ident / "model"
    content = repair_failed_model(ident, transcript_path, model_root)
    if content is None:
        model_output = model_root
        if model_root.exists() and not (model_root / "ANALYSIS_PROVENANCE.json").is_file():
            suffix = 2
            while (TMP / ident / f"model_retry_{suffix:02d}").exists():
                suffix += 1
            model_output = TMP / ident / f"model_retry_{suffix:02d}"
        run([str(PYTHON), str(PROJECT / "analysis_ppt/scripts/analyze_video_transcript.py"),
             "--input", str(transcript_path), "--output-dir", str(model_output),
             "--provider", "qwen-vllm", "--transport", "ssh", "--ssh-host", "llm-server",
             "--max-tokens", "24000"], timeout=2400)
        content = model_output / "content_analysis.json"
    # Analysis generation writes frame/clip files and deliberately refuses to
    # overwrite them. A prior interrupted attempt may leave a partial tree, so
    # reuse a complete hash-matched OCR review, or clear only generated scratch
    # directories before rebuilding an incomplete attempt.
    analysis_dir = stage / "解析_自动"
    reviewed_analysis_dir = stage / "解析_自动_视觉核验"
    visual_selection_path = TMP / ident / "visual_selection.json"
    analysis_path = reviewed_analysis_dir / "analysis.json"
    visual_result = None
    if analysis_path.is_file() and visual_selection_path.is_file():
        try:
            cached_selection = json.loads(visual_selection_path.read_text(encoding="utf-8"))
            cached_analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
            if (cached_selection.get("source_video_sha256") == sha(local_mp4)
                    and cached_analysis.get("metadata", {}).get("source_video_sha256") == sha(local_mp4)):
                visual_result = cached_selection
        except (OSError, json.JSONDecodeError):
            visual_result = None
    if visual_result is None:
        shutil.rmtree(analysis_dir, ignore_errors=True)
        shutil.rmtree(reviewed_analysis_dir, ignore_errors=True)
        initial_analysis = analysis_dir / "analysis.json"
        run([str(PYTHON), str(PROJECT / "analysis_ppt/scripts/build_video_analysis.py"),
             "--video-dir", str(stage), "--output-dir", str(analysis_dir),
             "--content-analysis", str(content), "--frames-per-segment", "2"], timeout=1800)
        visual_result = verify_frames_with_ocr(initial_analysis, visual_selection_path)
        run([str(PYTHON), str(PROJECT / "analysis_ppt/scripts/build_video_analysis.py"),
             "--video-dir", str(stage), "--output-dir", str(reviewed_analysis_dir),
             "--content-analysis", str(content), "--visual-selection", str(visual_selection_path),
             "--frames-per-segment", "2"], timeout=1800)
        shutil.copy2(visual_selection_path, reviewed_analysis_dir / "visual_selection.json")
    analysis_dir = reviewed_analysis_dir
    ppt_analysis_path = create_ppt_analysis(analysis_path, analysis_dir / "analysis_for_ppt.json", visual_result)
    plan_path = TMP / ident / "ppt_plan.json"
    reusable_plan = None
    if plan_path.is_file():
        try:
            candidate = json.loads(plan_path.read_text(encoding="utf-8"))
            if candidate.get("analysis_sha256") == sha(ppt_analysis_path):
                reusable_plan = candidate
        except (OSError, json.JSONDecodeError):
            reusable_plan = None
    if reusable_plan is None:
        build_plan(ppt_analysis_path, plan_path, visual_result)
    else:
        projected = json.loads(ppt_analysis_path.read_text(encoding="utf-8"))
        finalize_plan(projected, ppt_analysis_path, reusable_plan, visual_result)
        plan_path.write_text(json.dumps(reusable_plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    env = os.environ.copy()
    skill = presentation_skill_dir()
    env.update({"RUNTIME_NODE": str(NODE), "RUNTIME_NODE_MODULES": str(NODE_MODULES),
                "RUNTIME_PYTHON": str(PYTHON), "PRESENTATIONS_SKILL_DIR": str(skill)})
    ppt = stage / f"{stem}.pptx"
    build_dir = TMP / ident / ("ppt_build_" + sha(plan_path)[:10])
    prior_status = build_dir / "STATUS.json"
    if validated_ppt_build(ppt, build_dir):
        pass
    else:
        if prior_status.is_file():
            try:
                prior_state = json.loads(prior_status.read_text(encoding="utf-8")).get("state")
            except (OSError, json.JSONDecodeError):
                prior_state = "unknown"
            if prior_state != "complete":
                shutil.rmtree(build_dir, ignore_errors=True)
        build = run([str(PYTHON), str(PROJECT / "analysis_ppt/scripts/build_video_ppt.py"),
                     "--analysis", str(ppt_analysis_path), "--plan", str(plan_path),
                     "--build-dir", str(build_dir), "--output", str(ppt)],
                    timeout=5400, env=env)
        try:
            exporter_result = json.loads(build)
        except (ValueError, TypeError):
            exporter_result = {}
        if exporter_result.get("state") != "exported" or not validated_ppt_build(ppt, build_dir):
            raise MonitorError(f"ppt_not_validated:{ident}")
    for render_lock in stage.rglob(".render.lock"):
        try:
            render_lock.unlink()
        except FileNotFoundError:
            pass
    target.parent.mkdir(parents=True, exist_ok=True)
    os.rename(stage, target)
    return target / ppt.name, visual_result.get("unverified_entities", [])


def notify(video: dict, ppt: Path, info: dict) -> None:
    directory = json.loads((Path.home() / ".hermes/channel_directory.json").read_text(encoding="utf-8"))
    matches = [item for item in directory.get("platforms", {}).get("feishu", [])
               if isinstance(item, dict) and item.get("type") == "dm" and isinstance(item.get("name"), str)]
    if len(matches) != 1:
        raise MonitorError("hermes_feishu_dm_target_not_unique")
    target = "feishu:" + matches[0]["name"]
    message = (f"云聪 YouTube 新视频已处理完成\n标题：{video['title']}\n视频 ID：{video['video_id']}\n"
               f"转写：Mac mini 本地 Qwen ASR + ForcedAligner\n观点分析：远端 GPU Qwen（Hermes SSH）\n"
               f"归档：{ppt.parent}\n字幕复核：待人工听音抽检\nMEDIA:{ppt}")
    omitted = info.get("omitted_entities", []) if isinstance(info, dict) else []
    if omitted:
        message = message.replace("\nMEDIA:", "\n因原始画面 OCR 无法确认标的身份，PPT 未制作这些对象的截图详情：" +
                                  "、".join(omitted) + "。其原文仍保留在字幕和分析文件中，需人工复核后补充。\nMEDIA:")
    run(["hermes", "send", "--to", target, "--quiet", message], timeout=180)


def process(video: dict) -> dict:
    already_archived = archived_video(video["video_id"])
    if already_archived:
        ppt = already_archived[1] / f"{already_archived[1].name}.pptx"
        if ppt.is_file():
            visual_path = already_archived[1] / "解析_自动_视觉核验/visual_selection.json"
            visual = json.loads(visual_path.read_text(encoding="utf-8")) if visual_path.is_file() else {}
            notify(video, ppt, {"omitted_entities": visual.get("unverified_entities", [])})
            shutil.rmtree(TMP / video["video_id"], ignore_errors=True)
            return {"video_id": video["video_id"], "title": video["title"], "ppt": str(ppt)}
    ident, batch, result = prepare_download(video)
    production, asr_item = run_asr(ident, batch, result)
    ppt, omitted_entities = archive_and_analyze(video, result, batch, production, asr_item)
    notify(video, ppt, {**result, "omitted_entities": omitted_entities})
    shutil.rmtree(TMP / ident, ignore_errors=True)
    return {"video_id": ident, "title": video["title"], "ppt": str(ppt)}


def main() -> int:
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    TMP.mkdir(parents=True, exist_ok=True)
    lock_path = TMP / ".monitor.lock"
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("云聪视频监控上轮仍运行，本轮跳过。", file=sys.stderr)
            return 0
        state_path = TMP / "monitor_state.json"
        state = json.loads(state_path.read_text()) if state_path.exists() else {"pending": []}
        sync_tools()
        pending_ids = state.get("pending", [])
        if pending_ids:
            listing = {video["video_id"]: video for video in discover()}
            videos = []
            for ident in pending_ids:
                if ident in listing:
                    videos.append(listing[ident])
                    continue
                archived = archived_video(ident)
                if archived:
                    videos.append(archived[0])
                    continue
                stored = {item["video_id"]: item for item in state.get("pending_videos", [])}
                if ident in stored:
                    videos.append(stored[ident])
                    continue
                raise MonitorError("pending_video_disappeared_from_latest_listing")
        else:
            videos = discover()
            if not videos:
                return 0
            state["pending"] = [item["video_id"] for item in videos]
            state["pending_videos"] = videos.copy()
            atomic_json(state_path, state)
        completed = []
        for video in reversed(videos):
            result = process(video)
            completed.append(result)
            state["pending"].remove(video["video_id"])
            state["pending_videos"] = [item for item in state.get("pending_videos", [])
                                        if item.get("video_id") != video["video_id"]]
            state.setdefault("completed", []).append({**result, "completed_at": datetime.now().astimezone().isoformat()})
            atomic_json(state_path, state)
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"云聪视频监控失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
