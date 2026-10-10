import argparse
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('stream_id')
parser.add_argument('--start', type=float, default=0)
parser.add_argument('--end', type=float, required=True)
args = parser.parse_args()
if args.stream_id not in [p['stream_id'] for p in json.loads((root/'config_v0.1.json').read_text(encoding='utf-8'))['pilots']]:
    raise ValueError('Not a pilot stream')
extraction = root / 'extractions' / args.stream_id
speech = extraction / 'speech.json'
if speech.exists():
    rows = json.loads(speech.read_text(encoding='utf-8'))
else:
    rows = [s for p in sorted((extraction/'chunks').glob('[0-9][0-9][0-9][0-9][0-9].json'))
            for s in json.loads(p.read_text(encoding='utf-8'))['segments']]
for s in rows:
    if s['end'] >= args.start and s['start'] <= args.end:
        print(f"[{s['start']:.2f}-{s['end']:.2f}] {s['text']}")
