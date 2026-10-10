"""Bounded, original-resolution frame review with measured source timestamps.

No recognition model is loaded. Source media and extraction evidence are read-only.
Frames are independently sought, so long lectures need not be decoded in full.
"""
from __future__ import annotations

import argparse
import html
import json
import math
import re
import subprocess
from pathlib import Path

from media_platform.evidence import load_evidence, sha256_file
from media_platform.ingest import atomic_write, parse_time, probe, stamp


def sample_frame(source: Path, requested: float, target: Path, source_start: float = 0) -> dict:
    result = subprocess.run([
        "ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "info",
        "-ss", str(requested), "-copyts", "-threads", "2", "-i", str(source),
        "-map", "0:v:0", "-frames:v", "1", "-an", "-vf", "showinfo",
        "-q:v", "2", str(target),
    ], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    if result.returncode:
        raise RuntimeError(result.stderr[-2000:])
    matches = re.findall(r"showinfo[^\n]*?pts_time:([-\d.]+)", result.stderr)
    if not target.is_file() or not matches:
        raise RuntimeError(f"No frame or measured PTS at {requested}")
    pts = float(matches[0])
    actual = pts - source_start
    if not requested - .1 <= actual <= requested + 1:
        raise ValueError(f"Unexpected seek result: requested={requested}, decoded={actual}")
    return {"requested_time": requested, "time": round(actual, 6), "decoded_pts": pts,
            "frame": target.name, "sha256": sha256_file(target)}


def make_sheets(root: Path, rows: list[dict], title: str) -> list[str]:
    from PIL import Image, ImageDraw, ImageFont
    candidates = (Path("C:/Windows/Fonts/msyh.ttc"), Path("C:/Windows/Fonts/arial.ttf"))
    font = next((ImageFont.truetype(str(p), 23) for p in candidates if p.exists()), ImageFont.load_default())
    sheets = []
    for index in range(math.ceil(len(rows) / 6)):
        subset = rows[index * 6:index * 6 + 6]
        canvas = Image.new("RGB", (1920, 1760), "white")
        draw = ImageDraw.Draw(canvas)
        draw.text((10, 8), title, font=font, fill="black")
        for position, row in enumerate(subset):
            x, y = position % 2 * 960, position // 2 * 570 + 40
            draw.text((x + 10, y), f"{stamp(row['time'])}  ({row['time']:.3f}s)", font=font, fill="black")
            with Image.open(root / row["frame"]) as image:
                image.thumbnail((960, 540))
                canvas.paste(image, (x, y + 30))
        name = f"sheet_{index + 1:03d}.jpg"
        canvas.save(root / name, quality=90)
        sheets.append(name)
    return sheets


def make_html(root: Path, state: dict, segments: list[dict]) -> None:
    escape = html.escape
    cards = []
    for row in state["frames"]:
        nearby = [s for s in segments if s["end"] >= row["time"] - 25 and s["start"] <= row["time"] + 25]
        text = " ".join(s["text"] for s in nearby)
        cards.append(f'<article><h2>{stamp(row["time"])} · {row["time"]:.3f}s</h2>'
            f'<a href="{escape(row["frame"])}"><img loading="lazy" src="{escape(row["frame"])}"></a>'
            f'<p>{escape(text)}</p></article>')
    source = Path(state["source"])
    body = '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
    body += '<style>body{font:16px/1.7 system-ui;background:#f4f5f7;color:#18212a;margin:0 auto;max-width:1320px;padding:24px}article{background:white;padding:20px;margin:24px 0;border-radius:12px}img{width:100%}input{font:inherit;width:90%;padding:12px}h2{font-size:20px}</style>'
    body += f'<h1>{escape(source.stem)} · 画面与讲解复核</h1><p>画面保留原分辨率；时间来自实际解码 PTS。附近语音是自动识别候选。点击画面可查看原图。</p><input id="query" placeholder="搜索附近讲解"><main>'
    body += "".join(cards) + '</main><script>document.querySelector("#query").oninput=e=>document.querySelectorAll("article").forEach(a=>a.hidden=!a.textContent.includes(e.target.value));</script>'
    atomic_write(root / "index.html", body)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("media", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--times", help="Comma-separated source seconds or HH:MM:SS")
    parser.add_argument("--interval", type=float, default=60)
    parser.add_argument("--max-frames", type=int, default=300)
    parser.add_argument("--extraction", type=Path, help="Completed evidence to display beside the frames")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if not math.isfinite(args.interval) or args.interval <= 0 or args.max_frames < 1:
        parser.error("Interval and frame limit must be positive")
    source = args.media.resolve(strict=True)
    if not source.is_file():
        parser.error("Select one media file")
    root = args.output.resolve()
    metadata = probe(source)
    duration = float(metadata["format"]["duration"])
    source_start = float(metadata["format"].get("start_time", 0))
    times = sorted(set(parse_time(s.strip()) for s in args.times.split(","))) if args.times else [round(.5 + n * args.interval, 6) for n in range(math.ceil((duration - .5) / args.interval))]
    if not times or any(t < 0 or t >= duration for t in times) or len(times) > args.max_frames:
        parser.error("Frame times must fit duration and explicit frame limit")
    source_hash = sha256_file(source)
    params = {"source_sha256": source_hash, "times": times, "method": "accurate-seek-copyts-original-v1"}
    manifest_file = root / "frame_manifest.json"
    if root.exists():
        if not args.resume or not manifest_file.is_file():
            raise FileExistsError("Use a new output or --resume with matching recorded work")
        state = json.loads(manifest_file.read_text(encoding="utf-8"))
        if state["params"] != params:
            raise ValueError("Source or frame parameters changed")
        for row in state["frames"]:
            frame = (root / row["frame"]).resolve()
            if not frame.is_relative_to(root) or sha256_file(frame) != row["sha256"]:
                raise ValueError("Recorded frame changed")
    else:
        root.mkdir(parents=True)
        state = {"schema": 1, "source": str(source), "duration": duration, "params": params, "frames": [], "status": "running"}
        atomic_write(manifest_file, state)
    segments = []
    if args.extraction:
        bundle = load_evidence(args.extraction)
        if bundle["media_asset"]["source_sha256"] != source_hash:
            raise ValueError("Speech and frames have different source hashes")
        segments = bundle["speech_segments"]
        state["speech_extraction"] = str(args.extraction.resolve())
    completed = {r["requested_time"] for r in state["frames"]}
    try:
        for index, second in enumerate(times):
            if second in completed:
                continue
            frame = root / f"frame_{index:05d}_{int(second * 1000):09d}.jpg"
            state["frames"].append(sample_frame(source, second, frame, source_start))
            atomic_write(manifest_file, state)
            if (index + 1) % 10 == 0:
                print(f"{source.name}: {index + 1}/{len(times)} frames", flush=True)
        state["frames"].sort(key=lambda r: r["requested_time"])
        state["sheets"] = make_sheets(root, state["frames"], source.stem)
        make_html(root, state, segments)
        state["status"] = "complete"
        atomic_write(manifest_file, state)
    except BaseException:
        state["status"] = "interrupted"
        atomic_write(manifest_file, state)
        raise
    print(root, flush=True)


if __name__ == "__main__":
    main()
