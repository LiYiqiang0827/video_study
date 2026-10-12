#!/usr/bin/env python3
"""Extract desktop-clock evidence and map video time to civil-time intervals.

Six default frames are equally spaced between video seconds 30 and duration-30.
All original frames, clock crops, OCR observations and actual frame PTS remain
inspectable. Upload timestamps and dates discussed in the video are not clock
anchors. A displayed HH:MM is a 60-second interval, not a second-precise fact.
"""
from __future__ import annotations

import argparse
import bisect
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import struct
import subprocess
import sys
import uuid
from zoneinfo import ZoneInfo

sys.dont_write_bytecode = True
from tool_paths import data_root, external_path
from analysis_storage import sha256_file, atomic_json

def local_file(value):
    if "://" in str(value) or "\0" in str(value):
        raise ValueError("Only local filesystem paths are accepted")
    path = Path(value).expanduser().resolve(strict=True)
    if not path.is_file():
        raise ValueError("Source must be a local file")
    return path

def executable(value):
    if "://" in str(value) or "\0" in str(value):
        raise ValueError("Expected a local executable")
    resolved = shutil.which(os.path.expanduser(str(value)))
    if not resolved:
        raise ValueError(f"Executable unavailable: {value}")
    return os.path.abspath(resolved)

API_REFERENCES = [
    "https://developer.apple.com/documentation/vision/vnrecognizetextrequest",
    "https://developer.apple.com/documentation/vision/vnrecognizetextrequest/useslanguagecorrection",
    "https://ffmpeg.org/ffmpeg-filters.html#select_002c-aselect",
    "https://ffmpeg.org/ffmpeg-filters.html#showinfo",
]

# This production helper is embedded to keep the public entry point self-contained.
# Its generated source, compiler cache, executable and logs live in external runtime.
VISION_SWIFT = r'''import Foundation
import Vision
import ImageIO

var records: [[String: Any]] = []
for path in CommandLine.arguments.dropFirst() {
    do {
        let url = URL(fileURLWithPath: path)
        guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
              let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
            throw NSError(domain: "VideoClockOCR", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "Cannot read image"])
        }
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.usesLanguageCorrection = false
        request.revision = VNRecognizeTextRequestRevision3
        let supported = try request.supportedRecognitionLanguages()
        let wanted = ["zh-Hans", "en-US"]
        request.recognitionLanguages = wanted.filter { supported.contains($0) }
        if request.recognitionLanguages.isEmpty { request.recognitionLanguages = ["en-US"] }
        let handler = VNImageRequestHandler(cgImage: image, options: [:])
        try handler.perform([request])
        let observations = (request.results ?? []).map { observation -> [String: Any] in
            let box = observation.boundingBox
            let candidates = observation.topCandidates(3).map {
                ["text": $0.string, "confidence": Double($0.confidence)] as [String: Any]
            }
            return ["candidates": candidates,
                    "bounding_box": ["x": Double(box.origin.x), "y": Double(box.origin.y),
                                     "width": Double(box.size.width), "height": Double(box.size.height)]]
        }
        records.append(["image": path, "state": "complete", "width": image.width,
                        "height": image.height, "request_revision": request.revision,
                        "recognition_languages": request.recognitionLanguages,
                        "uses_language_correction": request.usesLanguageCorrection,
                        "bounding_box_origin": "bottom-left; normalized image coordinates",
                        "observations": observations])
    } catch {
        records.append(["image": path, "state": "failed", "error": String(describing: error)])
    }
}
let output: [String: Any] = ["engine": "macOS Vision VNRecognizeTextRequest",
                             "os_version": ProcessInfo.processInfo.operatingSystemVersionString,
                             "images": records]
let bytes = try JSONSerialization.data(withJSONObject: output, options: [.prettyPrinted, .sortedKeys])
FileHandle.standardOutput.write(bytes)
FileHandle.standardOutput.write(Data("\n".utf8))
'''


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def fresh_directory(value):
    directory = external_path(value)
    if directory.exists() and (not directory.is_dir() or any(directory.iterdir())):
        raise FileExistsError("Output already contains artifacts; choose a new directory")
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def png_size(path):
    with Path(path).open("rb") as handle:
        header = handle.read(24)
    if header[:8] != b"\x89PNG\r\n\x1a\n" or len(header) != 24:
        raise ValueError("Expected a PNG frame")
    return struct.unpack(">II", header[16:24])


