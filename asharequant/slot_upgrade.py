"""Versioned per-stock/horizon research and prospective daily-refit validation.

The frozen object is a learning procedure (recipe, training window, code), not
one weight vector. Each prediction is sealed before its target can be observed.
No test result is used to select/tune a recipe; final evaluation is one-shot.
Prediction acceptance never grants probability or portfolio trading admission.
"""
from pathlib import Path
import hashlib
import inspect
import json
import math
import sqlite3
import numpy as np
import pandas as pd
from . import lifecycle as lc, iteration as research
from .core import now, cutoff, write_json, atomic_text, read_snapshot, read_watchlist
from .forecast import DEFAULTS, FEATURES, features_and_labels, fit_predict, validate_forecast

VERSION = 'slot-v2'
POLICY = dict(version=VERSION, minimum_gain=0., minimum_positive_folds=3,
              test_origins={'1':60,'5':90,'20':253}, minimum_independent=12,
              family_alpha=.05, maximum_slots=150, confidence=.95,
              minimum_direction=.52, maximum_age_days=550,
              trial_recent_origins=20, trial_requires_recent_gain=True, trial_requires_direction_noninferiority=True,
              evaluation='one-shot prospective; nonoverlap pairs; Bonferroni with trial alpha spending')
TABLES=('specs','issues','outcomes','evaluations','releases','events','studies')


def database(root):
    p=Path(root)/'data/iteration/slot-v2.sqlite3';p.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(p,timeout=30)
    for table in TABLES:
        db.execute(f'CREATE TABLE IF NOT EXISTS su_{table}(key TEXT PRIMARY KEY,body TEXT NOT NULL,sha256 TEXT NOT NULL)')
        for action in ('UPDATE','DELETE'):
            db.execute(f"CREATE TRIGGER IF NOT EXISTS su_{table}_{action.lower()} BEFORE {action} ON su_{table} BEGIN SELECT RAISE(ABORT,'immutable slot evidence'); END")
    return db


def get(db,table,key):
    r=db.execute(f'SELECT body,sha256 FROM su_{table} WHERE key=?',(key,)).fetchone()
    if not r:return None
    x=json.loads(r[0])
    if lc.digest(x)!=r[1]:raise ValueError('SLOT_EVIDENCE_HASH_MISMATCH')
    return x


def values(db,table):
    return [get(db,table,k) for k, in db.execute(f'SELECT key FROM su_{table} ORDER BY key').fetchall()]


def put(db,table,key,x):
    old=get(db,table,key)
    if old is not None:
        if old!=x:raise ValueError('IMMUTABLE_SLOT_CONFLICT')
        return
    db.execute(f'INSERT INTO su_{table} VALUES(?,?,?)',(key,json.dumps(x,ensure_ascii=False,sort_keys=True,allow_nan=False),lc.digest(x)))


def fingerprint():
    # UI/report changes do not invalidate a frozen experiment. Model or protocol changes do.
    import importlib.metadata
    from .research_paper import forecast_candidate
    return lc.digest(dict(protocol=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        algorithm=[inspect.getsource(f) for f in (features_and_labels,fit_predict,validate_forecast,forecast_candidate,lc.predictive_metrics)],
        dependencies={p:importlib.metadata.version(p) for p in ('numpy','pandas','scipy')}))


def config(root):
    p=Path(root)/'forecast_config.json'
    return {**DEFAULTS,**(json.loads(p.read_text()) if p.exists() else {})}


def fallback(root,cfg):
    # Compare the recipe actually used by the current research simulation, when present.
    p=Path(root)/'reports/research_paper/latest/report.json'
    if p.exists():return json.loads(p.read_text())['spec']['candidate']
    return dict(alpha=cfg['ridge_alpha'],features=FEATURES)


def recipe(x):
    r=dict(alpha=float(x['alpha']),features=list(x['features']))
    if r not in research.CANDIDATES:raise ValueError('UNDECLARED_SLOT_RECIPE')
    return r


def window(horizon,attempt):
    # Sum over all attempts 1/[k(k+1)] = 1; at most 50 stocks x 3 horizons.
    alpha=POLICY['family_alpha']/(POLICY['maximum_slots']*attempt*(attempt+1))
    minimum=max(POLICY['minimum_independent'],math.ceil(math.log2(1/alpha)))
    count=max(POLICY['test_origins'][str(horizon)],(minimum-1)*(horizon+1)+1)
    return dict(alpha=alpha,minimum_independent=minimum,test_origins=count)


