from datetime import datetime
import os
import sqlite3
import time
from zoneinfo import ZoneInfo
import pytest
from policyStudy.policy.market_sentiment.upstream import check_without_receipt, expected_date, residual_files
from policyStudy.policy.market_sentiment.run import source_state

NOW = datetime(2026, 10, 5, 18, tzinfo=ZoneInfo('Asia/Shanghai'))

def database(root):
    with sqlite3.connect(root/'db_cn_basic.db') as c:
        c.executescript('CREATE TABLE tbl_cn_day(trade_date TEXT); CREATE TABLE tbl_cn_stk_limit(trade_date TEXT); CREATE TABLE tbl_cn_tradecal(cal_date TEXT,exchange TEXT,is_open INTEGER);')
        c.execute("INSERT INTO tbl_cn_day VALUES('20260930')")
        c.execute("INSERT INTO tbl_cn_stk_limit VALUES('20260930')")
        c.executemany('INSERT INTO tbl_cn_tradecal VALUES(?,?,?)',[(day,ex,opened) for ex in ('SSE','SZSE') for day,opened in [('20260929',1),('20260930',1),('20261005',0)]])
    with sqlite3.connect(root/'db_cn_kpl.db') as c:
        c.executescript("CREATE TABLE tbl_cn_kpl_list(trade_date TEXT); INSERT INTO tbl_cn_kpl_list VALUES('20260930');")
    with sqlite3.connect(root/'db_cn_index.db') as c:
        c.execute('CREATE TABLE tbl_cn_index_daily(trade_date TEXT,ts_code TEXT)')
        c.executemany('INSERT INTO tbl_cn_index_daily VALUES(?,?)',[('20260930',code) for code in ('000001.SH','399001.SZ','399006.SZ')])
    age(root)

def age(root):
    for p in root.glob('*.db'):os.utime(p,(time.time()-600,time.time()-600))

def test_no_receipt_three_databases_and_quiet_age_pass(tmp_path):
    database(tmp_path)
    before=source_state(tmp_path)
    result=check_without_receipt(tmp_path,source_state,now=NOW)
    assert result['expected_trade_date']=='20260930'
    assert len(result['latest_dates'])==6 and result['receipt_attached'] is False
    assert before==source_state(tmp_path)

@pytest.mark.parametrize('target',['kpl','index','limit'])
def test_each_latest_date_matters(tmp_path,target):
    database(tmp_path)
    names={'kpl':('kpl','tbl_cn_kpl_list'), 'index':('index','tbl_cn_index_daily'), 'limit':('basic','tbl_cn_stk_limit')}
    db,table=names[target]
    with sqlite3.connect(tmp_path/f'db_cn_{db}.db') as c:
        c.execute(f"UPDATE {table} SET trade_date='20260929'"+(" WHERE ts_code='399006.SZ'" if target=='index' else ''))
    age(tmp_path)
    with pytest.raises(RuntimeError,match='cutoff mismatch'):check_without_receipt(tmp_path,source_state,now=NOW)

def test_residual_blocks_but_empty_wal_and_shm_are_normal(tmp_path):
    database(tmp_path)
    (tmp_path/'db_cn_basic.db-wal').touch();(tmp_path/'db_cn_basic.db-shm').write_bytes(b'normal')
    assert residual_files(tmp_path)==[]
    (tmp_path/'db_cn_basic.db.incomplete_123').touch()
    with pytest.raises(RuntimeError,match='residual'):check_without_receipt(tmp_path,source_state,now=NOW)

def test_no_calendar_invention_and_before_close_uses_previous_day(tmp_path):
    database(tmp_path)
    with pytest.raises(RuntimeError,match='horizon'):expected_date(tmp_path,NOW.replace(day=6))
    assert expected_date(tmp_path,NOW.replace(month=9,day=30,hour=14))=='20260929'
    assert expected_date(tmp_path,NOW.replace(month=9,day=30,hour=15))=='20260930'
    with sqlite3.connect(tmp_path/'db_cn_basic.db') as c:c.execute("UPDATE tbl_cn_tradecal SET is_open=1 WHERE cal_date='20261005' AND exchange='SZSE'")
    with pytest.raises(RuntimeError,match='conflict'):expected_date(tmp_path,NOW)

