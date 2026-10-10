import json
import re
import time
from pathlib import Path
import numpy as np
from PIL import Image
from rapidocr_onnxruntime import RapidOCR

ROOT=Path(__file__).resolve().parents[1]
prior=json.loads((ROOT/'header_method_comparison.json').read_text(encoding='utf-8'))
samples=[r for r in prior['records'] if r['method']=='original']
engine=RapidOCR(intra_op_num_threads=2,inter_op_num_threads=2,det_limit_side_len=96,det_limit_type='min')
records=[]
for sample in samples:
    video_id=sample['stream_id']
    second=sample['time']
    index=int((second-.5)/10)
    path=ROOT/'frames'/video_id/f'frame_{index:05d}_{int(second*1000):09d}.jpg'
    for left in [988,995]:
        with Image.open(path) as image:
            crop=image.convert('RGB').crop((left,19,1125,38))
        began=time.monotonic()
        raw,_=engine(np.array(crop),use_det=False,use_cls=False)
        texts=[item[0] for item in (raw or [])]
        codes=re.findall(r'(?<!\d)\d{6}(?!\d)',' '.join(texts))
        records.append(dict(stream_id=video_id,time=second,expected_code=sample['expected_code'],
                            crop_left=left,texts=texts,codes=codes,correct=sample['expected_code'] in codes,
                            seconds=round(time.monotonic()-began,3)))
report=dict(scope='Nine main-title samples, not pilot acceptance',records=records,
            summary={str(left):dict(correct=sum(r['correct'] for r in records if r['crop_left']==left),total=9,
                                  seconds=round(sum(r['seconds'] for r in records if r['crop_left']==left),3)) for left in [988,995]})
(ROOT/'title_recognition_comparison.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(report,ensure_ascii=False))