def search(frame,horizon,cfg,champion):
    base,_=validate_forecast(frame,horizon,{**cfg,'ridge_alpha':champion['alpha']},champion['features'])
    rows=[]
    for choice in research.CANDIDATES:
        out,_=validate_forecast(frame,horizon,{**cfg,'ridge_alpha':choice['alpha']},choice['features'])
        if not base.date.equals(out.date):raise ValueError('UNPAIRED_SLOT_DATES')
        b=(base.actual-base.predicted).abs().to_numpy();c=(out.actual-out.predicted).abs().to_numpy()
        if not len(b) or not np.isfinite(np.r_[b,c]).all():raise ValueError('INVALID_SLOT_ERRORS')
        gain=float(1-c.mean()/max(b.mean(),1e-12))
        folds=int(sum(c[i].mean()<b[i].mean() for i in np.array_split(np.arange(len(b)),4) if len(i)))
        recent=int(POLICY['trial_recent_origins'])
        recent_gain=float(1-c[-recent:].mean()/max(b[-recent:].mean(),1e-12))
        direction=float(((out.actual.iloc[-recent:]>0)==(out.predicted.iloc[-recent:]>0)).mean())
        base_direction=float(((base.actual.iloc[-recent:]>0)==(base.predicted.iloc[-recent:]>0)).mean())
        good=len(b)>=recent and gain>0 and folds>=POLICY['minimum_positive_folds'] and recent_gain>0 and direction>=base_direction and recipe(choice)!=recipe(champion)
        rows.append(dict(candidate=choice,mean_gain=gain,recent_gain=recent_gain,recent_direction=direction,champion_recent_direction=base_direction,minimum_positive_folds=folds,eligible_for_future_test=bool(good),errors=[]))
    good=[r for r in rows if r['eligible_for_future_test']]
    return rows,max(good,key=lambda r:r['mean_gain']) if good else None


def freeze(root,db,symbol,horizon,winner,champion,asof,at,sessions,cfg,source_study):
    slot=f'{symbol}/{horizon}'
    active=[s for s in values(db,'specs') if s['slot']==slot and not get(db,'evaluations',s['id'])]
    if active:return active[0]
    old=next((s for s in values(db,'specs') if s['slot']==slot and s['source_study']==source_study),None)
    if old:return old
    attempt=1+sum(s['slot']==slot for s in values(db,'specs'))
    future=sessions[sessions>pd.Timestamp(asof)];budget=window(horizon,attempt)
    # Freeze the ordinal cohort rule and known calendar prefix, not invented holidays.
    if not len(future):raise ValueError('VALIDATION_CALENDAR_TOO_SHORT')
    spec=dict(version=VERSION,slot=slot,symbol=symbol,horizon=horizon,attempt=attempt,
        candidate=recipe(winner['candidate']),champion=recipe(champion),config=cfg,
        frozen_at=at.isoformat(),training_asof=asof,start_session=str(future[0].date()),
        known_calendar=[str(d.date()) for d in future],budget=budget,policy=POLICY,
        code=fingerprint(),source_study=source_study,trial_eligible=True,trial_basis='historical chronological screens only; not independent validation',protocol='fixed recipe; causal daily refit on matured labels; no tuning or early test examination',
        portfolio_admission=False,probability_admission=False)
    spec['id']=lc.digest(spec)[:32];put(db,'specs',spec['id'],spec)
    return spec


def timetable(spec,sessions):
    future=[str(d.date()) for d in sessions[sessions>=pd.Timestamp(spec['start_session'])]]
    known=spec['known_calendar']
    if future[:len(known)]!=known:raise ValueError('FROZEN_SLOT_CALENDAR_CHANGED')
    n=spec['budget']['test_origins'];h=spec['horizon']
    origins=future[:n];targets={d:future[i+h] for i,d in enumerate(origins) if i+h<len(future)}
    earliest=targets[origins[-1]]+'T18:00:00+08:00' if len(origins)==n and origins[-1] in targets else None
    return origins,targets,earliest


