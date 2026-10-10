import json
import re
import time
from pathlib import Path
import numpy as np
from PIL import Image
from rapidocr_onnxruntime import RapidOCR

ROOT=Path(__file__).resolve().parents[1]
SAMPLES=[('XSFwu5KOo5Y',600.5,'688636'),('XSFwu5KOo5Y',2400.5,'603067'),
         ('XSFwu5KOo5Y',610.5,'002025'),('XSFwu5KOo5Y',690.5,'688776'),
         ('XSFwu5KOo5Y',720.5,'600990'),('XSFwu5KOo5Y',740.5,'688511'),
         ('dMgT48e5NQo',1800.5,'600230'),('dMgT48e5NQo',3600.5,'002628'),
         ('zuo8FhWIdow',3600.5,'688658')]

engine=RapidOCR(intra_op_num_threads=2,inter_op_num_threads=2,det_limit_side_len=96,det_limit_type='min')
records=[]
for video_id,second,expected in SAMPLES:
    index=int((second-.5)/10)
    source=ROOT/'frames'/video_id/f'frame_{index:05d}_{int(second*1000):09d}.jpg'
    with Image.open(source) as im:
        pixels=np.array(im.convert('RGB').crop((960,14,1130,48)))
    rgb=pixels.astype(float)
    mask=(rgb[:,:,0]>40)&(rgb[:,:,1]>40)&(rgb[:,:,2]<.7*np.minimum(rgb[:,:,0],rgb[:,:,1]))
    selected=np.where(mask[:,:,None],0,255).astype('uint8')
    selected=np.repeat(selected,3,axis=2)
    for name,input_pixels in [('original',pixels),('yellow_title',selected)]:
        image=Image.fromarray(input_pixels)
        image=image.resize((image.width*2,image.height*2),Image.Resampling.LANCZOS)
        began=time.monotonic()
        raw,_=engine(np.array(image))
        texts=[item[1] for item in (raw or [])]
        codes=re.findall(r'(?<!\d)\d{6}(?!\d)',' '.join(texts))
        records.append(dict(stream_id=video_id,time=second,expected_code=expected,method=name,
                            texts=texts,codes=codes,correct=expected in codes,seconds=round(time.monotonic()-began,3)))
report=dict(scope='Nine manually read main-title samples, not whole-pilot acceptance',
            roi=[960,14,1130,48],upscale=2,det_limit_side_len=96,det_limit_type='min',records=records,
            summary={method:dict(correct=sum(r['correct'] for r in records if r['method']==method),total=9,
                    total_seconds=round(sum(r['seconds'] for r in records if r['method']==method),2))
                    for method in ['original','yellow_title']})
(ROOT/'header_method_comparison.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(report,ensure_ascii=False))
