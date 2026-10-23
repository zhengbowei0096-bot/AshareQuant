"""Daily research scheduling around the unchanged, fingerprinted model gates.

Completed studies are checkpointed once per data session. Frozen prospective
experiments are evaluated daily but never re-tuned while awaiting acceptance.
"""
from pathlib import Path
import json
import hashlib
import pandas as pd
from . import iteration as research, lifecycle as lc
from .core import now, cutoff, load_config, read_snapshot, read_watchlist, write_json
from .evaluation import prediction_quality as quality


SCHEDULE = dict(mode='DAILY_MATURED_OUTCOMES',timezone='Asia/Shanghai',hour=18,
                maximum_studies_per_session=1,requires_new_primary_outcomes=True)


def load_json(path):
    return json.loads(path.read_text()) if path.exists() else {}


def next_review(sessions, at):
    """Only use the verified exchange calendar; never invent holiday dates."""
    at=quality.aware(at)
    for day in sessions:
        when=pd.Timestamp(day).tz_localize('Asia/Shanghai')+pd.Timedelta(hours=18)
        if when>at:return when.isoformat()
    return None


def outcome_evidence(root, at):
    assessment=quality.report(root,at=at)
    with quality.database(root) as db:
        predictions=dict(quality.rows(db,'q9_predictions'))
        outcomes=quality.rows(db,'q9_outcomes')
    confirmed=[]
    for key,value in outcomes:
        p=predictions.get(key)
        if p is None:raise ValueError('ORPHAN_PREDICTION_OUTCOME')
        if quality.aware(value['confirmed_at'])>at or pd.Timestamp(p['target'])>cutoff(at.to_pydatetime()):
            raise ValueError('FUTURE_PREDICTION_OUTCOME')
        if p['primary_daily']:confirmed.append(key)
    return assessment, sorted(confirmed)


def inputs(root, sessions, asof, at):
    """Same features, windows, source rules and cutoff as iteration.run."""
    frames={};drift=[];errors=[]
    days=sessions[sessions<=pd.Timestamp(asof)]
    watch=read_watchlist(root)
    if watch.empty:errors.append(dict(error='EMPTY_WATCHLIST'))
    for item in watch.itertuples():
        try:
            snap=read_snapshot(root,item.symbol)
            if snap is None or snap['raw'].date.max()!=days.max():raise ValueError('STALE_OR_MISSING_SNAPSHOT')
            if snap['meta'].get('synthetic') or snap['meta'].get('source')!='tencent' or quality.aware(snap['meta']['fetched_at'])>at:
                raise ValueError('UNVERIFIED_OR_FUTURE_SNAPSHOT')
            for horizon in (1,5,20):
                f=research.features_and_labels(snap['hfq'],days,horizon)
                frames[item.symbol,horizon]=f[f.label_end<=days.max()]
            f=research.features_and_labels(snap['hfq'],days,1)[research.FEATURES]
            for window in (0,1):
                endpos=len(f)-window*60
                for feature in research.FEATURES:
                    drift.append(dict(symbol=item.symbol,feature=feature,window=window,
                        **research.drift(f.iloc[-372:-120][feature],f.iloc[endpos-60:endpos][feature])))
        except Exception as exc:errors.append(dict(symbol=item.symbol,error=str(exc)))
    from .model_research import holm
    for row,pvalue in zip(drift,holm([r['pvalue'] for r in drift])):
        row['adjusted_pvalue']=pvalue
        row['shift']=row['psi'] is not None and row['psi']>=research.POLICY['psi_threshold'] and pvalue<=research.POLICY['family_alpha']
    shifted={(r['symbol'],r['feature'],r['window']) for r in drift if r['shift']}
    persistent=any((s,f,0) in shifted and (s,f,1) in shifted for s,_ in frames for f in research.FEATURES)
    return frames,drift,persistent,errors


