"""Assemble a cited video analysis with dual time axes and original media assets."""
from __future__ import annotations

import argparse
import copy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

sys.dont_write_bytecode = True
from tool_paths import external_path
from video_analysis_assets import extract_segment_assets, sha256, timestamp, video_duration


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding='utf-8'))


def _time_text(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S')


def dual_time(alignment: dict, a: float, b: float | None = None) -> dict:
    b = a if b is None else b
    video_text = timestamp(a) if b == a else f'{timestamp(a)} – {timestamp(b)}'
    offset = alignment.get('offset_seconds_range')
    if not isinstance(offset, list) or len(offset) != 2 or offset[0] is None:
        return {'video_range_text': video_text, 'beijing_range_text': '北京时间未确认',
                'beijing_time_text': '北京时间未确认', 'beijing_start_interval': None,
                'beijing_end_interval': None}
    lo, hi = map(float, offset)
    if hi < lo:
        raise ValueError('inverted clock offset interval')
    midpoint = (lo + hi) / 2
    suffix = f'约，时钟映射误差区间±{(hi - lo) / 2:.1f}秒'
    text = _time_text(midpoint + a)
    if b != a:
        text += ' – ' + _time_text(midpoint + b)
    text += f'（{suffix}）'
    return {'video_range_text': video_text, 'beijing_range_text': text,
            'beijing_time_text': text,
            'beijing_start_interval': [_time_text(lo + a), _time_text(hi + a)],
            'beijing_end_interval': [_time_text(lo + b), _time_text(hi + b)]}


def verify_sources(video_dir: Path, provenance: dict) -> tuple[Path, Path]:
    stem = video_dir.name
    video, transcript = video_dir / f'{stem}.mp4', video_dir / f'{stem}.json'
    if not video.is_file() or not transcript.is_file():
        raise FileNotFoundError('expected same-stem .mp4 and transcript .json')
    declared = provenance.get('source_video_sha256')
    if declared and sha256(video) != declared:
        raise ValueError('video differs from archived source provenance')
    return video, transcript


def verify_segment_evidence(segment: dict, cue_by_id: dict, cue_order: dict) -> list[dict]:
    """Recheck grounded normalized input before producing any media."""
    cited = segment.get('cue_ids')
    if not isinstance(cited, list) or not cited or any(str(cid) not in cue_by_id for cid in cited):
        raise ValueError(f"unknown/missing source cues in {segment.get('id')}")
    positions = [cue_order[str(cid)] for cid in cited]
    if positions != list(range(positions[0], positions[-1] + 1)):
        raise ValueError('segment cues must be unique, ordered and contiguous')
    allowed = {str(cid) for cid in cited}

    def visit(value):
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            references = value.get('cue_ids')
            if references is not None and (not isinstance(references, list) or not references or
                                           any(str(cid) not in allowed for cid in references)):
                raise ValueError('nested statement references cues outside its segment')
            if 'cue_id' in value:
                cid, excerpt = str(value['cue_id']), value.get('text')
                if cid not in allowed or not isinstance(excerpt, str) or not excerpt or excerpt not in cue_by_id[cid]['text']:
                    raise ValueError('nested quote is not an exact source subtitle excerpt')
            for item in value.values():
                visit(item)
    # Only evidence fields: derived media and display metadata are not model evidence.
    for field in ('quotes', 'views', 'buy_conditions', 'sell_conditions', 'actions',
                  'pass_reason', 'visual_focus', 'uncertainties'):
        visit(segment.get(field))
    return [cue_by_id[str(cid)] for cid in cited]


def build_analysis(video_dir: Path, output_dir: Path, content_path: Path,
                   alignment_path: Path | None, selection_path: Path | None = None,
                   frames_per_segment: int = 3) -> dict:
    video_dir = Path(video_dir).resolve()
    output_dir = external_path(output_dir)
    if output_dir.parent != video_dir:
        raise ValueError('analysis output must be a direct child of the video directory, e.g. 解析 or 解析_v2')
    output_dir.mkdir(parents=True, exist_ok=True)
    provenance_path = video_dir / f'{video_dir.name}_来源记录.json'
    provenance = read_json(provenance_path)
    video, transcript_path = verify_sources(video_dir, provenance)
    all_input_files = [video, transcript_path, provenance_path, Path(content_path)]
    if alignment_path:
        all_input_files.append(Path(alignment_path))
    if selection_path:
        all_input_files.append(Path(selection_path))
    input_hashes = {str(p): sha256(p) for p in all_input_files}
    transcript, content = read_json(transcript_path), read_json(content_path)
    alignment = read_json(alignment_path) if alignment_path and alignment_path.is_file() else {}
    visual_review = read_json(selection_path) if selection_path else {}
    selection = visual_review.get('segments', visual_review)
    duration = video_duration(video)
    if alignment.get('source_video_sha256') and alignment['source_video_sha256'] != sha256(video):
        raise ValueError('clock mapping belongs to a different source video')
    if visual_review.get('source_video_sha256') and visual_review['source_video_sha256'] != sha256(video):
        raise ValueError('visual review belongs to a different source video')
    if alignment.get('duration_seconds') and abs(float(alignment['duration_seconds']) - duration) > 0.1:
        raise ValueError('clock mapping duration differs from source video')
    cues = transcript.get('cues', [])
    cue_by_id = {str(c['id']): c for c in cues}
    cue_order = {str(c['id']): i for i, c in enumerate(cues)}
    if not cues or len(cue_by_id) != len(cues):
        raise ValueError('missing or duplicated source subtitle cues')
    if content.get('input_sha256') != sha256(transcript_path):
        raise ValueError('content analysis is bound to a different transcript')
    assembled = copy.deepcopy(content)
    assembled['schema_version'] = 1
    assembled['alignment'] = alignment
    if alignment.get('offset_seconds_range'):
        assembled['alignment']['display_summary'] = (
            f"{alignment.get('used_anchor_count', '多个')}个可用桌面时钟锚点；"
            f"按北京时间解释，映射残余区间{float(alignment.get('residual_uncertainty_seconds', 0)):.1f}秒。"
            '部分时钟画面被遮挡；稀疏抽帧未发现矛盾，但不能排除未采样区间的剪辑。')
    else:
        assembled['alignment']['display_summary'] = '没有可靠的桌面时钟映射；录制北京时间未知。'
    assembled['metadata'] = {
        'title': provenance.get('title', video_dir.name), 'source_filename': video.name,
        'source_video_path': str(video), 'video_id': provenance.get('video_id'),
        'duration_seconds': duration, 'upload_time_beijing': provenance.get('upload_time_beijing'),
        'recording_time': dual_time(alignment, 0, duration)['beijing_range_text'],
        'source_video_sha256': sha256(video), 'transcript_sha256': sha256(transcript_path),
        'created_at_beijing': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
    }
    assembled['visual_context'] = visual_review.get('context', {})
    if assembled['visual_context'].get('recording_kind_display'):
        assembled['metadata']['recording_kind_display'] = assembled['visual_context']['recording_kind_display']
    warnings = list(content.get('warnings', []))
    if not alignment.get('offset_seconds_range'):
        warnings.append('桌面时钟未能可靠对齐；全部北京时间保留未知，上传时间只作为上传元数据。')
    else:
        warnings.append('北京时间采用桌面时钟映射的估计区间；上传时间不参与录制时刻推算。')
        if not alignment.get('timezone_confirmed', False):
            warnings.append('画面未显示时区设置；以下按桌面时钟处于北京时间（Asia/Shanghai）解释。')
    warnings.append('这是云聪在该视频内的观点与操作思路复述；财务、盈利及目标价宣称没有被当作已核实事实。')
    assembled['warnings'] = list(dict.fromkeys(warnings))
    entities = assembled.get('entities')
    if not isinstance(entities, list):
        raise ValueError('content analysis needs entities[]')
    timeline = []
    segment_ids = set()
    for entity in entities:
        if entity['name'] in visual_review.get('entities', {}):
            assembled_identity = visual_review['entities'][entity['name']]
            entity['visual_identity'] = assembled_identity
        if not isinstance(entity.get('segments'), list):
            raise ValueError('entity lacks segments[]')
        for segment in entity['segments']:
            segment_id = str(segment['id'])
            if segment_id in segment_ids:
                raise ValueError('segment ids must be globally unique')
            segment_ids.add(segment_id)
            cited = segment.get('cue_ids')
            selected_cues = verify_segment_evidence(segment, cue_by_id, cue_order)
            a, b = min(float(c['start']) for c in selected_cues), max(float(c['end']) for c in selected_cues)
            segment['start_seconds'], segment['end_seconds'] = a, b
            segment.update(dual_time(alignment, a, b))
            for quote in segment.get('quotes', []):
                if str(quote.get('cue_id')) not in {str(cid) for cid in cited}:
                    raise ValueError('quote must cite a cue in its own segment')
                if not quote.get('text') or quote['text'] not in cue_by_id[str(quote['cue_id'])]['text']:
                    raise ValueError('quote is not an exact source subtitle excerpt')
            media = extract_segment_assets(video, segment, cues, output_dir,
                                           selection.get(segment_id), frames_per_segment)
            for frame in media['frames']:
                frame.update(dual_time(alignment, float(frame['video_seconds'])))
                # Edited/speed-changed videos may lack a reliable global clock
                # offset. A reviewed clock visible in this exact frame remains
                # useful without interpolating other frames or segment times.
                if frame.get('desktop_clock_verified') is True:
                    visible_time = frame.get('desktop_time_text')
                    if not isinstance(visible_time, str) or not frame.get('visual_verified'):
                        raise ValueError('direct desktop time needs a verified frame and time text')
                    datetime.strptime(visible_time, '%Y-%m-%d %H:%M')
                    frame['beijing_time_text'] = visible_time
                    frame['beijing_range_text'] = visible_time
                    frame['beijing_time_basis'] = 'direct_visible_desktop_minute'
            media['clip']['vtt_text'] = (output_dir / media['clip']['subtitle_path']).read_text(encoding='utf-8')
            segment.update(media)
            if segment_id in selection:
                segment['visual_review'] = selection[segment_id].get('notes', [])
                if selection[segment_id].get('ppt_clip_bounds') is not None:
                    segment['ppt_clip_bounds'] = copy.deepcopy(selection[segment_id]['ppt_clip_bounds'])
            timeline.append({
                'entity_id': entity['id'], 'entity_name': entity['name'], 'segment_id': segment_id,
                'start_seconds': a, 'end_seconds': b, 'summary': segment.get('summary', ''),
                **dual_time(alignment, a, b),
            })
        entity['segments'].sort(key=lambda s: (s['start_seconds'], s['id']))
    entities.sort(key=lambda e: min((s['start_seconds'] for s in e['segments']), default=float('inf')))
    timeline.sort(key=lambda s: (s['start_seconds'], s['end_seconds'], s['entity_id']))
    assembled['timeline'] = timeline
    if not any(e.get('kind') in {'index', 'market'} for e in entities):
        assembled['coverage_notes'] = ['本片字幕没有单独形成可引用的指数观点；桌面上的指数行情没有被当作讲者判断。']
    frame_manifest = output_dir / 'clock' / 'frame_manifest.json'
    if frame_manifest.exists():
        clock_data = read_json(frame_manifest)
        anchors = {str(x.get('frame_id')): x for x in
                   alignment.get('anchors', []) + alignment.get('unresolved_anchors', [])}
        clock_frames = []
        for f in clock_data.get('frames', []):
            raw_path = Path(f['frame_path'])
            path = raw_path if raw_path.is_absolute() else frame_manifest.parent / raw_path
            anchor = anchors.get(str(f.get('frame_id')), {})
            used = bool(anchor.get('usable') or anchor.get('state') == 'used')
            reason = ('桌面日期与分钟时钟完整可见，已用于时间映射。' if used else
                      '时钟或日期被画笔工具条遮挡；未用于时间映射。' if
                      anchor.get('unresolved_reason') == 'desktop_clock_and_date_occluded_by_drawing_toolbar' else
                      anchor.get('reason') or anchor.get('anchor_evidence') or '没有可靠时钟锚点。')
            clock_frames.append({
                'path': str(path.resolve().relative_to(output_dir.resolve())),
                'video_seconds': f['video_seconds'],
                'video_time_text': timestamp(float(f['video_seconds'])),
                'desktop_time_text': anchor.get('display_text') or anchor.get('display_datetime') or
                                     ' '.join(str(anchor.get(k, '')) for k in ('display_date', 'display_time')).strip(),
                'usable': used,
                'reason': reason,
            })
        assembled['clock_frames'] = clock_frames
    if any(sha256(p) != input_hashes[str(p)] for p in all_input_files):
        raise ValueError('an input changed during assembly; keep inputs frozen and retry')
    assembled['provenance'] = {
        'inputs': input_hashes,
        'tools': {p.name: sha256(p) for p in Path(__file__).parent.glob('*.py')},
        'model': content.get('model_provenance', {}),
        'selection_file': str(selection_path) if selection_path else None,
        'timing_rule': 'segment times recomputed from cited source cues; desktop wall clock has an uncertainty interval',
    }
    analysis_path = output_dir / 'analysis.json'
    rendered_json = json.dumps(assembled, ensure_ascii=False, indent=2) + '\n'
    if analysis_path.exists():
        existing = read_json(analysis_path)
        existing['metadata']['created_at_beijing'] = assembled['metadata']['created_at_beijing']
        if existing != assembled:
            raise ValueError('analysis already exists with different content; use a new output directory')
        assembled = read_json(analysis_path)
    else:
        analysis_path.write_text(rendered_json, encoding='utf-8')
    from render_video_analysis import render_analysis
    render_analysis(assembled, output_dir, analysis_path=analysis_path)
    original = output_dir / 'SOURCE_MANIFEST.json'
    if original.exists():
        for item in read_json(original)['original_files']:
            path = video_dir / item['name']
            stat = path.stat()
            if stat.st_size != item['bytes'] or stat.st_mtime_ns != item['mtime_ns'] or sha256(path) != item['sha256']:
                raise ValueError(f'original source archive changed: {path}')
    print(json.dumps({'status': 'complete', 'html': str(output_dir / 'index.html'),
                      'summary': str(output_dir / 'summary.txt'), 'entities': len(entities),
                      'segments': len(timeline), 'duration_seconds': duration}, ensure_ascii=False))
    return assembled


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--video-dir', required=True, type=Path)
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--content-analysis', type=Path)
    parser.add_argument('--alignment', type=Path)
    parser.add_argument('--visual-selection', type=Path)
    parser.add_argument('--frames-per-segment', type=int, default=3)
    args = parser.parse_args()
    output = args.output_dir or args.video_dir / '解析'
    content = args.content_analysis or output / 'model' / 'content_analysis.json'
    alignment = args.alignment or output / 'clock' / 'time_alignment.json'
    build_analysis(args.video_dir, output, content, alignment if alignment.exists() else None,
                   args.visual_selection, args.frames_per_segment)


if __name__ == '__main__':
    main()
