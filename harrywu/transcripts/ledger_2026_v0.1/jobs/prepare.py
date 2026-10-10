import csv
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = Path(r'D:\YuncongVideoAnalysis\2026-01-01_to_2026-10-03')
LINKS = SOURCES / 'source-links-with-titles.txt'
PLAYLIST = Path(os.environ['TEMP']) / 'codex-yuncong-links-20261003' / 'playlist.json'
PILOTS = [('XSFwu5KOo5Y', '盘中候选'), ('dMgT48e5NQo', '盘后复盘'), ('zuo8FhWIdow', '闲聊答疑候选')]

def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(4 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()

def normalized(text):
    return re.sub(r'[\W_]', '', text).casefold()

def main():
    entries = {e['id']: e for e in json.loads(PLAYLIST.read_text(encoding='utf-8'))['entries']}
    files = {}
    for p in SOURCES.glob('*.mp4'):
        key = normalized(p.stem)
        if key in files:
            raise ValueError('Ambiguous source filename')
        files[key] = p
    pattern = r'(2026-\d{2}-\d{2}) \| ([^\n]+)\nhttps://www.youtube.com/watch\?v=([\w-]+)'
    records = []
    for date, title, video_id in re.findall(pattern, LINKS.read_text(encoding='utf-8')):
        p = files.get(normalized(title))
        record = dict(stream_id=video_id, title=title, title_date=date,
                      actual_start_beijing='', duration_seconds=entries[video_id].get('duration', ''),
                      local_duration_seconds='', source_path=str(p) if p else '',
                      download_status='downloaded' if p else 'members_only_unavailable',
                      content_completeness='pending' if p else 'missing',
                      resolution='', audio_codec='', source_sha256='', metadata_status='actual_start_pending')
        if p:
            info = json.loads(subprocess.run(['ffprobe', '-v', 'error', '-show_streams', '-show_format',
                '-of', 'json', str(p)], check=True, capture_output=True, text=True,
                encoding='utf-8', timeout=60).stdout)
            duration = float(info['format']['duration'])
            video = next(s for s in info['streams'] if s['codec_type'] == 'video')
            audio = next(s for s in info['streams'] if s['codec_type'] == 'audio')
            record.update(local_duration_seconds=duration, resolution=f"{video['width']}x{video['height']}",
                          audio_codec=audio['codec_name'], source_sha256=digest(p),
                          content_completeness='duration_matches_listing' if abs(duration-float(record['duration_seconds'])) <= 2 else 'duration_mismatch_review')
            (ROOT / 'metadata' / f'{video_id}.ffprobe.json').write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding='utf-8')
        records.append(record)
    if len(records) != 54 or len({r['stream_id'] for r in records}) != 54 or sum(bool(r['source_path']) for r in records) != 48:
        raise ValueError('Inventory counts do not match expected 54 / 48')
    with (ROOT / 'streams_inventory.csv').open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(records[0]))
        writer.writeheader()
        writer.writerows(records)
    (ROOT / 'source_SHA256SUMS').write_text('\n'.join(f"{r['source_sha256']}  {r['source_path']}" for r in records if r['source_path']) + '\n', encoding='utf-8')
    indexed = {r['stream_id']: r for r in records}
    config = dict(version='v0.1', window_start='2026-01-01T00:00:00+08:00', window_end_exclusive='2026-10-04T00:00:00+08:00',
                  channel='https://www.youtube.com/@yuncongtrade/streams', source_root=str(SOURCES),
                  privacy='local_only', platform_version='2.2.1', model='large-v3', device='cuda', compute_type='float16',
                  language='zh', chunk_seconds=300, vad='conservative', resolution_policy='720p_first_then_1080p_if_unreadable',
                  bulk_gate='User review of three pilots before bulk processing',
                  pilots=[dict(stream_id=i, kind=k, source_path=indexed[i]['source_path'],
                               duration_seconds=indexed[i]['local_duration_seconds']) for i,k in PILOTS])
    (ROOT / 'config_v0.1.json').write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(dict(listed=len(records), downloaded=48, missing=6, total_hours=round(sum(float(r['local_duration_seconds'] or 0) for r in records)/3600,2),
                          pilot_hours=round(sum(float(p['duration_seconds']) for p in config['pilots'])/3600,2),
                          duration_mismatches=[r['stream_id'] for r in records if r['content_completeness']=='duration_mismatch_review']), ensure_ascii=False))

if __name__ == '__main__':
    main()