def check_spec(spec):
    if spec['id']!=lc.digest({k:v for k,v in spec.items() if k!='id'})[:32]:raise ValueError('SLOT_SPEC_ID_MISMATCH')
    if spec['code']!=fingerprint():raise ValueError('FROZEN_SLOT_CODE_CHANGED')
    if spec['policy']!=POLICY or spec['budget']!=window(spec['horizon'],spec['attempt']):raise ValueError('SLOT_POLICY_CHANGED')


def point_prediction(frame,day,horizon,cfg,choice):
    train=frame.dropna(subset=choice['features']+['target','label_end'])
    train=train[train.label_end<=pd.Timestamp(day)].tail(cfg['training_cap'])
    x=frame.loc[frame.index==pd.Timestamp(day)]
    if len(x)!=1 or len(train)<cfg['minimum_train']:raise ValueError('SLOT_TRAINING_OR_FEATURES_MISSING')
    predicted,params=fit_predict(train,x,choice['alpha'],choice['features'])
    return dict(predicted=float(predicted[0]),parameters=params,baseline_mean=float(train.target.mean()),
        train_last_label_end=str(train.label_end.max().date()),features={c:float(x[c].iloc[0]) for c in choice['features']})


def issue(root,db,spec,asof,at,sessions):
    check_spec(spec)
    if config(root)!=spec['config']:raise ValueError('FROZEN_SLOT_CONFIG_CHANGED')
    origins,targets,_=timetable(spec,sessions)
    if asof not in origins:return
    if asof not in targets:raise ValueError('SLOT_TARGET_CALENDAR_UNKNOWN')
    key=spec['id']+'|'+asof
    if get(db,'issues',key):return
    lower,upper=lc._window(pd.Timestamp(asof),sessions)
    if not lower<=at<upper or at<=pd.Timestamp(spec['frozen_at']):raise ValueError('SLOT_ISSUE_DEADLINE_MISSED')
    snap=read_snapshot(root,spec['symbol'])
    if snap is None or str(snap['raw'].date.max().date())!=asof or snap['meta'].get('synthetic') or snap['meta'].get('source')!='tencent' or pd.Timestamp(snap['meta']['fetched_at'])>at:raise ValueError('SLOT_SNAPSHOT_NOT_CURRENT')
    f=features_and_labels(snap['hfq'],sessions[sessions<=pd.Timestamp(asof)],spec['horizon'])
    test=point_prediction(f,asof,spec['horizon'],spec['config'],spec['candidate'])
    champion=point_prediction(f,asof,spec['horizon'],spec['config'],spec['champion'])
    item=dict(experiment=spec['id'],symbol=spec['symbol'],horizon=spec['horizon'],origin=asof,
        target=targets[asof],issued_at=at.isoformat(),
        predicted=test['predicted'],champion_predicted=champion['predicted'],baseline_mean=test['baseline_mean'],
        candidate_fit=test,champion_fit=champion,input_version=snap['meta']['version'],
        input_sha256=snap['meta']['sha256'],source=snap['meta']['source'])
    put(db,'issues',key,item)


def settle(root,db,spec,at):
    snap=None
    for row in values(db,'issues'):
        if row['experiment']!=spec['id']:continue
        origin=row['origin'];key=spec['id']+'|'+origin
        if not row or get(db,'outcomes',key) or at<pd.Timestamp(row['target']).tz_localize('Asia/Shanghai')+pd.Timedelta(hours=15):continue
        if snap is None:snap=read_snapshot(root,spec['symbol'])
        if snap is None or snap['meta'].get('synthetic') or snap['meta'].get('source')!='tencent' or pd.Timestamp(snap['meta']['fetched_at'])>at:raise ValueError('SLOT_OUTCOME_SOURCE_INVALID')
        f=snap['hfq'].set_index('date')
        if any(pd.Timestamp(d) not in f.index for d in (origin,row['target'])):continue
        actual=float(f.loc[row['target'],'close']/f.loc[origin,'close']-1)
        put(db,'outcomes',key,dict(experiment=spec['id'],symbol=spec['symbol'],origin=origin,target=row['target'],actual=actual,
            observed_at=at.isoformat(),input_version=snap['meta']['version'],input_sha256=snap['meta']['sha256'],source='tencent'))


def joined(db,spec):
    rows=[]
    for a in values(db,'issues'):
        if a['experiment']!=spec['id']:continue
        b=get(db,'outcomes',spec['id']+'|'+a['origin'])
        if b:rows.append({**a,'actual':b['actual'],'outcome':b})
    return sorted(rows,key=lambda r:r['origin'])


