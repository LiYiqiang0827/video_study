"""2025 pooled priors, itemwise scaling, local reweighting, fixed weather rules."""
from __future__ import annotations
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
import pandas as pd

SPEC = json.loads((Path(__file__).parent / 'config/mkt_spec_v02.json').read_text(encoding='utf-8'))
METRICS = tuple(m for group in SPEC['readings'].values() for m in group)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def smooth(k, n, prior):
    k = pd.to_numeric(k, errors='coerce')
    n = pd.to_numeric(n, errors='coerce')
    if prior is None:
        return pd.Series(np.nan, index=n.index)
    return ((k + SPEC['pseudocount'] * prior) / (n + SPEC['pseudocount'])).where(n.gt(0) & k.ge(0) & k.le(n))


def values(raw, priors):
    result = pd.DataFrame(index=raw.index)
    for m in METRICS:
        if m in ('all_drop', 'chain_drop'):
            g = m.removesuffix('_drop')
            result[m] = smooth(raw[f'mkt_{g}_drop_k'], raw[f'mkt_{g}_observed_n'], priors[g]['prior'])
        else:
            result[m] = pd.to_numeric(raw[f'mkt_{m}'], errors='coerce')
    return result.replace([np.inf, -np.inf], np.nan)


def calibrate(raw, input_fingerprint=''):
    y = raw.loc[raw.trade_date.astype(str).str.startswith('2025')].copy()
    if y.empty:
        raise ValueError('2025 calibration input is required; cannot calibrate on 2026')
    priors = {}
    for g in ('all', 'chain'):
        valid = y[f'mkt_{g}_observed_n'].gt(0) & y[f'mkt_{g}_drop_k'].notna()
        k = int(y.loc[valid, f'mkt_{g}_drop_k'].sum())
        n = int(y.loc[valid, f'mkt_{g}_observed_n'].sum())
        priors[g] = {'k': k, 'n': n, 'prior': k / n if n else None, 'valid_days': int(valid.sum())}
    v = values(y, priors)
    anchors = {}
    for m in METRICS:
        x = v[m].dropna()
        anchors[m] = {'p10': float(x.quantile(.1, interpolation='linear')) if len(x) else None,
                      'p90': float(x.quantile(.9, interpolation='linear')) if len(x) else None,
                      'valid_days': len(x), 'missing_days': int(v[m].isna().sum()), 'direction': 'positive'}
    ddens = v.ddens.dropna()
    promotion = {}
    for h in range(1,5):
        kcol,ncol=f'mkt_promo_h{h}_k',f'mkt_promo_h{h}_n'
        if kcol in y and ncol in y:
            k,n=int(y[kcol].sum()),int(y[ncol].sum())
            promotion[f'h{h}']={'k':k,'n':n,'prior':k/n if n else None,'valid_days':int(y[ncol].gt(0).sum())}
    cal = {'version': 'MKT-v0.2-2025-a5-v1', 'calibration_year': 2025, 'calibration_days': len(y),
           'pseudocount': 5, 'quantile_method': 'linear', 'constant_anchor_value': 50,
           'input_fingerprint': input_fingerprint, 'spec_fingerprint': fingerprint(SPEC),
           'priors': priors, 'promotion_pooled_counts':promotion, 'anchors': anchors,
           'ddens_p99': float(ddens.quantile(.99, interpolation='linear')) if len(ddens) else None,
           'm1_source': 'F1 fixed 2025 pooled priors, a=5, height weights .25/.30/.25/.20; no refit'}
    cal['fingerprint'] = fingerprint(cal)
    return cal


def weather(hit, cont, act):
    """Only a branch's necessary readings must be observed."""
    if not np.isfinite(hit) or not np.isfinite(cont):
        return '', 'missing_hit_or_cont'
    t = SPEC['weather_thresholds']
    if hit >= t['hit']:
        return ('storm' if cont < t['cont_hit_high'] else 'thunder'), ''
    if cont >= t['cont_hit_low']:
        return 'sunny', ''
    if not np.isfinite(act):
        return '', 'missing_act_for_low_hit_low_cont'
    return ('cloudy' if act >= t['act'] else 'overcast'), ''


