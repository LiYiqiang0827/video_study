"""Extract original chart frames and browser-playable, subtitle-linked video clips.

This module edits no source media. All assets live in a separate analysis directory.
It does not infer that a sampled frame contains a drawing action without visual review.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

sys.dont_write_bytecode = True
from tool_paths import external_path


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def run(args: list[str]) -> str:
    completed = subprocess.run(args, check=True, capture_output=True, text=True)
    return completed.stdout


def probe(path: Path) -> dict:
    exe = shutil.which('ffprobe')
    if not exe:
        raise RuntimeError('ffprobe is required')
    value = json.loads(run([exe, '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(path)]))
    if not any(s.get('codec_type') == 'video' for s in value.get('streams', [])):
        raise ValueError('source has no video track')
    return value


def video_duration(path: Path) -> float:
    return float(probe(path)['format']['duration'])


def extract_frame(video: Path, output: Path, seconds: float) -> None:
    output = external_path(output)
    if output.exists():
        raise FileExistsError(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    exe = shutil.which('ffmpeg')
    if not exe:
        raise RuntimeError('ffmpeg is required')
    run([exe, '-v', 'error', '-nostdin', '-n', '-ss', f'{seconds:.6f}',
         '-i', str(video), '-frames:v', '1', '-q:v', '2', str(output)])
    if not output.is_file() or output.stat().st_size < 100:
        raise RuntimeError('frame extraction did not create a usable image')


def timestamp(seconds: float) -> str:
    ms = round(max(0.0, seconds) * 1000)
    hours, ms = divmod(ms, 3600000)
    minutes, ms = divmod(ms, 60000)
    secs, ms = divmod(ms, 1000)
    return f'{hours:02}:{minutes:02}:{secs:02}.{ms:03}'


def write_clip_vtt(cues: list[dict], start: float, end: float, path: Path) -> int:
    lines = ['WEBVTT', '']
    count = 0
    for cue in cues:
        a, b = max(float(cue['start']), start), min(float(cue['end']), end)
        if b <= a:
            continue
        count += 1
        lines += [str(count), f'{timestamp(a - start)} --> {timestamp(b - start)}',
                  str(cue['text']).replace('-->', '→'), '']
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return count


def _slug(value: str) -> str:
    clean = re.sub(r'[^A-Za-z0-9_-]', '_', str(value))[:80]
    if not clean or clean in {'.', '..'}:
        raise ValueError('invalid segment id')
    return clean


def extract_segment_assets(video: Path, segment: dict, cues: list[dict], output_dir: Path,
                           selection: dict | None = None, frames_per_segment: int = 3,
                           clip_padding: float = 1.0) -> dict:
    """Return relative media references; selected frames require explicit review evidence.

    Existing work is reused only when source, segment, selection, cues and tool hash match.
    Each clip is re-encoded at the exact window rather than copied at distant keyframes.
    """
    video = Path(video).resolve()
    output_dir = external_path(output_dir)
    source_sha = sha256(video)
    source_duration = video_duration(video)
    a, b = float(segment['start_seconds']), float(segment['end_seconds'])
    if not (0 <= a < b <= source_duration + 0.05):
        raise ValueError(f'invalid segment range {a}..{b} for {source_duration}')
    if not 1 <= frames_per_segment <= 12 or clip_padding < 0:
        raise ValueError('invalid frame count or padding')
    slug = _slug(segment['id'])
    checkpoint = output_dir / 'assets_records' / f'{slug}.json'
    clip_start, clip_end = max(0.0, a - clip_padding), min(source_duration, b + clip_padding)
    selected = (selection or {}).get('frames')
    if selected:
        frame_specs = selected
    else:
        frame_specs = [{'video_seconds': a + (b - a) * ((i + 0.5) / frames_per_segment),
                        'caption': '该讲解段的代表画面；画线细节可在完整片段中查看。',
                        'visual_verified': False} for i in range(frames_per_segment)]
    for frame in frame_specs:
        t = float(frame['video_seconds'])
        if not (max(0.0, a - 2.0) <= t <= min(source_duration - 0.03, b + 2.0)):
            raise ValueError(f'frame {t} outside segment {slug}')
        if frame.get('visual_verified') and not frame.get('review_note'):
            raise ValueError('visually verified frame requires a review note')
    contract = {'source_sha256': source_sha, 'source_duration': source_duration,
                'segment_id': str(segment['id']), 'start_seconds': a, 'end_seconds': b,
                'clip_padding': clip_padding, 'frame_specs': frame_specs,
                'cues_sha256': hashlib.sha256(json.dumps(cues, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
                'tool_sha256': sha256(Path(__file__).resolve())}
    if checkpoint.exists():
        saved = json.loads(checkpoint.read_text(encoding='utf-8'))
        if saved['contract'] != contract:
            raise ValueError(f'asset inputs changed; use a new analysis directory: {checkpoint}')
        for relative, fingerprint in saved['artifact_hashes'].items():
            path = output_dir / relative
            if not path.is_file() or sha256(path) != fingerprint:
                raise ValueError(f'asset changed or missing: {path}')
        return saved['assets']
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    (output_dir / 'frames').mkdir(exist_ok=True)
    (output_dir / 'clips').mkdir(exist_ok=True)
    artifacts = {}
    frames = []
    for i, spec in enumerate(frame_specs, 1):
        relative = f'frames/{slug}_{i:02}.jpg'
        path = output_dir / relative
        extract_frame(video, path, float(spec['video_seconds']))
        frames.append({**spec, 'path': relative, 'video_time_text': timestamp(float(spec['video_seconds']))})
        artifacts[relative] = sha256(path)
    relative = f'clips/{slug}.mp4'
    clip = output_dir / relative
    if clip.exists():
        raise FileExistsError(f'unregistered clip exists: {clip}')
    exe = shutil.which('ffmpeg')
    run([exe, '-v', 'error', '-nostdin', '-n', '-ss', f'{clip_start:.6f}',
         '-i', str(video), '-t', f'{clip_end - clip_start:.6f}', '-map', '0:v:0', '-map', '0:a:0?',
         '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '21', '-pix_fmt', 'yuv420p',
         '-c:a', 'aac', '-b:a', '128k', '-movflags', '+faststart', '-avoid_negative_ts', 'make_zero', str(clip)])
    details = probe(clip)
    actual_duration = float(details['format']['duration'])
    if abs(actual_duration - (clip_end - clip_start)) > 0.25:
        raise ValueError('clip duration differs from requested segment window')
    if not any(s.get('codec_type') == 'audio' for s in details['streams']):
        raise ValueError('source segment lacks audio')
    vtt_relative = f'clips/{slug}.vtt'
    vtt = output_dir / vtt_relative
    if vtt.exists():
        raise FileExistsError(vtt)
    subtitle_count = write_clip_vtt(cues, clip_start, clip_end, vtt)
    artifacts[relative] = sha256(clip)
    artifacts[vtt_relative] = sha256(vtt)
    assets = {'frames': frames,
              'clip': {'path': relative, 'subtitle_path': vtt_relative,
                       'start_seconds': clip_start, 'end_seconds': clip_end,
                       'duration_seconds': actual_duration, 'subtitle_count': subtitle_count}}
    checkpoint.write_text(json.dumps({'contract': contract, 'assets': assets,
                                     'artifact_hashes': artifacts}, ensure_ascii=False, indent=2) + '\n')
    return assets
