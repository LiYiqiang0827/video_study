"""Reproducible factual explanations and descriptive method diagnostics.

These outputs are not human labels. No network, returns, fitting, or mutation of
the frozen readings is involved. The controller owns methodological acceptance.
"""
from __future__ import annotations

from itertools import combinations
from pathlib import Path
import numpy as np
import pandas as pd

from .io import write_csv, write_json
from .readings import SPEC, METRICS

VERSION = 'project-review-v1-20261007'
NAMES = {'all_drop': '昨日涨停大跌', 'chain_drop': '连板股大跌', 'ddens': '跌停密度',
         'm1raw': '分板晋级', 'ladder': '梯队完整度', 'max_height': '最高板',
         'log_ratio20': '成交额相对20日', 'advance_pct': '上涨占比', 'udens': '涨停密度'}


def number(value, digits=1, factor=1.):
    return f'{float(value)*factor:.{digits}f}' if pd.notna(value) and np.isfinite(value) else '缺失'


def fraction(k, n):
    if pd.isna(k) or pd.isna(n):
        return '缺失'
    if not np.isfinite(k) or not np.isfinite(n) or k < 0 or n < 0 or k > n or k != int(k) or n != int(n):
        return '无效群体计数（比例缺失）'
    if n <= 0:
        return f'{number(k,0)}/{number(n,0)}（比例缺失）'
    return f'{number(k,0)}/{number(n,0)}（{k/n:.2%}）'


def explain_daily(daily):
    """Each row depends only on that row, so appending future days cannot edit it."""
    rows = []
    for _, r in daily.iterrows():
        get = lambda key: r.get(key, np.nan)
        hit = ('昨日涨停今日大跌' + fraction(get('mkt_all_drop_k'), get('mkt_all_observed_n'))
               + '，连板股大跌' + fraction(get('mkt_chain_drop_k'), get('mkt_chain_observed_n'))
               + f'，跌停{number(get("D"),0)}只')
        promotion, warnings = [], []
        for group, name in (('all','昨日涨停大跌'),('chain','连板股大跌')):
            n = get(f'mkt_{group}_observed_n')
            if pd.notna(n) and 0 < n < SPEC['pseudocount']:
                warnings.append(f'{name}分母仅{n:.0f}只')
            elif pd.notna(n) and n == 0:
                warnings.append(f'{name}无可观察样本')
        for h, name in ((1,'1板'),(2,'2板'),(3,'3板'),(4,'≥4板')):
            k, n = get(f'mkt_promo_h{h}_k'), get(f'mkt_promo_h{h}_n')
            promotion.append(name + ' ' + fraction(k,n))
            if pd.notna(n) and 0 < n < SPEC['pseudocount']:
                warnings.append(f'{name}晋级分母仅{n:.0f}只')
            elif pd.notna(n) and n == 0:
                warnings.append(f'{name}晋级无可观察样本')
        cont = f'最高{number(get("mkt_max_height"),0)}板；晋级：' + '、'.join(promotion)
        act = (f'成交{number(get("mkt_turnover_cny"),3,1e-12)}万亿元，'
               f'为此前20日均额{number(get("mkt_ratio20"),2)}倍；'
               f'上涨占比{number(get("mkt_advance_pct"),2,100)}%；涨停{number(get("U"),0)}只')
        indices = [get('mkt_index_' + x + '_pct') for x in ('sh','sz','cyb')]
        valid_indices = all(pd.notna(x) and np.isfinite(x) for x in indices)
        breadth = get('mkt_advance_pct')
        divergence = ''
        if valid_indices and pd.notna(breadth):
            if sum(x > 0 for x in indices) >= 2 and breadth < .5:
                divergence = '三大指数至少两个上涨，但上涨家数不足一半'
            elif sum(x < 0 for x in indices) >= 2 and breadth > .5:
                divergence = '三大指数至少两个下跌，但上涨家数超过一半'
        if not valid_indices:
            warnings.append('三大指数解释输入有缺失')
        saturated = [NAMES[m] for m in METRICS if pd.notna(get(f'mkt_{m}_q')) and get(f'mkt_{m}_q') in (0,100)]
        if saturated:
            warnings.append('2025标尺端点饱和：' + '、'.join(saturated))
        if r.get('mkt_quality_status','UNKNOWN') != 'OK':
            warnings.append('源或群体存在已披露质量缺口')
        coexistence = (pd.notna(get('mkt_hit')) and pd.notna(get('mkt_act'))
                       and get('mkt_hit') >= SPEC['weather_thresholds']['hit']
                       and get('mkt_act') >= SPEC['weather_thresholds']['act'])
        interpretation = '；'.join(x for x in [
            '挨打达到惩罚分支，同时活跃达到多云分支的活跃门槛' if coexistence else '', divergence] if x)
        label = r.get('mkt_weather_label','')
        label = label if isinstance(label,str) and label else '天气缺失'
        t = SPEC['weather_thresholds']
        basis = {
            'sunny':f'挨打<{t["hit"]}且延续≥{t["cont_hit_low"]}；此分支不使用活跃，晴不表示普涨',
            'thunder':f'挨打≥{t["hit"]}且延续≥{t["cont_hit_high"]}',
            'storm':f'挨打≥{t["hit"]}且延续<{t["cont_hit_high"]}',
            'cloudy':f'挨打<{t["hit"]}、延续<{t["cont_hit_low"]}且活跃≥{t["act"]}',
            'overcast':f'挨打<{t["hit"]}、延续<{t["cont_hit_low"]}且活跃<{t["act"]}',
        }.get(r.get('mkt_weather'),'必要读数缺失，天气未分类')
        text = (f'{r.trade_date} {label}｜'
                f'分类依据：{basis}。'
                f'挨打{number(get("mkt_hit"))}：{hit}。'
                f'延续{number(get("mkt_cont"))}：{cont}。'
                f'活跃{number(get("mkt_act"))}：{act}。')
        if interpretation: text += interpretation + '。'
        if warnings: text += '解释注意：' + '；'.join(warnings) + '。'
        rows.append({'trade_date': r.trade_date, 'review_version': VERSION,
                     'provenance': 'controller_authored_deterministic_explanation',
                     'weather_basis': basis,
                     'hit_facts': hit, 'continuation_facts': cont, 'activity_facts': act,
                     'breadth_index_divergence': divergence,
                     'active_under_pressure': bool(coexistence),
                     'small_group_notes': '；'.join(x for x in warnings if '分母仅' in x or '无可观察样本' in x),
                     'explanation_cautions': '；'.join(warnings), 'explanation': text})
    return pd.DataFrame(rows)