def test_waits_until_full_three_minutes_without_real_sleep(tmp_path):
    database(tmp_path)
    clock=[time.time()]
    initial=clock[0]
    for p in tmp_path.glob('*.db'):os.utime(p,(initial,initial))
    sleeps=[]
    def sleep(seconds):sleeps.append(seconds);clock[0]+=seconds
    result=check_without_receipt(tmp_path,source_state,now=NOW,wall_clock=lambda:clock[0],monotonic=lambda:clock[0],sleep=sleep)
    assert sum(sleeps)>=179.999 and max(sleeps)<=15
    assert result['quiet_age_seconds']>=180

def test_continuous_writes_time_out(tmp_path):
    database(tmp_path)
    clock=[time.time()]
    def unstable(_):return {name:{'mtime_ns':int(clock[0]*1e9)} for name in ('basic','kpl','index')}
    def sleep(seconds):clock[0]+=seconds
    with pytest.raises(RuntimeError,match='not_quiet'):
        check_without_receipt(tmp_path,unstable,now=NOW,wall_clock=lambda:clock[0],monotonic=lambda:clock[0],sleep=sleep)

def test_change_during_queries_is_not_accepted(tmp_path):
    database(tmp_path);clock=[time.time()];calls=[0]
    def changing(root):
        calls[0]+=1;clock[0]+=181
        state=source_state(root)
        state['basic']['bytes']+=calls[0]
        return state
    with pytest.raises(RuntimeError,match='changed_during_preflight'):
        check_without_receipt(tmp_path,changing,now=NOW,wall_clock=lambda:clock[0],monotonic=lambda:clock[0])

def test_today_missing_receipt_runs_quality_alerts_and_repeats_without_revisions(tmp_path,monkeypatch):
    import json
    from policyStudy.policy.market_sentiment import run,upstream
    from policyStudy.policy.market_sentiment.io import read_csv,sha256
    data=tmp_path/'data';data.mkdir();database(data)
    report=run.REPO/'docs/research/mkt_weather_v02_20261005/data'
    raw=read_csv(report/'mkt_raw_daily.csv')
    cal=report/'mkt_calibration_v02.json';cal_sha=sha256(cal)
    real_preflight=upstream.check_without_receipt
    monkeypatch.setattr(upstream,'check_without_receipt',lambda root,reader:real_preflight(root,reader,now=NOW))
    calls=[]
    def build(root,end,output,*unused):
        calls.append(end)
        return raw.copy(),raw.trade_date.tolist(),{'identity':'synthetic-db-integration-with-published-derived-input'}
    monkeypatch.setattr(run,'build_from_database',build)
    out=tmp_path/'output'
    def args():return run.parser().parse_args(['today','--output',str(out),'--data-root',str(data),'--calibration',str(cal),'--upstream-receipt',str(tmp_path/'optional_missing_receipt.json')])
    assert run.today(args())==0
    first=read_csv(out/'mkt_daily.csv')
    assert len(first)==424 and first.mkt_quality_status.eq('DEGRADED').any()
    assert first.iloc[-1].mkt_summary.endswith('未附凭证')
    assert run.today(args())==0
    second=read_csv(out/'mkt_daily.csv')
    receipt=json.loads((out/'today_receipt.json').read_text())
    assert len(calls)==1 and run.historical_changes(first,second)==[]
    assert receipt['changed_historical_dates']==[] and receipt['duplicates']==0
    assert receipt['upstream_completion']['status']=='local_checks_passed_without_receipt'
    assert sha256(cal)==cal_sha

def test_source_change_at_write_gate_preserves_existing_output(tmp_path,monkeypatch):
    from policyStudy.policy.market_sentiment import run
    report=run.REPO/'docs/research/mkt_weather_v02_20261005/data'
    out=tmp_path/'output';out.mkdir();target=out/'mkt_daily.csv';target.write_bytes(b'existing output sentinel')
    args=run.parser().parse_args(['history','--output',str(out),'--raw-daily',str(report/'mkt_raw_daily.csv'),'--calibration',str(report/'mkt_calibration_v02.json')])
    args.expected_source_state={'before':'snapshot'}
    monkeypatch.setattr(run,'source_state',lambda _: {'after':'changed'})
    with pytest.raises(RuntimeError,match='no derived outputs written'):run.history(args)
    assert target.read_bytes()==b'existing output sentinel'
