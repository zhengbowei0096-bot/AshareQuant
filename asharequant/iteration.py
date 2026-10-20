"""Bounded candidate research; never tune on the prospective acceptance cohort."""
from pathlib import Path
import json
import numpy as np
import pandas as pd
from scipy.stats import ks_2samp
from .core import now, cutoff, load_config, read_snapshot, read_watchlist, write_json
from .forecast import FEATURES, DEFAULTS, features_and_labels, validate_forecast
from . import lifecycle as lc

POLICY = dict(research_every_days=90, trigger_cooldown_days=30, reference_sessions=252,
              recent_sessions=60, psi_threshold=.25, family_alpha=.01, persistent_windows=2,
              minimum_research_gain=.02, minimum_positive_folds=3)
# Finite, predeclared search family. Selection history is never final-test evidence.
CANDIDATES = [dict(alpha=a,features=f) for a in (1.,10.,100.) for f in
              (FEATURES, [x for x in FEATURES if x!='volume_ratio'],
               [x for x in FEATURES if x not in ('r1','range20')])]


def drift(reference,recent):
    a=np.asarray(reference,float);b=np.asarray(recent,float)
    if len(a)<252 or len(b)<60 or not np.isfinite(a).all() or not np.isfinite(b).all():
        return dict(status='INSUFFICIENT_OR_INVALID',psi=None,pvalue=1.)
    edges=np.unique(np.quantile(a,np.linspace(0,1,11)))
    if len(edges)<3:
        edges=np.array([-np.inf,float(a[0])-1e-9,float(a[0])+1e-9,np.inf])
    else:edges=np.r_[-np.inf,edges[1:-1],np.inf]
    p=np.maximum(np.histogram(a,edges)[0]/len(a),1e-4);q=np.maximum(np.histogram(b,edges)[0]/len(b),1e-4);p=p/p.sum();q=q/q.sum()
    return dict(status='MEASURED',psi=float(np.sum((q-p)*np.log(q/p))),pvalue=float(ks_2samp(a,b).pvalue))


def should_research(at,previous,trigger):
    if not previous:return 'INITIAL_RESEARCH'
    elapsed=(pd.Timestamp(at)-pd.Timestamp(previous['at'])).days
    if elapsed>=POLICY['research_every_days']:return 'SCHEDULED_90_DAYS'
    if trigger and elapsed>=POLICY['trigger_cooldown_days']:return 'PERSISTENT_DRIFT_OR_DEGRADATION'
    return None


def select_candidate(frames,cfg):
    results=[]
    for candidate in CANDIDATES:
        gains=[];positive=[];errors=[]
        for key,frame in frames.items():
            try:
                base,_=validate_forecast(frame,key[1],cfg)
                test,_=validate_forecast(frame,key[1],{**cfg,'ridge_alpha':candidate['alpha']},candidate['features'])
                if not base.date.equals(test.date):raise ValueError('UNPAIRED_VALIDATION_DATES')
                b=(base.actual-base.predicted).abs().to_numpy();c=(test.actual-test.predicted).abs().to_numpy()
                gains.append(float(1-c.mean()/max(b.mean(),1e-12)))
                positive.append(int(sum(c[i].mean()<b[i].mean() for i in np.array_split(np.arange(len(b)),4))))
            except Exception as exc:errors.append(dict(slot=str(key),error=str(exc)))
        gain=float(np.mean(gains)) if gains else None
        eligible=not errors and len(gains)==len(frames) and bool(gains) and gain>=POLICY['minimum_research_gain'] and min(positive)>=POLICY['minimum_positive_folds']
        results.append(dict(candidate=candidate,mean_gain=gain,minimum_positive_folds=min(positive) if positive else 0,eligible_for_future_test=eligible,errors=errors))
    eligible=[r for r in results if r['eligible_for_future_test']]
    winner=max(eligible,key=lambda r:r['mean_gain']) if eligible else None
    return results,winner