def contributions(daily):
    """Audit exact component sums, including the existing local missing reweighting."""
    result = []
    for _, r in daily.iterrows():
        for dimension, weights in SPEC['readings'].items():
            observed = [(m,w,r.get(f'mkt_{m}_q',np.nan)) for m,w in weights.items()]
            denominator = sum(w for m,w,q in observed if pd.notna(q))
            total = 0.
            for m,w,q in observed:
                contribution = q*w/denominator if pd.notna(q) and denominator else np.nan
                if pd.notna(contribution): total += contribution
                result.append({'trade_date':r.trade_date,'dimension':dimension,'metric':m,
                               'nominal_weight':w,'available_weight':denominator,'q':q,
                               'effective_weight':w/denominator if pd.notna(q) and denominator else np.nan,
                               'contribution':contribution})
            actual = r[f'mkt_{dimension}']
            if denominator:
                if not np.isclose(total,actual,rtol=0,atol=1e-10):
                    raise ValueError(f'Component sum mismatch {r.trade_date}/{dimension}')
            elif pd.notna(actual):
                raise ValueError(f'Nonmissing reading without components {r.trade_date}/{dimension}')
    return pd.DataFrame(result)


def classify_with_thresholds(row, thresholds):
    h,c,a = (row[f'mkt_{s}'] for s in ('hit','cont','act'))
    if not np.isfinite(h) or not np.isfinite(c): return ''
    if h >= thresholds['hit']:
        return 'storm' if c < thresholds['cont_hit_high'] else 'thunder'
    if c >= thresholds['cont_hit_low']: return 'sunny'
    if not np.isfinite(a): return ''
    return 'cloudy' if a >= thresholds['act'] else 'overcast'