def previous_study(root, state, at):
    """Read-only fallback for old reports; do not fill today's study with old rows."""
    records=[]
    for path in list((root/'data/iteration/daily_studies').glob('*.json'))+list((root/'reports/iteration').glob('*/report.json')):
        record=load_json(path)
        if record.get('studies') and quality.aware(record['generated_at'])<=at:
            records.append(record)
    if not records:return None
    record=max(records,key=lambda r:quality.aware(r['generated_at']))
    return {k:record.get(k) for k in ('generated_at','asof','studies','research_reason')}


def progress(root, state):
    pending=state.get('candidate_experiment')
    if not pending:return None
    with lc.database(root) as db:spec=lc.get(db,'specs',pending)
    if not spec or spec['runtime_hash']!=lc.runtime_hash():
        raise ValueError('FROZEN_RUNTIME_CHANGED_RESEARCH_RESTART_REQUIRED')
    lc.settle(root,spec);lc.select_portfolio(root,spec);lc.collect(root,spec)
    assessment=lc.evaluate(root,spec);release=lc.promote(root,spec,assessment)
    if not assessment['status'].startswith('WAITING'):state.pop('candidate_experiment',None)
    return dict(experiment=pending,evaluation=assessment['status'],release=release)


def decision(asof, at, checkpoint, pending, errors, new_ids):
    if errors:return 'INPUT_ERRORS'
    if pending:return 'FROZEN_VALIDATION_PENDING'
    if checkpoint:return 'ALREADY_RESEARCHED_SESSION'
    if at<pd.Timestamp(asof).tz_localize('Asia/Shanghai')+pd.Timedelta(hours=18):return 'WAIT_AFTER_CLOSE'
    if not new_ids:return 'NO_NEW_MATURED_OUTCOMES'
    return 'DAILY_MATURED_OUTCOMES'


