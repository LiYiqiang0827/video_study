"""Versioned, source-linked evidence view over a completed local extraction."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0"


def sha256_file(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


@dataclass(frozen=True)
class EvidenceRef:
    source_sha256: str
    modality: str
    start: float
    end: float
    artifact: str | None = None
    artifact_sha256: str | None = None


@dataclass(frozen=True)
class MediaAsset:
    source_path: str
    source_sha256: str
    size_bytes: int
    playable_duration_seconds: float
    selected_interval: tuple[float, float]
    streams: list[dict[str, Any]]


@dataclass(frozen=True)
class SpeechSegment:
    id: str
    start: float
    end: float
    text: str
    speaker_id: str | None
    confidence: dict[str, float | None]
    review_required: bool
    evidence: EvidenceRef


@dataclass(frozen=True)
class VisualEvent:
    id: str
    time: float
    frame: str
    frame_sha256: str
    change_score: float | None
    ocr: list[dict[str, Any]]
    evidence: EvidenceRef


@dataclass(frozen=True)
class DerivedArtifact:
    path: str
    sha256: str
    kind: str


@dataclass(frozen=True)
class RunManifest:
    extraction_version: str
    params_sha256: str
    params: dict[str, Any]
    steps: dict[str, str]
    model: dict[str, Any] | None
    chunks: list[dict[str, Any]]


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def build_evidence(output: Path, manifest: dict | None = None, *, verify: bool = True) -> dict:
    """Produce a neutral view; never infer speaker identity or continuous motion."""
    output = output.resolve()
    manifest = manifest or _read(output / "manifest.json")
    source_hash = manifest["source_sha256"]
    steps = manifest["steps"]
    artifacts = []
    for name, expected in manifest.get("outputs", {}).items():
        path = output / name
        if verify and (not path.is_file() or sha256_file(path) != expected):
            raise ValueError(f"Extraction output hash mismatch: {name}")
        artifacts.append(asdict(DerivedArtifact(name, expected, "extraction")))
    speech = []
    if steps.get("audio") == "complete":
        if "speech.json" not in manifest.get("outputs", {}):
            raise ValueError("Completed audio lacks speech hash")
        for row in _read(output / "speech.json"):
            begin, end = float(row["start"]), float(row["end"])
            if not manifest["start"] - 0.1 <= begin < end <= manifest["end"] + 0.1:
                raise ValueError("Speech timestamp outside selected interval")
            speech.append(asdict(SpeechSegment(
                id=row["id"], start=begin, end=end, text=row["text"], speaker_id=None,
                confidence={"avg_logprob": row.get("avg_logprob"),
                            "no_speech_prob": row.get("no_speech_prob")},
                review_required=bool(row.get("review", False)),
                evidence=EvidenceRef(source_hash, "audio", begin, end))))
    visual = []
    if steps.get("video") == "complete":
        if "visual.json" not in manifest.get("outputs", {}):
            raise ValueError("Completed video lacks visual hash")
        for index, row in enumerate(_read(output / "visual.json"), 1):
            moment = float(row["time"])
            frame = row["frame"]
            frame_path = (output / frame).resolve()
            if not frame_path.is_relative_to(output) or not manifest["start"] <= moment <= manifest["end"]:
                raise ValueError("Frame path or timestamp outside extraction")
            if verify and sha256_file(frame_path) != row["frame_sha256"]:
                raise ValueError(f"Frame hash mismatch: {frame}")
            visual.append(asdict(VisualEvent(
                id=f"v{index:05d}", time=moment, frame=frame,
                frame_sha256=row["frame_sha256"], change_score=row.get("change"),
                ocr=[{"text": item["text"], "confidence": item.get("confidence"),
                      "origin": "frame_ocr"} for item in row.get("ocr", [])],
                evidence=EvidenceRef(source_hash, "video_frame", moment, moment,
                                     frame, row["frame_sha256"]))))
    bundle = {
        "schema_version": SCHEMA_VERSION,
        "media_asset": asdict(MediaAsset(manifest["source"], source_hash,
            manifest["source_bytes"], manifest["duration"],
            (manifest["start"], manifest["end"]), manifest.get("streams", []))),
        "speech_segments": speech,
        "visual_events": visual,
        "derived_artifacts": artifacts,
        "run_manifest": asdict(RunManifest(manifest["version"], manifest["params_sha256"],
            manifest.get("params", {}), dict(steps), manifest.get("model"), manifest.get("chunks", []))),
        "limitations": ["ASR text and OCR are unverified; speaker IDs and continuous actions are not inferred"],
    }
    return bundle


def load_evidence(output: Path, *, verify: bool = True) -> dict:
    output = output.resolve(strict=True)
    manifest = _read(output / "manifest.json")
    if verify and manifest.get("evidence_sha256"):
        path = output / "evidence.json"
        if not path.is_file() or sha256_file(path) != manifest["evidence_sha256"]:
            raise ValueError("Evidence bundle hash mismatch")
    return build_evidence(output, manifest, verify=verify)