def sensitivity(daily):
    """Predeclared ±3 per cutpoint, one at a time; no search/selection or prediction eval."""
    base = daily.apply(classify_with_thresholds,axis=1,thresholds=SPEC['weather_thresholds'])
    if not base.equals(daily.mkt_weather.fillna('')):
        raise ValueError('Published weather does not match frozen classification')
    rows, details = [], []
    for parameter in SPEC['weather_thresholds']:
        for delta in (-3,3):
            thresholds = dict(SPEC['weather_thresholds'])
            thresholds[parameter] += delta
            trial = daily.apply(classify_with_thresholds,axis=1,thresholds=thresholds)
            for year, g in daily.groupby(daily.trade_date.str[:4]):
                valid = base.loc[g.index].ne('') & trial.loc[g.index].ne('')
                changed = valid & base.loc[g.index].ne(trial.loc[g.index])
                availability_changed = base.loc[g.index].ne('') != trial.loc[g.index].ne('')
                rows.append({'year':year,'parameter':parameter,'delta':delta,
                             'baseline_cutpoint':SPEC['weather_thresholds'][parameter],
                             'trial_cutpoint':thresholds[parameter], 'all_days':len(g),
                             'comparable_n':int(valid.sum()),'unavailable_n':int((~valid).sum()),
                             'changed_n':int(changed.sum()),
                             'availability_changed_n':int(availability_changed.sum()),
                             'changed_rate':float(changed.sum()/valid.sum()) if valid.sum() else np.nan})
                for i in g.index[changed | availability_changed]:
                    details.append({'trade_date':daily.at[i,'trade_date'],'parameter':parameter,'delta':delta,
                                    'baseline_weather':base.at[i],'trial_weather':trial.at[i],
                                    'change_type':'availability' if availability_changed.at[i] else 'weather'})
    return pd.DataFrame(rows), pd.DataFrame(details,columns=['trade_date','parameter','delta','baseline_weather','trial_weather','change_type'])


def correlations(daily):
    rows = []
    for year,g in daily.groupby(daily.trade_date.str[:4]):
        for scope, columns in [('readings',[f'mkt_{s}' for s in SPEC['readings']]),
                               ('components',[f'mkt_{s}_q' for s in METRICS])]:
            for a,b in combinations(columns,2):
                valid = g[[a,b]].dropna()
                value = valid[a].corr(valid[b]) if len(valid)>1 and valid[a].nunique()>1 and valid[b].nunique()>1 else np.nan
                rows.append({'year':year,'scope':scope,'a':a,'b':b,'paired_n':len(valid),'pearson_r':value})
    return pd.DataFrame(rows)


def run_review(daily, output):
    if daily.trade_date.duplicated().any(): raise ValueError('Duplicate market dates')
    output = Path(output)
    explanations = explain_daily(daily)
    component_table = contributions(daily)
    trials, changed = sensitivity(daily)
    corr = correlations(daily)
    for name,frame in [('daily_explanations',explanations),('component_contributions',component_table),
                       ('threshold_sensitivity',trials),('threshold_changed_dates',changed),('correlations',corr)]:
        write_csv(output/(name+'.csv'),frame)
    summary = {'version':VERSION,'status':'computed','rows':len(daily),
               'date_start':daily.trade_date.min(),'date_end':daily.trade_date.max(),
               'human_labels_used':False,'threshold_changes_applied':False,
               'calibration_refitted':False,'returns_used':False,'component_sums_verified':True,
               'sensitivity_design':'each of four frozen weather cutpoints +/-3 individually; no selection',
               'sample_note':'2025 calibration is retrospective; 2026 already used in project research',
               'by_year':{}}
    for year,g in explanations.groupby(explanations.trade_date.str[:4]):
        summary['by_year'][year]={'days':len(g),'breadth_index_divergence_n':int(g.breadth_index_divergence.ne('').sum()),
                                 'active_under_pressure_n':int(g.active_under_pressure.sum()),
                                 'small_or_zero_group_n':int(g.small_group_notes.ne('').sum())}
    write_json(output/'review_summary.json',summary)
    return summary
