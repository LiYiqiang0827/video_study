"""F1 production from quiescent SQLite: historical identities and market cohorts.

Only raw input frames may be cached. A cached frame is physically filtered to
the requested cutoff before any event, cohort, rolling window, or score exists.
"""
from __future__ import annotations
import argparse, bisect, hashlib, json, sqlite3, sys, time
from contextlib import contextmanager
from pathlib import Path
import numpy as np
import pandas as pd
from score import fit_calibration, score_raw, clipping_table

METHOD='F1-formal-live-v1'
CODE=Path(__file__).resolve().parent
PREFIXES=('60','00','30','68')
PRICE_COLS=['open','high','low','close','pre_close']

def compact(s):return str(s).replace('-','')[:8]
def iso(s):return f'{s[:4]}-{s[4:6]}-{s[6:8]}'
def json_write(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=True),encoding='utf-8')
def csv_write(path,frame):frame.to_csv(path,index=False,encoding='utf-8')
def finite_mean(values):
    a=np.asarray(values,dtype=float);a=a[np.isfinite(a)]
    return float(a.mean()) if len(a) else np.nan
def file_sha(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        while block:=f.read(8*1024*1024):h.update(block)
    return h.hexdigest()
def pennies(values):
    a=np.asarray(values,dtype=float)
    return np.where(np.isfinite(a),np.floor(np.nan_to_num(a)*100+.5+1e-9),-1).astype(np.int64)

@contextmanager
def ro(path):
    path=Path(path).resolve()
    if not path.is_file():raise FileNotFoundError(path)
    for suffix in ['-wal','-journal']:
        side=Path(str(path)+suffix)
        if side.exists() and side.stat().st_size:raise RuntimeError(f'Input has active SQLite sidecar: {side}')
    c=sqlite3.connect(path.as_uri()+'?mode=ro&immutable=1',uri=True)
    c.execute('pragma query_only=on');c.execute('pragma cache_size=-65536')
    try:yield c
    finally:c.close()

def load_inputs(root,start,end,manifest_path=None,cache_dir=None):
    root=Path(root).resolve();manifest={}
    if manifest_path and Path(manifest_path).is_file():
        manifest=json.loads(Path(manifest_path).read_text(encoding='utf-8'))
        fingerprint=hashlib.sha256(Path(manifest_path).read_bytes()).hexdigest()
        for db,info in manifest.get('databases',{}).items():
            p=root/f'db_cn_{db}.db';st=p.stat()
            if st.st_size!=info['size'] or st.st_mtime_ns!=info['mtime_ns']:
                raise RuntimeError(f'Input modified after S01 manifest: {p}')
    else:
        files=[root/'db_cn_basic.db',root/'db_cn_kpl.db']
        states={p.name:{'path':str(p),'size':p.stat().st_size,'mtime_ns':p.stat().st_mtime_ns} for p in files}
        catalog=Path(cache_dir)/'source_content_hashes.json' if cache_dir else None
        previous=json.loads(catalog.read_text(encoding='utf-8')) if catalog and catalog.is_file() else {}
        contents={}
        for path in files:
            state=states[path.name];old=previous.get('databases',{}).get(path.name,{})
            if all(old.get(k)==v for k,v in state.items()) and old.get('sha256'):
                digest=old['sha256'];basis='previous content SHA256 with unchanged path/size/mtime_ns'
            else:
                digest=file_sha(path);after=path.stat()
                if after.st_size!=state['size'] or after.st_mtime_ns!=state['mtime_ns']:raise RuntimeError(f'Input changed during hashing: {path}')
                basis='direct full-file SHA256'
            contents[path.name]={**state,'sha256':digest,'hash_basis':basis}
            print(json.dumps({'content_hash':path.name,'basis':basis}),flush=True)
        manifest={'databases_by_filename':contents,'source':'local content SHA256; cached hashes validated against unchanged metadata'}
        if catalog:catalog.parent.mkdir(parents=True,exist_ok=True);json_write(catalog,{'databases':contents})
        fingerprint=hashlib.sha256(json.dumps({k:v['sha256'] for k,v in contents.items()},sort_keys=True).encode()).hexdigest()
    key=hashlib.sha256((fingerprint+'|'+str(root)+'|'+start).encode()).hexdigest()[:20]
    cache=Path(cache_dir)/key if cache_dir else None
    cache_meta=cache/'metadata.json' if cache else None
    tables={}
    if cache_meta and cache_meta.is_file():
        meta=json.loads(cache_meta.read_text(encoding='utf-8'))
        if meta['input_fingerprint']==fingerprint and meta['read_end']>=end:
            tables={name:pd.read_parquet(cache/f'{name}.parquet') for name in meta['tables']}
            print(json.dumps({'raw_cache_hit':str(cache),'read_end':meta['read_end'],'requested_end':end}),flush=True)
    if not tables:
        prefix="substr(ts_code,1,2) in ('60','00','30','68') and substr(ts_code,-2) in ('SH','SZ')"
        def wide(c,table,columns):
            query=f'select {columns} from {table} where trade_date between ? and ? and {prefix}'
            plan=[r[3] for r in c.execute('explain query plan '+query,[start,end])]
            random_lookup=any('USING INDEX' in detail and 'COVERING' not in detail for detail in plan)
            # Two-year production extraction on USB HDD benefits from sequential
            # table pages instead of a date-index lookup for each qualifying row.
            if random_lookup:query=query.replace(f'from {table} where',f'from {table} NOT INDEXED where')
            actual=[r[3] for r in c.execute('explain query plan '+query,[start,end])]
            print(json.dumps({'extract_table':table,'default_query_plan':plan,'executed_query_plan':actual,'where_unchanged':True}),flush=True)
            return pd.read_sql_query(query,c,params=[start,end])
        with ro(root/'db_cn_basic.db') as c:
            tables['daily']=wide(c,'tbl_cn_day','ts_code,trade_date,open,high,low,close,pre_close,vol,amount')
            print(json.dumps({'daily_loaded':len(tables['daily'])}),flush=True)
            tables['limits']=wide(c,'tbl_cn_stk_limit','ts_code,trade_date,up_limit,down_limit')
            tables['basic']=pd.read_sql_query(f'select ts_code,name,list_date,delist_date from tbl_cn_basic where {prefix}',c)
            tables['calendar']=pd.read_sql_query("select cal_date from tbl_cn_tradecal where exchange='SSE' and is_open=1 and cal_date<=? order by cal_date",c,params=[end])
            tables['names']=pd.read_sql_query(f'select ts_code,name,start_date,end_date,ann_date,raw_record_hash from tbl_cn_namechange where start_date<=? and {prefix}',c,params=[end])
            tables['suspends']=wide(c,'tbl_cn_suspend','ts_code,trade_date,suspend_type,suspend_timing')
        with ro(root/'db_cn_kpl.db') as c:
            tables['kpl']=wide(c,'tbl_cn_kpl_list','ts_code,trade_date,name')
        if cache:
            cache.mkdir(parents=True,exist_ok=True)
            for name,frame in tables.items():frame.to_parquet(cache/f'{name}.parquet',index=False)
            json_write(cache_meta,{'input_fingerprint':fingerprint,'read_start':start,'read_end':end,'tables':list(tables),'contents':'raw source frames only'})
    # Physical filtering happens before the event builder, including on cache hits.
    for name in ['daily','limits','kpl','suspends']:
        t=tables[name];tables[name]=t[t.trade_date.between(start,end)].copy()
    tables['calendar']=tables['calendar'][tables['calendar'].cal_date<=end].copy()
    tables['names']=tables['names'][tables['names'].start_date<=end].copy()
    return tables,fingerprint,manifest

def resolve_history(events,names,kpl):
    n=len(events);dates=events.trade_date.astype(int).to_numpy()
    status=np.full(n,-1,dtype=np.int8);source=np.full(n,'unknown',dtype=object)
    chosen=np.full(n,'',dtype=object);candidate_n=np.zeros(n,dtype=np.int16)
    st_mask=np.zeros(n,dtype=np.int8);latest_mask=np.zeros(n,dtype=np.int8)
    latest_start=np.zeros(n,dtype=np.int32)
    groups={str(code):idx for code,idx in events.groupby('ts_code',observed=True).indices.items()}
    names=names.fillna('').sort_values(['ts_code','start_date','ann_date','raw_record_hash'])
    for code,g in names.groupby('ts_code',sort=False):
        idx=groups.get(str(code))
        if idx is None:continue
        dd=dates[idx]
        for row in g.itertuples(index=False):
            if not str(row.start_date).isdigit():continue
            s=int(row.start_date);e=int(row.end_date) if str(row.end_date).isdigit() else 99991231
            if e<s:continue
            use=idx[(dd>=s)&(dd<=e)]
            if not len(use):continue
            st=int('ST' in str(row.name).upper());bit=2 if st else 1
            candidate_n[use]+=1;st_mask[use]|=bit
            higher=use[latest_start[use]<s];same=use[latest_start[use]==s]
            latest_mask[higher]=bit;latest_mask[same]|=bit
            latest_start[use]=s;status[use]=st;chosen[use]=str(row.name);source[use]='namechange'
    conflicted=latest_mask==3
    status[conflicted]=-1;source[conflicted]='unknown';chosen[conflicted]=''
    kp=kpl.dropna(subset=['name']).copy()
    kp['kst']=kp.name.astype(str).str.upper().str.contains('ST',regex=False)
    if len(kp):
        ag=kp.groupby(['ts_code','trade_date']).agg(kst_min=('kst','min'),kst_max=('kst','max'),kname=('name','first')).reset_index()
        ag=ag[ag.kst_min==ag.kst_max]
        lookup=ag.set_index(['ts_code','trade_date'])[['kst_min','kname']]
        for i in np.flatnonzero(status<0):
            key=(str(events.ts_code.iloc[i]),str(events.trade_date.iloc[i]))
            if key in lookup.index:
                row=lookup.loc[key];status[i]=int(row.kst_min);chosen[i]=str(row.kname);source[i]='same_day_kpl'
    pre_main=(events.board.to_numpy()=='main')&(events.trade_date.to_numpy()<'20260706')
    def regime_fallback(regime,ref_ok):
        resolved=status.copy();resolved_source=source.copy();missing=resolved<0
        isst=missing&(regime.astype(str).to_numpy()=='st5')&ref_ok.to_numpy()
        isnormal=missing&pre_main&(regime.astype(str).to_numpy()=='n10')&ref_ok.to_numpy()
        resolved[isst]=1;resolved[isnormal]=0;resolved_source[isst|isnormal]='identifiable_limit_regime'
        return resolved,resolved_source
    # Keep the pre-repair identity fallback for the history/own-row attribution
    # stage; the formal stage alone receives the exact nominal classification.
    history_own,history_source=regime_fallback(events.get('legacy_regime',events.regime),events.get('legacy_ref_ok',events.ref_ok))
    status,source=regime_fallback(events.regime,events.ref_ok)
    events['historical_st_history_own']=history_own;events['name_source_history_own']=pd.Categorical(history_source)
    events['historical_st']=status;events['name_source']=pd.Categorical(source)
    events['effective_name']=pd.Categorical(chosen);events['name_candidate_n']=candidate_n
    events['name_overlap']=candidate_n>1;events['name_st_conflict']=st_mask==3
    events['latest_name_st_conflict']=conflicted
    return events

def board_heights(codes,ups,market_index,market_adjacent):
    h=np.zeros(len(ups),dtype=np.int16);run=0;prior_code=None;prior_day=-100
    for i,(code,u,day) in enumerate(zip(codes,ups,market_index)):
        if code!=prior_code or (market_adjacent and day!=prior_day+1):run=0
        run=run+1 if u else 0;h[i]=run;prior_code=code;prior_day=day
    return h

def build_events(tables):
    daily=tables['daily'];limits=tables['limits'];basic=tables['basic']
    if daily.duplicated(['ts_code','trade_date']).any() or limits.duplicated(['ts_code','trade_date']).any():raise ValueError('Duplicate daily/limit key')
    events=daily.merge(limits,on=['ts_code','trade_date'],how='left',validate='one_to_one')
    events=events.sort_values(['ts_code','trade_date']).reset_index(drop=True)
    events['board']=np.where(events.ts_code.str[:2].isin(['30','68']),'20pct','main')
    for col in PRICE_COLS+['up_limit','down_limit','amount','vol']:events[col]=pd.to_numeric(events[col],errors='coerce')
    for col,label in [('close','cC'),('open','cO'),('high','cH'),('low','cL'),('pre_close','cP'),('up_limit','cU'),('down_limit','cD')]:events[label]=pennies(events[col])
    events['ohlc_ok']=(events.cH>=events[['cO','cC','cL']].max(axis=1))&(events.cL<=events[['cO','cC','cH']].min(axis=1))
    events['price_ok']=np.isfinite(events[PRICE_COLS]).all(axis=1)&(events[PRICE_COLS]>0).all(axis=1)&events.ohlc_ok
    events['limit_ok']=np.isfinite(events[['up_limit','down_limit']]).all(axis=1)&(events[['up_limit','down_limit']]>0).all(axis=1)
    r=events.up_limit/events.pre_close-1
    events['legacy_regime']=np.select([(events.board=='main')&r.between(.04,.065),(events.board=='main')&r.between(.085,.125),(events.board=='20pct')&r.between(.18,.22)],['st5','n10','n20'],'other')
    nominal=events.legacy_regime.map({'st5':.05,'n10':.1,'n20':.2});nn=nominal.fillna(0)
    events['legacy_ref_ok']=nominal.notna()&events.price_ok&events.limit_ok&(pennies(events.pre_close*(1+nn))==events.cU)&(pennies(events.pre_close*(1-nn))==events.cD)
    reference_valid=np.isfinite(events.pre_close)&events.pre_close.gt(0)&events.limit_ok
    def nominal_match(rate):
        return reference_valid&(pennies(events.pre_close*(1+rate))==events.cU)&(pennies(events.pre_close*(1-rate))==events.cD)
    main=events.board.eq('main');five=main&nominal_match(.05);ten=main&nominal_match(.1)
    twenty=events.board.eq('20pct')&nominal_match(.2)
    events['nominal_regime_ambiguous']=five&ten
    events['regime']=np.select([five&~ten,ten&~five,twenty],['st5','n10','n20'],'other')
    events['ref_ok']=events.regime.ne('other')&events.price_ok&events.limit_ok
    event_ok=events.price_ok&events.limit_ok
    events['U']=event_ok&(events.cC==events.cU);events['Touch']=event_ok&(events.cH==events.cU)
    events['Z']=events.Touch&~events.U;events['Dn']=event_ok&(events.cC==events.cD);events['H2L']=events.Touch&events.Dn
    events['legacy_U']=events.cC==events.cU;events['legacy_Touch']=events.cH==events.cU
    events['legacy_Z']=events.legacy_Touch&~events.legacy_U;events['legacy_Dn']=events.cC==events.cD
    events['legacy_H2L']=events.legacy_Touch&events.legacy_Dn
    events['ret']=(events.close/events.pre_close-1).where((events.close>0)&(events.pre_close>0))
    events['oret']=(events.open/events.pre_close-1).where((events.open>0)&(events.pre_close>0))
    cal=sorted(set(tables['calendar'].cal_date.astype(str)))
    market_map={d:i for i,d in enumerate(cal)}
    unknown=sorted(set(events.trade_date)-set(cal))
    if unknown:raise ValueError(f'Price dates absent from official SSE calendar: {unknown}')
    events['market_index']=events.trade_date.map(market_map).astype(np.int32)
    listing=basic.drop_duplicates('ts_code').set_index('ts_code').list_date.fillna('').to_dict()
    starts={code:bisect.bisect_left(cal,date) for code,date in listing.items() if date and date.isdigit()}
    listed=events.ts_code.map(listing).fillna('')
    events['list_date']=listed
    events['age']=events.market_index-events.ts_code.map(starts)+1
    events['age_lower_bound']=listed.ne('')&(listed<cal[0])
    events['legacy_age']=np.where(listed.eq('')|(listed<cal[0]),10**6,events.age)
    events=resolve_history(events,tables['names'],tables['kpl'])
    current_st=set(basic[basic.name.fillna('').str.upper().str.contains('ST',regex=False)].ts_code)
    kp=tables['kpl'].dropna(subset=['name']).drop_duplicates(['ts_code','trade_date']).copy()
    kp['kst']=kp.name.astype(str).str.contains('ST',regex=False)
    kmap=kp.set_index(['ts_code','trade_date']).kst.to_dict()
    post=events.trade_date>='20260706'
    old_st=np.array([kmap.get((code,date),code in current_st) if late else regime=='st5' for code,date,late,regime in zip(events.ts_code,events.trade_date,post,events.legacy_regime)])
    events['legacy_st']=old_st|events.legacy_regime.eq('st5').to_numpy()
    legacy_standard=events.legacy_regime.isin(['n10','n20'])
    standard=events.regime.isin(['n10','n20'])
    events['eligible_legacy']=legacy_standard&~events.legacy_st&(events.legacy_age>20)
    events['eligible_history_own']=legacy_standard&events.historical_st_history_own.eq(0)&(events.legacy_age>20)
    events['eligible']=standard&events.historical_st.eq(0)&(events.age>20)&events.price_ok&events.limit_ok&events.ref_ok
    events['historical_delisting_mark']=events.effective_name.astype(str).str.contains('退',regex=False)
    broad_base=standard&events.historical_st.eq(0)&events.price_ok&events.limit_ok&events.ref_ok
    events['comparison_delisting_excluded']=broad_base&events.historical_delisting_mark
    events['broad_eligible']=broad_base&~events.historical_delisting_mark
    legacy_broad=legacy_standard&events.historical_st_history_own.eq(0)&events.price_ok&events.limit_ok&events.legacy_ref_ok
    events['legacy_comparison_delisting_excluded']=legacy_broad&events.historical_delisting_mark
    events['legacy_broad_eligible']=legacy_broad&~events.historical_delisting_mark
    reason=np.select([listed.eq(''),events.age<=20,~events.price_ok,~events.limit_ok,events.nominal_regime_ambiguous,events.historical_st.lt(0),events.historical_st.eq(1),~standard,~events.ref_ok],
                     ['missing_listing_date','new_listing_age_le_20','invalid_price','missing_or_invalid_limit','ambiguous_nominal_limit_regime','historical_name_unavailable','historical_ST','unmatched_or_special_limit_regime','reference_price_mismatch'],default='eligible')
    events['eligibility_reason']=pd.Categorical(reason)
    legacy_reason=np.select([listed.eq(''),events.age<=20,~events.price_ok,~events.limit_ok,events.historical_st_history_own.lt(0),events.historical_st_history_own.eq(1),~legacy_standard,~events.legacy_ref_ok],
                            ['missing_listing_date','new_listing_age_le_20','invalid_price','missing_or_invalid_limit','historical_name_unavailable','historical_ST','special_limit_regime','reference_price_mismatch'],default='eligible')
    events['legacy_eligibility_reason']=pd.Categorical(legacy_reason)
    events['height_own']=board_heights(events.ts_code.to_numpy(),events.legacy_U.to_numpy(),events.market_index.to_numpy(),False)
    events['height']=board_heights(events.ts_code.to_numpy(),events.U.to_numpy(),events.market_index.to_numpy(),True)
    grouped=events.groupby('ts_code',sort=False)
    events['prev_height_own']=grouped.height_own.shift(1).fillna(0).astype(np.int16)
    events['prev_eligible_legacy']=grouped.eligible_legacy.shift(1).eq(True)
    events['prev_eligible_history_own']=grouped.eligible_history_own.shift(1).eq(True)
    events['prev_broad_own']=grouped.legacy_broad_eligible.shift(1).eq(True)
    for col in ['ts_code','trade_date','board','regime','legacy_regime','list_date']:events[col]=events[col].astype('category')
    return events,cal

def high_outcomes(cohort,cent_boundaries=True):
    ret=cohort.ret.to_numpy(dtype=float)
    le5=(cohort.cC.to_numpy()*20<=cohort.cP.to_numpy()*19) if cent_boundaries else ret<=-.05
    zero=(cohort.cC.to_numpy()==cohort.cP.to_numpy()) if cent_boundaries else ret==0
    neg=(cohort.cC.to_numpy()<cohort.cP.to_numpy()) if cent_boundaries else ret<0
    return np.select([cohort.H2L,cohort.Dn,cohort.promoted,le5,neg,zero,ret>0],[0,0,100,15,35,50,75],default=np.nan)

def matched_cohort(yesterday,today,paused_codes,formal=True):
    cols=['ts_code','eligible','price_ok','limit_ok','ref_ok','height','U','Touch','Z','Dn','H2L','ret','oret','cC','cP']
    y=yesterday[['ts_code','height']].rename(columns={'height':'prev_height'})
    c=y.merge(today[cols],on='ts_code',how='left',indicator=True,validate='one_to_one')
    c['found']=c['_merge'].eq('both');c=c.drop(columns='_merge')
    c['paused']=~c.found&c.ts_code.astype(str).isin(paused_codes)
    c['missing']=~c.found&~c.paused
    for col in ['eligible','price_ok','limit_ok','ref_ok','U','Touch','Z','Dn','H2L']:c[col]=c[col].eq(True)
    c['return_obs']=c.found&c.price_ok&c.ref_ok&np.isfinite(c.ret)&np.isfinite(c.oret)
    c['event_obs']=c.found&c.price_ok&c.limit_ok&c.ref_ok
    c['promotion_obs']=c.event_obs;c['high_obs']=c.event_obs&c.return_obs;c['mid_obs']=c.event_obs&np.isfinite(c.ret)
    c['promoted']=c.promotion_obs&c.U&c.height.eq(c.prev_height+1)
    return c

def raw_statistics(x,c,eligible_col,height_col,cent_boundaries,formal):
    e=x[x[eligible_col]];u=e[e.U];touch=e[e.Touch];N=len(e)
    heights=u[height_col]
    r={'N':N,'U':len(u),'Z':int(e.Z.sum()),'D':int(e.Dn.sum()),'Touch':len(touch),'lianban':int((heights>=2).sum()),
       'max_h':int(heights.max()) if len(u) else 0,'ladder':len(set(np.minimum(heights,6)))/6}
    r['cohort_original_n']=len(c);r['cohort_suspended_n']=int(c.paused.sum());r['cohort_missing_n']=int(c.missing.sum())
    r['cohort_observed_n']=int(c.return_obs.sum());r['cohort_unobservable_n']=int((c.found&~c.return_obs).sum())
    for name,mask in [('h1',c.prev_height==1),('h2',c.prev_height==2),('h3',c.prev_height==3),('h4',c.prev_height>=4)]:
        full=c[mask];g=full[full.promotion_obs]
        r['k_'+name]=int(g.promoted.sum());r['n_'+name]=len(g);r['original_'+name]=len(full)
    p=c[c.return_obs];cc=c[(c.prev_height>=2)&c.return_obs]
    r.update(P_open=finite_mean(p.oret),P_close=finite_mean(p.ret),P_n=len(p),C_open=finite_mean(cc.oret),C_close=finite_mean(cc.ret),C_n=len(cc))
    high_levels=sorted(set(c.loc[c.prev_height>=2,'prev_height']),reverse=True)[:3]
    high_selected=c.prev_height.isin(high_levels);hc=c[high_selected&c.high_obs]
    r.update(H_score=finite_mean(high_outcomes(hc,cent_boundaries)),H_n=len(hc),H_original_n=int(high_selected.sum()),H_h2l=int(hc.H2L.sum()))
    att=np.where(touch['prev_eligibility']&touch['previous_height'].ge(1),touch['previous_height']+1,1)
    for name,mask in [('b1',att==1),('b2',att==2),('b3',att>=3)]:
        g=touch[mask];r['kz_'+name]=int(g.Z.sum());r['nz_'+name]=len(g)
    mid=c[c.prev_height.between(2,4)&c.mid_obs]
    failures=(mid.cC*20<=mid.cP*19) if cent_boundaries else mid.ret<=-.05
    r['k_mid']=int((failures|mid.Dn).sum());r['n_mid']=len(mid);r['MID_original_n']=int(c.prev_height.between(2,4).sum())
    r['median_ret']=finite_mean([]) if not N else float(e.ret.median())
    amount_ok=np.isfinite(x.amount)&(x.amount>0)
    r['amount']=float(x.loc[amount_ok,'amount'].sum(min_count=1)*1000) if formal and len(x) else float(x.amount.sum()*1000) if len(x) else np.nan
    r['amount_observed_n']=int(amount_ok.sum());r['amount_original_n']=len(x)
    r['amount_complete']=bool(len(x) and amount_ok.all())
    r['Udens']=r['U']/N*1000 if N else np.nan;r['Ddens']=r['D']/N*1000 if N else np.nan;r['BL']=r['Udens']*r['ladder']
    c=c.copy();c['high_selected']=high_selected;c['high_outcome']=np.nan
    c.loc[high_selected&c.high_obs,'high_outcome']=high_outcomes(hc,cent_boundaries)
    return r,c

def compute_raw(events,calendar,suspends,mode='formal',keep_cohorts=False,start=None):
    formal=mode=='formal';hist=mode!='legacy';eligible_col='eligible' if formal else 'eligible_history_own' if hist else 'eligible_legacy'
    height_col='height' if formal else 'height_own'
    date_groups={str(d):idx for d,idx in events.groupby('trade_date',observed=True,sort=False).indices.items()}
    days=[d for d in calendar if d>=(start or str(events.trade_date.astype(str).min()))]
    susp={str(d):g for d,g in suspends.groupby('trade_date',sort=False)}
    active=set();prior=None;rows=[];quality=[];cohorts=[]
    for day in days:
        x=events.iloc[date_groups.get(day,np.array([],dtype=int))].copy()
        for row in susp.get(day,pd.DataFrame()).itertuples(index=False):
            if row.suspend_type=='R':active.discard(row.ts_code)
            elif row.suspend_type=='S' and (pd.isna(row.suspend_timing) or not str(row.suspend_timing).strip()):active.add(row.ts_code)
        if formal:
            if prior is None:y=x.iloc[:0]
            else:y=events.iloc[date_groups.get(prior,np.array([],dtype=int))]
            y=y[y[eligible_col]&y.U]
            c=matched_cohort(y,x,active)
            prevmap=y.set_index('ts_code').height.to_dict()
            x['previous_height']=x.ts_code.astype(str).map(prevmap).fillna(0).astype(int);x['prev_eligibility']=x.ts_code.isin(prevmap)
        else:
            for flag in ['U','Touch','Z','Dn','H2L']:x[flag]=x['legacy_'+flag]
            # These diagnostics and the wide comparison pool belonged to the
            # old nominal classifier too; do not leak the formal repair here.
            for target,source in [('regime','legacy_regime'),('ref_ok','legacy_ref_ok'),('historical_st','historical_st_history_own'),
                                  ('name_source','name_source_history_own'),('eligibility_reason','legacy_eligibility_reason'),
                                  ('broad_eligible','legacy_broad_eligible'),('comparison_delisting_excluded','legacy_comparison_delisting_excluded')]:x[target]=x[source]
            x['eligible']=x.regime.isin(['n10','n20'])&x.historical_st.eq(0)&x.age.gt(20)&x.price_ok&x.limit_ok&x.ref_ok
            pcol='prev_eligible_history_own' if hist else 'prev_eligible_legacy'
            y=x[x[pcol]&x.prev_height_own.ge(1)].copy()
            c=y[['ts_code','prev_height_own','eligible','price_ok','limit_ok','ref_ok','U','Touch','Z','Dn','H2L','ret','oret','cC','cP',height_col]].copy()
            c=c.rename(columns={'prev_height_own':'prev_height',height_col:'current_height'})
            c['height']=c.current_height;c['found']=True;c['paused']=False;c['missing']=False
            c['return_obs']=True
            c['promotion_obs']=True;c['high_obs']=True;c['mid_obs']=True
            c['promoted']=c.U&c.height.eq(c.prev_height+1)
            x['previous_height']=x.prev_height_own;x['prev_eligibility']=x[pcol]
        r,c=raw_statistics(x,c,eligible_col,height_col,formal,formal);r['date']=day
        # Wide comparison pool has the same historical identity policy, without age.
        broad=x[x.broad_eligible];r['cls_U']=int(broad.U.sum());r['cls_Z']=int(broad.Z.sum());r['cls_D']=int(broad.Dn.sum())
        r['comparison_delisting_excluded_n']=int(x.comparison_delisting_excluded.sum())
        r['cls_lianban']=int((broad.U&(broad[height_col]>=2)).sum())
        if formal and prior is not None:
            prev=events.iloc[date_groups.get(prior,np.array([],dtype=int))];prev=prev[prev.broad_eligible&prev.U]
            bc=matched_cohort(prev,x,active)
            b1=bc[(bc.prev_height==1)&bc.promotion_obs];b2=bc[(bc.prev_height>=2)&bc.promotion_obs]
        else:
            bc=x[x.broad_eligible&x.prev_height_own.ge(1)].copy();bc['promoted']=bc.U&bc[height_col].eq(bc.prev_height_own+1)
            b1=bc[bc.prev_height_own==1];b2=bc[bc.prev_height_own>=2]
        r['cls_1to2']=finite_mean(b1.promoted);r['cls_lb_promo']=finite_mean(b2.promoted)
        rows.append(r)
        reasons=x.eligibility_reason.astype(str).value_counts().to_dict()
        q={'date':day,'eligible_n':r['N'],'market_observed_n':len(x),'excluded_n':len(x)-r['N'],
           'excluded_pct':(len(x)-r['N'])/len(x) if len(x) else np.nan,
           'historical_name_n':int(x.name_source.eq('namechange').sum()),'st_fallback_n':int(x.name_source.isin(['same_day_kpl','identifiable_limit_regime']).sum()),
           'name_unknown_n':int(x.historical_st.lt(0).sum()),'name_overlap_n':int(x.name_overlap.sum()),'name_st_conflict_n':int(x.name_st_conflict.sum()),
           'latest_name_st_conflict_n':int(x.latest_name_st_conflict.sum()),'missing_listing_date_n':int(x.list_date.astype(str).eq('').sum()),
           'ref_validated_n':int(x.ref_ok.sum()),'ref_candidate_n':int(x.regime.ne('other').sum()),
           'ambiguous_nominal_regime_n':int(x.nominal_regime_ambiguous.sum()) if formal else 0,
           'nominal_regime_unmatched_n':int((x.regime.eq('other')&~x.nominal_regime_ambiguous).sum()) if formal else 0,
           'reference_price_mismatch_n':int((x.regime.ne('other')&~x.ref_ok).sum()),'invalid_price_n':int((~x.price_ok).sum()),
           'invalid_limit_n':int((~x.limit_ok).sum()),'amount_missing_n':int((~np.isfinite(x.amount)).sum()),
           'comparison_delisting_excluded_n':r['comparison_delisting_excluded_n'],
           'amount_nonpositive_n':int((np.isfinite(x.amount)&(x.amount<=0)).sum()),
           'amount_observed_n':r['amount_observed_n'],'amount_original_n':len(x),'amount_complete':r['amount_complete'],
           'amount_coverage':r['amount_observed_n']/len(x) if len(x) else np.nan,
           'prev_cohort_original_n':r['cohort_original_n'],'prev_cohort_observed_n':r['cohort_observed_n'],
           'prev_cohort_suspended_n':r['cohort_suspended_n'],'missing_prev_cohort_n':r['cohort_missing_n'],
           'prev_cohort_unobservable_n':r['cohort_unobservable_n'],'exclusion_reasons_json':json.dumps(reasons,sort_keys=True)}
        q['st_fallback_pct']=q['st_fallback_n']/len(x) if len(x) else np.nan
        q['namechange_coverage']=q['historical_name_n']/len(x) if len(x) else np.nan
        quality.append(q)
        if keep_cohorts and len(c):c['date']=day;cohorts.append(c)
        active.difference_update(x.loc[x.price_ok,'ts_code'].astype(str))
        prior=day
    raw=pd.DataFrame(rows)
    raw['amt_ma20']=raw.amount.shift(1).rolling(20,min_periods=20).mean()
    raw['ratio20']=raw.amount/raw.amt_ma20;raw['lr20']=np.log(raw.ratio20)
    return raw,pd.DataFrame(quality),pd.concat(cohorts,ignore_index=True) if cohorts else pd.DataFrame()

def score_versions(raw,quality,calibration,fingerprint,spec):
    scored=score_raw(raw,calibration,spec['pseudocount'])
    qcols=['date']+[c for c in quality if c!='date' and c not in scored]
    scored=scored.merge(quality[qcols],on='date',validate='one_to_one')
    aliases={'N':'eligible_n','U':'limit_up_n','Touch':'touched_n','Z':'broken_n','D':'limit_down_n',
             'lianban':'chain_n','max_h':'max_height','ladder':'ladder_coverage','P_open':'yesterday_limit_open_mean',
             'P_close':'yesterday_limit_close_mean','amount':'turnover_cny','ratio20':'turnover_ratio20'}
    for source,target in aliases.items():
        if source!=target:scored[target]=scored[source]
    scored['promo_1to2']=scored.k_h1/scored.n_h1.replace(0,np.nan)
    scored['promo_chain']=scored[['k_h2','k_h3','k_h4']].sum(axis=1)/scored[['n_h2','n_h3','n_h4']].sum(axis=1).replace(0,np.nan)
    scored['mid_failure_rate']=scored.k_mid/scored.n_mid.replace(0,np.nan)
    scored['trade_date']=scored.date.map(iso)
    scored['method_version']=METHOD;scored['calibration_version']=calibration['version']
    scored['input_snapshot_id']=fingerprint;scored['information_cutoff']=scored.trade_date+' market close'
    scored['available_at_basis']=spec['available_at_basis']
    reasons=[]
    for r in scored.itertuples(index=False):
        flags=[]
        if r.market_observed_n==0:flags.append('whole_market_price_missing')
        for col in ['name_unknown_n','latest_name_st_conflict_n','missing_listing_date_n','reference_price_mismatch_n','ambiguous_nominal_regime_n','invalid_price_n','invalid_limit_n','missing_prev_cohort_n','prev_cohort_unobservable_n']:
            if getattr(r,col)>0:flags.append(col)
        if not r.amount_complete:flags.append('partial_or_missing_turnover')
        if pd.isna(r.score5):flags.append('required_module_unavailable')
        reasons.append(';'.join(flags))
    scored['quality_reasons']=reasons
    scored['quality_status']=np.where(scored.score5.isna(),'UNKNOWN',np.where(scored.quality_reasons.ne(''),'PARTIAL','OK'))
    return scored

def differences(formal,legacy,history_own,baseline_dir,b0cal,formal_calibration):
    braw=pd.read_csv(baseline_dir/'daily_raw.csv',dtype={'date':str})
    bscore=score_raw(braw,b0cal,5)
    formal_old=score_raw(formal,b0cal,5)
    fscore=score_raw(formal,formal_calibration,5)
    stages=[('score_B0',bscore),('score_live_legacy',score_raw(legacy,b0cal,5)),
            ('score_live_history_own',score_raw(history_own,b0cal,5)),('score_formal_B0cal',formal_old),('score_formal_F1cal',fscore)]
    out=pd.DataFrame({'date':formal.date})
    for label,frame in stages:out=out.merge(frame[['date','score5']].rename(columns={'score5':label}),on='date',how='left',validate='one_to_one')
    out['common_date']=out.score_B0.notna();out['trade_date']=out.date.map(iso)
    out['delta_live_input']=out.score_live_legacy-out.score_B0
    out['delta_historical_ST']=out.score_live_history_own-out.score_live_legacy
    out['delta_calendar_cohort_quality']=out.score_formal_B0cal-out.score_live_history_own
    out['delta_calibration']=out.score_formal_F1cal-out.score_formal_B0cal
    out['delta_total']=out.score_formal_F1cal-out.score_B0
    out['delta_step_sum']=out[['delta_live_input','delta_historical_ST','delta_calendar_cohort_quality','delta_calibration']].sum(axis=1,min_count=4)
    out['attribution_residual']=out.delta_total-out.delta_step_sum
    out['attribution_basis']='sequential score deltas; calendar/cohort/price/listing/turnover quality combined in third step'
    return out,stages

def differences_without_baseline(raw,calibration):
    out=pd.DataFrame({'date':raw.date,'trade_date':raw.date.map(iso),'common_date':False})
    out['score_formal_F1cal']=score_raw(raw,calibration,5).score5
    for col in ['score_B0','score_live_legacy','score_live_history_own','score_formal_B0cal','delta_live_input','delta_historical_ST',
                'delta_calendar_cohort_quality','delta_calibration','delta_total','delta_step_sum','attribution_residual']:out[col]=np.nan
    out['attribution_basis']='not_run_no_B0_input'
    return out,[]

def external_table(raw,path):
    out=pd.read_csv(path,dtype={'trade_date':str});r=raw.copy();r['trade_date']=r.date.map(iso)
    r['production_promo_1to2']=r.k_h1/r.n_h1.replace(0,np.nan)
    r['production_promo_chain']=r[['k_h2','k_h3','k_h4']].sum(axis=1)/r[['n_h2','n_h3','n_h4']].sum(axis=1).replace(0,np.nan)
    mapping={'U':'production_up','Z':'production_broken','lianban':'production_chain','cls_U':'comparison_up','cls_Z':'comparison_broken',
             'cls_lianban':'comparison_chain','cls_lb_promo':'comparison_promo_chain','cls_1to2':'comparison_promo_1to2'}
    for old,new in mapping.items():r[new]=r[old]
    r['turnover_trillion']=r.amount/1e12
    cols=['trade_date']+list(mapping.values())+['production_promo_1to2','production_promo_chain','turnover_trillion','comparison_delisting_excluded_n']
    out=out.merge(r[cols],on='trade_date',how='left',validate='one_to_one')
    for metric in ['up','broken','chain','promo_chain','promo_1to2']:
        out['production_minus_external_'+metric]=out['production_'+metric]-out['external_'+metric]
        out['comparison_minus_external_'+metric]=out['comparison_'+metric]-out['external_'+metric]
    out['turnover_minus_external_trillion']=out.turnover_trillion-out.external_turnover_trillion
    out['scope_explanation']='Production age>20. Comparison non-ST any age, excluding effective historical name containing the Chinese delisting marker 退; exclusion count is explicit. Both SH/SZ 60/00/30/68 with validated standard prices and historical ST. External coverage/rounding may differ; original conflicting values retained in source_note.'
    return out

def validate_outputs(events,raw,scored,cohorts,calendar,end,diff):
    expected=[d for d in calendar if '20250101'<=d<=end]
    actual=scored.date.astype(str).tolist()
    checks={'dates_unique':not scored.date.duplicated().any(),'calendar_exact':actual==expected,
            'touch_identity':bool((raw.Touch==raw.U+raw.Z).all()),'eligible_price_consistent':bool(events.loc[events.eligible,'ref_ok'].all())}
    for group in ['h1','h2','h3','h4']:checks['promotion_k_le_n_'+group]=bool((raw['k_'+group]<=raw['n_'+group]).all())
    for group in ['b1','b2','b3']:checks['broken_k_le_n_'+group]=bool((raw['kz_'+group]<=raw['nz_'+group]).all())
    checks['mid_k_le_n']=bool((raw.k_mid<=raw.n_mid).all())
    for col in ['M1','M2','M3','M4','M5','score5','score_available']:checks['range_'+col]=bool(scored[col].dropna().between(-1e-10,100+1e-10).all())
    common=diff[diff.common_date&diff.attribution_residual.notna()]
    residual=float(common.attribution_residual.abs().max()) if len(common) else None
    attribution_status='checked' if diff.common_date.any() else 'not_run_no_B0_input'
    if attribution_status=='checked':checks['attribution_additive']=bool(residual is not None and residual<1e-10)
    checks['cohort_accounting']=bool((raw.cohort_original_n==raw.cohort_suspended_n+raw.cohort_missing_n+raw.cohort_observed_n+raw.cohort_unobservable_n).all())
    return {'status':'computed_for_parent_acceptance','checks':checks,'all_deterministic_checks_pass':all(checks.values()),
            'dates':len(scored),'expected_dates':len(expected),'year_counts':scored.date.str[:4].value_counts().sort_index().to_dict(),
            'events':len(events),'cohort_rows':len(cohorts),'score_missing_dates':scored.loc[scored.score5.isna(),'trade_date'].tolist(),
            'max_attribution_residual':residual,'attribution_status':attribution_status,'input_cutoff':iso(end),'raw_max_date':str(events.trade_date.astype(str).max()),
            'missing_market_dates':scored.loc[scored.market_observed_n.eq(0),'trade_date'].tolist(),
            'remaining_quality_totals':scored[['name_unknown_n','latest_name_st_conflict_n','missing_listing_date_n','reference_price_mismatch_n','ambiguous_nominal_regime_n','nominal_regime_unmatched_n','invalid_price_n','invalid_limit_n','missing_prev_cohort_n','prev_cohort_unobservable_n','amount_missing_n','amount_nonpositive_n']].sum().to_dict()}

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data-root',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--baseline-dir',type=Path,help='Optional B0 input for sequential attribution');p.add_argument('--end',default='2026-09-30')
    p.add_argument('--start',default=None);p.add_argument('--fixed-calibration',type=Path)
    p.add_argument('--input-manifest',type=Path);p.add_argument('--cache-dir',type=Path)
    p.add_argument('--sample-dates',default='2026-09-09,2026-09-10,2026-09-11')
    p.add_argument('--task-result',type=Path)
    a=p.parse_args();spec=json.loads((CODE/'config/formal_spec.json').read_text(encoding='utf-8'))
    a.output=a.output.resolve();a.output.mkdir(parents=True,exist_ok=True);end=compact(a.end);start=compact(a.start or spec['warmup_start'])
    tables,fingerprint,source_manifest=load_inputs(a.data_root,start,end,a.input_manifest,a.cache_dir)
    events,calendar=build_events(tables);print(json.dumps({'events_built':len(events),'calendar_days':len(calendar)}),flush=True)
    raw,quality,cohorts=compute_raw(events,calendar,tables['suspends'],'formal',True,start=start)
    calibration=json.loads(a.fixed_calibration.read_text(encoding='utf-8')) if a.fixed_calibration else fit_calibration(raw)
    if not a.fixed_calibration and len(raw[raw.date.str.startswith('2025')])!=243:raise RuntimeError('Fitting requires complete 2025 calendar')
    json_write(a.output/'calibration.json',calibration)
    scored=score_versions(raw,quality,calibration,fingerprint,spec)
    if a.baseline_dir:
        legacy,_,_=compute_raw(events,calendar,tables['suspends'],'legacy',start=start);history_own,_,_=compute_raw(events,calendar,tables['suspends'],'history_own',start=start)
        b0cal=json.loads((a.baseline_dir/'calibration_reference.json').read_text(encoding='utf-8'))
        b0raw=pd.read_csv(a.baseline_dir/'daily_raw.csv',dtype={'date':str});b0cal['panic_ddens_p90']=float(b0raw.loc[b0raw.date.str.startswith('2025'),'Ddens'].quantile(.9))
        diff,stages=differences(raw,legacy,history_own,a.baseline_dir,b0cal,calibration)
    else:diff,stages=differences_without_baseline(raw,calibration)
    mask=scored.date.between('20250101',end);daily=scored.loc[mask].copy()
    raw_export=raw.merge(scored[['date']+[c for c in scored if c.startswith(('Q_','clip_','endpoint_','constant_'))]+['M1raw','SQ','MIDfail']],on='date',validate='one_to_one')
    raw_export['trade_date']=raw_export.date.map(iso)
    csv_write(a.output/'sentiment_daily.csv',daily);csv_write(a.output/'daily_raw.csv',raw_export)
    quality['trade_date']=quality.date.map(iso);csv_write(a.output/'quality_daily.csv',quality[quality.date.between('20250101',end)])
    csv_write(a.output/'clipping_statistics.csv',clipping_table(daily))
    events.to_parquet(a.output/'events.parquet',index=False);cohorts.to_parquet(a.output/'cohorts.parquet',index=False)
    st_changes=events[events.historical_st.ge(0)&events.legacy_st.ne(events.historical_st.astype(bool))].copy()
    csv_write(a.output/'st_changes.csv',st_changes[['ts_code','trade_date','effective_name','historical_st','historical_st_history_own','legacy_st','name_source','name_overlap','name_st_conflict','latest_name_st_conflict','regime','legacy_regime','nominal_regime_ambiguous','eligible','eligible_legacy']])
    csv_write(a.output/'reference_to_formal_diff.csv',diff[diff.date.between('20250101',end)])
    csv_write(a.output/'external_reconciliation.csv',external_table(raw,CODE/'config/external_reconciliation.csv'))
    for label,frame in stages:csv_write(a.output/(label+'.csv'),frame[frame.date.between('20250101',end)])
    samples=[compact(d) for d in a.sample_dates.split(',') if compact(d)<=end]
    sample=a.output/'sample_reconciliation';sample.mkdir(exist_ok=True)
    csv_write(sample/'daily.csv',daily[daily.date.isin(samples)])
    events[events.trade_date.astype(str).isin(samples)].to_parquet(sample/'events.parquet',index=False)
    if len(cohorts):cohorts[cohorts.date.isin(samples)].to_parquet(sample/'cohorts.parquet',index=False)
    validation=validate_outputs(events,raw,daily,cohorts,calendar,end,diff)
    json_write(a.output/'production_validation.json',validation)
    manifest={'method_version':METHOD,'calibration_version':calibration['version'],'input_fingerprint':fingerprint,
              'input_manifest_path':str(a.input_manifest) if a.input_manifest else None,'source_manifest':source_manifest,
              'data_root':str(a.data_root.resolve()),'read_start':iso(start),'information_cutoff':iso(end),
              'available_at_basis':spec['available_at_basis'],'raw_only_cache':str(a.cache_dir) if a.cache_dir else None,
              'fixed_calibration_path':str(a.fixed_calibration) if a.fixed_calibration else None,
              'code_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'score_sha256':hashlib.sha256((CODE/'score.py').read_bytes()).hexdigest(),
              'spec_sha256':hashlib.sha256((CODE/'config/formal_spec.json').read_bytes()).hexdigest(),
              'denominator_policy':'Yesterday eligible U frozen on previous market day; current missing/suspended/unusable separated; returns require positive consistent pre-close and ref_ok, including newly ST members',
              'turnover_policy':'Positive finite raw amount*1000; partial sums flagged with actual observed/original counts; all missing NaN; prior 20 market-day rolling mean without interpolation',
              'name_interval_policy':'Inclusive start/end; latest start preferred and overlap marked; same-latest-start conflicting ST uses marked fallback or unknown'}
    manifest['comparison_pool_policy']='No age restriction; non-ST, validated standard prices; exclude effective historical name containing 退 and record comparison_delisting_excluded_n. Formal pool remains unchanged.'
    json_write(a.output/'input_manifest.json',manifest)
    if a.task_result:
        result={'task_id':'S04','status':'computed_for_parent_acceptance' if validation['all_deterministic_checks_pass'] else 'partial',
                'model_configuration':{'model':'gpt-6.1-sol','reasoning_effort':'xhigh','basis':'parent supplied native worker configuration; runtime introspection unavailable'},
                'files':[str(f) for f in a.output.glob('*') if f.is_file()],'counts':validation,'input_fingerprint':fingerprint,
                'command':sys.argv,'remaining':'Parent independent prefix/three-day/actual event review and acceptance'}
        json_write(a.task_result,result)
    print(json.dumps(validation,ensure_ascii=False),flush=True)
    if not validation['all_deterministic_checks_pass']:raise SystemExit(2)

if __name__=='__main__':main()