def run(root):
    root=Path(root);at=now();end=cutoff(at);cfg={**DEFAULTS,**json.loads((root/'forecast_config.json').read_text())}
    from .data import calendar
    sessions,_=calendar(root,load_config(root),end,offline=True);days=sessions[sessions<=end];asof=str(days.max().date())
    store=root/'data/iteration';store.mkdir(parents=True,exist_ok=True)
    statepath=store/'state.json';state=json.loads(statepath.read_text()) if statepath.exists() else {}
    rows=[];frames={};errors=[]
    for item in read_watchlist(root).itertuples():
        try:
            snap=read_snapshot(root,item.symbol)
            if snap is None or snap['raw'].date.max()!=days.max():raise ValueError('STALE_OR_MISSING_SNAPSHOT')
            if snap['meta'].get('synthetic') or snap['meta'].get('source')!='tencent' or pd.Timestamp(snap['meta']['fetched_at'])>pd.Timestamp(at):raise ValueError('UNVERIFIED_OR_FUTURE_SNAPSHOT')
            for h in (1,5,20):
                frame=features_and_labels(snap['hfq'],days,h)
                frames[item.symbol,h]=frame[frame.label_end<=days.max()]
            f=features_and_labels(snap['hfq'],days,1)[FEATURES]
            # Two disjoint recent windows; reference ends before either window.
            for window in (0,1):
                endpos=len(f)-window*60;recent=f.iloc[endpos-60:endpos];reference=f.iloc[-372:-120]
                for feature in FEATURES:rows.append(dict(symbol=item.symbol,feature=feature,window=window,**drift(reference[feature],recent[feature])))
        except Exception as exc:errors.append(dict(symbol=item.symbol,error=str(exc)))
    from .model_research import holm
    for r,p in zip(rows,holm([r['pvalue'] for r in rows])):
        r['adjusted_pvalue']=p;r['shift']=r['psi'] is not None and r['psi']>=POLICY['psi_threshold'] and p<=POLICY['family_alpha']
    persistent=any(all(next((r['shift'] for r in rows if r['symbol']==s and r['feature']==f and r['window']==w),False) for w in (0,1)) for s,_ in frames for f in FEATURES)
    monitor=root/'data/model_monitor.json';m=json.loads(monitor.read_text()) if monitor.exists() else {}
    degraded=any(g.get('status')=='degraded' for g in m.get('groups',{}).values())
    reason=should_research(at,state.get('last_research'),persistent or degraded)
    if state.get('last_research',{}).get('runtime_hash')!=lc.runtime_hash():reason='INITIAL_RESEARCH' if not state.get('last_research') else 'IMPLEMENTATION_CHANGED'
    studies=[];winner=None;candidate_spec=None
    # Never abandon or tune an experiment while its untouched test is pending.
    pending=state.get('candidate_experiment')
    if pending:
        with lc.database(root) as db:
            spec=lc.get(db,'specs',pending)
        if spec and spec['runtime_hash']==lc.runtime_hash():
            lc.settle(root,spec);lc.select_portfolio(root,spec);lc.collect(root,spec)
            assessment=lc.evaluate(root,spec);release=lc.promote(root,spec,assessment)
            candidate_spec=dict(experiment=pending,evaluation=assessment['status'],release=release)
            if assessment['status'].startswith('WAITING'):reason=None
            else:state.pop('candidate_experiment',None)
        else:
            errors.append(dict(experiment=pending,error='FROZEN_RUNTIME_CHANGED_RESEARCH_RESTART_REQUIRED'))
            state.pop('candidate_experiment',None)
    if reason and not errors:
        studies,winner=select_candidate(frames,cfg)
        state['last_research']=dict(at=at.isoformat(),reason=reason,asof=asof,runtime_hash=lc.runtime_hash())
        if winner:
            candidate={**winner['candidate'],'expected_improvement':winner['mean_gain'],'reason':reason,'research_asof':asof}
            spec=lc.prepare(root,candidate=candidate);state['candidate_experiment']=spec['experiment']
            candidate_spec=dict(experiment=spec['experiment'],evaluation='WAITING_FOR_FIRST_FUTURE_CLOSE',release=None)
    registry=lc.monitor(root)
    report=dict(generated_at=at.isoformat(),asof=asof,status='INPUT_ERRORS' if errors else 'COMPLETE',policy=POLICY,search_family=CANDIDATES,
        drift=rows,persistent_drift=persistent,persistent_degradation=degraded,research_reason=reason,studies=studies,
        candidate=candidate_spec,errors=errors,production_admission=lc.admission(root,registry),submitted_orders=0,
        limitations=['漂移是研究触发信号，不能证明模型失效或触发直接升级。','历史参数/特征搜索仅用于选择未来待测候选；绝不当独立测试。','尚未到期的实际改善保留为空。'])
    with lc.database(root) as db:
        report['update_history']=[dict(version=s['experiment'],reason=(s.get('candidate') or {}).get('reason','BASELINE_FREEZE'),training_time=s['frozen_at'],data_end=s['training_asof'],candidate=s.get('candidate'),
            expected_improvement=(s.get('candidate') or {}).get('expected_improvement'),test_results=lc.get(db,'evaluations',s['experiment']),
            actual_improvement={k:v.get('champion_improvement') for k,v in (lc.get(db,'evaluations',s['experiment']) or {}).get('models',{}).items()} or None,
            releases=[r for r in lc.values(db,'releases') if r['experiment']==s['experiment']],rollback_events=lc.values(db,'events')) for s in lc.values(db,'specs')]
    stamp=at.strftime('%Y%m%d-%H%M%S-%f');write_json(root/'reports/iteration'/stamp/'report.json',report)
    write_json(root/'reports/iteration/latest.json',report);write_json(statepath,state)
    return report