def compute_readings(raw, calibration, generated_at=None):
    if calibration.get('spec_fingerprint') != fingerprint(SPEC):
        raise ValueError('Calibration/config version mismatch')
    core = {k: v for k, v in calibration.items() if k != 'fingerprint'}
    if calibration.get('fingerprint') != fingerprint(core):
        raise ValueError('Calibration fingerprint mismatch')
    if raw.trade_date.duplicated().any():
        raise ValueError('Duplicate market dates')
    out = raw.sort_values('trade_date').reset_index(drop=True).copy()
    v = values(out, calibration['priors'])
    extra = {}
    for m in METRICS:
        x = v[m]; a = calibration['anchors'][m]; lo, hi = a['p10'], a['p90']
        valid = x.notna() & (lo is not None) & (hi is not None)
        q = pd.Series(np.nan, index=x.index)
        if lo is not None and hi is not None:
            q = pd.Series(50., index=x.index).where(valid) if hi == lo else ((x-lo)/(hi-lo)*100).clip(0,100).where(valid)
        p = f'mkt_{m}_'
        extra.update({p+'value': x, p+'q': q,
                      p+'clip_low': valid & x.lt(lo if lo is not None else -np.inf),
                      p+'clip_high': valid & x.gt(hi if hi is not None else np.inf),
                      p+'endpoint_low': q.eq(0), p+'endpoint_high': q.eq(100),
                      p+'constant_anchor': valid & (lo == hi)})
    out = pd.concat([out, pd.DataFrame(extra)], axis=1)
    for reading, weights in SPEC['readings'].items():
        numerator = pd.Series(0., index=out.index); denominator = numerator.copy()
        for metric, weight in weights.items():
            q = out[f'mkt_{metric}_q']
            numerator += q.fillna(0) * weight
            denominator += q.notna() * weight
        out[f'mkt_{reading}'] = numerator / denominator.replace(0, np.nan)
        out[f'mkt_{reading}_available_weight'] = denominator
        out[f'mkt_{reading}_quality'] = np.select([denominator.eq(0), denominator.lt(1-1e-12)], ['UNAVAILABLE','DEGRADED'], 'OK')
        out[f'mkt_{reading}_reasons'] = [';'.join('missing_'+m for m in weights if pd.isna(out.at[i,f'mkt_{m}_q'])) for i in out.index]
    w = [weather(h,c,a) for h,c,a in out[['mkt_hit','mkt_cont','mkt_act']].itertuples(index=False,name=None)]
    out['mkt_weather'] = [x[0] for x in w]
    out['mkt_weather_reason'] = [x[1] for x in w]
    out['mkt_weather_label'] = out.mkt_weather.map(SPEC['weather_labels']).fillna('')
    out['mkt_extreme'] = out.mkt_ddens.ge(calibration['ddens_p99']) if calibration['ddens_p99'] is not None else False
    qualities = out[[f'mkt_{x}_quality' for x in SPEC['readings']]]
    source_bad = out.get('mkt_source_quality', pd.Series('OK', index=out.index)).fillna('UNKNOWN').ne('OK')
    if 'mkt_adapter_quality' in out:source_bad |= out.mkt_adapter_quality.ne('OK')
    out['mkt_quality_status'] = np.select([qualities.eq('UNAVAILABLE').all(axis=1), qualities.ne('OK').any(axis=1) | source_bad], ['UNAVAILABLE', 'DEGRADED'], 'OK')
    out['mkt_quality_reasons'] = [';'.join(filter(None, [str(out.at[i, f'mkt_{r}_reasons']) for r in SPEC['readings']] +
                                              [str(out.at[i,col]) if col in out and pd.notna(out.at[i,col]) else '' for col in ('mkt_source_reasons','mkt_adapter_reasons')])) for i in out.index]
    out['method_version'] = SPEC['method_version']
    out['threshold_version'] = SPEC['threshold_version']
    out['calibration_version'] = calibration['version']
    out['mkt_calibration_fingerprint'] = calibration['fingerprint']
    out['input_snapshot_id'] = raw.attrs.get('input_snapshot_id', calibration['input_fingerprint'])
    out['generated_at'] = generated_at or datetime.now(timezone.utc).isoformat()
    out['data_cutoff'] = out.trade_date.max()
    return out


def clipping_statistics(daily, calibration):
    rows = []
    for year, g in daily.groupby(daily.trade_date.str[:4]):
        for m in METRICS:
            p = f'mkt_{m}_'; n = int(g[p+'q'].notna().sum())
            r = {'year': year, 'metric': m, 'valid_n': n, 'missing_n': len(g)-n, **calibration['anchors'][m]}
            for suffix in ('clip_low','clip_high','endpoint_low','endpoint_high','constant_anchor'):
                count = int(g[p+suffix].sum()); r[suffix+'_n'] = count; r[suffix+'_rate'] = count/n if n else None
            rows.append(r)
    return pd.DataFrame(rows)
