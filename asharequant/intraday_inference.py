"""Research inference only: reuse saved ridge coefficients, never fit or promote.
Daily-trained features evaluated on an unfinished daily bar are out-of-domain.
This is explicitly unvalidated research; original published forecasts stay immutable.
"""
import copy
import math
import numpy as np
import pandas as pd
from .core import read_snapshot
from .forecast import features_and_labels
from .decision import covariance
from .research_policy import targets, POLICY
from .probability_calibration import predict
from .paper_account import digest


def observed_bar(q, at):
    f=q['raw_fields']
    # Tencent day OHLC and cumulative volume in lots; historical snapshots use shares.
    b=dict(date=pd.Timestamp(str(at.date())),open=float(f[5]),high=float(f[33]),low=float(f[34]),close=q['price'],volume=float(f[6])*100,amount=float(f[37])*10000)
    if not all(math.isfinite(b[k]) and b[k]>0 for k in ('open','high','low','close','volume','amount')) or not b['low']<=min(b['open'],b['close'])<=max(b['open'],b['close'])<=b['high']:
        raise ValueError('PARTIAL_BAR_INVALID')
    return b


def infer_frame(hfq, raw, q, cal, columns, model, at):
    today=pd.Timestamp(str(at.date()));history=hfq[hfq.date<today].copy();raw=raw[raw.date<today]
    if history.empty or raw.empty or history.date.iloc[-1]!=cal[cal<today][-1]:raise ValueError('PRIOR_SESSION_HISTORY_REQUIRED')
    if model['final_training_last_label_end']>=str(at.date()):raise ValueError('MODEL_LABEL_NOT_MATURE_BEFORE_SESSION')
    params=model['model_parameters'];a=np.array(params['coefficients']);mu=np.array(params['means']);sd=np.array(params['scales'])
    if any(len(x)!=len(columns) for x in (a,mu,sd)) or not np.isfinite(np.r_[a,mu,sd,params['intercept']]).all() or (sd<=0).any():raise ValueError('FROZEN_PARAMETERS_INVALID')
    b=observed_bar(q,at);factor=history.close.iloc[-1]/raw.close.iloc[-1]
    for k in ('open','high','low','close'):b[k]*=factor
    frame=features_and_labels(pd.concat([history,pd.DataFrame([b])],ignore_index=True),cal[(cal>=history.date.min())&(cal<=today)],model['horizon'])
    x=frame.iloc[-1][columns].to_numpy(float)
    if not np.isfinite(x).all():raise ValueError('INTRADAY_FEATURE_MISSING')
    y=float(params['intercept']+((x-mu)/sd)@a)
    return y,dict(features=dict(zip(columns,map(float,x))),partial_bar=b|{'date':str(b['date'].date())},parameters_sha=digest(params),vol20=float(frame.vol20.iloc[-1]),training_label_end=model['final_training_last_label_end'])


def dynamic_plan(root, source, state, quotes, checks, cal, at):
    original=source['predictions'];before=digest(original);day=original['asof']
    if pd.Timestamp(original['generated_at'])>at or pd.Timestamp(day)!=cal[cal<pd.Timestamp(str(at.date()))][-1]:raise ValueError('MODEL_SNAPSHOT_STALE_OR_FUTURE')
    payload=copy.deepcopy(original);columns=source['spec']['candidate']['features'];histories={};audit={};c=source['meta']['config']
    for row in payload['forecasts']:
        k=row['symbol'];snap=read_snapshot(root,k)
        if snap is None or pd.Timestamp(snap['meta']['fetched_at'])>at or snap['meta'].get('synthetic'):raise ValueError('HISTORY_NOT_AVAILABLE_AT_DECISION')
        if snap['meta']['version']!=original['data_versions'][k]:raise ValueError('HISTORY_MODEL_VERSION_MISMATCH')
        histories[k]=snap['hfq'].set_index('date').close.pct_change(fill_method=None).loc[:day]
        if k not in quotes or checks.get(k):raise ValueError('MODEL_QUOTE_NOT_VALID:'+k)
        q=quotes[k];row['close']=q['price'];row['price_source']=q['source'];row['price_fetched_at']=q['asof']
        audit[k]={}
        for h,f in row['forecasts'].items():
            y,e=infer_frame(snap['hfq'],snap['raw'],q,cal,columns,f,at);delta=y-f['predicted_return'];f['predicted_return']=y
            f['interval80_empirical']=[v+delta for v in f['interval80_empirical']]
            f['volatility_horizon']=e['vol20']*math.sqrt(f['horizon']);f['downside_es95_normal_proxy']=min(0.,y-2.063*f['volatility_horizon'])
            calibration=f.get('probability_calibration',{})
            if calibration.get('model'):
                f['up_probability_calibrated']=float(predict(calibration['model'],np.array([y]))[0])
            else:
                # No residual distribution available: never fabricate an updated probability.
                raise ValueError('FROZEN_CALIBRATOR_REQUIRED:'+k)
            calibration['independent_validated']=False
            f['model_status']='INTRADAY_PARTIAL_DAILY_BAR_UNVALIDATED'
            audit[k][h]=dict(predicted_return=y,original_predicted_return=y-delta,**e)
    cov=covariance(pd.concat(histories,axis=1).dropna().tail(c['covariance_days']).to_numpy(),c['minimum_covariance_days'],c['horizon'])
    policy=copy.deepcopy(source['spec'].get('policy',POLICY));policy['exposure']={'risk_off':.45,'neutral':.675,'risk_on':.9}
    weights,rows=targets(payload,state['positions'],state['equity'],cov,c,source['plan']['environment'],policy)
    v=np.array([weights[r['symbol']] for r in payload['forecasts']]);sigma=float(np.sqrt(max(0.,v@cov@v)*252/c['horizon']))
    ceiling=source['plan']['relative_volatility_ceiling'];scale=min(1.,ceiling/sigma) if sigma>0 else 1.
    weights={k:w*scale for k,w in weights.items()}
    plan=copy.deepcopy(source['plan']);plan.update(weights=weights,rows=rows,risk_priorities={r['symbol']:r['risk_priority'] for r in rows},edges={r['symbol']:dict(reference_price=r['close'],predicted_return=r['forecasts'][str(c['horizon'])]['predicted_return'],uncertainty=r['forecasts'][str(c['horizon'])]['model_uncertainty']['std']) for r in payload['forecasts']},dynamic_at=at.isoformat())
    assert digest(original)==before
    return plan,dict(at=at.isoformat(),stocks=audit,original_sha=before,formal_validated=False,mode='UNVALIDATED_RESEARCH_INFERENCE',limitation='日线训练模型应用于未完成日线，输入分布不同；区间与参数不确定性沿用盘后估计，未验证盘中覆盖率；不修改正式模型或原评分排名')
