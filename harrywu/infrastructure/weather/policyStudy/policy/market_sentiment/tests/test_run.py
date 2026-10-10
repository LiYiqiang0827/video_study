import json
from pathlib import Path
import numpy as np
import pandas as pd
import pytest
from policyStudy.policy.market_sentiment.run import historical_changes,source_state,exclusive_output,parser,today,summary_line

def test_revision_detection_distinguishes_csv_null_and_actual_change():
    old=pd.DataFrame({'trade_date':['2026-01-05','2026-01-06'],'score':[20.,40.],'reason':[np.nan,'partial'],'generated_at':['old','old']})
    new=old.copy();new['reason']=['','partial'];new['generated_at']='new'
    assert historical_changes(old,new)==[]
    new.loc[1,'score']=41
    assert historical_changes(old,new)==['2026-01-06']

def test_source_writing_sidecar_refused(tmp_path):
    for name in ('basic','kpl','index'):(tmp_path/f'db_cn_{name}.db').write_bytes(b'synthetic test only')
    before=source_state(tmp_path)
    assert len(before)==3
    (tmp_path/'db_cn_basic.db-wal').write_bytes(b'active')
    with pytest.raises(RuntimeError,match='upstream_busy'):source_state(tmp_path)

def test_concurrent_output_lock_and_cleanup(tmp_path):
    with exclusive_output(tmp_path):
        with pytest.raises(RuntimeError,match='busy'):
            with exclusive_output(tmp_path):pass
    assert not (tmp_path/'.mkt.lock').exists()

def test_today_never_refits_without_fixed_calibration(tmp_path):
    args=parser().parse_args(['today','--output',str(tmp_path),'--raw-daily','unused'])
    with pytest.raises(ValueError,match='fixed --calibration'):today(args)

def make_test_database(root,day,limit_day=None):
    import sqlite3
    root.mkdir()
    with sqlite3.connect(root/'db_cn_basic.db') as c:
        c.execute('create table tbl_cn_day (trade_date text)');c.execute('insert into tbl_cn_day values (?)',(day,))
        c.execute('create table tbl_cn_stk_limit (trade_date text)');c.execute('insert into tbl_cn_stk_limit values (?)',(limit_day or day,))
    for name in ('kpl','index'):
        with sqlite3.connect(root/f'db_cn_{name}.db') as c:c.execute('create table unused (id integer)')

def test_incomplete_upstream_refuses_before_output(tmp_path):
    root=tmp_path/'data';make_test_database(root,'20260930','20260929')
    args=parser().parse_args(['today','--output',str(tmp_path/'output'),'--data-root',str(root),'--calibration','unused'])
    with pytest.raises(RuntimeError,match='cutoff mismatch'):today(args)
    assert not (tmp_path/'output'/'mkt_daily.csv').exists()

def test_failed_completion_receipt_never_uses_stale_data(tmp_path):
    root=tmp_path/'data';make_test_database(root,'20250930')
    receipt=tmp_path/'failed.json';receipt.write_text(json.dumps({'status':'failed','trade_date':'2025-09-30'}))
    args=parser().parse_args(['today','--output',str(tmp_path/'output'),'--data-root',str(root),'--calibration','unused','--upstream-receipt',str(receipt)])
    with pytest.raises(RuntimeError,match='upstream_failed_or_stale_receipt'):today(args)
    assert not (tmp_path/'output'/'mkt_daily.csv').exists()

def test_missing_next_calendar_date_is_printed_explicitly():
    r=pd.Series({'trade_date':'2026-09-30','forecast_target_date':'','mkt_forecast_n':0})
    line=summary_line(r)
    assert '待日历补齐' in line and '尚无同类转移样本' in line