def assess(rows,spec):
    p={**lc.POLICY,'minimum_mae_improvement':0.,'minimum_independent':spec['budget']['minimum_independent']}
    result=lc.predictive_metrics(rows,p)
    if result.get('sign_pvalue',1)>spec['budget']['alpha']:
        result['passed']=False;result['blockers'].append('MULTIPLE_TEST_AND_REPEAT_TRIAL_CORRECTION')
    if len(rows)!=spec['budget']['test_origins']:
        result['passed']=False;result['blockers'].append('COMPLETE_PROSPECTIVE_COHORT_REQUIRED')
    return result


def verify_rows(root,spec,rows,evaluated_at,sessions):
    origins,targets,_=timetable(spec,sessions)
    for row in rows:
        if row['origin'] not in origins or row['target']!=targets.get(row['origin']):raise ValueError('SLOT_COHORT_CHANGED')
        lower,upper=lc._window(pd.Timestamp(row['origin']),sessions)
        if not pd.Timestamp(spec['frozen_at'])<pd.Timestamp(row['issued_at']) or not lower<=pd.Timestamp(row['issued_at'])<upper:raise ValueError('SLOT_BACKDATED_PREDICTION')
        if not pd.Timestamp(row['target']).tz_localize('Asia/Shanghai')+pd.Timedelta(hours=15)<=pd.Timestamp(row['outcome']['observed_at'])<=evaluated_at:raise ValueError('SLOT_FUTURE_OUTCOME')
        for k,predicted,choice in [('candidate_fit',row['predicted'],spec['candidate']),('champion_fit',row['champion_predicted'],spec['champion'])]:
            fit=row[k];params=fit['parameters']
            if pd.Timestamp(fit['train_last_label_end'])>pd.Timestamp(row['origin']):raise ValueError('SLOT_TRAINING_LEAKAGE')
            x=np.array([fit['features'][c] for c in choice['features']])
            expected=float(params['intercept']+((x-np.array(params['means']))/np.array(params['scales']))@np.array(params['coefficients']))
            if not np.isclose(expected,predicted,rtol=1e-10,atol=1e-12):raise ValueError('SLOT_INFERENCE_CHANGED')
        lc._source_integrity(root,row)


def evaluate(root,db,spec,at,sessions):
    old=get(db,'evaluations',spec['id'])
    if old:return old
    check_spec(spec)
    rows=joined(db,spec)
    _,_,earliest=timetable(spec,sessions)
    if earliest is None or at<pd.Timestamp(earliest):
        # Only counts/timing before the frozen final date; never inspect test error early.
        return dict(status='WAITING',confirmed=len(rows),required=spec['budget']['test_origins'],earliest=earliest)
    verify_rows(root,spec,rows,at,sessions)
    result=assess(rows,spec)
    outcome=dict(status='ACCEPTED' if result['passed'] else 'REJECTED',evaluated_at=at.isoformat(),
        first_evaluation_at=earliest,spec_sha=lc.digest(spec),evidence_sha=lc.digest(rows),metrics=result,experiment=spec['id'],
        scope='PREDICTION_RECIPE_ONLY',portfolio_admission=False,probability_admission=False)
    put(db,'evaluations',spec['id'],outcome)
    if result['passed']:
        release=dict(experiment=spec['id'],slot=spec['slot'],recipe=spec['candidate'],evaluation_sha=lc.digest(outcome),released_at=at.isoformat())
        put(db,'releases',spec['id'],release)
    return outcome


