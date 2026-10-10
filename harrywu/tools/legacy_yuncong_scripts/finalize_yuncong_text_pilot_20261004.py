"""Create text-only, time-linked pilot deliverables after semantic review."""
from __future__ import annotations
import csv
import hashlib
import json
import re
import time
import sys
from datetime import datetime, timezone
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(r"D:\YuncongVideoAnalysis\ledger_2026_v0.1\text_pilot_20261004_0804")
VOCAB = Path(r"D:\YuncongVideoAnalysis\ledger_2026_v0.1\planning\stock_vocabulary_v0.1_20261004")

def stamp(value):
    value = int(value)
    return f"{value//3600:02}:{value//60%60:02}:{value%60:02}"

def write_csv(path, rows, fields):
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

def groups(rows):
    group = []
    result = []
    for row in rows:
        if group and (row["start"] - group[0]["start"] >= 30 or row["start"] - group[-1]["end"] > 8):
            result.append(f"[{stamp(group[0]['start'])}–{stamp(group[-1]['end'])}] " + "".join(r["text"] for r in group))
            group = []
        group.append(row)
    if group:
        result.append(f"[{stamp(group[0]['start'])}–{stamp(group[-1]['end'])}] " + "".join(r["text"] for r in group))
    return result

def main():
    began = time.monotonic()
    spec = json.loads((ROOT / "task.json").read_text(encoding="utf-8"))
    report = json.loads((ROOT / "timing_report.json").read_text(encoding="utf-8"))
    sections = json.loads((ROOT / "reviewed_sections.json").read_text(encoding="utf-8"))
    corrections_spec = json.loads((ROOT / "reviewed_name_candidates.json").read_text(encoding="utf-8"))
    rows = json.loads((ROOT / "asr/speech.json").read_text(encoding="utf-8"))
    assert sections[0]["start"] == 0
    assert abs(sections[-1]["end"] - spec["duration_seconds"]) < .01
    assert all(abs(a["end"] - b["start"]) < .01 for a, b in zip(sections, sections[1:]))
    assert all(s["start"] < s["end"] for s in sections)
    assert all(0 <= r["start"] < r["end"] <= spec["duration_seconds"] + 2 for r in rows)
    body = [f"{spec['title']}\n场次：{spec['stream_id']}\n北京时间开播：{spec['actual_start_beijing']}\n"
            f"完整时长：{stamp(spec['duration_seconds'])}\n时间戳均为源视频偏移。\n"
            "原始ASR未校正；含股票同音错字，股票名称候选另列。\n"]
    body.extend(f"{row['id']} [{stamp(row['start'])}–{stamp(row['end'])}] {row['text']}" for row in rows)
    (ROOT / "全文_带时间.txt").write_text("\n".join(body) + "\n", encoding="utf-8")
    noise = re.compile(r"请不吝点赞\s*订阅\s*转发\s*打赏支持明镜与点点栏目|优优独播剧场[—\-\s]*YoYo Television Series Exclusive|中文字幕志愿者\s*李宗盛")
    noise_rows, kept_ids = [], set()
    selected = [f"{spec['title']} — 按内容切分的研究文本\n"
                "时间戳可回到原视频。本人操作、候选计划、学员问答按语境标注；未核实实际成交。\n"
                "股票名称保持ASR原文，请配合股票名称校正候选表阅读。\n"
                "与画面有关的锚点、指代股票或价位留待以后补看K线，不在这里补猜。\n"]
    quick = ["学员个股快评（文本候选，含混合段；不计为本人买入）\n"]
    for number, section in enumerate(sections, 1):
        section["section_id"] = f"p{number:02}"
        selected_rows = [r for r in rows if section["start"] <= (r["start"] + r["end"]) / 2 < section["end"]]
        section["source_segment_ids"] = [r["id"] for r in selected_rows]
        section["raw_segment_count"] = len(selected_rows)
        if not section["keep"]:
            continue
        cleaned = []
        for row in selected_rows:
            text = noise.sub("", row["text"]).strip()
            if text != row["text"]:
                noise_rows.append({"segment_id": row["id"], "start": row["start"], "end": row["end"],
                                   "raw_text": row["text"], "reason": "重复广告句样式的无效识别候选；原稿保留"})
            if text:
                cleaned.append({**row, "text": text})
                kept_ids.add(row["id"])
        header = f"\n{section['section_id']} [{stamp(section['start'])}–{stamp(section['end'])}] {section['type']}：{section['label']}\n{section['notes']}\n"
        selected.append(header)
        selected.extend(groups(cleaned))
        if section["type"] == "学员个股快评":
            quick.append(header)
            quick.extend(groups(cleaned))
    (ROOT / "研究文本_切分版.txt").write_text("\n".join(selected) + "\n", encoding="utf-8")
    (ROOT / "学员个股快评.txt").write_text("\n".join(quick) + "\n", encoding="utf-8")
    (ROOT / "切分索引.json").write_text(json.dumps(sections, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    flat = [{"section_id":s["section_id"], "start":s["start"], "end":s["end"], "start_time":stamp(s["start"]),
             "end_time":stamp(s["end"]), "keep":s["keep"], "type":s["type"], "label":s["label"], "notes":s["notes"]} for s in sections]
    write_csv(ROOT / "切分索引.csv", flat, list(flat[0]))

    with (VOCAB / "a_share_names.csv").open(encoding="utf-8-sig", newline="") as f:
        by_name = {row["name"]:row for row in csv.DictReader(f)}
    corrections = []
    for candidate in corrections_spec:
        target = by_name[candidate["candidate"]]
        hits = [r for r in rows if candidate["raw"] in r["text"]]
        if not hits:
            continue
        corrections.append({"raw_name": candidate["raw"], "candidate": candidate["candidate"], "ts_code":target["ts_code"],
            "status": candidate.get("status", "文本语境候选，未核图或回听"), "first_time":stamp(hits[0]["start"]),
            "occurrences":len(hits), "source_refs":"|".join(r["id"] for r in hits), "example":hits[0]["text"]})
    write_csv(ROOT / "股票名称校正候选.csv", corrections,
              ["raw_name","candidate","ts_code","status","first_time","occurrences","source_refs","example"])
    (ROOT / "无效识别候选.json").write_text(json.dumps(noise_rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    selected_source_seconds = sum(s["end"] - s["start"] for s in sections if s["keep"])
    complete_now = datetime.now(timezone.utc)
    elapsed_from_task = (complete_now - datetime.fromisoformat(spec["created_utc"])).total_seconds()
    report.update(semantic_segmentation="complete", semantic_reviewer="current_codex_primary",
        semantic_review_mode="read_completed_chunks_while_asr_runs_then_finalize",
        semantic_model_latency_separately_measured=False, secondary_worker_used=False,
        overall_wall_seconds_from_task_creation=round(elapsed_from_task,3),
        retained_source_seconds=round(selected_source_seconds,3), original_segments=len(rows),
        retained_nonempty_segments=len(kept_ids), invalid_recognition_candidate_segments=len(noise_rows),
        sections=len(sections), stock_name_candidate_pairs=len(corrections),
        segmentation_assembly_seconds=round(time.monotonic()-began,3), finalized_utc=complete_now.isoformat(),
        checks={"source_timestamps_in_range":True,"section_coverage_complete":True,"raw_asr_preserved":True,
                "name_candidates_exist_in_local_dictionary":True,"visual_processing":False})
    (ROOT / "timing_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    duration = report["duration_seconds"]
    summary = (f"8月4日盘中直播：文本试跑结果\n\n视频时长：{duration/60:.2f}分钟\n"
        f"本地转写实际耗时：{report['asr_wall_seconds']/60:.2f}分钟（含模型加载和分块处理）\n"
        f"转写速度：{report['media_speed_multiple']:.2f}倍实时\n"
        f"从试跑任务创建到文本交付：{elapsed_from_task/60:.2f}分钟（包含首次接入修正、转写、并行阅读和切分）\n"
        "阅读切分与转写并行；没有单独实测Luna或其他worker的切分延迟。\n"
        f"切分后保留源视频时段：{selected_source_seconds/60:.2f}分钟\n"
        f"原始转写段数：{len(rows)}；研究文本非空段数：{len(kept_ids)}\n"
        f"无效识别候选段数：{len(noise_rows)}；股票名称校正候选：{len(corrections)}组\n\n"
        "文件：研究文本_切分版.txt、全文_带时间.txt、学员个股快评.txt、切分索引.csv、股票名称校正候选.csv。\n"
        "此试跑只处理这一场的文本，不核K线、委托或成交，不运行其他视频。\n"
        "股票错字已提供数据库候选；图上股票归属和不清楚的同音名称仍待以后核实。\n")
    (ROOT / "试跑结果.txt").write_text(summary, encoding="utf-8")
    status = json.loads((ROOT / "status.json").read_text(encoding="utf-8"))
    status.update(status="text_pilot_complete", finalized_utc=complete_now.isoformat(),current=None,child_pid=None)
    (ROOT / "status.json").write_text(json.dumps(status,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))

if __name__ == "__main__":
    main()
