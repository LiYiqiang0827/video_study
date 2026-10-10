"""Task-specific OCR of the main Tongdaxin title region, using installed RapidOCR."""
import argparse
import json
import os
import time
from pathlib import Path
import numpy as np
from PIL import Image
from rapidocr_onnxruntime import RapidOCR
import psutil

ROOT=Path(__file__).resolve().parents[1]
ROI=(995,19,1125,38)
SCALE=1

def scan(engine, root, manifest, video_id):
    result=[]
    began=time.monotonic()
    for row in manifest['frames']:
        if time.monotonic()-began>3600:
            raise RuntimeError('One-hour OCR stream bound reached')
        path=root/row['frame']
        with Image.open(path) as image:
            if image.size!=(1280,720):
                raise ValueError('ROI requires recorded 1280x720 source')
            crop=image.convert('RGB').crop(ROI)
            crop=crop.resize((crop.width*SCALE,crop.height*SCALE),Image.Resampling.LANCZOS)
            pixels=np.array(crop)
        raw,_=engine(pixels,use_det=False,use_cls=False)
        tokens=[]
        for text,confidence in (raw or []):
            original=[[ROI[0],ROI[1]],[ROI[2],ROI[1]],[ROI[2],ROI[3]],[ROI[0],ROI[3]]]
            tokens.append(dict(text=text,confidence=float(confidence),recognition_region_original_pixels=original))
        result.append(dict(time=row['time'],frame=str(path),frame_sha256=row['sha256'],ocr=tokens))
    return dict(stream_id=video_id,region=ROI,upscale=SCALE,method='RapidOCR recognition only, fixed title region',scope='Main Tongdaxin title only; not whole-frame OCR',
                limitation='Other layouts, prices, holding tables and clocks require separate OCR/review. No stock identity matching or event classification is done.',frames=result)

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--all',action='store_true')
    args=parser.parse_args()
    if args.all:
        state=json.loads((ROOT/'scene_status.json').read_text(encoding='utf-8'))
        try:
            process=psutil.Process(state['runner_pid'])
            if 'pilot_scene_frames.py' not in ' '.join(process.cmdline()):
                raise RuntimeError('Scene process identity mismatch')
            process.wait(timeout=14400)
        except psutil.NoSuchProcess:
            pass
        if json.loads((ROOT/'scene_status.json').read_text(encoding='utf-8'))['status']!='complete':
            raise RuntimeError('Scene extraction has not completed')
    engine=RapidOCR(intra_op_num_threads=2,inter_op_num_threads=2,det_limit_side_len=96,det_limit_type='min')
    sources=ROOT/'frames' if args.all else ROOT/'quality_frames'
    ids=[p['stream_id'] for p in json.loads((ROOT/'config_v0.1.json').read_text(encoding='utf-8'))['pilots']]
    summary=[]
    (ROOT/'ocr').mkdir(exist_ok=True)
    for video_id in ids:
        base=sources/video_id
        manifest=json.loads((base/'frame_manifest.json').read_text(encoding='utf-8'))
        if args.all and (base/'scene_changes/frame_manifest.json').exists():
            changes=json.loads((base/'scene_changes/frame_manifest.json').read_text(encoding='utf-8'))
            if manifest['params']['source_sha256']!=changes['params']['source_sha256']:
                raise ValueError('Mixed sources')
            manifest['frames'].extend([{**r,'frame':'scene_changes/'+r['frame']} for r in changes['frames']])
            manifest['frames'].sort(key=lambda r:r['time'])
        result=scan(engine,base,manifest,video_id)
        target=ROOT/'ocr'/f"{video_id}.{'title_recognition' if args.all else 'quality_title_recognition'}.json"
        target.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        summary.append(dict(stream_id=video_id,frames=len(result['frames']),
                            tokens=[(r['time'],[t['text'] for t in r['ocr']]) for r in result['frames']] if not args.all else None))
    (ROOT/('header_ocr_status.json' if args.all else 'quality_header_ocr_status.json')).write_text(
        json.dumps(dict(status='complete',summary=summary),ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False))

if __name__=='__main__':
    main()
