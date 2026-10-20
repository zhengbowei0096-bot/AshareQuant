"""Walk-forward OOS label proxy. Explicitly insufficient for live promotion.

Uses only training-window covariance; labels are consumed after target construction.
Does not pretend close-to-close adjusted labels are next-open executable returns.
"""
from pathlib import Path
import json
import numpy as np
import pandas as pd
from .core import read_snapshot, write_json
from .decision import METHODS, DEFAULTS, covariance, optimize, costs, validate_config, load_fee_config


def compare_oos(records, histories, sectors, c, horizon=20):
    needed={'symbol','date','label_end','predicted','actual'}
    if not needed.issubset(records.columns): raise ValueError('样本外记录字段缺失')
    records=records.copy()
    records['date']=pd.to_datetime(records.date); records['label_end']=pd.to_datetime(records.label_end)
    if records.duplicated(['symbol','date']).any() or (records.label_end<=records.date).any(): raise ValueError('样本重复或标签日期无效')
    if not np.isfinite(records[['predicted','actual']].to_numpy(float)).all(): raise ValueError('样本外预测/标签非有限')
    symbols=sorted(records.symbol.unique()); result={m:[] for m in METHODS}
    last_end=pd.Timestamp.min
    previous={m:np.zeros(len(symbols)) for m in METHODS}
    for day,chunk in records.groupby('date',sort=True):
        if day<=last_end or set(chunk.symbol)!=set(symbols): continue
        chunk=chunk.set_index('symbol').reindex(symbols)
        frame=pd.concat([histories[s].loc[:day].rename(s) for s in symbols],axis=1).dropna().tail(c['covariance_days'])
        try: cov=covariance(frame.to_numpy(),c['minimum_covariance_days'],horizon)
        except ValueError: continue
        mu=chunk.predicted.to_numpy(float); actual=chunk.actual.to_numpy(float)
        for method in METHODS:
            w=optimize(method,mu,cov,[sectors[s] for s in symbols],c,mu>0)
            old=previous[method]; delta=w-old
            # Enforce turnover from preceding drifted portfolio; cost gate can freeze.
            turn=abs(delta).sum()
            if turn>c['max_turnover']: w=old+delta*c['max_turnover']/turn
            fee=sum(costs(abs(w[i]-old[i])*c['initial_cash'],'BUY' if w[i]>old[i] else 'SELL',c,symbols[i])['total'] for i in range(len(w)))/c['initial_cash']
            if fee>c['max_cost_fraction']: w=old.copy(); fee=0.
            gross=float(w@actual); net=gross-fee
            if net<=-1: raise ValueError('标签导致净值非正')
            result[method].append(dict(date=str(day.date()),label_end=str(chunk.label_end.max().date()),gross_return=gross,net_return=net,
                estimated_cost_fraction=fee,turnover=float(abs(w-old).sum()),weights=dict(zip(symbols,map(float,w))),
                covariance_last_date=str(frame.index.max().date())))
            previous[method]=w*(1+actual)/(1+gross)
        last_end=chunk.label_end.max()
    summary={}
    for method,periods in result.items():
        r=np.array([p['net_return'] for p in periods]); curve=np.r_[1.,np.cumprod(1+r)]
        draw=float(np.min(curve/np.maximum.accumulate(curve)-1))
        summary[method]=dict(periods=len(periods),net_return=float(curve[-1]-1),max_drawdown=draw,
                             mean_turnover=float(np.mean([p['turnover'] for p in periods])) if periods else None,
                             return_std=float(np.std(r)) if len(r)>1 else None)
    return dict(status='RESEARCH_PROXY_NOT_PROMOTION_EVIDENCE',formal_method=None,summary=summary,periods=result,
                limitations=['使用当下观察池，存在选择和幸存者偏差','收益为后复权收盘到收盘标签，不是次日开盘实际可成交收益',
                '未模拟历史停牌/涨跌停排队、整数股、T+1及真实交割单；成本是固定15万元名义规模估算',
                '未通过历史执行回放及独立概率校准，不得据此选择正式方法'])


def run(root):
    root=Path(root); c={**DEFAULTS}
    if (root/'decision_config.json').exists(): c.update(json.loads((root/'decision_config.json').read_text()))
    load_fee_config(root,c)
    validate_config(c)
    payload=json.loads((root/'reports/forecasts/latest/forecast.json').read_text())
    original=root/'reports/forecasts'/pd.Timestamp(payload['generated_at']).strftime('%Y%m%d-%H%M%S-%f')/'validation_predictions.csv'
    if not original.exists():
        # Resolve through the existing pointer, never use a random older OOS file.
        ptr=json.loads((root/'reports/forecast_latest.json').read_text())
        original=Path(ptr['folder'])/'validation_predictions.csv'
    records=pd.read_csv(original,dtype={'symbol':str})
    records=records[records.horizon==c['horizon']]
    histories={}; sectors={r['symbol']:r['sector'] for r in payload['forecasts']}
    for s in records.symbol.unique():
        snap=read_snapshot(root,s)
        if snap is None: raise ValueError(f'{s}快照缺失')
        if snap['meta']['version']!=payload.get('data_versions',{}).get(s): raise ValueError('验证快照版本与预测不一致')
        histories[s]=snap['hfq'].set_index('date').close.pct_change(fill_method=None)
    result=compare_oos(records,histories,sectors,c,c['horizon'])
    stressed={**c,'slippage_bps':c['slippage_bps']*3,'commission_bps':c['commission_bps']*2}
    result['cost_stress']=compare_oos(records,histories,sectors,stressed,c['horizon'])['summary']
    result['source']=str(original)
    write_json(root/'reports/decisions/validation.json',result)
    return result
