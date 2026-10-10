"""F1 fixed 2025 priors and anchors. No access to strategy outcomes."""
import hashlib, json
import numpy as np
import pandas as pd

W1={'h1':.25,'h2':.30,'h3':.25,'h4':.20}
WB={'b1':.2,'b2':.3,'b3':.5}
ANCHORS=['M1raw','P_open','P_close','C_open','C_close','BL','SQ','Ddens','MIDfail','lr20']

def combine(parts):
    vals = np.vstack([np.asarray(p,dtype=float) for p,w in parts]); ws=np.array([w for p,w in parts])[:,None]
    den=np.where(np.isfinite(vals),ws,0).sum(axis=0)
    return np.divide(np.nansum(vals*ws,axis=0),den,out=np.full(vals.shape[1],np.nan),where=den>0)

def smoothed(raw, priors, a):
    f=raw.copy()
    def rate(g,k,n):
        return ((f[k]+a*priors[g])/(f[n]+a)).where(f[n]>0)
    f['M1raw']=combine([(rate(g,'k_'+g,'n_'+g),w) for g,w in W1.items()])
    f['SQ']=1-combine([(rate(g,'kz_'+g,'nz_'+g),w) for g,w in WB.items()])
    f['MIDfail']=rate('mid','k_mid','n_mid')
    return f

def fit_calibration(raw):
    cal=raw[raw.date.astype(str).str[:4]=='2025']
    pri={}
    for g in W1:
        pri[g]=float(cal['k_'+g].sum()/cal['n_'+g].sum())
    for g in WB:
        pri[g]=float(cal['kz_'+g].sum()/cal['nz_'+g].sum())
    pri['mid']=float(cal.k_mid.sum()/cal.n_mid.sum())
    cal=smoothed(cal,pri,5)
    anchors={c:[float(cal[c].quantile(.1)),float(cal[c].quantile(.9))] for c in ANCHORS}
    result={'version':'F1-2025-a5-v1','calibration_year':2025,'calibration_days':len(cal),'pseudocount':5,'priors':pri,'anchors':anchors,'quantile_method':'linear','constant_anchor_value':50,'panic_ddens_p90':float(cal.Ddens.quantile(.9)), 'metric_valid_n':{c:int(cal[c].notna().sum()) for c in ANCHORS},'pooled_counts':{g:{'k':int(cal['k_'+g].sum()),'n':int(cal['n_'+g].sum())} for g in W1}}
    for g in WB:
        result['pooled_counts'][g]={'k':int(cal['kz_'+g].sum()),'n':int(cal['nz_'+g].sum())}
    result['pooled_counts']['mid']={'k':int(cal.k_mid.sum()),'n':int(cal.n_mid.sum())}
    result['fingerprint']=hashlib.sha256(json.dumps(result,sort_keys=True).encode()).hexdigest()
    return result

def score_raw(raw, calibration, pseudocount=5):
    f=smoothed(raw,calibration['priors'],pseudocount)
    for col,(lo,hi) in calibration['anchors'].items():
        f['Q_'+col]=((f[col]-lo)/(hi-lo)*100).clip(0,100) if hi!=lo else pd.Series(np.where(f[col].notna(),50,np.nan),index=f.index)
        f['clip_low_'+col]=f[col]<lo
        f['clip_high_'+col]=f[col]>hi
        f['endpoint_low_'+col]=f[col]==lo
        f['endpoint_high_'+col]=f[col]==hi
        f['constant_anchor_'+col]=hi==lo
    f['M1']=f.Q_M1raw
    P=.4*f.Q_P_open+.6*f.Q_P_close; C=.4*f.Q_C_open+.6*f.Q_C_close
    f['M2']=combine([(P,.4),(C,.35),(f.H_score,.25)])
    f['M3']=combine([(f.Q_BL,.4),(f.Q_SQ,.35),(100-f.Q_Ddens,.25)])
    f['M4']=100-f.Q_MIDfail
    f['panic']=(f.ratio20>1)&(f.median_ret<0)&(((f.Ddens>0)&(f.Ddens>=calibration['panic_ddens_p90']))|(f.H_h2l>=1))
    f['M5']=np.where(f.panic,np.minimum(f.Q_lr20,50),f.Q_lr20)
    cols=['M1','M2','M3','M4','M5']; weights=np.array([25,20,20,15,10])
    f['score5']=(f[cols]*weights).sum(axis=1,min_count=5)/90
    f['available_weight_sum']=(f[cols].notna()*weights).sum(axis=1)/90
    f['score_available']=(f[cols]*weights).sum(axis=1,min_count=1)/(f.available_weight_sum*90)
    f['grade5']=pd.cut(f.score5,[-np.inf,35,50,65,80,np.inf],right=False,labels=['D','C','B','A','S']).astype('string')
    f['delta1']=f.score5.diff(); f['delta5']=f.score5.diff(5)
    f['direction']=np.select([f.delta1>=2,f.delta1<=-2,f.delta1.notna()],['上','下','平'],default='UNKNOWN')
    f['pseudocount']=pseudocount
    return f

def clipping_table(scored):
    rows=[]
    for year,g in scored.groupby(scored.date.astype(str).str[:4]):
        for metric in ANCHORS:
            n=int(g[metric].notna().sum())
            r={'year':year,'metric':metric,'total_n':len(g),'valid_n':n,'missing_n':len(g)-n}
            for label in ['clip_low','clip_high','endpoint_low','endpoint_high']:
                r[label+'_n']=int(g[label+'_'+metric].sum()); r[label+'_rate']=r[label+'_n']/n if n else np.nan
            r['saturated_low_n']=int((g['Q_'+metric]==0).sum()); r['saturated_high_n']=int((g['Q_'+metric]==100).sum()); r['constant_anchor']=bool(g['constant_anchor_'+metric].all())
            rows.append(r)
    return pd.DataFrame(rows)
