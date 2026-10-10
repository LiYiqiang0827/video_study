"""Unsmoothed expanding transition counts; evaluation keyed by TARGET date."""
from collections import Counter
import numpy as np
import pandas as pd
from .readings import SPEC

CODES=SPEC['weather_order']

def choose(counts,current=None):
    if not counts or max(counts.values(),default=0)<=0:return ''
    best=max(counts.values());ties=[c for c in CODES if counts.get(c,0)==best]
    return current if current in ties else ties[0]

def add_forecasts(daily,calendar=None):
    d=daily.sort_values('trade_date').reset_index(drop=True).copy()
    if d.trade_date.duplicated().any():raise ValueError('Duplicate dates')
    days=sorted(set(calendar if calendar is not None else d.trade_date.tolist()))
    position={x:i for i,x in enumerate(days)}
    if not set(d.trade_date).issubset(position):raise ValueError('Input dates absent from calendar')
    rows=[];counts={c:Counter({k:0 for k in CODES}) for c in CODES};known=Counter();prev=None
    for day,w in d[['trade_date','mkt_weather']].itertuples(index=False,name=None):
        if prev is not None:
            pd_,pw=prev
            if position[day]==position[pd_]+1 and pw in CODES and w in CODES:counts[pw][w]+=1
        if w in CODES:known[w]+=1
        n=sum(counts[w].values()) if w in CODES else 0
        idx=position[day];target=days[idx+1] if idx+1<len(days) else ''
        r={'forecast_origin_date':day,'forecast_target_date':target,
           'mkt_forecast_target_reason':'' if target else 'next_market_day_not_in_local_calendar',
           'mkt_forecast_n':n,'mkt_forecast_top1':choose(counts[w],w) if w in CODES else '',
           'mkt_forecast_status':'weather_unavailable' if w not in CODES else 'no_samples' if n==0 else 'small_sample' if n<10 else 'OK',
           'mkt_baseline_persistence':w if w in CODES else '', 'mkt_baseline_mode_known':choose(known,w)}
        for code in CODES:
            c=counts[w][code] if w in CODES else 0
            r[f'mkt_forecast_{code}_count']=c;r[f'mkt_forecast_{code}_prob']=c/n if n else np.nan
        rows.append(r);prev=(day,w)
    return pd.concat([d,pd.DataFrame(rows)],axis=1)

def evaluate_forecasts(daily,start='2026-01-01',end='2026-09-30'):
    d=daily.copy();truth=d.set_index('trade_date').mkt_weather.to_dict()
    targets=d[d.trade_date.between(start,end)]
    mode=choose(Counter(x for x in targets.mkt_weather if x in CODES))
    origin_map={r.forecast_target_date:r for r in d.itertuples() if isinstance(r.forecast_target_date,str) and r.forecast_target_date}
    rows=[]
    for target,actual in targets[['trade_date','mkt_weather']].itertuples(index=False,name=None):
        origin=origin_map.get(target);reasons=[]
        r={'target_date':target,'actual':actual,'forecast_origin_date':getattr(origin,'forecast_origin_date',''),
           'frequency':getattr(origin,'mkt_forecast_top1',''), 'persistence':getattr(origin,'mkt_baseline_persistence',''),
           'mode_known':getattr(origin,'mkt_baseline_mode_known',''),'mode_ex_post':mode,
           'sample_n':getattr(origin,'mkt_forecast_n',0)}
        if actual not in CODES:reasons.append('actual_unavailable')
        if origin is None:reasons.append('origin_unavailable')
        elif origin.forecast_origin_date>=target:raise ValueError('Prediction not strictly earlier than target')
        for name in ('frequency','persistence','mode_known','mode_ex_post'):
            if r[name] not in CODES:reasons.append(name+'_unavailable')
        r['evaluable']=not reasons;r['reason']=';'.join(reasons)
        for name in ('frequency','persistence','mode_known','mode_ex_post'):r[name+'_hit']=not reasons and r[name]==actual
        rows.append(r)
    detail=pd.DataFrame(rows);valid=detail[detail.evaluable] if len(detail) else detail;n=len(valid)
    methods={name:{'n':n,'hits':int(valid[name+'_hit'].sum()) if n else 0,
                   'accuracy':float(valid[name+'_hit'].mean()) if n else None,'ex_post':name=='mode_ex_post'}
             for name in ('frequency','persistence','mode_known','mode_ex_post')}
    f=methods['frequency']['accuracy'];available=[methods[k]['accuracy'] for k in ('persistence','mode_known')]
    conclusion=('暂无可评估预报' if not n else '暂无预报价值——本次命中率未超过简单基准' if f<=max(available)
                else '本次频率预报超过当时可用简单基准；仍需区分事后全期众数与有限历史样本')
    summary={'target_start':start,'target_end':end,'target_days':len(targets),'common_n':n,'methods':methods,
             'mode_ex_post':mode,'mode_ex_post_basis':'All valid actual weather in evaluation target period; hindsight, not executable',
             'sample_small_n':int(valid.sample_n.between(1,9).sum()) if n else 0,
             'exclusions':detail.loc[~detail.evaluable,'reason'].value_counts().to_dict() if len(detail) else {},
             'tie_break':'current if tied else sunny,cloudy,overcast,thunder,storm; ex-post mode fixed order',
             'conclusion':conclusion}
    return summary,detail

def transition_table(daily):
    d=daily.sort_values('trade_date');counts={c:Counter({k:0 for k in CODES}) for c in CODES}
    # Use explicit forecast targets when available to avoid joining across absent market dates.
    previous=None
    for row in d.itertuples():
        if previous is not None:
            adjacent=getattr(previous,'forecast_target_date',row.trade_date)==row.trade_date
            if adjacent and previous.mkt_weather in CODES and row.mkt_weather in CODES:counts[previous.mkt_weather][row.mkt_weather]+=1
        previous=row
    return pd.DataFrame([{'from_weather':a,'to_weather':b,'count':counts[a][b],
                          'n':sum(counts[a].values()),'probability':counts[a][b]/sum(counts[a].values()) if sum(counts[a].values()) else np.nan}
                         for a in CODES for b in CODES])