def run(root):
    from .data import calendar
    root=Path(root);at=quality.aware(now());end=cutoff(at.to_pydatetime())
    sessions,_=calendar(root,load_config(root),end,offline=True)
    asof=str(sessions[sessions<=end].max().date())
    statepath=root/'data/iteration/state.json';state=load_json(statepath)
    checkpoint_path=root/'data/iteration/daily_studies'/f'{asof}.json'
    checkpoint=load_json(checkpoint_path)
    if checkpoint and checkpoint.get('sha256')!=lc.digest({k:v for k,v in checkpoint.items() if k!='sha256'}):
        raise ValueError('DAILY_STUDY_CHECKSUM_MISMATCH')
    frames,drift,persistent,errors=inputs(root,sessions,asof,at)
    cfg={**research.DEFAULTS,**load_json(root/'forecast_config.json')}
    assessment={};ids=[];candidate=None;studies=[];reason=None
    try:
        assessment,ids=outcome_evidence(root,at)
        # A failed outcome confirmation is visible and blocks new research.
        errors.extend(assessment.get('settlement',{}).get('errors',[]))
        candidate=progress(root,state)
    except Exception as exc:errors.append(dict(error=str(exc)))
    if checkpoint and checkpoint['runtime_hash']!=lc.runtime_hash():
        errors.append(dict(error='DAILY_STUDY_RUNTIME_CHANGED_REVIEW_REQUIRED'))
    consumed=set(state.get('daily_outcome_ids',[]))
    new_ids=sorted(set(ids)-consumed)
    status=decision(asof,at,checkpoint,state.get('candidate_experiment'),errors,new_ids)
    try:
        if status=='DAILY_MATURED_OUTCOMES':
            studies,winner=research.select_candidate(frames,cfg)
            if any(r.get('errors') for r in studies):
                errors.append(dict(error='CANDIDATE_COMPARISON_FAILED'))
                status='INPUT_ERRORS'
            else:
                reason='DAILY_MATURED_OUTCOMES'
                checkpoint=dict(generated_at=at.isoformat(),asof=asof,runtime_hash=lc.runtime_hash(),
                    research_reason=reason,studies=studies,winner=winner,outcome_ids=ids,
                    new_primary_outcomes=len(new_ids),quality_summary=assessment.get('summary'),
                    scheduler_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
                # Write first: a crash cannot trigger a second search on this date.
                checkpoint['sha256']=lc.digest(checkpoint)
                write_json(checkpoint_path,checkpoint)
        if checkpoint and not errors:
            studies=checkpoint['studies']
            state['last_research']=dict(at=checkpoint['generated_at'],asof=asof,
                reason=checkpoint['research_reason'],runtime_hash=checkpoint['runtime_hash'])
            state['daily_outcome_ids']=checkpoint['outcome_ids']
            winner=checkpoint.get('winner')
            if winner and not state.get('candidate_experiment'):
                spec=lc.prepare(root,candidate={**winner['candidate'],'expected_improvement':winner['mean_gain'],
                    'reason':checkpoint['research_reason'],'research_asof':asof})
                # Do not reopen an experiment that already has a terminal evaluation.
                with lc.database(root) as db:finished=lc.get(db,'evaluations',spec['experiment'])
                if not finished:
                    state['candidate_experiment']=spec['experiment']
                    candidate=dict(experiment=spec['experiment'],evaluation='WAITING_FOR_FIRST_FUTURE_CLOSE',release=None)
    except Exception as exc:
        errors.append(dict(error=str(exc)));status='INPUT_ERRORS'
    registry=lc.monitor(root)
    monitor=load_json(root/'data/model_monitor.json')
    following=next_review(sessions,at)
    report=dict(generated_at=at.isoformat(),asof=asof,status='INPUT_ERRORS' if errors else 'COMPLETE',
        scheduler_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        policy=research.POLICY,schedule=SCHEDULE,search_family=research.CANDIDATES,drift=drift,
        persistent_drift=persistent,persistent_degradation=any(g.get('status')=='degraded' for g in monitor.get('groups',{}).values()),
        research_reason=reason,studies=studies,candidate=candidate,errors=errors,
        research_progress=dict(status=status,new_primary_outcomes=len(new_ids),confirmed=assessment.get('confirmed'),
            primary_confirmed=assessment.get('primary_confirmed'),quality_summary=assessment.get('summary'),
            next_review_at=following,next_research_at=None if state.get('candidate_experiment') or errors else following,
            last_study=previous_study(root,state,at)),
        production_admission=lc.admission(root,registry),submitted_orders=0,
        limitations=['每日比较只使用已确认到期结果，未到期预测不计误差。',
          '每天研究不等于每天升级；历史筛选和未来独立验收门槛不变。',
          '候选验证期间每日跟踪结果，不用该验证期间的数据重新调参。'])
    with lc.database(root) as db:
        report['update_history']=[dict(version=s['experiment'],reason=(s.get('candidate') or {}).get('reason','BASELINE_FREEZE'),
            training_time=s['frozen_at'],data_end=s['training_asof'],candidate=s.get('candidate'),
            expected_improvement=(s.get('candidate') or {}).get('expected_improvement'),test_results=lc.get(db,'evaluations',s['experiment']),
            actual_improvement={k:v.get('champion_improvement') for k,v in (lc.get(db,'evaluations',s['experiment']) or {}).get('models',{}).items()} or None,
            releases=[r for r in lc.values(db,'releases') if r['experiment']==s['experiment']],rollback_events=lc.values(db,'events')) for s in lc.values(db,'specs')]
    folder=root/'reports/iteration'/at.strftime('%Y%m%d-%H%M%S-%f')
    write_json(folder/'report.json',report)
    write_json(root/'reports/iteration/latest.json',report);write_json(statepath,state)
    from .reporting.iteration_status import render
    from .core import atomic_text
    page='<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>模型研究进度</title><style>body{font:16px/1.7 system-ui;color:#17253a;margin:28px}td,th{padding:8px;border-bottom:1px solid #ddd;text-align:left}.scroll{overflow:auto}.bad{color:#9b3e18}</style>'+render(root,report,assessment,asof)+'</html>'
    atomic_text(folder/'index.html',page)
    atomic_text(root/'reports/iteration/latest/index.html',page)
    return report
