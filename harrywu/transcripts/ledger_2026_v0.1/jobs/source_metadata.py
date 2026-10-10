"""Read only source-page livestream metadata; no market data or account access."""
import concurrent.futures
import csv
import json
import re
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def fetch(row):
    video_id = row['stream_id']
    request = urllib.request.Request(f'https://www.youtube.com/watch?v={video_id}&hl=en',
        headers={'User-Agent':'Mozilla/5.0'})
    result = dict(stream_id=video_id, fetched=datetime.now(timezone.utc).isoformat(),
                  source_url=request.full_url)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            html = response.read(6 * 1024 * 1024).decode('utf-8', errors='replace')
        match = re.search(r'ytInitialPlayerResponse\s*=\s*', html)
        if not match:
            raise ValueError('Player response not present')
        player, _ = json.JSONDecoder().raw_decode(html[match.end():])
        details = player.get('microformat',{}).get('playerMicroformatRenderer',{}).get('liveBroadcastDetails',{})
        result.update(actual_start_utc=details.get('startTimestamp'), actual_end_utc=details.get('endTimestamp'),
                      duration_seconds=player.get('videoDetails',{}).get('lengthSeconds'),
                      playability=player.get('playabilityStatus',{}).get('status'),
                      reason=player.get('playabilityStatus',{}).get('reason'),
                      status='verified_source_start' if details.get('startTimestamp') else 'source_start_missing')
        if details.get('startTimestamp'):
            start = datetime.fromisoformat(details['startTimestamp'].replace('Z','+00:00'))
            result['actual_start_beijing'] = start.astimezone(timezone(timedelta(hours=8))).isoformat()
    except Exception as exc:
        result.update(status='fetch_failed', error=f'{type(exc).__name__}: {exc}')
    (ROOT / 'metadata' / f'{video_id}.youtube.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result

def main():
    path = ROOT / 'streams_inventory.csv'
    with path.open(encoding='utf-8-sig', newline='') as f:
        rows = list(csv.DictReader(f))
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = {r['stream_id']:r for r in pool.map(fetch, rows)}
    for row in rows:
        result = results[row['stream_id']]
        row['metadata_status'] = result['status']
        row['actual_start_beijing'] = result.get('actual_start_beijing','')
    rows.sort(key=lambda r:(r['actual_start_beijing'] or r['title_date'], r['stream_id']))
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps(dict(verified_starts=sum(r['metadata_status']=='verified_source_start' for r in rows),
                         unresolved=[r['stream_id'] for r in rows if r['metadata_status']!='verified_source_start']), ensure_ascii=False))

if __name__=='__main__':
    main()
