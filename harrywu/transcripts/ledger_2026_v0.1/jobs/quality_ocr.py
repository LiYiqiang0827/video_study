import hashlib
import json
from pathlib import Path
from rapidocr_onnxruntime import RapidOCR

ROOT = Path(__file__).resolve().parents[1]

def main():
    engine = RapidOCR(intra_op_num_threads=2, inter_op_num_threads=2)
    results = []
    for directory in sorted((ROOT / 'quality_frames').iterdir()):
        manifest = json.loads((directory / 'frame_manifest.json').read_text(encoding='utf-8'))
        for frame in manifest['frames']:
            path = directory / frame['frame']
            raw, elapsed = engine(str(path))
            results.append(dict(stream_id=directory.name, time=frame['time'], frame=str(path),
                                frame_sha256=frame['sha256'], ocr=[dict(box=item[0], text=item[1], confidence=float(item[2])) for item in (raw or [])]))
    target = ROOT / 'quality_ocr.json'
    target.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps([dict(stream_id=r['stream_id'],time=r['time'],header=[x['text'] for x in r['ocr'] if min(p[1] for p in x['box'])<45 and min(p[0] for p in x['box'])>900]) for r in results], ensure_ascii=False))

if __name__=='__main__':
    main()
