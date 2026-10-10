import copy
import numpy as np
import pandas as pd
import pytest
from policyStudy.policy.market_sentiment.review import explain_daily, contributions, sensitivity, run_review
from policyStudy.policy.market_sentiment.readings import SPEC, METRICS, weather


def row(**changes):
    r={'trade_date':'2026-09-07','mkt_hit':50.,'mkt_cont':50.,'mkt_act':50.,'mkt_weather':'sunny',
       'mkt_weather_label':'晴','mkt_all_drop_k':0,'mkt_all_observed_n':0,
       'mkt_chain_drop_k':1,'mkt_chain_observed_n':1,'mkt_quality_status':'OK',
       'mkt_advance_pct':.35,'mkt_index_sh_pct':.1,'mkt_index_sz_pct':.2,'mkt_index_cyb_pct':-.1}
    for m in METRICS:r[f'mkt_{m}_q']=50.
    for h in range(1,5):r[f'mkt_promo_h{h}_k']=0;r[f'mkt_promo_h{h}_n']=0
    r.update(changes)
    return r


def test_facts_zero_small_denominator_and_no_invented_label():
    d=pd.DataFrame([row(mkt_promo_h4_k=1,mkt_promo_h4_n=1)])
    x=explain_daily(d).iloc[0]
    assert '0/0（比例缺失）' in x.hit_facts
    assert '≥4板 1/1（100.00%）' in x.continuation_facts
    assert '≥4板晋级分母仅1只' in x.small_group_notes
    assert '至少两个上涨' in x.breadth_index_divergence
    assert x.provenance=='controller_authored_deterministic_explanation'
    assert '此分支不使用活跃' in x.weather_basis


def test_prefix_and_input_nonmutation():
    d=pd.DataFrame([row(),row(trade_date='2026-09-08',mkt_hit=65.)])
    before=d.copy(deep=True)
    pd.testing.assert_frame_equal(explain_daily(d).iloc[:1],explain_daily(d.iloc[:1]))
    pd.testing.assert_frame_equal(d,before)


def test_missing_index_does_not_imply_divergence():
    x=explain_daily(pd.DataFrame([row(mkt_index_cyb_pct=np.nan)])).iloc[0]
    assert x.breadth_index_divergence==''
    assert '三大指数解释输入有缺失' in x.explanation_cautions


def test_component_sum_local_reweight_and_tamper_detection():
    d=pd.DataFrame([row(mkt_all_drop_q=np.nan,mkt_chain_drop_q=60.,mkt_ddens_q=20.,mkt_hit=(.35*60+.25*20)/.6)])
    c=contributions(d)
    assert c[c.dimension.eq('hit')].contribution.sum()==pytest.approx(d.mkt_hit.iloc[0])
    d.loc[0,'mkt_hit']+=1
    with pytest.raises(ValueError,match='Component sum mismatch'):contributions(d)


def test_sensitivity_branch_and_no_mutation():
    spec=copy.deepcopy(SPEC)
    d=pd.DataFrame([row(mkt_hit=61.,mkt_cont=45.,mkt_act=20.,mkt_weather='thunder')])
    table,changes=sensitivity(d)
    assert len(table)==8
    assert changes.to_dict('records')==[{'trade_date':'2026-09-07','parameter':'hit','delta':3,
                                       'baseline_weather':'thunder','trial_weather':'overcast','change_type':'weather'}]
    assert SPEC==spec


def test_threshold_boundaries_match_production():
    from policyStudy.policy.market_sentiment.review import classify_with_thresholds
    for h in (np.nan,59.999,60.):
        for c in (39.999,40.,49.999,50.):
            for a in (np.nan,34.999,35.):
                r=row(mkt_hit=h,mkt_cont=c,mkt_act=a)
                assert classify_with_thresholds(r,SPEC['weather_thresholds'])==weather(h,c,a)[0]


def test_review_missing_reading_retained(tmp_path):
    r=row(mkt_hit=np.nan,mkt_weather='')
    for m in SPEC['readings']['hit']:r[f'mkt_{m}_q']=np.nan
    summary=run_review(pd.DataFrame([r]),tmp_path)
    assert summary['rows']==1 and summary['human_labels_used'] is False
    trials=pd.read_csv(tmp_path/'threshold_sensitivity.csv')
    assert trials.comparable_n.eq(0).all() and trials.unavailable_n.eq(1).all()


def test_duplicate_rejected(tmp_path):
    with pytest.raises(ValueError,match='Duplicate'):run_review(pd.DataFrame([row(),row()]),tmp_path)


def test_small_hit_group_and_missing_weather_label():
    r=row(mkt_chain_observed_n=4,mkt_weather_label=np.nan)
    x=explain_daily(pd.DataFrame([r])).iloc[0]
    assert '连板股大跌分母仅4只' in x.small_group_notes
    assert '天气缺失' in x.explanation and 'nan' not in x.explanation


def test_sensitivity_reports_loss_of_availability():
    d=pd.DataFrame([row(mkt_act=np.nan)])
    trials,changed=sensitivity(d)
    affected=trials[trials.parameter.eq('cont_hit_low') & trials.delta.eq(3)].iloc[0]
    assert affected.comparable_n==0 and affected.availability_changed_n==1
    x=changed[changed.parameter.eq('cont_hit_low')].iloc[0]
    assert x.baseline_weather=='sunny' and x.trial_weather=='' and x.change_type=='availability'


def test_invalid_cohort_counts_never_look_like_observed_percentages():
    from policyStudy.policy.market_sentiment.review import fraction
    for k,n in [(2,1),(-1,1),(1,np.inf),(1,0),(.5,1)]:
        assert fraction(k,n)=='无效群体计数（比例缺失）'
