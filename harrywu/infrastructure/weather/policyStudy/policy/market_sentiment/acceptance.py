"""Controller acceptance: actual event truncation, independent arithmetic, repeat."""
import argparse
import json
from pathlib import Path
import numpy as np
import pandas as pd
from .adapter import build_raw,load_formal,FIELD_MAPPING
from .readings import calibrate,compute_readings,clipping_statistics,METRICS,SPEC
from .forecast import add_forecasts,evaluate_forecasts,CODES
from .io import read_csv,write_csv,write_json,sha256

def accept(formal_dir,published_daily,daily_path,cal_path,calendar_path,output):
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    paths=[Path(formal_dir)/n for n in ('events.parquet','cohorts.parquet','sentiment_daily.csv','calibration.json')]
    before={str(p):sha256(p) for p in paths}
    assert sha256(published_daily)==sha256(paths[2]),'Wrong final F1 input'
    e,c,f=load_formal(formal_dir);calendar=read_csv(calendar_path).trade_date.tolist()
    raw=build_raw(e,c,f);cal=json.loads(Path(cal_path).read_text(encoding='utf-8'));saved=read_csv(daily_path)
    computed=add_forecasts(compute_readings(raw,cal,generated_at='acceptance'),calendar)
    columns=[x for x in computed if (x.startswith('mkt_') or x.startswith('forecast_')) and x not in ('mkt_summary',)]
    # Input-only raw evidence columns retained; missing CSV strings normalize.
    def equal(a,b,cols):
        for col in cols:
            if pd.api.types.is_numeric_dtype(a[col]) and pd.api.types.is_numeric_dtype(b[col]):
                np.testing.assert_allclose(a[col],b[col],rtol=0,atol=1e-12,equal_nan=True,err_msg=col)
            else:assert a[col].fillna('').astype(str).tolist()==b[col].fillna('').astype(str).tolist(),col
    equal(computed,saved,columns)
    assert len(raw)==424 and raw.trade_date.str[:4].value_counts().to_dict()=={'2025':243,'2026':181}
    for new,old in FIELD_MAPPING.items():equal(raw,f,[old]) if new==old else None
    for new,old in FIELD_MAPPING.items():
        if pd.api.types.is_numeric_dtype(raw[new]):np.testing.assert_array_equal(raw[new],f[old])
        else:assert raw[new].fillna('').equals(f[old].fillna(''))
    m1diff=float(np.max(np.abs(computed.mkt_m1raw_q-computed.mkt_f1_m1)))
    assert m1diff<1e-10
    fitted=calibrate(raw,cal['input_fingerprint'])
    assert fitted==cal,'Independent calibration mismatch'
    prefixes=[]
    for cutoff in ('2026-03-31','2026-06-30','2026-09-09'):
        compact=cutoff.replace('-','')
        # Physically discard all future input records before adapter/new calculations.
        pe=e[e.trade_date.astype(str).le(compact)].copy();pc=c[c.date.astype(str).le(compact)].copy();pf=f[f.trade_date.le(cutoff)].copy()
        pr=build_raw(pe,pc,pf)
        got=add_forecasts(compute_readings(pr,cal,generated_at='acceptance'),calendar)
        expected=computed[computed.trade_date.le(cutoff)].reset_index(drop=True)
        equal(got,expected,columns)
        prefixes.append({'cutoff':cutoff,'events':len(pe),'cohorts':len(pc),'days':len(got),'all_calculation_fields_equal':True,'input_truncated_before_compute':True})
    repeat=add_forecasts(compute_readings(build_raw(e,c,f),cal,generated_at='acceptance'),calendar)
    pd.testing.assert_frame_equal(computed,repeat,check_exact=True)
    for r in computed.itertuples():
        assert r.mkt_forecast_n==sum(getattr(r,f'mkt_forecast_{x}_count') for x in CODES)
        if r.mkt_forecast_n:assert abs(sum(getattr(r,f'mkt_forecast_{x}_prob') for x in CODES)-1)<1e-12
        else:assert all(pd.isna(getattr(r,f'mkt_forecast_{x}_prob')) for x in CODES)
    selected=['2025-04-07','2026-08-31','2026-09-10','2026-09-28','2026-09-30','2026-07-14']
    checks=[]
    for day in selected:
        x=e[e.trade_date.astype(str).eq(day.replace('-',''))];g=c[c.date.astype(str).eq(day.replace('-',''))];row=computed.set_index('trade_date').loc[day]
        result={'trade_date':day}
        for group,subset in [('all',g),('chain',g[g.prev_height.ge(2)])]:
            obs=subset[subset.mid_obs];k=sum((int(z.cC)*20<=int(z.cP)*19) or bool(z.Dn) for z in obs.itertuples())
            assert k==row[f'mkt_{group}_drop_k'] and len(obs)==row[f'mkt_{group}_observed_n']
            result[group+'_k']=k;result[group+'_n']=len(obs)
        eligible=x[x.eligible];assert int(eligible.cC.gt(eligible.cP).sum())==row.mkt_advance_n
        manual={}
        for m in METRICS:
            value=row[f'mkt_{m}_value'];a=cal['anchors'][m]
            q=50 if a['p90']==a['p10'] else min(100,max(0,100*(value-a['p10'])/(a['p90']-a['p10'])))
            assert abs(q-row[f'mkt_{m}_q'])<1e-12;manual[m]=q
        for reading,weights in SPEC['readings'].items():
            value=sum(manual[m]*w for m,w in weights.items())/sum(weights.values())
            assert abs(value-row[f'mkt_{reading}'])<1e-12;result[reading]=value
        result.update(weather=row.mkt_weather,quality=row.mkt_quality_status,chain_height_over4_n=int(g.prev_height.gt(4).sum()),forecast_n=int(row.mkt_forecast_n))
        checks.append(result)
    write_csv(output/'direct_cases.csv',pd.DataFrame(checks))
    details=computed[computed.trade_date.isin(selected)][['trade_date']+[x for x in computed if x.startswith('mkt_')]]
    write_csv(output/'direct_case_components.csv',details)
    after={str(p):sha256(p) for p in paths};assert before==after
    summary,_=evaluate_forecasts(computed)
    result={'status':'passed_controller_deterministic_checks','calendar_days':len(raw),'three_reading_days':int(computed[['mkt_hit','mkt_cont','mkt_act']].notna().all(axis=1).sum()),
            'weather_days':int(computed.mkt_weather.isin(CODES).sum()),'f1_relay_score_exact':True,'all_f1_mapping_equal':True,'m1_q_max_abs_diff':m1diff,
            'calibration_independently_refit_equal':True,'actual_event_prefixes':prefixes,'repeat_all_fields_exact':True,
            'source_sha256_before_after_equal':True,'source_hashes':before,'evaluation':summary,
            'note':'2025 full-year calibration is retrospective; 2026 already studied. No strategy-return test.'}
    write_json(output/'controller_acceptance.json',result)
    return result

def main():
    p=argparse.ArgumentParser();
    for n in ('formal-dir','published-daily','daily','calibration','calendar','output'):p.add_argument('--'+n,required=True)
    a=p.parse_args();print(accept(a.formal_dir,a.published_daily,a.daily,a.calibration,a.calendar,a.output))
if __name__=='__main__':main()