def sample_times(duration, count=6, exclude_start_seconds=30, exclude_end_seconds=30):
    if not finite(duration) or duration <= 0 or isinstance(count, bool) or not isinstance(count, int) or count < 2:
        raise ValueError("Positive duration and at least two frames required")
    if any(not finite(v) or v < 0 for v in (exclude_start_seconds, exclude_end_seconds)):
        raise ValueError("Excluded head/tail durations must be finite and nonnegative")
    last = duration - exclude_end_seconds
    if last <= exclude_start_seconds:
        raise ValueError("No interval remains after removing head/tail")
    return [exclude_start_seconds + (last - exclude_start_seconds) * index / (count - 1)
            for index in range(count)]


def run_logged(command, log, *, stdout_path=None, timeout=600):
    with Path(log).open("xb") as stderr:
        if stdout_path:
            with Path(stdout_path).open("xb") as stdout:
                subprocess.run([str(v) for v in command], stdin=subprocess.DEVNULL,
                               stdout=stdout, stderr=stderr, check=True, timeout=timeout)
        else:
            subprocess.run([str(v) for v in command], stdin=subprocess.DEVNULL,
                           stdout=stderr, stderr=stderr, check=True, timeout=timeout)


def vision_executable(runtime_dir=None, swiftc="swiftc"):
    if platform.system() != "Darwin":
        raise RuntimeError("macOS Vision OCR requires macOS")
    runtime = external_path(runtime_dir or data_root() / "runtime" / "vision_ocr")
    compiler = executable(swiftc)
    version = subprocess.run([compiler, "--version"], check=True, capture_output=True, text=True).stdout
    identity = hashlib.sha256((VISION_SWIFT + version + platform.platform()).encode()).hexdigest()
    directory = runtime / identity[:20]
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "vision_ocr"
    with (directory / ".compile.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not target.exists():
            source = directory / "vision_ocr.swift"
            source.write_text(VISION_SWIFT, encoding="utf-8")
            cache, temp = runtime / "module_cache", runtime / "tmp"
            cache.mkdir(exist_ok=True)
            temp.mkdir(exist_ok=True)
            temporary = directory / ("vision_ocr." + uuid.uuid4().hex)
            env = {**os.environ, "TMPDIR": str(temp), "CLANG_MODULE_CACHE_PATH": str(cache),
                   "SWIFT_MODULECACHE_PATH": str(cache)}
            command = [compiler, "-O", "-module-cache-path", str(cache), str(source), "-o", str(temporary)]
            try:
                with (directory / "compile.log").open("wb") as log:
                    subprocess.run(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                   env=env, check=True, timeout=180)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
            atomic_json(directory / "build.json", {"source_sha256": hashlib.sha256(VISION_SWIFT.encode()).hexdigest(),
                        "compiler": compiler, "compiler_version": version, "command": command,
                        "binary_sha256": sha256_file(target), "api_references": API_REFERENCES})
    return target


def ocr_clock_images(paths, output_path, *, runtime_dir=None, swiftc="swiftc"):
    output_path = external_path(output_path)
    if output_path.exists():
        raise FileExistsError("OCR output already exists")
    binary = vision_executable(runtime_dir, swiftc)
    command = [str(binary)] + [str(local_file(p)) for p in paths]
    run_logged(command, output_path.with_suffix(".log"), stdout_path=output_path, timeout=max(120, len(paths) * 30))
    result = json.loads(output_path.read_text(encoding="utf-8"))
    result["binary"] = str(binary)
    result["binary_sha256"] = sha256_file(binary)
    atomic_json(output_path, result)
    return result


def extract_clock_frames(video, output_dir, *, count=6, exclude_start_seconds=30,
                         exclude_end_seconds=30, roi=(.84, .90, .16, .10),
                         ocr=True, runtime_dir=None, ffmpeg="ffmpeg", ffprobe="ffprobe",
                         swiftc="swiftc", expected_sha256=None):
    """Return/write frame_manifest; exact decoded frame PTS are kept with evidence.

    ROI uses normalized x/y/width/height with a top-left image origin. Original
    full frames/crops and separately upscaled crops are all retained. A full
    ffprobe pass selects actual frames; FFmpeg selects those same frame indexes.
    """
    video = local_file(video)
    ffmpeg, ffprobe = executable(ffmpeg), executable(ffprobe)
    if len(roi) != 4 or any(not finite(v) for v in roi) or not (0 <= roi[0] < 1 and 0 <= roi[1] < 1
            and 0 < roi[2] <= 1 - roi[0] + 1e-9 and 0 < roi[3] <= 1 - roi[1] + 1e-9):
        raise ValueError("ROI must be a normalized image rectangle")
    source_hash, source_bytes = sha256_file(video), video.stat().st_size
    if expected_sha256 is not None and source_hash != expected_sha256:
        raise ValueError("Source video SHA-256 differs from verified metadata")
    directory = fresh_directory(output_dir)
    commands = []
    manifest = {"schema_version": 1, "state": "running", "source_video": str(video),
                "source_video_sha256": source_hash, "source_bytes": source_bytes,
                "code_sha256": sha256_file(Path(__file__)), "api_references": API_REFERENCES,
                "clock_role": "recording desktop clock; upload time and discussion trading date are separate",
                "frames": []}
    atomic_json(directory / "frame_manifest.json", manifest)
    try:
        probe_path = directory / "video_probe.json"
        command = [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", video]
        commands.append(command)
        run_logged(command, directory / "video_probe.log", stdout_path=probe_path)
        profile = json.loads(probe_path.read_text())
        stream = next((s for s in profile["streams"] if s.get("codec_type") == "video"), None)
        if stream is None:
            raise ValueError("Source has no video stream")
        duration = float(profile["format"]["duration"])
        container_start = float(profile["format"].get("start_time", 0))
        if not finite(container_start):
            raise ValueError("Container start timestamp unavailable")
        requested = sample_times(duration, count, exclude_start_seconds, exclude_end_seconds)
        timeline_path = directory / "frame_timestamps.json"
        command = [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_frames",
                   "-show_entries", "frame=best_effort_timestamp_time,pts_time", "-of", "json", video]
        commands.append(command)
        run_logged(command, directory / "frame_timestamps.log", stdout_path=timeline_path)
        decoded = json.loads(timeline_path.read_text()).get("frames", [])
        times = []
        for frame in decoded:
            value = float(frame.get("best_effort_timestamp_time", frame.get("pts_time", "nan")))
            if not math.isfinite(value):
                raise ValueError("A decoded frame has no usable timestamp")
            times.append(value - container_start)
        if not times or any(second < first for first, second in zip(times, times[1:])):
            raise ValueError("Missing/nonmonotonic frame timeline; review cuts/timestamps separately")
        indices = [bisect.bisect_left(times, value) for value in requested]
        if any(i >= len(times) for i in indices) or len(set(indices)) != count:
            raise ValueError("Requested sample frames are missing or not distinct")
        frames, crops = directory / "frames", directory / "roi"
        frames.mkdir(); crops.mkdir()
        selection = "+".join(f"eq(n,{index})" for index in indices)
        command = [ffmpeg, "-hide_banner", "-v", "info", "-nostdin", "-n", "-copyts", "-i", video,
                   "-map", "0:v:0", "-an", "-vf", f"select='{selection}',showinfo",
                   "-fps_mode", "passthrough", "-frames:v", str(count), frames / "frame_%03d.png"]
        commands.append(command)
        run_logged(command, directory / "extract_frames.log")
        shown = [float(value) for value in re.findall(r"\bn:\s*\d+\s+pts:\s*[-\d]+\s+pts_time:([-\d.eE+]+)", (directory / "extract_frames.log").read_text())]
        if len(shown) != count:
            raise ValueError("Cannot verify selected frame timestamps from FFmpeg showinfo")
        for order, index in enumerate(indices, 1):
            full = frames / f"frame_{order:03d}.png"
            width, height = png_size(full)
            expected_pts = times[index] + container_start
            if abs(shown[order - 1] - expected_pts) > .0006:
                raise ValueError("FFmpeg selected frame PTS differs from probed frame")
            x, y = math.floor(width * roi[0]), math.floor(height * roi[1])
            w, h = min(width - x, round(width * roi[2])), min(height - y, round(height * roi[3]))
            crop, enlarged = crops / f"clock_{order:03d}.png", crops / f"clock_{order:03d}_4x.png"
            command = [ffmpeg, "-hide_banner", "-v", "error", "-nostdin", "-n", "-i", full,
                       "-vf", f"crop={w}:{h}:{x}:{y}", "-frames:v", "1", crop]
            commands.append(command); run_logged(command, directory / f"crop_{order:03d}.log")
            command = [ffmpeg, "-hide_banner", "-v", "error", "-nostdin", "-n", "-i", crop,
                       "-vf", "scale=iw*4:ih*4:flags=neighbor", "-frames:v", "1", enlarged]
            commands.append(command); run_logged(command, directory / f"enlarge_{order:03d}.log")
            manifest["frames"].append({"frame_id": f"frame_{order:03d}", "frame_index": index,
                "requested_video_seconds": requested[order - 1], "video_seconds": times[index],
                "source_pts_seconds": expected_pts, "ffmpeg_showinfo_pts_seconds": shown[order - 1],
                "video_timestamp_precision_seconds": .000001, "frame_path": str(full),
                "frame_sha256": sha256_file(full), "roi_path": str(crop), "roi_sha256": sha256_file(crop),
                "ocr_image_path": str(enlarged), "ocr_image_sha256": sha256_file(enlarged),
                "frame_size": [width, height], "roi_pixels": [x, y, w, h], "visually_checked": False})
        manifest.update(duration_seconds=duration, container_start_pts_seconds=container_start,
                        sampling={"count": count, "exclude_start_seconds": exclude_start_seconds,
                                  "exclude_end_seconds": exclude_end_seconds, "requested_seconds": requested,
                                  "policy": "equal spacing including both retained-interval endpoints; first decoded frame at/after each target"},
                        roi_normalized=list(roi), commands=[[str(v) for v in command] for command in commands])
        if ocr:
            try:
                result = ocr_clock_images([f["ocr_image_path"] for f in manifest["frames"]],
                    directory / "ocr.json", runtime_dir=runtime_dir, swiftc=swiftc)
                manifest["ocr_state"] = "complete" if all(i.get("state") == "complete" for i in result["images"]) else "partial"
                for frame, observation in zip(manifest["frames"], result["images"]):
                    frame["ocr"] = observation
            except Exception as error:
                manifest.update(ocr_state="failed", ocr_error={"type": type(error).__name__, "message": str(error)})
        else:
            manifest["ocr_state"] = "not_requested"
        if sha256_file(video) != source_hash or video.stat().st_size != source_bytes:
            raise ValueError("Source video changed during extraction")
        manifest["state"] = "complete"
    except BaseException as error:
        manifest.update(state="failed", exception={"type": type(error).__name__, "message": str(error)})
        atomic_json(directory / "frame_manifest.json", manifest)
        raise
    atomic_json(directory / "frame_manifest.json", manifest)
    return manifest


def clock_anchor_from_ocr(frame):
    """Conservatively pair a desktop date with its nearby time observation.

    Keep other time strings as auxiliary observations; never borrow their seconds
    to increase the desktop clock's precision. Ambiguous dates/times stay pending.
    """
    anchor = {"frame_id": frame["frame_id"], "video_seconds": frame["video_seconds"],
              "clock_source": "desktop_date_and_time", "visually_checked": False,
              "video_timestamp_precision_seconds": frame.get("video_timestamp_precision_seconds", .000001),
              "frame_path": frame["frame_path"], "roi_path": frame["roi_path"],
              "ocr_observations": frame.get("ocr", {}).get("observations", []), "state": "unresolved"}
    dates, times = [], []
    for observation in anchor["ocr_observations"]:
        candidates = observation.get("candidates", [])
        if not candidates:
            continue
        candidate = candidates[0]
        text = candidate.get("text", "")
        box = observation.get("bounding_box", {})
        date = re.search(r"(?<!\d)(20\d{2})\s*[/\-.年]\s*(\d{1,2})\s*[/\-.月]\s*(\d{1,2})(?:日)?(?!\d)", text)
        if date:
            try:
                parsed = dt.date(*map(int, date.groups())).isoformat()
                dates.append({"date": parsed, "raw_text": text, "box": box, "confidence": candidate.get("confidence")})
            except ValueError:
                pass
        found = re.search(r"(?<!\d)(上午|下午|AM|PM|am|pm)?\s*(\d{1,2})\s*[:：]\s*(\d{2})(?:\s*[:：]\s*(\d{2}))?(?!\d)", text)
        if found:
            prefix, hour, minute, second = found.groups()
            hour, minute = int(hour), int(minute)
            if not 0 <= hour <= 23 or not 0 <= minute <= 59 or second is not None and not 0 <= int(second) <= 59:
                continue
            if prefix:
                if not 1 <= hour <= 12:
                    continue
                hour = hour % 12 + (12 if prefix.lower() in {"下午", "pm"} else 0)
            times.append({"hour": hour, "minute": minute, "second": int(second) if second is not None else None,
                          "prefix": prefix, "raw_text": text, "box": box, "confidence": candidate.get("confidence")})
    anchor["auxiliary_time_observations"] = times
    if len({date["date"] for date in dates}) != 1:
        anchor["unresolved_reason"] = "missing_or_ambiguous_desktop_date"
        return anchor
    date = min(dates, key=lambda item: item["box"].get("y", 1))
    if not times:
        anchor["unresolved_reason"] = "missing_desktop_time"
        return anchor
    def distance(item):
        return abs(item["box"].get("y", 1) - date["box"].get("y", 0)) + abs(item["box"].get("x", 1) - date["box"].get("x", 0))
    chosen = min(times, key=distance)
    # A neighboring second-precise software clock is not the minute-precise OS clock.
    if chosen["second"] is not None and any(item["prefix"] for item in times):
        chosen = min([item for item in times if item["prefix"]], key=distance)
    if abs(chosen["box"].get("y", 1) - date["box"].get("y", 0)) > .35:
        anchor["unresolved_reason"] = "time_observation_not_adjacent_to_desktop_date"
        return anchor
    if chosen["second"] is not None:
        anchor["unresolved_reason"] = "second_precision_clock_source_requires_visual_verification"
        return anchor
    if not chosen["prefix"] and chosen["hour"] in range(1, 13):
        anchor["unresolved_reason"] = "unmarked_12_or_24_hour_time_requires_visual_check"
        return anchor
    display = f"{chosen['hour']:02d}:{chosen['minute']:02d}"
    if chosen["second"] is not None:
        display += f":{chosen['second']:02d}"
    anchor.update(state="parsed", display_date=date["date"], display_time=display,
                  precision_seconds=1 if chosen["second"] is not None else 60,
                  display_semantics="floor_to_display_precision", date_raw_text=date["raw_text"],
                  time_raw_text=chosen["raw_text"], ocr_confidence=min(date["confidence"] or 0, chosen["confidence"] or 0))
    return anchor


def iso_bj(epoch):
    return dt.datetime.fromtimestamp(epoch, ZoneInfo("Asia/Shanghai")).isoformat(timespec="milliseconds")


def align_clock_anchors(anchors, duration, timezone="Asia/Shanghai", *, timezone_confirmed=False,
                        output_path=None):
    """Return an interval intersection for epoch_seconds - video_seconds.

    HH:MM implies [displayed minute, next minute). Quantization is retained per
    anchor; intersecting anchors can narrow the conditional range. No outlier is
    silently discarded and no upload timestamp or trading date is substituted.
    """
    if not finite(duration) or duration <= 0:
        raise ValueError("Duration must be finite and positive")
    zone = ZoneInfo(timezone)
    used, unresolved = [], []
    for index, original in enumerate(anchors):
        anchor = dict(original)
        anchor.setdefault("anchor_index", index)
        try:
            video_seconds = anchor.get("video_seconds")
            precision = anchor.get("precision_seconds")
            timestamp_precision = anchor.get("video_timestamp_precision_seconds", .000001)
            if not finite(video_seconds) or not 0 <= video_seconds <= duration:
                raise ValueError("invalid_video_seconds")
            if precision not in {1, 60} or isinstance(precision, bool):
                raise ValueError("missing_or_unsupported_clock_precision")
            if anchor.get("display_semantics", "floor_to_display_precision") != "floor_to_display_precision":
                raise ValueError("clock_display_rounding_unknown")
            if not finite(timestamp_precision) or timestamp_precision < 0:
                raise ValueError("invalid_timestamp_precision")
            display_date, display_time = anchor["display_date"], anchor["display_time"]
            expected = r"\d{2}:\d{2}" if precision == 60 else r"\d{2}:\d{2}:\d{2}"
            if not re.fullmatch(expected, display_time):
                raise ValueError("display_time_precision_conflict")
            base = dt.datetime.fromisoformat(display_date + "T" + display_time)
            if base.tzinfo is not None:
                raise ValueError("display_time_must_not_contain_invented_timezone")
            base = base.replace(tzinfo=zone)
            epoch = base.timestamp()
            half = timestamp_precision / 2
            anchor.update(state="used", recording_clock={"display_date": display_date, "display_time": display_time,
                          "precision_seconds": precision, "timezone": timezone, "timezone_confirmed": timezone_confirmed},
                          displayed_epoch_seconds_range=[epoch, epoch + precision],
                          offset_seconds_range=[epoch - video_seconds - half, epoch + precision - video_seconds + half],
                          interval_upper_exclusive=True)
            used.append(anchor)
        except (ValueError, KeyError, TypeError) as error:
            anchor.update(state="unresolved", alignment_exclusion_reason=str(error))
            unresolved.append(anchor)
    low = max((a["offset_seconds_range"][0] for a in used), default=None)
    high = min((a["offset_seconds_range"][1] for a in used), default=None)
    consistent = bool(used) and low < high
    progression = []
    ordered = sorted(used, key=lambda a: a["video_seconds"])
    for first, second in zip(ordered, ordered[1:]):
        elapsed = second["video_seconds"] - first["video_seconds"]
        clock_low = second["displayed_epoch_seconds_range"][0] - first["displayed_epoch_seconds_range"][1]
        clock_high = second["displayed_epoch_seconds_range"][1] - first["displayed_epoch_seconds_range"][0]
        progression.append({"from": first.get("frame_id", first["anchor_index"]),
                            "to": second.get("frame_id", second["anchor_index"]),
                            "video_elapsed_seconds": elapsed, "clock_elapsed_seconds_range": [clock_low, clock_high],
                            "consistent": clock_low <= elapsed <= clock_high})
    enough = len(used) >= 2 and ordered[-1]["video_seconds"] > ordered[0]["video_seconds"]
    visual = all(a.get("visually_checked") is True for a in used) if used else False
    assumptions = ["Desktop clock is running normally and represents recording wall-clock time.",
                   "Displayed digits are truncated to their stated minute/second precision.",
                   "A single uninterrupted video-to-clock offset applies; sparse frames cannot rule out unsampled edits."]
    if not timezone_confirmed:
        assumptions.append(f"Clock timezone is assumed to be {timezone}; it is not displayed or independently confirmed.")
    report = {"schema_version": 1, "state": "aligned" if consistent else "inconsistent" if used else "unavailable",
              "duration_seconds": duration, "recording_clock_timezone": timezone,
              "timezone_confirmed": bool(timezone_confirmed), "beijing_timezone": "Asia/Shanghai",
              "anchors": used + unresolved, "used_anchor_count": len(used), "unresolved_anchors": unresolved,
              "offset_definition": "Unix epoch seconds of recording clock minus video-relative seconds",
              "offset_seconds_range": [low, high] if consistent else None,
              "interval_upper_exclusive": True, "sampled_clock_progression": progression,
              "multi_anchor_consistent": consistent and enough, "all_used_anchors_visually_checked": visual,
              "confirmed": bool(consistent and enough and visual and timezone_confirmed),
              "confidence_grade": "multi_anchor_consistent_timezone_confirmed" if consistent and enough and visual and timezone_confirmed
                  else "multi_anchor_consistent_timezone_assumed" if consistent and enough and visual
                  else "ocr_only_multi_anchor_consistent" if consistent and enough else "single_anchor_conditional" if consistent else "not_aligned",
              "assumptions": assumptions,
              "edit_check": "No inconsistency detected at sampled anchors; this does not prove absence of cuts." if consistent
                  else "Anchor offsets do not intersect: OCR errors, clock stalls/time changes or video edits require review." if used else "No usable clock anchors.",
              "discussion_trading_date": None, "session_type": None,
              "classification_note": "Recording desktop time is separate from the date discussed and live/review classification. Trading windows alone do not prove session type."}
    if consistent:
        midpoint = (low + high) / 2
        report.update(offset_estimate_seconds=midpoint, point_estimate_is_estimated=True,
                      start_beijing_interval=[iso_bj(low), iso_bj(high)],
                      end_beijing_interval=[iso_bj(low + duration), iso_bj(high + duration)],
                      start_beijing_estimate=iso_bj(midpoint), end_beijing_estimate=iso_bj(midpoint + duration),
                      residual_uncertainty_seconds=high - low,
                      endpoint_display_note="Interval endpoints derive from displayed clock bins and frame PTS; fractional digits do not imply second-precise clock observations.")
    if output_path:
        output_path = external_path(output_path)
        if output_path.exists():
            raise FileExistsError("Alignment already exists; use a new output path")
        atomic_json(output_path, report)
    return report


def map_video_interval(start, end, alignment):
    if not finite(start) or not finite(end) or not 0 <= start <= end <= alignment["duration_seconds"]:
        raise ValueError("Video interval is outside the source duration")
    bounds = alignment.get("offset_seconds_range")
    if bounds is None:
        return {"video_seconds_range": [start, end], "state": "unavailable", "beijing_range": None,
                "reason": "No consistent video-to-recording-clock alignment"}
    low, high = bounds
    return {"video_seconds_range": [start, end], "state": "mapped", "timezone": "Asia/Shanghai",
            "start_beijing_interval": [iso_bj(low + start), iso_bj(high + start)],
            "end_beijing_interval": [iso_bj(low + end), iso_bj(high + end)],
            "beijing_range": [iso_bj(low + start), iso_bj(high + end)],
            "estimated_start_beijing": iso_bj((low + high) / 2 + start),
            "estimated_end_beijing": iso_bj((low + high) / 2 + end),
            "point_estimate_is_estimated": True, "confirmed": alignment.get("confirmed", False),
            "clock_precision_seconds": sorted({a["precision_seconds"] for a in alignment["anchors"] if a.get("state") == "used"}),
            "interval_upper_exclusive": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    extract = commands.add_parser("extract", help="Extract full frames/crops and local Vision OCR")
    extract.add_argument("--video", type=Path, required=True)
    extract.add_argument("--output-dir", type=Path, required=True)
    extract.add_argument("--count", type=int, default=6)
    extract.add_argument("--exclude-start-seconds", type=float, default=30)
    extract.add_argument("--exclude-end-seconds", type=float, default=30)
    extract.add_argument("--roi", type=float, nargs=4, default=(.84, .90, .16, .10), metavar=("X", "Y", "W", "H"))
    extract.add_argument("--no-ocr", action="store_true")
    extract.add_argument("--runtime-dir", type=Path)
    extract.add_argument("--ffmpeg", default="ffmpeg")
    extract.add_argument("--ffprobe", default="ffprobe")
    extract.add_argument("--swiftc", default="swiftc")
    extract.add_argument("--expected-sha256")
    align = commands.add_parser("align", help="Align a JSON anchor list without substituting upload metadata")
    align.add_argument("--anchors", type=Path, required=True)
    align.add_argument("--duration", type=float, required=True)
    align.add_argument("--timezone", default="Asia/Shanghai")
    align.add_argument("--timezone-confirmed", action="store_true")
    align.add_argument("--output", type=Path, required=True)
    map_command = commands.add_parser("map", help="Map a video interval using existing alignment")
    map_command.add_argument("--alignment", type=Path, required=True)
    map_command.add_argument("--start", type=float, required=True)
    map_command.add_argument("--end", type=float, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "extract":
            manifest = extract_clock_frames(args.video, args.output_dir, count=args.count,
                exclude_start_seconds=args.exclude_start_seconds, exclude_end_seconds=args.exclude_end_seconds,
                roi=args.roi, ocr=not args.no_ocr, runtime_dir=args.runtime_dir, ffmpeg=args.ffmpeg,
                ffprobe=args.ffprobe, swiftc=args.swiftc, expected_sha256=args.expected_sha256)
            anchors = [clock_anchor_from_ocr(frame) for frame in manifest["frames"]]
            atomic_json(external_path(args.output_dir) / "ocr_anchors.json", {"anchors": anchors, "visually_checked": False})
            report = {"state": manifest["state"], "ocr_state": manifest["ocr_state"],
                      "frame_manifest": str(external_path(args.output_dir) / "frame_manifest.json"),
                      "parsed_anchors": sum(a["state"] == "parsed" for a in anchors)}
        elif args.command == "align":
            value = json.loads(local_file(args.anchors).read_text(encoding="utf-8"))
            anchors = value.get("anchors", []) if isinstance(value, dict) else value
            report = align_clock_anchors(anchors, args.duration, args.timezone,
                        timezone_confirmed=args.timezone_confirmed, output_path=args.output)
        else:
            report = map_video_interval(args.start, args.end, json.loads(local_file(args.alignment).read_text(encoding="utf-8")))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
