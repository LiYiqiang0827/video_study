"""Read-only verification of this frozen handoff, without models or source databases."""
from pathlib import Path
import hashlib
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent

def digest(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()

def main():
    errors = []
    manifest_path = ROOT / 'provenance/SHA256SUMS.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    expected = {r['path']: r for r in manifest['files']}
    for rel, row in expected.items():
        p = REPO / rel
        if not p.resolve().is_relative_to(REPO.resolve()):
            errors.append({'path': rel, 'error': 'path_outside_repository'})
        elif not p.is_file():
            errors.append({'path': rel, 'error': 'missing'})
        elif p.stat().st_size != row['bytes'] or digest(p) != row['sha256']:
            errors.append({'path': rel, 'error': 'content_mismatch'})
    source_rows = [json.loads(line) for line in (ROOT/'provenance/source_manifest.jsonl').read_text(encoding='utf-8').splitlines()]
    for row in source_rows:
        p = REPO / row['path']
        if not p.is_file() or digest(p) != row['sha256']:
            errors.append({'path': row['path'], 'error': 'source_copy_mismatch'})
    streams = json.loads((ROOT/'provenance/primary_20_streams.json').read_text(encoding='utf-8'))
    if len(streams) != 20 or len({s['stream_id'] for s in streams}) != 20:
        errors.append({'error': 'invalid_primary_20_stream_count'})
    for stream in streams:
        if len(stream['files']) != 7:
            errors.append({'stream': stream['stream_id'], 'error': 'expected_seven_deliverables'})
        for row in stream['files']:
            p = REPO / row['path']
            if not p.is_file() or digest(p) != row['sha256']:
                errors.append({'path': row['path'], 'error': 'primary_deliverable_mismatch'})
    for rel in expected:
        p = REPO / rel
        if p.suffix.lower() == '.json':
            try:
                json.loads(p.read_text(encoding='utf-8-sig'))
            except (ValueError, UnicodeError) as exc:
                errors.append({'path': rel, 'error': type(exc).__name__})
        if p.name.lower() in ['secrets.env', '.env', 'cookies.txt']:
            errors.append({'path': rel, 'error': 'prohibited_filename'})
        if p.stat().st_size >= 95*1024*1024:
            errors.append({'path': rel, 'error': 'oversize_git_blob'})
    result = {'status': 'passed' if not errors else 'failed',
              'files_checked': len(expected), 'source_copies_checked': len(source_rows),
              'primary_streams': len(streams), 'primary_files': sum(len(s['files']) for s in streams),
              'errors': errors,
              'scope': 'Frozen handoff integrity and JSON readability; not transcript semantic, ASR, or trading validation.'}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if errors else 0

if __name__ == '__main__':
    sys.exit(main())
