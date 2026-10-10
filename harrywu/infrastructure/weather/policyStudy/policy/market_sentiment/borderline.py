"""Describe nearby active weather boundaries without changing classification."""
import json
import numpy as np
from .readings import SPEC,weather

def boundary_details(hit,cont,act):
    current,_=weather(hit,cont,act)
    if not current:return []
    thresholds=SPEC['weather_thresholds']
    candidates=[('hit',thresholds['hit'])]
    if hit>=thresholds['hit']:candidates.append(('cont',thresholds['cont_hit_high']))
    else:
        candidates.append(('cont',thresholds['cont_hit_low']))
        if cont<thresholds['cont_hit_low']:candidates.append(('act',thresholds['act']))
    readings={'hit':hit,'cont':cont,'act':act};result=[]
    for name,cut in candidates:
        value=readings[name]
        if not np.isfinite(value) or abs(value-cut)>=3:continue
        # >= belongs to the upper side. Test the strict other side with the next
        # representable float, rather than subtracting an arbitrary epsilon.
        other=dict(readings);other[name]=cut if value<cut else np.nextafter(float(cut),-np.inf)
        opposite,reason=weather(other['hit'],other['cont'],other['act'])
        if opposite==current:continue
        result.append({'reading':name,'value':float(value),'threshold':float(cut),
                       'distance':float(abs(value-cut)),'direction':'up' if value<cut else 'down',
                       'other_weather':opposite,'other_weather_reason':reason})
    return result

def _number(value):
    # More precision only where one-decimal rounding would hide the boundary.
    if 0<abs(value)<.05 or abs(value-3)<.05:return f'{value:.4f}'.rstrip('0').rstrip('.')
    return f'{value:.1f}'

def boundary_hint(hit,cont,act):
    parts=[];names={'hit':'挨打','cont':'延续','act':'活跃'}
    for item in boundary_details(hit,cont,act):
        label=SPEC['weather_labels'].get(item['other_weather'],'天气待缺项补齐')
        value=_number(item['value']);gap=_number(item['distance']);cut=f'{item["threshold"]:g}'
        if item['direction']=='up':text=f'{names[item["reading"]]} {value}，再高 {gap} 至{cut}即{label}'
        else:text=f'{names[item["reading"]]} {value}，低于{cut}即{label}（距 {gap} 分）'
        parts.append(text)
    return '临界：'+'；'.join(parts) if parts else ''

def add_borderline(daily):
    result=daily.copy()
    args=list(result[['mkt_hit','mkt_cont','mkt_act']].itertuples(index=False,name=None))
    result['mkt_borderline_hint']=[boundary_hint(*r) for r in args]
    result['mkt_borderline']=result.mkt_borderline_hint.ne('')
    result['mkt_borderline_details_json']=[json.dumps(boundary_details(*r),ensure_ascii=False,allow_nan=False,separators=(',',':')) for r in args]
    return result
