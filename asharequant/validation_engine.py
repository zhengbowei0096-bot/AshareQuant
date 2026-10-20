"""Integrated fixed-spec train/validation/test diagnostics with sealed test access.
Existing inspected historical data is never described as untouched evidence.
"""
from pathlib import Path
import hashlib
import json
import sqlite3
import numpy as np
import pandas as pd
from .core import now,cutoff,load_config,read_watchlist,read_snapshot,write_json
from .data import calendar
from .forecast import DEFAULTS,FEATURES,features_and_labels,fit_predict
from .strict_validation import chronological_split,metrics,formal_gate


def infer(path,expected_sha256,features):
    content=Path(path).read_bytes()
    if hashlib.sha256(content).hexdigest()!=expected_sha256:raise ValueError('模型工件哈希不符')
    artifact=json.loads(content)
    if artifact.get('kind')!='ridge_numeric_v1' or artifact.get('features')!=FEATURES:raise ValueError('模型类型或特征版本不匹配')
    params=artifact['parameters'];mu=np.asarray(params['means']);sd=np.asarray(params['scales']);coef=np.asarray(params['coefficients'])
    if any(a.shape!=(len(FEATURES),) or not np.isfinite(a).all() for a in (mu,sd,coef)) or (sd<=0).any():raise ValueError('工件参数无效')
    intercept=params['intercept'];x=features[FEATURES].to_numpy(float)
    if not np.isfinite(x).all() or not np.isfinite(intercept):raise ValueError('推断输入无效')
    return intercept+((x-mu)/sd)@coef


def run(root):
    root=Path(root);cfg={**DEFAULTS,**json.loads((root/'forecast_config.json').read_text())};end=cutoff()
    cal,_=calendar(root,load_config(root),end,offline=True)
    stamp=now().strftime('%Y%m%d-%H%M%S-%f');folder=root/'reports/strict_validation'/stamp;folder.mkdir(parents=True)
    rows=[];errors=[];artifacts=[];spec=dict(features=FEATURES,alpha=cfg['ridge_alpha'],validation_days=126,test_days=126,
        algorithm='fixed ridge, zero hyperparameter search',historical_data_previously_inspected=True)
    from .model_candidates import signature
    spec['pipeline_hash']=signature()
    specid=hashlib.sha256(json.dumps(spec,sort_keys=True).encode()).hexdigest();write_json(folder/'specification.json',spec)
    with sqlite3.connect(root/'data/market.sqlite3') as db:
        db.execute('CREATE TABLE IF NOT EXISTS sealed_test_access (symbol TEXT,horizon INTEGER,first_date TEXT,last_date TEXT,spec TEXT,input_version TEXT,result TEXT,PRIMARY KEY(symbol,horizon,first_date,last_date))')
        for stock in read_watchlist(root).itertuples():
            try:
                snap=read_snapshot(root,stock.symbol)
                if snap is None or snap['raw'].date.max()!=pd.DatetimeIndex(cal)[pd.DatetimeIndex(cal)<=end].max():raise ValueError('快照过期或缺失')
                for horizon in (1,5,20):
                    frame=features_and_labels(snap['hfq'],cal[cal<=end],horizon).dropna(subset=FEATURES+['target','label_end'])
                    train,val,test=chronological_split(frame,126,126,horizon)
                    if len(train)<cfg['minimum_train']:raise ValueError('三段隔离后训练样本不足')
                    first,last=str(test.index.min().date()),str(test.index.max().date());key=(stock.symbol,horizon,first,last)
                    saved=db.execute('SELECT spec,input_version,result FROM sealed_test_access WHERE symbol=? AND horizon=? AND first_date=? AND last_date=?',key).fetchone()
                    if saved:
                        if saved[0]!=specid or saved[1]!=snap['meta']['version']:raise ValueError('封存测试集已使用，拒绝以新参数/修订数据重测')
                        cached=json.loads(saved[2]);cached['cached_test_result']=True;rows.append(cached);artifacts.append(dict(symbol=stock.symbol,horizon=horizon,path=cached['artifact'],sha256=cached['artifact_sha256'],status='RESEARCH_ONLY'));continue
                    overlap=db.execute('SELECT 1 FROM sealed_test_access WHERE symbol=? AND horizon=? AND first_date<=? AND last_date>=?',(stock.symbol,horizon,last,first)).fetchone()
                    if overlap:raise ValueError('测试日期与已使用封存区间重叠，禁止滑动重用作调参证据')
                    vp,_=fit_predict(train,val,cfg['ridge_alpha'])
                    # Parameters remain fixed. Refit on all matured pre-test labels.
                    final_train=pd.concat([train,val]);final_train=final_train[final_train.label_end<test.index.min()]
                    tp,params=fit_predict(final_train,test,cfg['ridge_alpha'])
                    def records(part,pred):return pd.DataFrame(dict(date=part.index,symbol=stock.symbol,label_end=part.label_end.to_numpy(),actual=part.target.to_numpy(),predicted=pred))
                    v,t=records(val,vp),records(test,tp)
                    artifact=dict(kind='ridge_numeric_v1',status='RESEARCH_ONLY',features=FEATURES,parameters=params,symbol=stock.symbol,horizon=horizon,
                        train_last_label_end=str(final_train.label_end.max().date()),spec_id=specid,input_version=snap['meta']['version'])
                    ap=folder/f'{stock.symbol}-{horizon}.model.json';write_json(ap,artifact);sha=hashlib.sha256(ap.read_bytes()).hexdigest()
                    np.testing.assert_allclose(infer(ap,sha,test),tp)
                    result=dict(symbol=stock.symbol,horizon=horizon,train_rows=len(train),validation_rows=len(val),test_rows=len(test),
                        train_last_label_end=str(train.label_end.max().date()),validation_first=str(val.index.min().date()),validation_last_label_end=str(val.label_end.max().date()),test_first=first,test_last=last,
                        validation=metrics(v),test=metrics(t),zero_baseline_test_mae=float(abs(test.target).mean()),mean_baseline_test_mae=float(abs(test.target-final_train.target.mean()).mean()),
                        artifact=str(ap),artifact_sha256=sha,input_version=snap['meta']['version'],formal_eligible=False,
                        limitation='已查看历史上的封存诊断，不是未见独立测试，也不是实际交易收益证据')
                    db.execute('INSERT INTO sealed_test_access VALUES(?,?,?,?,?,?,?)',(*key,specid,snap['meta']['version'],json.dumps(result,ensure_ascii=False)))
                    rows.append(result);artifacts.append(dict(symbol=stock.symbol,horizon=horizon,path=str(ap),sha256=sha,status='RESEARCH_ONLY'))
            except Exception as exc:errors.append(dict(symbol=stock.symbol,error=str(exc)))
    result=dict(generated_at=now().isoformat(),specification=spec,spec_id=specid,results=rows,errors=errors,formal_gate=formal_gate(root),status='HISTORICAL_DIAGNOSTIC_ONLY')
    write_json(folder/'validation.json',result)
    write_json(root/'reports/strict_validation/latest.json',result)
    # A dedicated research registry prevents collision with legacy production labels.
    write_json(root/'data/strict_research_registry.json',dict(status='RESEARCH_ONLY',artifacts=artifacts,report=str(folder/'validation.json')))
    return result