def registry(root,at=None):
    at=pd.Timestamp(at or now());active={};trials={};errors=[]
    p=Path(root)/'data/iteration/slot-v2.sqlite3'
    if not p.exists():return dict(models=active,research_models=trials,errors=errors)
    with database(root) as db:
        for r in sorted(values(db,'releases'),key=lambda x:x['released_at']):
            try:
                s=get(db,'specs',r['experiment']);v=get(db,'evaluations',r['experiment']);check_spec(s)
                if pd.Timestamp(r['released_at'])>at or at-pd.Timestamp(r['released_at'])>pd.Timedelta(days=POLICY['maximum_age_days']):raise ValueError('SLOT_RELEASE_NOT_CURRENT')
                if s['config']!=config(root) or r['recipe']!=s['candidate'] or r['slot']!=s['slot']:raise ValueError('SLOT_RELEASE_BINDING_CHANGED')
                rows=joined(db,s)
                if v['status']!='ACCEPTED' or lc.digest(v)!=r['evaluation_sha'] or v['spec_sha']!=lc.digest(s) or v['evidence_sha']!=lc.digest(rows):raise ValueError('SLOT_RELEASE_PROOF_CHANGED')
                expected_time=timetable(s,lc._sessions(root,cutoff(at.to_pydatetime())))[2]
                if v.get('first_evaluation_at')!=expected_time:raise ValueError('SLOT_EVALUATION_DATE_CHANGED')
                if pd.Timestamp(v['evaluated_at'])<pd.Timestamp(v['first_evaluation_at']) or pd.Timestamp(v['evaluated_at'])>pd.Timestamp(r['released_at']):raise ValueError('SLOT_RELEASE_EARLY')
                if not assess(rows,s)['passed']:raise ValueError('SLOT_RELEASE_GATE_NOT_REPRODUCIBLE')
                verify_rows(root,s,rows,pd.Timestamp(v['evaluated_at']),lc._sessions(root,cutoff(at.to_pydatetime())))
                active[r['slot']]={**r,'scope':'PREDICTION_RECIPE_ONLY'}
            except (ValueError,KeyError,OSError) as exc:errors.append(dict(slot=r.get('slot'),error=str(exc)))
    with database(root) as db:
        for spec in values(db,'specs'):
            if not spec.get('trial_eligible') or get(db,'evaluations',spec['id']):continue
            try:
                check_spec(spec)
                if spec['config']!=config(root) or pd.Timestamp(spec['frozen_at'])>at:raise ValueError('TRIAL_CONFIG_OR_TIME_CHANGED')
                study=get(db,'studies',spec['training_asof'])
                if not study or lc.digest(study)!=spec['source_study']:raise ValueError('TRIAL_STUDY_PROOF_MISSING')
                match=next((r for r in study['studies'] if r['slot']==spec['slot']),None)
                winner=match['winner'] if match else None
                if not winner or not winner['eligible_for_future_test'] or recipe(winner['candidate'])!=spec['candidate']:raise ValueError('TRIAL_HISTORICAL_GATE_MISSING')
                trials[spec['slot']]=dict(experiment=spec['id'],slot=spec['slot'],recipe=spec['candidate'],released_at=spec['frozen_at'],
                    scope='RESEARCH_TRIAL_ONLY',independent_validated=False)
            except (ValueError,KeyError,OSError) as exc:errors.append(dict(slot=spec.get('slot'),error=str(exc)))
    return dict(models=active,research_models=trials,errors=errors)


def choice(state,symbol,horizon,default):
    entry=state.get('research_models',{}).get(f'{symbol}/{horizon}') or state['models'].get(f'{symbol}/{horizon}')
    return (entry['recipe'],entry) if entry else (default,None)


