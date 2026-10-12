#!/usr/bin/env python3
"""Compress original, screenshot-linked commentary clips for offline PowerPoint.

No model calls, source edits, or chart crops. The manifest joins a PPT detail to
its original entity/segment/frame and can be consumed by the presentation exporter.
Requires only Python's standard library, ffmpeg and ffprobe.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

sys.dont_write_bytecode = True
from tool_paths import external_path, TEMP_ROOT


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def dump(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def run(args: list[str]) -> str:
    result = subprocess.run(args, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(f'{Path(args[0]).name} failed: {result.stderr[-4000:]}')
    return result.stdout


def probe(path: Path, ffprobe: str) -> dict:
    return json.loads(run([ffprobe, '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(path)]))


def track(details: dict, kind: str) -> dict:
    matching = [s for s in details.get('streams', []) if s.get('codec_type') == kind]
    if len(matching) != 1:
        raise ValueError(f'Expected one {kind} track, found {len(matching)}')
    return matching[0]


def local_asset(base: Path, relative: str) -> Path:
    path = (base / relative).resolve()
    if Path(relative).is_absolute() or not path.is_relative_to(base) or not path.is_file():
        raise ValueError(f'Expected an existing local analysis asset: {relative}')
    return path


def finite(value: object, name: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f'Non-finite {name}')
    return number


def select_window(segment: dict, frame_seconds: float, cues: list[dict], duration: float) -> tuple[float, float, str]:
    """Select a frozen same-segment interval, near the frame and sentence ends."""
    a = finite(segment['start_seconds'], 'segment start')
    b = finite(segment['end_seconds'], 'segment end')
    if not 0 <= a <= frame_seconds < b:
        raise ValueError('Screenshot must lie inside its own commentary segment')
    if b - a <= duration:
        return a, b, 'complete_same_entity_segment'
    left = max(a, min(frame_seconds - duration / 2, b - duration))
    right = left + duration
    ids = {str(i) for i in segment.get('cue_ids', [])}
    selected = [c for c in cues if str(c.get('id')) in ids]
    starts = [a] + [finite(c['start'], 'cue start') for c in selected
                    if a <= float(c['start']) < frame_seconds]
    ends = [b] + [finite(c['end'], 'cue end') for c in selected
                  if frame_seconds < float(c['end']) <= b]
    start = min(starts, key=lambda x: abs(x - left))
    end = min(ends, key=lambda x: abs(x - right))
    # Keep a long cue from expanding a short insert into a whole lecture.
    if start < frame_seconds < end and end - start <= duration + 8:
        return start, end, 'nearest_registered_sentence_boundaries'
    return left, right, 'frame_centered_same_entity_segment'


def decode(path: Path, ffmpeg: str) -> None:
    run([ffmpeg, '-v', 'error', '-nostdin', '-i', str(path), '-map', '0:v:0', '-map', '0:a:0',
         '-f', 'null', '-'])


def verify_clip(path: Path, expected_duration: float, options: dict, ffmpeg: str, ffprobe: str) -> dict:
    details = probe(path, ffprobe)
    video, audio = track(details, 'video'), track(details, 'audio')
    actual = finite(details['format']['duration'], 'encoded duration')
    tolerance = max(0.25, 2 / options['fps'])
    if abs(actual - expected_duration) > tolerance:
        raise ValueError(f'Encoded duration {actual} differs from {expected_duration}')
    if video.get('codec_name') != 'h264' or video.get('pix_fmt') != 'yuv420p':
        raise ValueError('Expected H.264 yuv420p video')
    if audio.get('codec_name') != 'aac':
        raise ValueError('Expected AAC audio')
    if int(video['width']) > options['max_width'] or int(video['height']) > options['max_height']:
        raise ValueError('Video dimensions exceed compression limits')
    numerator, denominator = video['avg_frame_rate'].split('/')
    if abs(float(numerator) / float(denominator) - options['fps']) > .01:
        raise ValueError('Encoded frame rate differs from the requested frame rate')
    if abs(float(video.get('start_time', 0)) - float(audio.get('start_time', 0))) > .15:
        raise ValueError('Encoded audio/video start offset exceeds 150 ms')
    decode(path, ffmpeg)
    return {'state': 'passed', 'complete_decode': True, 'requested_duration_seconds': expected_duration,
            'duration_tolerance_seconds': tolerance, 'ffprobe': details}


def prepare(args: argparse.Namespace) -> dict:
    analysis_path, plan_path = Path(args.analysis).expanduser().resolve(), Path(args.plan).expanduser().resolve()
    base = analysis_path.parent
    output = external_path(args.output_dir)
    if (not output.is_relative_to(base) and not output.is_relative_to(TEMP_ROOT.resolve())) or output == base:
        raise ValueError('Video clips must be in a separate analysis subdirectory or project tmp/')
    if not math.isfinite(args.duration) or args.duration <= 0 or not math.isfinite(args.fps) or not 1 <= args.fps <= 60 or not 0 <= args.crf <= 51:
        raise ValueError('Invalid duration, fps, or CRF')
    if args.max_width < 2 or args.max_height < 2 or args.audio_kbps <= 0:
        raise ValueError('Invalid dimension or audio bitrate')
    ffmpeg, ffprobe = shutil.which(args.ffmpeg), shutil.which(args.ffprobe)
    if not ffmpeg or not ffprobe:
        raise RuntimeError('ffmpeg and ffprobe must be installed')
    analysis_hash, plan_hash = sha256(analysis_path), sha256(plan_path)
    analysis = json.loads(analysis_path.read_text(encoding='utf-8'))
    plan = json.loads(plan_path.read_text(encoding='utf-8'))
    if plan.get('schema_version') != 'yuncong_ppt_plan_v1' or plan.get('analysis_sha256') != analysis_hash:
        raise ValueError('PPT plan does not match the frozen analysis')
    source = Path(analysis['metadata']['source_video_path']).expanduser().resolve()
    source_hash = sha256(source)
    if source_hash != analysis['metadata']['source_video_sha256']:
        raise ValueError('Original video hash no longer matches the analysis')
    source_probe = probe(source, ffprobe)
    track(source_probe, 'video')
    track(source_probe, 'audio')
    source_duration = finite(source_probe['format']['duration'], 'source duration')
    transcript_path = Path(analysis['source']['input_path']).expanduser().resolve()
    transcript_hash = sha256(transcript_path)
    if transcript_hash != analysis['source']['input_sha256']:
        raise ValueError('Original transcript hash no longer matches the analysis')
    cues = json.loads(transcript_path.read_text(encoding='utf-8')).get('cues', [])
    entities = {e['id']: e for e in analysis['entities']}
    options = {'duration': args.duration, 'fps': args.fps, 'crf': args.crf, 'preset': args.preset,
               'max_width': args.max_width, 'max_height': args.max_height, 'audio_kbps': args.audio_kbps,
               'video_codec': 'libx264', 'profile': 'main', 'pixel_format': 'yuv420p',
               'audio_codec': 'aac', 'crop': None}
    contract = {'schema_version': 'yuncong_ppt_media_v1', 'analysis_sha256': analysis_hash,
                'plan_sha256': plan_hash, 'source_video_sha256': source_hash,
                'transcript_sha256': transcript_hash, 'options': options,
                'tool_sha256': sha256(Path(__file__).resolve())}
    manifest_path = output / 'media_manifest.json'
    if manifest_path.exists():
        saved = json.loads(manifest_path.read_text(encoding='utf-8'))
        if saved['contract'] != contract:
            raise ValueError('Inputs/options changed; select a new media output directory')
        for clip in saved['clips']:
            path = local_asset(base, clip['path'])
            if sha256(path) != clip['sha256']:
                raise ValueError(f'Previously completed clip changed: {path}')
            verify_clip(path, clip['end_seconds'] - clip['start_seconds'], options, ffmpeg, ffprobe)
        return saved
    output.mkdir(parents=True, exist_ok=True)
    clips, omissions = [], []
    seen = set()
    if not isinstance(plan.get('stocks'), list) or not isinstance(plan.get('market_items', []), list):
        raise ValueError('Expected stocks and optional market_items arrays')
    for stock in [*plan.get('market_items', []), *plan['stocks']]:
        entity = entities[stock['entity_id']]
        for detail in stock['details']:
            frame_path = detail['frame_path']
            key = (entity['id'], frame_path)
            if key in seen:
                continue
            seen.add(key)
            matching = [(segment, frame) for segment in entity['segments']
                        for frame in segment.get('frames', []) if frame['path'] == frame_path]
            if len(matching) != 1:
                raise ValueError(f'Screenshot lacks a unique same-entity segment: {frame_path}')
            segment, frame = matching[0]
            if not frame.get('visual_verified'):
                raise ValueError(f'Screenshot needs visual identity verification: {frame_path}')
            identity = entity.get('visual_identity', {})
            display_name = identity.get('display_name') if identity.get('visual_verified') else None
            visible_name = frame.get('visible_entity_name') or frame.get('visible_stock_name')
            if visible_name and visible_name not in {entity['name'], display_name}:
                raise ValueError(f'Screenshot displays a different entity: {frame_path}')
            if entity.get('kind') in {'index', 'market'} and not frame.get('chart_verified'):
                raise ValueError(f'Market screenshot needs chart verification: {frame_path}')
            frame_file = local_asset(base, frame_path)
            frame_hash = sha256(frame_file)
            frame_seconds = finite(frame['video_seconds'], 'frame time')
            if type(detail.get('embed_video', True)) is not bool:
                raise ValueError('embed_video must be a boolean')
            if detail.get('embed_video', True) is False:
                reason = detail.get('video_omission_reason')
                if not isinstance(reason, str) or not reason.strip():
                    raise ValueError('Screenshot-only detail needs a recorded video omission reason')
                omissions.append({'entity_id': entity['id'], 'frame_path': frame_path,
                                  'frame_sha256': frame_hash, 'reason': reason})
                continue
            bounds = segment.get('ppt_clip_bounds')
            window_segment = segment
            if bounds is not None:
                if not isinstance(bounds, dict) or bounds.get('visual_verified') is not True or not bounds.get('basis'):
                    raise ValueError('PPT clip bounds need original-frame visual verification and basis')
                lower = finite(bounds['start_seconds'], 'reviewed clip start')
                upper = finite(bounds['end_seconds'], 'reviewed clip end')
                if not float(segment['start_seconds']) <= lower <= frame_seconds < upper <= float(segment['end_seconds']):
                    raise ValueError('Reviewed clip bounds must remain inside their segment and contain the frame')
                window_segment = {**segment, 'start_seconds': lower, 'end_seconds': upper}
            start, end, rule = select_window(window_segment, frame_seconds, cues, args.duration)
            if end > source_duration + .03:
                raise ValueError('Registered commentary interval exceeds source duration')
            existing = segment.get('clip')
            existing_clip = None
            if existing and existing.get('path'):
                old = local_asset(base, existing['path'])
                old_probe = probe(old, ffprobe)
                track(old_probe, 'video')
                track(old_probe, 'audio')
                if abs(float(old_probe['format']['duration']) - (float(existing['end_seconds']) - float(existing['start_seconds']))) > .25:
                    raise ValueError(f'Existing registered segment clip has inconsistent duration: {old}')
                existing_clip = {'path': existing['path'], 'sha256': sha256(old),
                                 'start_seconds': existing['start_seconds'], 'end_seconds': existing['end_seconds'],
                                 'ffprobe': old_probe, 'encoding_source': False}
            slug = re.sub(r'[^A-Za-z0-9_-]', '_', Path(frame_path).stem)
            target = output / f'{slug}.mp4'
            checkpoint = output / f'{slug}.json'
            specification = {'contract': contract, 'entity_id': entity['id'], 'frame_path': frame_path,
                             'frame_sha256': frame_hash, 'segment_id': segment['id'],
                             'start_seconds': start, 'end_seconds': end, 'reviewed_bounds': bounds}
            saved_clip = None
            if checkpoint.exists():
                cached = json.loads(checkpoint.read_text(encoding='utf-8'))
                if cached['specification'] != specification:
                    raise ValueError(f'Cached clip inputs changed: {checkpoint}')
                saved_clip = cached['clip']
                if not target.is_file() or sha256(target) != saved_clip['sha256']:
                    raise ValueError(f'Cached clip missing or changed: {target}')
                verify_clip(target, end - start, options, ffmpeg, ffprobe)
            elif target.exists():
                raise FileExistsError(f'Unregistered existing clip: {target}')
            else:
                temporary = output / f'.{slug}.encoding.mp4'
                # An interrupted unpublished encoding can be safely overwritten;
                # the registered final video and original inputs are never overwritten.
                scale = f'scale=w=min({args.max_width}\\,iw):h=min({args.max_height}\\,ih):force_original_aspect_ratio=decrease:force_divisible_by=2'
                command = [ffmpeg, '-v', 'error', '-nostdin', '-y', '-ss', f'{start:.6f}', '-i', str(source),
                           '-t', f'{end-start:.6f}', '-map', '0:v:0', '-map', '0:a:0',
                           '-vf', f'{scale},fps={args.fps}', '-c:v', 'libx264', '-preset', args.preset,
                           '-crf', str(args.crf), '-profile:v', 'main', '-pix_fmt', 'yuv420p', '-g', str(round(args.fps * 2)),
                           '-c:a', 'aac', '-b:a', f'{args.audio_kbps}k', '-movflags', '+faststart', str(temporary)]
                run(command)
                validation = verify_clip(temporary, end - start, options, ffmpeg, ffprobe)
                temporary.replace(target)
                validation['ffprobe']['format']['filename'] = str(target)
                saved_clip = {'entity_id': entity['id'], 'entity_name': entity['name'],
                              'frame_path': frame_path, 'frame_sha256': frame_hash, 'frame_seconds': frame_seconds,
                              'segment_id': segment['id'], 'reviewed_bounds': bounds, 'segment_start_seconds': segment['start_seconds'],
                              'segment_end_seconds': segment['end_seconds'], 'path': Path(os.path.relpath(target, base)).as_posix(),
                              'start_seconds': start, 'end_seconds': end,
                              'duration_seconds': float(validation['ffprobe']['format']['duration']),
                              'selection_rule': rule, 'sha256': sha256(target), 'bytes': target.stat().st_size,
                              'encoding_source': str(source), 'source_video_sha256': source_hash,
                              'encoding': options, 'validation': validation, 'existing_segment_clip': existing_clip}
                dump(checkpoint, {'specification': specification, 'clip': saved_clip})
            clips.append(saved_clip)
            print(f"{entity['name']}: {start:.2f}–{end:.2f}s, {target.stat().st_size / 1048576:.2f} MiB", flush=True)
    if sha256(analysis_path) != analysis_hash or sha256(plan_path) != plan_hash or sha256(source) != source_hash or sha256(transcript_path) != transcript_hash:
        raise ValueError('An input changed while encoding; final manifest not published')
    for clip in [*clips, *omissions]:
        if sha256(local_asset(base, clip['frame_path'])) != clip['frame_sha256']:
            raise ValueError('A screenshot changed while encoding; final manifest not published')
    manifest = {'schema_version': 'yuncong_ppt_media_v1', 'state': 'completed',
                'created_at_utc': datetime.now(timezone.utc).isoformat(), 'contract': contract,
                'analysis_path': str(analysis_path), 'analysis_sha256': analysis_hash, 'plan_sha256': plan_hash,
                'source_video_path': str(source), 'source_video_sha256': source_hash, 'source_ffprobe': source_probe,
                'ffmpeg_version': run([ffmpeg, '-version']).splitlines()[0],
                'clips': clips, 'clip_count': len(clips), 'omissions': omissions,
                'total_bytes': sum(c['bytes'] for c in clips),
                'total_duration_seconds': sum(c['duration_seconds'] for c in clips)}
    dump(manifest_path, manifest)
    return manifest


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--analysis', required=True, help='Frozen analysis.json')
    p.add_argument('--plan', required=True, help='Matching ppt_plan.json')
    p.add_argument('--output-dir', required=True, help='New media subdirectory of the analysis folder, outside tool code')
    p.add_argument('--duration', type=float, default=32, help='Target seconds near each screenshot; shorter segments stay complete')
    p.add_argument('--fps', type=float, default=15)
    p.add_argument('--crf', type=int, default=24, help='H.264 CRF; smaller values preserve more detail')
    p.add_argument('--preset', default='medium', choices=['ultrafast', 'superfast', 'veryfast', 'faster', 'fast', 'medium', 'slow', 'slower'])
    p.add_argument('--max-width', type=int, default=1280)
    p.add_argument('--max-height', type=int, default=720)
    p.add_argument('--audio-kbps', type=int, default=64)
    p.add_argument('--ffmpeg', default='ffmpeg')
    p.add_argument('--ffprobe', default='ffprobe')
    return p


def main() -> None:
    args = parser().parse_args()
    try:
        result = prepare(args)
    except (ValueError, KeyError, OSError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps({'state': result['state'], 'clip_count': result['clip_count'],
                      'total_bytes': result['total_bytes'], 'total_duration_seconds': result['total_duration_seconds'],
                      'manifest': str(Path(args.output_dir).expanduser().resolve() / 'media_manifest.json')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
