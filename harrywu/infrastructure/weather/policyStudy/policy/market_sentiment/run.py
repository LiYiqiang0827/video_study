"""Read-only MKT history/today; independent derived-table reproduction."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
from datetime import datetime
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd
from .io import read_csv, sha256, write_csv, write_json
from .readings import SPEC, calibrate, compute_readings, clipping_statistics, fingerprint

CODE=Path(__file__).resolve().parent
REPO=CODE.parents[2]
OLD_REPORT=REPO/'docs/research/mrwu_sentiment_cycle_20261005/data'

def iso(value):
    s=str(value).replace('-','')[:8]
    return f'{s[:4]}-{s[4:6]}-{s[6:8]}'

def engine():
    p=CODE.parent/'speculation_sentiment_lite'
    if str(p) not in sys.path:sys.path.insert(0,str(p))
    spec=importlib.util.spec_from_file_location('mkt_reused_f1',p/'production.py')
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    return m

def source_state(root):
    result={}
    for name in ('basic','kpl','index'):
        p=Path(root)/f'db_cn_{name}.db'
        if not p.is_file():raise FileNotFoundError(p)
        for suffix in ('-wal','-journal'):
            side=Path(str(p)+suffix)
            if side.exists() and side.stat().st_size:raise RuntimeError(f'upstream_busy: nonempty {side.name}')
        st=p.stat();result[name]={'path':str(p.resolve()),'bytes':st.st_size,'mtime_ns':st.st_mtime_ns}
    return result

def supplementary(root):
    """Small calendar/index queries; only stable local SQLite, never downloads."""
    m=engine()
    with m.ro(Path(root)/'db_cn_basic.db') as c:
        calendar=pd.read_sql_query("SELECT cal_date FROM tbl_cn_tradecal WHERE exchange='SSE' AND is_open=1 AND cal_date>='20250101' ORDER BY cal_date",c).cal_date.map(iso).tolist()
    with m.ro(Path(root)/'db_cn_index.db') as c:
        tables={x[0] for x in c.execute("select name from sqlite_master where type='table'")}
        table='tbl_cn_index_daily'
        if table not in tables:
            raise ValueError(f'Missing known index table {table}; do not silently invent index source')
        columns={x[1] for x in c.execute(f'pragma table_info({table})')}
        if 'pct_chg' in columns:
            index=pd.read_sql_query(f"select trade_date,ts_code,pct_chg from {table} where ts_code in ('000001.SH','399001.SZ','399006.SZ') and trade_date>='20250101'",c)
        else:
            index=pd.read_sql_query(f"select trade_date,ts_code,100*(close/pre_close-1) as pct_chg from {table} where ts_code in ('000001.SH','399001.SZ','399006.SZ') and trade_date>='20250101'",c)
    index.trade_date=index.trade_date.map(iso)
    names={'000001.SH':'mkt_index_sh_pct','399001.SZ':'mkt_index_sz_pct','399006.SZ':'mkt_index_cyb_pct'}
    if index.duplicated(['trade_date','ts_code']).any():raise ValueError('Duplicate index dates')
    index=index.pivot(index='trade_date',columns='ts_code',values='pct_chg').rename(columns=names).reset_index()
    return calendar,index

def build_from_database(root,end,output,input_manifest=None,cache_dir=None):
    from .adapter import build_raw
    m=engine();before=source_state(root)
    tables,identity,manifest=m.load_inputs(root,'20241101',str(end).replace('-',''),manifest_path=input_manifest,cache_dir=Path(cache_dir) if cache_dir else output/'f1_raw_cache')
    events,calendar=m.build_events(tables)
    raw,quality,cohorts=m.compute_raw(events,calendar,tables['suspends'],'formal',True,start='20241101')
    oldcal=json.loads((OLD_REPORT/'calibration.json').read_text(encoding='utf-8'))
    oldspec=json.loads((CODE.parent/'speculation_sentiment_lite/config/formal_spec.json').read_text(encoding='utf-8'))
    formal=m.score_versions(raw,quality,oldcal,identity,oldspec)
    formal=formal[formal.trade_date.ge('2025-01-02')].copy()
    result=build_raw(events,cohorts,formal)
    days,index=supplementary(root)
    result=result.merge(index,on='trade_date',how='left',validate='one_to_one')
    if before!=source_state(root):raise RuntimeError('upstream_changed: source changed during read; no outputs accepted')
    result.attrs['input_snapshot_id']=identity
    return result,days,{'source_state':before,'source_manifest':manifest,'identity':identity,'cutoff':iso(end)}

def summary_line(row):
    def num(key,digits=1,scale=1):
        x=row.get(key,np.nan)
        return f'{x/scale:.{digits}f}' if pd.notna(x) else '缺失'
    probs=[(code,row.get(f'mkt_forecast_{code}_prob',np.nan)) for code in SPEC['weather_order']]
    valid=sorted([(c,p) for c,p in probs if pd.notna(p)],key=lambda x:-x[1])
    forecast='、'.join(f'{SPEC["weather_labels"][c]} {p:.0%}' for c,p in valid[:2]) if valid else '尚无同类转移样本'
    target=row.get('forecast_target_date','')
    target_text=target if isinstance(target,str) and target else '待日历补齐'
    labels=[str(row.get('mkt_quality_status',''))]
    if row.get('mkt_extreme',False):labels.append('极端')
    if row.get('mkt_forecast_n',0)<10:labels.append('样本不足' if row.get('mkt_forecast_n',0)>0 else '尚无同类转移样本')
    hint=row.get('mkt_borderline_hint','')
    boundary=f'{hint}｜' if isinstance(hint,str) and hint else ''
    receipt='｜未附凭证' if row.get('mkt_upstream_receipt_status')=='not_attached' else ''
    return (f'{row.trade_date} 大盘天气：{row.get("mkt_weather_label","") or "待输入"}｜挨打 {num("mkt_hit")} 延续 {num("mkt_cont")} 活跃 {num("mkt_act")}｜'
            +boundary+
            f'昨日涨停 {num("mkt_all_original_n",0)} 只，可观测 {num("mkt_all_observed_n",0)} 只，今日大跌 {num("mkt_all_drop_k",0)} 只｜'
            f'最高 {num("mkt_max_height",0)} 板｜成交 {num("mkt_turnover_cny",2,1e12)} 万亿（20日均的 {num("mkt_ratio20",2)} 倍）｜'
            f'下一交易日 {target_text}：{forecast}（过去 {num("mkt_forecast_n",0)} 次已完成同类转移）｜'+ '；'.join(labels)+receipt)

def history(args):
    from .adapter import build_raw,load_formal
    from .forecast import add_forecasts,evaluate_forecasts,transition_table
    from .borderline import add_borderline
    output=Path(args.output);output.mkdir(parents=True,exist_ok=True)
    metadata={};calendar=None
    if args.raw_daily:
        raw=read_csv(args.raw_daily);identity=sha256(args.raw_daily)
        metadata={'route':'published_derived_table','raw_daily_sha256':identity}
    elif args.formal_dir:
        events,cohorts,formal=load_formal(Path(args.formal_dir))
        raw=build_raw(events,cohorts,formal)
        identities={n:sha256(Path(args.formal_dir)/n) for n in ('events.parquet','cohorts.parquet','sentiment_daily.csv')}
        identity=fingerprint(identities);metadata={'route':'verified_f1_outputs','files':identities}
    elif args.data_root:
        raw,calendar,metadata=build_from_database(Path(args.data_root),args.end,output,args.f1_input_manifest,args.f1_cache_dir)
        identity=metadata['identity']
    else:raise ValueError('Supply --raw-daily, --formal-dir or --data-root')
    if args.calendar:
        calendar=read_csv(args.calendar).trade_date.tolist()
    if args.index_daily:
        ix=read_csv(args.index_daily)
        raw=raw.drop(columns=[c for c in ix if c!='trade_date' and c in raw]).merge(ix,on='trade_date',how='left',validate='one_to_one')
    raw=raw[raw.trade_date.le(iso(args.end))].copy()
    raw.attrs['input_snapshot_id']=identity
    cal=json.loads(Path(args.calibration).read_text(encoding='utf-8')) if args.calibration else calibrate(raw,identity)
    daily=compute_readings(raw,cal,generated_at=args.generated_at)
    daily=add_forecasts(daily,calendar)
    daily=add_borderline(daily)
    if getattr(args,'receipt_status',None):
        daily['mkt_upstream_receipt_status']=''
        daily.loc[daily.trade_date.eq(daily.trade_date.max()),'mkt_upstream_receipt_status']=args.receipt_status
    daily['mkt_summary']=daily.apply(summary_line,axis=1)
    evaluation,detail=evaluate_forecasts(daily)
    daily=daily[daily.trade_date.ge(iso(args.start))]
    if getattr(args,'expected_source_state',None) is not None and args.expected_source_state!=source_state(args.data_root):
        raise RuntimeError('upstream_changed_during_run: no derived outputs written')
    write_csv(output/'mkt_raw_daily.csv',raw);write_csv(output/'mkt_daily.csv',daily)
    write_json(output/'mkt_calibration_v02.json',cal)
    write_csv(output/'clipping_statistics.csv',clipping_statistics(daily,cal))
    write_json(output/'forecast_evaluation.json',evaluation);write_csv(output/'forecast_evaluation_daily.csv',detail)
    write_csv(output/'transition_table.csv',transition_table(daily))
    if calendar:write_csv(output/'market_calendar.csv',pd.DataFrame({'trade_date':calendar}))
    write_json(output/'input_manifest.json',metadata)
    from .review import explain_daily
    write_csv(output/'daily_explanations.csv',explain_daily(daily))
    print(json.dumps({'days':len(daily),'cutoff':daily.trade_date.max(),'output':str(output)},ensure_ascii=False))
    return daily

@contextmanager
def exclusive_output(output):
    output.mkdir(parents=True,exist_ok=True);lock=output/'.mkt.lock'
    try:fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
    except FileExistsError:raise RuntimeError('Weather output busy; inspect the existing process before removing lock')
    try:
        os.write(fd,str(os.getpid()).encode());os.close(fd);yield
    finally:lock.unlink(missing_ok=True)

def historical_changes(old,daily):
    if old is None:return []
    a=old.set_index('trade_date');b=daily.set_index('trade_date')
    cols=[c for c in b if c in a and c not in ('generated_at','data_cutoff','input_snapshot_id','mkt_summary','mkt_upstream_receipt_status')]
    def missing(v):return pd.isna(v) or (isinstance(v,str) and v=='')
    def equal(x,y):return (missing(x) and missing(y)) or (not missing(x) and not missing(y) and x==y)
    return [day for day in a.index.intersection(b.index) if any(not equal(a.at[day,c],b.at[day,c]) for c in cols)]

def today(args):
    from .upstream import check_without_receipt
    started=time.perf_counter();out=Path(args.output)
    if not args.calibration:raise ValueError('today requires fixed --calibration; never refits')
    with exclusive_output(out):
        args.start='20250102'
        current=datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        preflight=None
        args.receipt_status='not_attached'
        if args.data_root:
            m=engine();before=source_state(args.data_root)
            with m.ro(Path(args.data_root)/'db_cn_basic.db') as c:
                last=c.execute('select max(trade_date) from tbl_cn_day').fetchone()[0]
                limit=c.execute('select max(trade_date) from tbl_cn_stk_limit').fetchone()[0]
            if not last or last!=limit:raise RuntimeError('upstream_incomplete: daily/limit cutoff mismatch')
            if iso(last)>current:raise RuntimeError('future_input_date')
            if iso(last)==current and datetime.now(ZoneInfo('Asia/Shanghai')).hour<15:raise RuntimeError('upstream_incomplete: market has not closed')
            receipt_file=Path(args.upstream_receipt) if args.upstream_receipt else None
            if receipt_file is not None and receipt_file.exists():
                u=json.loads(receipt_file.read_text(encoding='utf-8'))
                if u.get('status')!='success' or iso(u.get('trade_date',''))!=iso(last) or not u.get('completed_at') or u.get('source_state')!=before:
                    raise RuntimeError('upstream_failed_or_stale_receipt: completion and exact source snapshot required')
                args.receipt_status='attached'
                preflight={'status':'verified_receipt','receipt_attached':True,'receipt_sha256':sha256(receipt_file)}
            else:
                try:preflight=check_without_receipt(args.data_root,source_state)
                except (RuntimeError,sqlite3.Error) as exc:
                    write_json(out/'today_preflight.json',{'status':'blocked','reason':str(exc),'receipt_attached':False})
                    raise
                before=preflight['source_state'];last=preflight['expected_trade_date']
            args.expected_source_state=before
            args.end=last
            receipt_path=out/'today_receipt.json'
            prev=json.loads(receipt_path.read_text(encoding='utf-8')) if receipt_path.exists() else {}
            if prev.get('source_state')==before and (out/'mkt_raw_daily.csv').exists():
                args.raw_daily=str(out/'mkt_raw_daily.csv')
                args.calendar=str(out/'market_calendar.csv') if (out/'market_calendar.csv').exists() else None
        else:
            before=None
            if not args.raw_daily:raise ValueError('today requires --data-root or explicit --raw-daily snapshot')
            args.end=read_csv(args.raw_daily).trade_date.max()
        old=read_csv(out/'mkt_daily.csv') if (out/'mkt_daily.csv').exists() else None
        cal_before=sha256(args.calibration)
        daily=history(args)
        if before is not None and before!=source_state(args.data_root):raise RuntimeError('upstream_changed_during_run')
        if cal_before!=sha256(args.calibration):raise RuntimeError('fixed_calibration_changed')
        changed=historical_changes(old,daily)
        latest=daily.iloc[-1];state='latest_completed_snapshot' if latest.trade_date!=current else 'latest_available_unverified_completion'
        if before is not None:
            state='completed_today' if latest.trade_date==current else 'market_closed_latest_completed'
            write_json(out/'today_preflight.json',preflight)
        print(f'[{state}] {latest.mkt_summary}')
        write_json(out/'today_receipt.json',{'status':state,'latest_completed_date':latest.trade_date,'source_state':before,
                   'calibration_sha256':cal_before,'elapsed_seconds':time.perf_counter()-started,
                   'upstream_completion':preflight,'receipt_attached':args.receipt_status=='attached',
                   'changed_historical_dates':changed,'row_count':len(daily),'duplicates':int(daily.trade_date.duplicated().sum()),
                   'recompute_policy':'on upstream snapshot change recompute affected history and successors conservatively as full prefix; frozen calibration'})
        if state=='upstream_stale':return 2
    return 0

def parser():
    p=argparse.ArgumentParser(description=__doc__);sp=p.add_subparsers(dest='command',required=True)
    for name in ('history','today'):
        q=sp.add_parser(name);q.add_argument('--data-root');q.add_argument('--formal-dir');q.add_argument('--raw-daily')
        q.add_argument('--output',required=True);q.add_argument('--calibration');q.add_argument('--calendar');q.add_argument('--index-daily')
        q.add_argument('--start',default='20250102');q.add_argument('--end',default='20260930');q.add_argument('--generated-at');q.add_argument('--upstream-receipt')
        q.add_argument('--f1-input-manifest');q.add_argument('--f1-cache-dir')
    q=sp.add_parser('human-pack');q.add_argument('--daily',required=True);q.add_argument('--output',required=True);q.add_argument('--nominations')
    q=sp.add_parser('li-pack');q.add_argument('--daily',required=True);q.add_argument('--output',required=True)
    q=sp.add_parser('evaluate-human');q.add_argument('--daily',required=True);q.add_argument('--li',required=True);q.add_argument('--output',required=True);q.add_argument('--revision-ledger')
    q=sp.add_parser('review');q.add_argument('--daily',required=True);q.add_argument('--output',required=True)
    return p

def main():
    a=parser().parse_args()
    if a.command=='history':history(a);return 0
    if a.command=='today':return today(a)
    if a.command=='review':
        from .review import run_review
        print(json.dumps(run_review(read_csv(a.daily),a.output),ensure_ascii=False));return 0
    from .human import create_packets
    if a.command=='human-pack':print(create_packets(read_csv(a.daily),a.output,a.nominations))
    elif a.command=='li-pack':
        from .li_packet import create_li_packet
        print(create_li_packet(read_csv(a.daily),a.output))
    else:
        from .li_review import evaluate_li_labels
        ledger=json.loads(Path(a.revision_ledger).read_text(encoding='utf-8')) if a.revision_ledger else None
        print(evaluate_li_labels(read_csv(a.daily),a.li,a.output,ledger))
    return 0

if __name__=='__main__':raise SystemExit(main())
