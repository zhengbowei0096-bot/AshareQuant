"""Chronological, purged probability calibration with cross-sectional date blocks."""
from pathlib import Path
import json
import hashlib
import numpy as np
import pandas as pd
from .core import write_json, now
from .connections import save_record


def sigmoid(x): return 1/(1+np.exp(-np.clip(x,-35,35)))


def independent_dates(frame):
    selected=[]; last=pd.Timestamp.min
    for day,g in frame.groupby('date',sort=True):
        if day>last:
            selected.append(g); last=g.label_end.max()
    return pd.concat(selected,ignore_index=True) if selected else frame.iloc[:0]


def fit(frame):
    x=frame.predicted.to_numpy(float); y=(frame.actual.to_numpy(float)>0).astype(float)
    mean=float(x.mean()); scale=max(float(x.std()),1e-6); design=np.c_[np.ones(len(x)),(x-mean)/scale]
    beta=np.zeros(2)
    for _ in range(100):
        p=sigmoid(design@beta); penalty=np.diag([1e-6,1.])
        grad=design.T@(p-y)+penalty@beta
        h=design.T@((p*(1-p))[:,None]*design)+penalty
        delta=np.linalg.solve(h,grad); beta-=delta
        if abs(delta).max()<1e-9: break
    return dict(mean=mean,scale=scale,coefficients=beta.tolist(),base_rate=float((y.sum()+1)/(len(y)+2)))


def predict(model,x):
    b=model['coefficients']; return sigmoid(b[0]+b[1]*(np.asarray(x)-model['mean'])/model['scale'])


def calibrate(frame,asof):
    f=frame.copy()
    f['date']=pd.to_datetime(f.date);f['label_end']=pd.to_datetime(f.label_end)
    if f.duplicated(['symbol','date']).any() or (f.label_end<=f.date).any(): raise ValueError('概率样本重复或标签日期无效')
    if not np.isfinite(f[['predicted','actual']].to_numpy()).all(): raise ValueError('概率样本非有限')
    f=f[f.label_end<=pd.Timestamp(asof)].sort_values(['date','symbol'])
    days=sorted(f.date.unique())
    if len(days)<40: return dict(status='INSUFFICIENT_DATA',independent_validated=False,holdout_blocks=0,model=None)
    split=pd.Timestamp(days[int(len(days)*.6)])
    train=independent_dates(f[f.label_end<split]); test=independent_dates(f[f.date>=split])
    if len(train)<40 or test.empty: return dict(status='INSUFFICIENT_DATA',independent_validated=False,holdout_blocks=0,model=None)
    model=fit(train); p=predict(model,test.predicted.to_numpy()); y=(test.actual.to_numpy()>0).astype(float)
    brier=float(np.mean((p-y)**2)); baseline=float(np.mean((model['base_rate']-y)**2))
    bins=[]; ece=0.
    for lo,hi in zip(np.arange(0,1,.2),np.arange(.2,1.01,.2)):
        mask=(p>=lo)&(p<(hi if hi<1 else 1.00001))
        if mask.any():
            predicted=float(p[mask].mean()); observed=float(y[mask].mean()); n=int(mask.sum())
            ece+=n/len(y)*abs(predicted-observed);bins.append(dict(low=float(lo),high=float(hi),n=n,predicted=predicted,observed=observed))
    blocks=int(test.date.nunique()); reasons=[]
    if blocks<30: reasons.append('INDEPENDENT_HOLDOUT_BLOCKS_BELOW_30')
    if brier>=baseline: reasons.append('BRIER_NOT_BETTER_THAN_BASE_RATE')
    if ece>.10: reasons.append('CALIBRATION_ERROR_ABOVE_10PCT')
    if model['coefficients'][1]<=0: reasons.append('NONPOSITIVE_PROBABILITY_SLOPE')
    return dict(status='VALIDATED' if not reasons else 'CALIBRATED_NOT_VALIDATED',independent_validated=not reasons,model=model,
        holdout_blocks=blocks,train_blocks=int(train.date.nunique()),train_rows=len(train),holdout_rows=len(test),brier=brier,baseline_brier=baseline,ece=ece,bins=bins,
        split_date=str(split.date()),train_last_label_end=str(train.label_end.max().date()),test_first_date=str(test.date.min().date()),
        evaluation_last_label_end=str(test.label_end.max().date()),blockers=reasons,
        method='purged chronological logistic calibration v1; nonoverlapping dates; pooled stocks; no refit on holdout',
        limitations=['股票间有相关性，以独立日期块计样本量','样本外区间已用于此前研究，不等于全新未见留出集'])


def source_records(root,payload):
    stamp=pd.Timestamp(payload['generated_at']).strftime('%Y%m%d-%H%M%S-%f')
    path=Path(root)/'reports/forecasts'/stamp/'validation_predictions.csv'
    if not path.exists():
        pointer=Path(root)/'reports/forecast_latest.json'
        meta=json.loads(pointer.read_text())
        folder=Path(meta['folder']).resolve()
        if not folder.is_relative_to((Path(root)/'reports/forecasts').resolve()): raise ValueError('预测路径超出项目归档')
        archived=json.loads((folder/'forecast.json').read_text())
        if archived.get('generated_at')!=payload.get('generated_at') or archived.get('model')!=payload.get('model'): raise ValueError('归档版本与最新预测不一致')
        path=folder/'validation_predictions.csv'
        if not path.exists(): raise ValueError('对应预测版本的样本外记录缺失')
    f=pd.read_csv(path,dtype={'symbol':str})
    return f,path


def run(root):
    root=Path(root); path=root/'reports/forecasts/latest/forecast.json'; payload=json.loads(path.read_text())
    f,source=source_records(root,payload); reports={}
    for h in (1,5,20): reports[str(h)]=calibrate(f[f.horizon==h],payload['asof'])
    result=dict(status=reports['20']['status'],independent_validated=reports['20']['independent_validated'],holdout_blocks=reports['20']['holdout_blocks'],
        horizons=reports,forecast_generated_at=payload['generated_at'],forecast_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        model=payload['model'],source=str(source),source_sha256=hashlib.sha256(source.read_bytes()).hexdigest())
    write_json(root/'reports/decisions/calibration.json',result)
    return save_record(root,'calibration',result)


def attach(root,rows,payload,horizon):
    p=Path(root)/'data/connections/calibration/latest.json'
    if not p.exists(): return dict(status='MISSING')
    x=json.loads(p.read_text())
    if x.get('forecast_generated_at')!=payload.get('generated_at') or x.get('model')!=payload.get('model'): return dict(status='STALE')
    report=x['horizons'][str(horizon)]
    if report.get('model'):
        for row in rows:
            f=row.get('forecast',{})
            if isinstance(f.get('predicted_return'),(int,float)):
                f['up_probability_calibrated']=float(predict(report['model'],f['predicted_return']))
                f['probability_calibration']=report
    return report