def run(root):
    from . import daily_iteration as daily
    from .data import calendar
    root=Path(root);at=pd.Timestamp(now());end=cutoff(at.to_pydatetime())
    sessions,_=calendar(root,daily.load_config(root),end,offline=True)
    asof=str(sessions[sessions<=end].max().date());cfg=config(root)
    frames,drift,persistent,errors=daily.inputs(root,sessions,asof,at)
    watch=read_watchlist(root);names=dict(zip(watch.symbol,watch.name))
    if len(watch)>50:raise ValueError('MAXIMUM_VALIDATION_UNIVERSE_50')
    assessment,ids=daily.outcome_evidence(root,at);errors.extend(assessment.get('settlement',{}).get('errors',[]))
    releases=registry(root,at);errors.extend(releases['errors']);default=fallback(root,cfg)
    progress=[];studies=[];status='ALREADY_RESEARCHED_SESSION';new=0
    with database(root) as db:
        specs=values(db,'specs')
        if len({s['symbol'] for s in specs}|set(watch.symbol))>50:raise ValueError('LIFETIME_VALIDATION_UNIVERSE_EXCEEDS_50')
        for spec in specs:
            if get(db,'evaluations',spec['id']):continue
            try:
                earliest=timetable(spec,sessions)[2]
                issue(root,db,spec,asof,at,sessions);settle(root,db,spec,at)
                evaluation=evaluate(root,db,spec,at,sessions)
                progress.append(dict(slot=spec['slot'],name=names.get(spec['symbol'],spec['symbol']),**evaluation))
            except Exception as exc:
                earliest=None
                errors.append(dict(symbol=spec['symbol'],error=str(exc)))
                progress.append(dict(slot=spec['slot'],name=names.get(spec['symbol'],spec['symbol']),status='ERROR',confirmed=len(joined(db,spec)),required=spec['budget']['test_origins'],earliest=earliest))
        pending={s['slot'] for s in values(db,'specs') if not get(db,'evaluations',s['id'])}
        checkpoint=get(db,'studies',asof)
        prior=values(db,'studies');consumed={i for s in prior for i in s['outcome_ids']}
        new=len(set(ids)-consumed)
        if not checkpoint:
            status=daily.decision(asof,at,None,False,errors,sorted(set(ids)-consumed))
            if status=='DAILY_MATURED_OUTCOMES':
                for (symbol,h),frame in sorted(frames.items()):
                    key=f'{symbol}/{h}'
                    if key in pending:continue
                    champion,_=choice(releases,symbol,h,default)
                    comparisons,winner=search(frame,h,cfg,champion)
                    studies.append(dict(slot=key,name=names.get(symbol,symbol),candidate_count=len(comparisons),comparisons=comparisons,winner=winner,champion=champion))
                checkpoint=dict(asof=asof,generated_at=at.isoformat(),version=VERSION,code=fingerprint(),studies=studies,outcome_ids=ids)
                put(db,'studies',asof,checkpoint)
        if checkpoint:
            if checkpoint['code']!=fingerprint():raise ValueError('SLOT_STUDY_CODE_CHANGED')
            studies=checkpoint['studies']
            for row in studies:
                if row['winner']:
                    symbol,h=row['slot'].split('/')
                    try:
                        spec=freeze(root,db,symbol,int(h),row['winner'],row['champion'],asof,at,sessions,cfg,lc.digest(checkpoint))
                        earliest=timetable(spec,sessions)[2]
                        if not any(x['slot']==row['slot'] for x in progress):
                            evaluation=get(db,'evaluations',spec['id']) or dict(status='WAITING',confirmed=0,required=spec['budget']['test_origins'],earliest=earliest)
                            progress.append(dict(slot=row['slot'],name=row['name'],**evaluation))
                    except Exception as exc:errors.append(dict(symbol=symbol,error=str(exc)))
        history=values(db,'evaluations');last=max(values(db,'studies'),key=lambda r:r['asof'],default=None)
    latest=registry(root,at)
    report=dict(version=VERSION,generated_at=at.isoformat(),asof=asof,status='INPUT_ERRORS' if errors else 'COMPLETE',
        policy=POLICY,schedule=daily.SCHEDULE,research_reason=status,studies=[],slot_studies=studies,slot_progress=progress,
        accepted_slots=list(latest['models']),research_trial_slots=list(latest['research_models']),update_history=history,errors=errors,submitted_orders=0,
        persistent_drift=persistent,research_progress=dict(status='INPUT_ERRORS' if errors else status,new_primary_outcomes=new,
            confirmed=assessment.get('confirmed'),primary_confirmed=assessment.get('primary_confirmed'),quality_summary=assessment.get('summary'),
            next_review_at=daily.next_review(sessions,at),next_research_at=daily.next_review(sessions,at),last_slot_study=last),
        limitations=['单组预测方案验证不等于概率、组合策略或收益验证；费用与风险准入保持原规则。',
            '前瞻逐日验证预先冻结参数方案，权重按原算法仅用当时已成熟标签更新；待验证期间不重新挑选该组方案。',
            '每组仅在完整观察窗口结束并全部到期后验收一次；缺失预测不补写，失败后必须使用新的未来样本。'])
    from .reporting.iteration_status import render
    folder=root/'reports/iteration'/at.strftime('%Y%m%d-%H%M%S-%f')
    write_json(folder/'report.json',report);write_json(root/'reports/iteration/latest.json',report)
    page='<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>模型研究进度</title><style>body{font:16px/1.7 system-ui;color:#17253a;margin:28px}td,th{padding:8px;border-bottom:1px solid #ddd;text-align:left}.scroll{overflow:auto}.bad{color:#9b3e18}</style>'+render(root,report,assessment,asof)+'</html>'
    atomic_text(folder/'index.html',page);atomic_text(root/'reports/iteration/latest/index.html',page)
    return report
