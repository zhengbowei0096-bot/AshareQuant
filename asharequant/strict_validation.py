"""Chronological validation primitives. Synthetic fixtures never qualify as evidence."""
import hashlib
import json
from pathlib import Path
import numpy as np
import pandas as pd
from .core import now, write_json


def chronological_split(frame, validation_days, test_days, embargo_sessions=0):
    if not frame.index.is_monotonic_increasing or frame.index.has_duplicates:
        raise ValueError('时间索引必须递增且唯一')
    if min(validation_days,test_days)<1 or embargo_sessions<0:
        raise ValueError('隔离窗口无效')
    if len(frame)<=validation_days+test_days+2*embargo_sessions:
        raise ValueError('独立训练数据不足')
    test=frame.iloc[-test_days:]
    validation_end=len(frame)-test_days-embargo_sessions
    validation=frame.iloc[validation_end-validation_days:validation_end]
    train=frame.iloc[:validation_end-validation_days-embargo_sessions]
    train=train[train.label_end<validation.index.min()]
    validation=validation[validation.label_end<test.index.min()]
    if train.empty or validation.empty: raise ValueError('标签隔离后样本不足')
    return train,validation,test


def walk_forward(frame, minimum_train, block_days, window=None, embargo_sessions=0):
    if block_days<1 or minimum_train<1 or embargo_sessions<0: raise ValueError('窗口无效')
    if not frame.index.is_monotonic_increasing or frame.index.has_duplicates: raise ValueError('时间索引无效')
    for offset in range(minimum_train+embargo_sessions,len(frame),block_days):
        test=frame.iloc[offset:offset+block_days]
        train=frame.iloc[:offset-embargo_sessions if embargo_sessions else offset]
        train=train[train.label_end<test.index.min()]
        if window is not None: train=train.tail(window)
        if len(train)>=minimum_train: yield train,test


def point_in_time_join(observations, releases):
    """Use availability timestamp, never fiscal period end or revised latest value."""
    left=observations.copy();right=releases.copy()
    for f in (left,right):
        if 'symbol' not in f: raise ValueError('证券代码缺失')
    left['asof']=pd.to_datetime(left['asof'],utc=True)
    right['available_at']=pd.to_datetime(right['available_at'],utc=True)
    if left['asof'].isna().any() or right['available_at'].isna().any(): raise ValueError('时点缺失')
    if right.duplicated(['symbol','available_at']).any(): raise ValueError('披露版本冲突')
    return pd.merge_asof(left.sort_values('asof'),right.sort_values('available_at'),left_on='asof',right_on='available_at',by='symbol',direction='backward')


def metrics(records):
    f=records.copy();required=['actual','predicted']
    if f.empty or not np.isfinite(f[required].to_numpy(float)).all(): raise ValueError('评价样本无效')
    e=f.predicted-f.actual
    result=dict(samples=len(f),dates=int(f.date.nunique()),mae=float(abs(e).mean()),rmse=float(np.sqrt((e*e).mean())),direction_accuracy=float(((f.actual>0)==(f.predicted>0)).mean()))
    daily=[];groups=[]
    for day,g in f.groupby('date',sort=True):
        if len(g)<5 or g.predicted.nunique()<2 or g.actual.nunique()<2: continue
        daily.append(float(g.predicted.rank().corr(g.actual.rank())))
        ordered=g.sort_values('predicted');parts=np.array_split(np.arange(len(g)),5)
        groups.append([float(ordered.iloc[ix].actual.mean()) for ix in parts])
    result.update(rank_ic_mean=float(np.mean(daily)) if daily else None,rank_ic_dates=len(daily),quintile_returns=np.mean(groups,axis=0).tolist() if groups else None)
    # Moving block bootstrap preserves adjacent overlapping-label dependence.
    days=list(f.groupby('date',sort=True));values=np.array([abs(g.predicted-g.actual).mean() for _,g in days]);n=len(values)
    block=max(1,int(np.sqrt(n)));rng=np.random.default_rng(1729);means=[]
    for _ in range(300):
        starts=rng.integers(0,n,size=int(np.ceil(n/block)));ix=np.concatenate([(s+np.arange(block))%n for s in starts])[:n]
        means.append(float(values[ix].mean()))
    result['date_block_mae_95pct']=np.quantile(means,[.025,.975]).tolist();result['bootstrap_block_dates']=block
    if 'probability' in f:
        p=f.probability.to_numpy(float);y=(f.actual>0).to_numpy(float)
        if not np.isfinite(p).all() or ((p<0)|(p>1)).any(): raise ValueError('概率无效')
        bins=[]
        for i in range(10):
            mask=(p>=i/10)&(p<(i+1)/10 if i<9 else p<=1)
            if mask.any(): bins.append(dict(n=int(mask.sum()),predicted=float(p[mask].mean()),observed=float(y[mask].mean())))
        result.update(brier=float(np.mean((p-y)**2)),ece=sum(b['n']*abs(b['predicted']-b['observed']) for b in bins)/len(p),reliability=bins)
    return result


def formal_gate(root):
    from .lifecycle import admission
    try:
        return admission(root)
    except (ValueError,KeyError,OSError) as exc:
        return dict(status='BLOCKED',formal_model=None,formal_method=None,blockers=['PRODUCTION_EVIDENCE_INVALID:'+str(exc)],submitted_orders=0)



def audit(root):
    root=Path(root);files=sorted((root/'asharequant').glob('*.py'))
    result=dict(generated_at=now().isoformat(),gate=formal_gate(root),
        source_sha256={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
        evidence={name:(root/name).exists() for name in ['data/market.sqlite3','data/model_registry.json','data/execution_history.json','data/decision_account.json','data/calendar.csv']})
    path=root/'reports/strict_audit.json';write_json(path,result);return result
