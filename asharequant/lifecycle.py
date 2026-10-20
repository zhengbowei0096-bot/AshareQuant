"""Frozen prospective experiments, one-shot acceptance, releases and revocation.

Only this collector can issue observations; historical imports cannot become
prospective evidence. Production registry flags alone never grant admission.
"""
from pathlib import Path
from datetime import datetime
import hashlib, inspect, json, math, sqlite3, sys
import numpy as np
import pandas as pd
from .core import now, cutoff, write_json, read_snapshot, read_watchlist, load_config

POLICY = dict(calibration_origins=126, test_origins=672, minimum_independent=12,
              minimum_mae_improvement=.02, minimum_direction=.52, confidence=.95,
              minimum_portfolio_sessions=120, minimum_trades=20, maximum_drawdown=.15,
              minimum_sharpe=.5, maximum_stability_l1=.10, model_max_age_days=550, portfolio_monitor_sessions=20, candidate_refresh_days=90,
              monitor_independent=12, monitor_windows=2, deterioration=.20)
TABLES = ('specs','issues','outcomes','bars','targets','selections','evaluations','releases','events','live','live_outcomes')


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()


def runtime_hash():
    from .forecast import features_and_labels, fit_predict, FEATURES
    from .validation_engine import infer
    import importlib.metadata
    return digest(dict(features=FEATURES,features_code=inspect.getsource(features_and_labels),
        estimator=inspect.getsource(fit_predict),loader=inspect.getsource(infer),
        lifecycle=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        portfolio_code={name:hashlib.sha256((Path(__file__).parent/name).read_bytes()).hexdigest() for name in ('decision.py','execution_replay.py','history_sources.py','iteration.py')},
        dependencies={p:importlib.metadata.version(p) for p in ('numpy','pandas','scipy')}))


def database(root):
    path=Path(root)/'data/market.sqlite3';path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path,timeout=30)
    for table in TABLES:
        db.execute(f'CREATE TABLE IF NOT EXISTS lc_{table} (key TEXT PRIMARY KEY, body TEXT NOT NULL, sha256 TEXT NOT NULL)')
        for action in ('UPDATE','DELETE'):
            db.execute(f"CREATE TRIGGER IF NOT EXISTS lc_{table}_{action.lower()} BEFORE {action} ON lc_{table} BEGIN SELECT RAISE(ABORT,'immutable lifecycle evidence'); END")
    return db


def put(db, table, key, body):
    encoded=json.dumps(body,sort_keys=True,ensure_ascii=False,allow_nan=False);sha=digest(body)
    old=db.execute(f'SELECT sha256 FROM lc_{table} WHERE key=?',(key,)).fetchone()
    if old:
        if old[0]!=sha: raise ValueError('IMMUTABLE_EVIDENCE_CONFLICT:'+table+':'+key)
        return False
    db.execute(f'INSERT INTO lc_{table} VALUES(?,?,?)',(key,encoded,sha));return True


def get(db, table, key):
    row=db.execute(f'SELECT body,sha256 FROM lc_{table} WHERE key=?',(key,)).fetchone()
    if not row:return None
    body=json.loads(row[0])
    if digest(body)!=row[1]:raise ValueError('EVIDENCE_HASH_MISMATCH:'+table)
    return body


def values(db, table, experiment=None):
    out=[]
    for body,sha in db.execute(f'SELECT body,sha256 FROM lc_{table} ORDER BY key'):
        value=json.loads(body)
        if digest(value)!=sha:raise ValueError('EVIDENCE_HASH_MISMATCH:'+table)
        if experiment is None or value.get('experiment')==experiment:out.append(value)
    return out


def folder(root, exp):return Path(root)/'data/lifecycle/experiments'/exp


def _artifact(root, spec, symbol, horizon):
    key=f'{symbol}/{horizon}';entry=spec['artifacts'][key]
    path=(Path(root)/entry['path']).resolve()
    if not path.is_relative_to((Path(root)/'data/lifecycle').resolve()):raise ValueError('ARTIFACT_PATH_ESCAPE')
    if hashlib.sha256(path.read_bytes()).hexdigest()!=entry['sha256']:raise ValueError('ARTIFACT_HASH_MISMATCH')
    body=json.loads(path.read_text())
    if body.get('symbol')!=symbol or body.get('horizon')!=horizon or pd.Timestamp(body['train_last_label_end'])>pd.Timestamp(spec['training_asof']):raise ValueError('ARTIFACT_SLOT_OR_TRAINING_CUTOFF_INVALID')
    return path,entry


def _sessions(root, end):
    from .data import calendar
    return calendar(root,load_config(root),pd.Timestamp(end),offline=True)[0]


def _phases(spec, sessions):
    origins=pd.DatetimeIndex(sessions);origins=origins[origins>=pd.Timestamp(spec['start_session'])]
    n=spec['policy']['calibration_origins'];gap=max(spec['horizons']);m=spec['policy']['test_origins']
    return origins[:n],origins[n:n+gap],origins[n+gap:n+gap+m]


def prepare(root, candidate=None):
    """Freeze fixed ridge estimators using already observable, matured labels.
    Previously inspected history is training/diagnostic data, never final test.
    """
    from .forecast import DEFAULTS,FEATURES,features_and_labels,fit_predict
    from .decision import DEFAULTS as D, load_fee_config
    root=Path(root);at=pd.Timestamp(now());end=cutoff(now());sessions=_sessions(root,end)
    watch=read_watchlist(root);universe=[dict(symbol=r.symbol,name=r.name,sector=getattr(r,'sector','未分类')) for r in watch.itertuples()]
    cfg={**DEFAULTS,**json.loads((root/'forecast_config.json').read_text())}
    decision={**D,**json.loads((root/'decision_config.json').read_text())};load_fee_config(root,decision)
    configuration_hash=digest(dict(forecast=cfg,portfolio=decision))
    identity=digest(universe);rh=runtime_hash();pointer=root/'data/lifecycle/current.json' if candidate is None else root/'data/iteration/candidates'/(digest(candidate)+'.json')
    if candidate is not None:
        if candidate['alpha'] not in (1.,10.,100.) or not candidate['features'] or not set(candidate['features'])<=set(FEATURES):raise ValueError('UNDECLARED_CANDIDATE')
        configuration_hash=digest(dict(base=configuration_hash,candidate=candidate))
    with database(root) as db:
        if pointer.exists():
            old=get(db,'specs',json.loads(pointer.read_text())['experiment'])
            completed=get(db,'evaluations',old['experiment']) if old else None
            refresh_due=bool(completed and (at-pd.Timestamp(completed['evaluated_at'])).days>=POLICY['candidate_refresh_days'])
            if old and old['runtime_hash']==rh and old['universe_sha256']==identity and old.get('configuration_hash')==configuration_hash and not refresh_due:
                return old
        latest=pd.DatetimeIndex(sessions)[pd.DatetimeIndex(sessions)<=end].max()
        future=pd.DatetimeIndex(sessions)
        # Start strictly after freezing, at a future session close (not retroactive).
        future=future[(future.tz_localize('Asia/Shanghai')+pd.Timedelta(hours=15))>at]
        if not len(future):raise ValueError('FUTURE_EXCHANGE_SESSION_UNAVAILABLE')
        spec=dict(candidate=candidate,configuration_hash=configuration_hash,schema=1,frozen_at=at.isoformat(),start_session=str(future[0].date()),training_asof=str(latest.date()),
            universe=universe,universe_sha256=identity,runtime_hash=rh,horizons=[1,5,20],policy=POLICY.copy(),
            portfolio_config=decision,artifacts={},input_versions={},
            universe_basis='fixed cohort observed before prospective evaluation; all members retained including future delistings',
            historical_data_is_independent_test=False)
        pending={}
        for item in universe:
            s=item['symbol'];snap=read_snapshot(root,s)
            if snap is None or snap['raw'].date.max()!=latest:raise ValueError('FREEZE_SNAPSHOT_MISSING_OR_STALE:'+s)
            if snap['meta'].get('synthetic') or snap['meta'].get('source') not in ('tencent',):raise ValueError('SYNTHETIC_OR_UNVERIFIED_MARKET_INPUT')
            if pd.Timestamp(snap['meta']['fetched_at'])>at:raise ValueError('FUTURE_INPUT_FETCH_TIME')
            spec['input_versions'][s]=snap['meta']['version']
            for h in spec['horizons']:
                f=features_and_labels(snap['hfq'],sessions[sessions<=latest],h)
                train=f.dropna(subset=FEATURES+['target','label_end']);train=train[train.label_end<=latest].tail(cfg['training_cap'])
                if len(train)<cfg['minimum_train']:raise ValueError('FREEZE_TRAINING_INSUFFICIENT:'+s)
                _,params=fit_predict(train,train.iloc[[-1]],candidate['alpha'] if candidate else cfg['ridge_alpha'],candidate['features'] if candidate else None)
                if candidate:
                    # Keep the numeric inference contract; unused features receive zero coefficients.
                    columns=candidate['features']
                    params={**params,**{field:[params[field][columns.index(c)] if c in columns else default for c in FEATURES] for field,default in [('means',0.),('scales',1.),('coefficients',0.)]}}
                key=f'{s}/{h}'
                pending[key]=dict(kind='ridge_numeric_v1',features=FEATURES,parameters=params,symbol=s,horizon=h,
                    baseline_mean=float(train.target.mean()),train_last_label_end=str(train.label_end.max().date()),
                    training_rows=len(train),input_version=snap['meta']['version'])
        # Candidate content, cohort, policy and freeze timestamp all enter identity.
        exp=digest(dict(spec=spec,models=pending))[:32];spec['experiment']=exp
        dest=folder(root,exp)
        for key,artifact in pending.items():
            path=dest/'models'/(key.replace('/','-')+'.json');write_json(path,artifact)
            spec['artifacts'][key]=dict(path=str(path.relative_to(root)),sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        if candidate:
            registry=refresh_registry(root);spec['champions']={}
            from .forecast import fit_predict
            for key in pending:
                symbol,h=key.split('/');entry=registry['models'].get(key)
                if entry:
                    prior=get(db,'specs',entry['experiment']);path,_=_artifact(root,prior,symbol,int(h));content=json.loads(path.read_text())
                    provenance=dict(release=entry['release'],status='VALIDATED_CHAMPION')
                else:
                    snap=read_snapshot(root,symbol);frame=features_and_labels(snap['hfq'],sessions[sessions<=latest],int(h))
                    train=frame.dropna(subset=FEATURES+['target','label_end']);train=train[train.label_end<=latest].tail(cfg['training_cap'])
                    _,params=fit_predict(train,train.iloc[[-1]],cfg['ridge_alpha'])
                    content=dict(kind='ridge_numeric_v1',features=FEATURES,parameters=params,symbol=symbol,horizon=int(h),train_last_label_end=str(train.label_end.max().date()))
                    provenance=dict(release=None,status='RESEARCH_BASELINE_NO_CHAMPION')
                path=dest/'champions'/(key.replace('/','-')+'.json');write_json(path,content)
                spec['champions'][key]=dict(path=str(path.relative_to(root)),sha256=hashlib.sha256(path.read_bytes()).hexdigest(),**provenance)
        put(db,'specs',exp,spec);write_json(dest/'spec.json',spec)
    write_json(pointer,dict(experiment=exp));return spec


def _window(origin,sessions):
    future=pd.DatetimeIndex(sessions);future=future[future>pd.Timestamp(origin)]
    if not len(future):raise ValueError('NEXT_SESSION_UNKNOWN')
    return pd.Timestamp(origin).tz_localize('Asia/Shanghai')+pd.Timedelta(hours=15),future[0].tz_localize('Asia/Shanghai')+pd.Timedelta(hours=9,minutes=15)


def collect(root,spec,at=None):
    from .forecast import FEATURES,features_and_labels
    from .validation_engine import infer
    from .decision import covariance,optimize,METHODS
    from .connections import market_environment
    root=Path(root);at=pd.Timestamp(at or now());end=cutoff(at.to_pydatetime());sessions=_sessions(root,end)
    day=sessions[sessions<=end].max();cal,gap,test=_phases(spec,sessions)
    if day<pd.Timestamp(spec['start_session']):return dict(status='WAITING_FOR_FIRST_FUTURE_CLOSE',issued=0)
    lower,upper=_window(day,sessions)
    if not lower<=at<upper:return dict(status='MISSED_PREOPEN_DEADLINE',issued=0)
    if runtime_hash()!=spec['runtime_hash']:raise ValueError('FROZEN_RUNTIME_CHANGED')
    exp=spec['experiment'];date=str(day.date());histories={};predictions={};risk_inputs={};issued=0
    with database(root) as db:
        for item in spec['universe']:
            s=item['symbol'];snap=read_snapshot(root,s)
            if snap is None or snap['raw'].date.max()!=day:raise ValueError('COHORT_MEMBER_MISSING_OR_STALE:'+s)
            if snap['meta'].get('synthetic') or snap['meta'].get('source') not in ('tencent',):raise ValueError('SYNTHETIC_OR_UNVERIFIED_MARKET_INPUT')
            if pd.Timestamp(snap['meta']['fetched_at'])>at:raise ValueError('FUTURE_INPUT_FETCH_TIME')
            version=snap['meta']['version'];bar=snap['raw'].iloc[-1]
            bkey=f'{exp}|{s}|{date}'
            if get(db,'bars',bkey) is None:
                put(db,'bars',bkey,dict(experiment=exp,symbol=s,date=date,observed_at=at.isoformat(),version=version,
                    source=snap['meta']['source'],bar={k:float(bar[k]) for k in ('open','close','high','low','volume')},input_sha256=snap['meta']['sha256']))
            histories[s]=snap['hfq'].set_index('date').close.pct_change(fill_method=None)
            for h in spec['horizons']:
                frame=features_and_labels(snap['hfq'],sessions[sessions<=day],h)
                risk_inputs[s,h]=dict(vol=float(frame.vol20.iloc[-1]*np.sqrt(h)),eligible=bool(float(bar.volume)>0 and snap['raw'].amount.tail(20).mean()>=load_config(root)['min_average_amount']))
                key=f'{exp}|{s}|{h}|{date}';old=get(db,'issues',key)
                if old:
                    if old['input_sha256']!=snap['meta']['sha256']:raise ValueError('PROSPECTIVE_INPUT_CHANGED_AFTER_ISSUE')
                    predictions[(s,h)]=old['predicted'];continue
                future=sessions[sessions>day]
                if len(future)<h:raise ValueError('TARGET_SESSION_UNKNOWN')
                frame=features_and_labels(snap['hfq'],sessions[sessions<=day],h);x=frame.iloc[[-1]]
                if x[FEATURES].isna().any().any():raise ValueError('PIT_FEATURES_INCOMPLETE:'+s)
                path,entry=_artifact(root,spec,s,h);params=json.loads(path.read_text());p=float(infer(path,entry['sha256'],x)[0])
                observation=dict(experiment=exp,symbol=s,horizon=h,origin=date,target=str(future[h-1].date()),issued_at=at.isoformat(),
                    predicted=p,baseline_mean=params['baseline_mean'],baseline_zero=0.,artifact_sha256=entry['sha256'],
                    features={k:float(x[k].iloc[0]) for k in FEATURES},input_version=version,input_sha256=snap['meta']['sha256'],source=snap['meta']['source'])
                if spec.get('candidate'):
                    champion=spec['champions'][f'{s}/{h}']
                    observation['champion_predicted']=float(infer(root/champion['path'],champion['sha256'],x)[0])
                put(db,'issues',key,observation);issued+=1;predictions[(s,h)]=p
        h=spec['portfolio_config']['horizon'];symbols=[x['symbol'] for x in spec['universe']]
        f=pd.concat([histories[s].loc[:day].rename(s) for s in symbols],axis=1).dropna().tail(spec['portfolio_config']['covariance_days'])
        try:cov=covariance(f.to_numpy(),spec['portfolio_config']['minimum_covariance_days'],h)
        except ValueError:return dict(status='ISSUED_COVARIANCE_BLOCKED',issued=issued)
        c=spec['portfolio_config'];env=market_environment(root,date)['state'];cap=min(c['max_invested'],c['market_exposure'][env])
        mu=np.array([predictions[s,h] for s in symbols]);eligible=mu>0
        selection=get(db,'selections',exp)
        if day in test:
            from .decision import research_blockers
            from .probability_calibration import predict as predict_probability
            forecasters=selection.get('forecasters',{}) if selection else {}
            flags=[]
            for symbol,item in zip(symbols,spec['universe']):
                fitted=forecasters.get(symbol)
                if not fitted:flags.append(False);continue
                p=predictions[symbol,h];vol=risk_inputs[symbol,h]['vol'];q=fitted['residual80']
                f=dict(predicted_return=p,up_probability_empirical=float(predict_probability(fitted['probability_model'],np.array([p]))[0]),volatility_horizon=vol,downside_es95_normal_proxy=min(0.,p-2.063*vol),interval80_empirical=[p+q[0],p+q[1]],validation={'preliminary_screen_passed':True})
                row=dict(symbol=symbol,sector=item['sector'],data_eligible=risk_inputs[symbol,h]['eligible'],forecast=f)
                flags.append(not research_blockers(row,c))
            eligible=np.array(flags,dtype=bool)
        sectors=[x['sector'] for x in spec['universe']]
        index=list(sessions[sessions>=pd.Timestamp(spec['start_session'])]).index(day)
        for method in METHODS:
            key=f'{exp}|{method}|{date}'
            if get(db,'targets',key):continue
            weights=optimize(method,mu,cov,sectors,c,eligible);weights*=min(1.,cap/max(weights.sum(),1e-12))
            sensitivity=None
            if index%21==0:
                perturbed=optimize(method,mu*1.01,cov*1.05,sectors,c,eligible);perturbed*=min(1.,cap/max(perturbed.sum(),1e-12))
                sensitivity=float(abs(weights-perturbed).sum())
            put(db,'targets',key,dict(experiment=exp,method=method,origin=date,issued_at=at.isoformat(),
                weights=dict(zip(symbols,map(float,weights))),environment=env,covariance_last_date=str(f.index.max().date()),sensitivity=sensitivity,
                issues_sha256=digest([get(db,'issues',f'{exp}|{s}|{h}|{date}') for s in symbols])))
    return dict(status='PROSPECTIVE_SHADOW_ISSUED',issued=issued)


def settle(root,spec,at=None):
    """Seal first observable outcomes, never overwrite with later revised prices."""
    at=pd.Timestamp(at or now());count=0;cache={};exp=spec['experiment']
    with database(root) as db:
        for row in values(db,'issues',exp):
            key=f"{exp}|{row['symbol']}|{row['horizon']}|{row['origin']}"
            if get(db,'outcomes',key):continue
            if at<pd.Timestamp(row['target']).tz_localize('Asia/Shanghai')+pd.Timedelta(hours=15):continue
            s=row['symbol']
            if s not in cache:cache[s]=read_snapshot(Path(root),s)
            snap=cache[s]
            if snap is None or snap['meta'].get('synthetic') or snap['meta'].get('source')!='tencent' or pd.Timestamp(snap['meta']['fetched_at'])>at:continue
            frame=snap['hfq'].set_index('date')
            if any(pd.Timestamp(d) not in frame.index for d in (row['origin'],row['target'])):continue
            actual=float(frame.loc[row['target'],'close']/frame.loc[row['origin'],'close']-1)
            put(db,'outcomes',key,dict(experiment=exp,symbol=s,horizon=row['horizon'],origin=row['origin'],target=row['target'],
                actual=actual,observed_at=at.isoformat(),input_version=snap['meta']['version'],input_sha256=snap['meta']['sha256'],source=snap['meta']['source']))
            count+=1
    return count


def _joined(db,spec,symbol,horizon,days):
    exp=spec['experiment'];out=[]
    for day in days:
        key=f'{exp}|{symbol}|{horizon}|{str(day.date())}'
        issue=get(db,'issues',key);actual=get(db,'outcomes',key)
        if issue is None or actual is None:continue
        out.append({**issue,'actual':actual['actual'],'outcome':actual})
    return out


def predictive_metrics(rows,policy):
    """Chronological nonoverlap pairs, conservative paired confidence interval."""
    independent=[];last=pd.Timestamp.min
    for r in sorted(rows,key=lambda r:r['origin']):
        if pd.Timestamp(r['origin'])>last:independent.append(r);last=pd.Timestamp(r['target'])
    n=len(independent)
    if not n:return dict(passed=False,independent=0,blockers=['NO_INDEPENDENT_OUTCOMES'])
    a=np.array([r['actual'] for r in independent]);p=np.array([r['predicted'] for r in independent]);b=np.array([r['baseline_mean'] for r in independent])
    mae=float(abs(a-p).mean());base=min(float(abs(a).mean()),float(abs(a-b).mean()))
    # Compare against both frozen simple baselines; winner not chosen as model.
    delta=np.minimum(abs(a),abs(a-b))-abs(a-p)
    from scipy.stats import t
    ci=float(delta.mean()-t.ppf(.975,n-1)*delta.std(ddof=1)/np.sqrt(n)) if n>1 else -1e10
    from .model_research import sign_pvalue
    champion_improvement=None
    champion_failed=False
    if any('champion_predicted' in r for r in independent):
        if not all('champion_predicted' in r for r in independent):raise ValueError('INCOMPLETE_CHAMPION_PAIRS')
        champion=np.array([r['champion_predicted'] for r in independent]);cb=float(abs(a-champion).mean())
        champion_improvement=1-mae/max(cb,1e-12)
        delta=np.minimum(delta,abs(a-champion)-abs(a-p))
        ci=float(delta.mean()-t.ppf(.975,n-1)*delta.std(ddof=1)/np.sqrt(n)) if n>1 else -1e10
        champion_failed=champion_improvement<policy['minimum_mae_improvement'] or ((a>0)==(p>0)).mean()<((a>0)==(champion>0)).mean()
    sign=sign_pvalue(delta)
    hit=float(((a>0)==(p>0)).mean());z=1.96
    lower=(hit+z*z/(2*n)-z*np.sqrt(hit*(1-hit)/n+z*z/(4*n*n)))/(1+z*z/n)
    improvement=1-mae/max(base,1e-12);reasons=[]
    if champion_failed:reasons.append('CHAMPION_IMPROVEMENT_NOT_PROVEN')
    if n<policy['minimum_independent']:reasons.append('INDEPENDENT_SAMPLE_COUNT')
    if improvement<policy['minimum_mae_improvement'] or ci<=0:reasons.append('PAIRED_BASELINE_IMPROVEMENT_NOT_CONFIDENT')
    if hit<policy['minimum_direction'] or lower<=.5:reasons.append('DIRECTION_NOT_CONFIDENT')
    return dict(passed=not reasons,independent=n,observations=len(rows),mae=mae,baseline_mae=base,mae_improvement=improvement,
        champion_improvement=champion_improvement,sign_pvalue=sign,rmse=float(np.sqrt(np.mean((a-p)**2))),direction=hit,direction_wilson_lower=lower,paired_error_gain_lower95=ci,blockers=reasons)


def _replay(root,db,spec,origins,through):
    """Replay only issued targets and first-observed raw bars; no backfilled PIT bars."""
    from .history_sources import execution_states
    from .execution_replay import simulate
    exp=spec['experiment'];symbols=[r['symbol'] for r in spec['universe']]
    if not len(origins):return dict(status='BLOCKED',blockers=['NO_PROSPECTIVE_ORIGINS'])
    sessions=_sessions(root,through);sessions=sessions[(sessions>=origins.min())&(sessions<=pd.Timestamp(through))]
    states,proof,errors=execution_states(root,symbols,sessions)
    if errors:return dict(status='BLOCKED',blockers=errors,history=proof)
    raw=values(db,'bars',exp);bars={}
    for s in symbols:
        items=[dict(date=pd.Timestamp(r['date']),**r['bar']) for r in raw if r['symbol']==s and pd.Timestamp(r['date']) in sessions]
        if len(items)!=len(sessions):return dict(status='BLOCKED',blockers=['FIRST_OBSERVED_BAR_COVERAGE:'+s])
        bars[s]=pd.DataFrame(items).set_index('date')
        for day in sessions:
            state=states[s,str(day.date())];reference=state.get('reference_ohlc')
            if state['status']=='trading' and (not reference or any(abs(float(bars[s].loc[day,k])-float(reference[k]))>.011 for k in ('open','close','high','low'))):return dict(status='BLOCKED',blockers=['HISTORICAL_PRICE_SOURCE_CONFLICT:'+s])
    targets=values(db,'targets',exp);result={};c=spec['portfolio_config']
    for method in ('equal_weight','risk_parity','mean_variance','constrained_score'):
        selected=[r for r in targets if r['method']==method and pd.Timestamp(r['origin']) in origins]
        if len(selected)!=len(origins):return dict(status='BLOCKED',blockers=['ISSUED_TARGET_COVERAGE:'+method])
        signals={r['origin']:r['weights'] for r in selected}
        replay=simulate(sessions,bars,signals,states,c)
        replay['cost_stress']=simulate(sessions,bars,signals,states,{**c,'commission_bps':c['commission_bps']*2,'slippage_bps':c['slippage_bps']*3})['summary']
        probes=[r['sensitivity'] for r in selected if r['sensitivity'] is not None]
        replay['stability']=max(probes) if probes else None
        result[method]=replay
    return dict(status='COMPLETE',results=result,history=proof,raw_bars_sha256=digest(raw),targets_sha256=digest(targets))


def select_portfolio(root,spec,at=None):
    """Choose using calibration only, lock before first final-test signal."""
    at=pd.Timestamp(at or now());end=cutoff(at.to_pydatetime());sessions=_sessions(root,end)
    cal,gap,test=_phases(spec,sessions);exp=spec['experiment']
    if not len(test) or end<test[0]:return None
    with database(root) as db:
        old=get(db,'selections',exp)
        if old:return old
        lower,upper=_window(test[0],sessions)
        if not lower<=at<upper:
            selected=dict(experiment=exp,status='BLOCKED',method=None,blockers=['SELECTION_DEADLINE_MISSED'],decided_at=at.isoformat())
        else:
            replay=_replay(root,db,spec,cal,cal[-1])
            method=None
            if replay['status']=='COMPLETE':
                good={m:r for m,r in replay['results'].items() if r['summary']['trades']>0 and r['coverage']>=.99}
                if good:method=max(good,key=lambda m:good[m]['summary']['net_return'])
            selected=dict(experiment=exp,status='LOCKED' if method else 'BLOCKED',method=method,decided_at=at.isoformat(),
                validation_last_date=str(cal[-1].date()),first_test_origin=str(test[0].date()),validation=replay,
                blockers=[] if method else ['VALIDATION_EXECUTION_EVIDENCE_INCOMPLETE'])
        selected['forecasters']={}
        from .probability_calibration import fit
        horizon=spec['portfolio_config']['horizon']
        for item in spec['universe']:
            rows=_joined(db,spec,item['symbol'],horizon,cal)
            if len(rows)!=len(cal) or len(rows)<40:continue
            data=pd.DataFrame(rows)
            model=fit(data);residual=np.array([r['actual']-r['predicted'] for r in rows])
            selected['forecasters'][item['symbol']]=dict(probability_model=model,residual80=np.quantile(residual,[.1,.9]).tolist(),calibration_rows=len(rows))
        put(db,'selections',exp,selected);return selected


def evaluate(root,spec,at=None):
    """A single final-test examination per frozen experiment, never early peeking."""
    at=pd.Timestamp(at or now());end=cutoff(at.to_pydatetime());sessions=_sessions(root,end)
    cal,gap,test=_phases(spec,sessions);exp=spec['experiment'];policy=spec['policy']
    with database(root) as db:
        old=get(db,'evaluations',exp)
        if old:return old
        if len(test)<policy['test_origins'] or test[-1]>end:
            return dict(status='WAITING_FOR_UNTOUCHED_TEST',experiment=exp,available_origins=int(sum(test<=end)),required_origins=policy['test_origins'])
        future=sessions[sessions>test[-1]]
        if len(future)<max(spec['horizons']) or at<future[max(spec['horizons'])-1].tz_localize('Asia/Shanghai')+pd.Timedelta(hours=15):
            return dict(status='WAITING_FOR_LABEL_MATURITY',experiment=exp)
        models={};evidence=[];coverage=True
        for item in spec['universe']:
            for h in spec['horizons']:
                key=f"{item['symbol']}/{h}";rows=_joined(db,spec,item['symbol'],h,test)
                if len(rows)!=len(test):coverage=False
                models[key]=predictive_metrics(rows,policy)
                if len(rows)!=len(test):models[key].update(passed=False,blockers=models[key]['blockers']+['COMPLETE_COHORT_OUTCOMES_REQUIRED'])
                calibration=_joined(db,spec,item['symbol'],h,cal)
                models[key]['probability']=_probability(calibration,rows,h)
                evidence+=rows
        from .model_research import holm
        adjusted=holm([m.get('sign_pvalue',1.) for m in models.values()])
        for assessment,pvalue in zip(models.values(),adjusted):
            assessment['holm_pvalue']=pvalue
            if pvalue>.05:assessment['passed']=False;assessment['blockers'].append('FAMILYWISE_SIGN_TEST_FAILED')
        choice=get(db,'selections',exp);portfolio=dict(passed=False,blockers=['VALIDATION_SELECTION_NOT_LOCKED'])
        if choice and choice['status']=='LOCKED':
            replay=_replay(root,db,spec,test,future[0]);method=choice['method'];reasons=[]
            if replay['status']!='COMPLETE':reasons=replay['blockers']
            else:
                r=replay['results'][method];summary=r['summary'];bench=replay['results']['equal_weight']['summary']
                if summary['sessions']<policy['minimum_portfolio_sessions'] or summary['trades']<policy['minimum_trades']:reasons.append('PORTFOLIO_SAMPLE_COUNT')
                if r['coverage']<.99:reasons.append('EXECUTION_COVERAGE')
                if summary['max_drawdown'] < -policy['maximum_drawdown']:reasons.append('DRAWDOWN_LIMIT')
                if summary.get('sharpe_zero_rf') is None or summary['sharpe_zero_rf']<policy['minimum_sharpe']:reasons.append('SHARPE_BELOW_GATE')
                if summary['net_return']<=0 or summary['net_return']<bench['net_return'] or r['cost_stress']['net_return']<=0:reasons.append('NET_OR_STRESSED_BENCHMARK_RETURN')
                if r['stability'] is None or r['stability']>policy['maximum_stability_l1']:reasons.append('WEIGHT_STABILITY')
                used={s for target in values(db,'targets',exp) if target['method']==method and pd.Timestamp(target['origin']) in test for s,w in target['weights'].items() if w>1e-10}
                if any(not models[f"{s}/{spec['portfolio_config']['horizon']}"]['passed'] for s in (r['symbol'] for r in spec['universe'])):reasons.append('UNQUALIFIED_MODEL_IN_FROZEN_COHORT')
                if any(not models[f"{s}/{spec['portfolio_config']['horizon']}"]['probability']['independent_validated'] for s in (r['symbol'] for r in spec['universe'])):reasons.append('INDEPENDENT_PROBABILITY_CALIBRATION_REQUIRED')
            portfolio=dict(passed=not reasons,method=method,blockers=reasons,replay=replay)
        result=dict(experiment=exp,status='ACCEPTED' if coverage and portfolio['passed'] else 'REJECTED_OR_PARTIAL',evaluated_at=at.isoformat(),
            models=models,portfolio=portfolio,test_first=str(test[0].date()),test_last=str(test[-1].date()),
            evidence_sha256=digest(evidence),runtime_hash=spec['runtime_hash'],spec_sha256=digest(spec))
        put(db,'evaluations',exp,result);write_json(folder(root,exp)/'final_evaluation.json',result)
        return result


def _probability(calibration,rows,horizon):
    from .probability_calibration import fit,predict,independent_dates
    if not calibration or not rows:return dict(independent_validated=False,status='INSUFFICIENT_CALIBRATION')
    def frame(rs):return pd.DataFrame([dict(date=pd.Timestamp(r['origin']),label_end=pd.Timestamp(r['target']),actual=r['actual'],predicted=r['predicted'],symbol=r['symbol']) for r in rs])
    # Training may contain overlapping labels; all mature before the embargoed test.
    # Reliability is judged only on nonoverlapping final-test dates.
    train=frame(calibration);test=independent_dates(frame(rows))
    if len(train)<40 or len(test)<30:return dict(independent_validated=False,status='INSUFFICIENT_INDEPENDENT_BLOCKS',train_rows=len(train),train_blocks=len(independent_dates(train)),test_blocks=len(test))
    model=fit(train);p=predict(model,test.predicted.to_numpy());y=(test.actual.to_numpy()>0).astype(float)
    brier=float(np.mean((p-y)**2));base=float(np.mean((model['base_rate']-y)**2));ece=0.;bins=[]
    for i in range(10):
        mask=(p>=i/10)&((p<(i+1)/10) if i<9 else (p<=1))
        if mask.any():
            a=float(p[mask].mean());b=float(y[mask].mean());n=int(mask.sum());ece+=n/len(p)*abs(a-b);bins.append(dict(n=n,predicted=a,observed=b))
    from scipy.stats import t
    gain=(model['base_rate']-y)**2-(p-y)**2
    lower=float(gain.mean()-t.ppf(.975,len(gain)-1)*gain.std(ddof=1)/np.sqrt(len(gain)))
    good=brier<base and lower>0 and ece<=.1 and model['coefficients'][1]>0
    return dict(independent_validated=bool(good),status='VALIDATED' if good else 'FAILED',model=model,brier=brier,brier_gain_lower95=lower,baseline_brier=base,ece=ece,bins=bins,train_rows=len(train),train_blocks=len(independent_dates(train)),test_blocks=len(test))


def _source_integrity(root, row):
    import re
    for value in (row,row.get('outcome',{})):
        version=value.get('input_version')
        if not isinstance(version,str) or not re.fullmatch(r'[a-f0-9]{32}',version):raise ValueError('SOURCE_VERSION_INVALID')
        if value.get('source')!='tencent':raise ValueError('SOURCE_NOT_VERIFIED')
        if set(value.get('input_sha256',{}))!={'raw','hfq'}:raise ValueError('SOURCE_HASH_BINDINGS_MISSING')
        for kind,sha in value['input_sha256'].items():
            path=Path(root)/'data/normalized'/row['symbol']/'versions'/version/(kind+'.csv')
            if hashlib.sha256(path.read_bytes()).hexdigest()!=sha:raise ValueError('SEALED_INPUT_SOURCE_CHANGED')
    return True


def _release_valid(root,db,release,full=True):
    spec=get(db,'specs',release['experiment']);evaluation=get(db,'evaluations',release['experiment'])
    if evaluation and (evaluation.get('status')!='ACCEPTED' or not evaluation.get('portfolio',{}).get('passed')):raise ValueError('FULL_COST_RISK_STABILITY_ACCEPTANCE_REQUIRED')
    if not spec or not evaluation or runtime_hash()!=spec['runtime_hash']:raise ValueError('RELEASE_RUNTIME_OR_EVIDENCE_MISSING')
    if digest(evaluation)!=release['evaluation_sha256'] or digest(spec)!=evaluation['spec_sha256']:raise ValueError('RELEASE_EVIDENCE_HASH_MISMATCH')
    if pd.Timestamp(spec['frozen_at'])>=pd.Timestamp(evaluation['test_first']).tz_localize('Asia/Shanghai'):raise ValueError('RETROSPECTIVE_TEST_NOT_ALLOWED')
    if pd.Timestamp(release['released_at'])>pd.Timestamp(now()):raise ValueError('FUTURE_RELEASE_TIME')
    if pd.Timestamp(evaluation['evaluated_at'])>pd.Timestamp(now()):raise ValueError('FUTURE_RELEASE_EVIDENCE')
    if (pd.Timestamp(now())-pd.Timestamp(evaluation['evaluated_at'])).days>spec['policy']['model_max_age_days']:raise ValueError('RELEASE_EXPIRED')
    events=values(db,'events')
    if any(e.get('release')==release['release'] and e['action']=='REVOKE' for e in events):raise ValueError('RELEASE_REVOKED')
    for key in release['models']:
        s,h=key.split('/');_artifact(root,spec,s,int(h))
        if not evaluation['models'][key]['passed']:raise ValueError('MODEL_NOT_PASSED')
    if full:
        sessions=_sessions(root,cutoff(now()));_,_,test=_phases(spec,sessions);evidence=[];checks={}
        if len(test)!=spec['policy']['test_origins'] or str(test[0].date())!=evaluation['test_first'] or str(test[-1].date())!=evaluation['test_last']:raise ValueError('FROZEN_TEST_COHORT_CHANGED')
        for item in spec['universe']:
            for h in spec['horizons']:
                rows=_joined(db,spec,item['symbol'],h,test);key=f"{item['symbol']}/{h}"
                if len(rows)!=len(test):raise ValueError('RELEASE_OUTCOME_COVERAGE_INCOMPLETE')
                for row in rows:
                    if spec.get('candidate'):
                        from .validation_engine import infer
                        champion=spec['champions'][key];path=(Path(root)/champion['path']).resolve()
                        if not path.is_relative_to((Path(root)/'data/lifecycle').resolve()):raise ValueError('CHAMPION_PATH_ESCAPE')
                        expected=float(infer(path,champion['sha256'],pd.DataFrame([row['features']]))[0])
                        if not np.isclose(expected,row.get('champion_predicted',np.nan),rtol=1e-10,atol=1e-12):raise ValueError('CHAMPION_EVIDENCE_CHANGED')
                    lower,upper=_window(pd.Timestamp(row['origin']),sessions)
                    if not pd.Timestamp(spec['frozen_at'])<=pd.Timestamp(row['issued_at']) or not lower<=pd.Timestamp(row['issued_at'])<upper:raise ValueError('BACKDATED_RELEASE_PREDICTION')
                    target_close=pd.Timestamp(row['target']).tz_localize('Asia/Shanghai')+pd.Timedelta(hours=15)
                    if not target_close<=pd.Timestamp(row['outcome']['observed_at'])<=pd.Timestamp(evaluation['evaluated_at']):raise ValueError('OUTCOME_OBSERVATION_CHRONOLOGY_INVALID')
                    if row['artifact_sha256']!=spec['artifacts'][key]['sha256']:raise ValueError('ISSUED_MODEL_HASH_CHANGED')
                    _source_integrity(root,row)
                checks[key]=predictive_metrics(rows,spec['policy']);evidence+=rows
        from .model_research import holm
        adjusted=holm([m.get('sign_pvalue',1.) for m in checks.values()])
        for (key,metrics),pvalue in zip(checks.items(),adjusted):
            if key in release['models'] and (not metrics['passed'] or pvalue>.05):raise ValueError('RELEASE_PREDICTIVE_GATE_NOT_REPRODUCIBLE')
        if digest(evidence)!=evaluation['evidence_sha256']:raise ValueError('RELEASE_EVIDENCE_ROWS_CHANGED')
        if release.get('portfolio_method'):
            if not evaluation['portfolio']['passed'] or release['portfolio_method']!=evaluation['portfolio']['method']:raise ValueError('PORTFOLIO_RELEASE_NOT_PASSED')
            selection=get(db,'selections',spec['experiment'])
            if not selection or selection.get('method')!=release['portfolio_method'] or pd.Timestamp(selection['validation_last_date'])>=pd.Timestamp(evaluation['test_first']):raise ValueError('PORTFOLIO_SELECTION_LEAKAGE')
            proof=evaluation['portfolio']['replay']['history'];p=Path(root)/proof['manifest_path']
            if digest(json.loads(p.read_text()))!=proof['manifest_sha256']:raise ValueError('PORTFOLIO_HISTORY_PROOF_CHANGED')
            from .history_sources import verified
            for ref in json.loads(p.read_text()).get('references',[]):verified(root,ref)
    return spec,evaluation


def promote(root,spec,evaluation):
    if evaluation.get('status') in ('WAITING_FOR_UNTOUCHED_TEST','WAITING_FOR_LABEL_MATURITY'):return None
    if evaluation.get('status')!='ACCEPTED' or not evaluation.get('portfolio',{}).get('passed'):return None
    approved=[k for k,m in evaluation['models'].items() if m['passed']]
    if not approved:return None
    release=dict(experiment=spec['experiment'],released_at=now().isoformat(),evaluation_sha256=digest(evaluation),models=approved,
        portfolio_method=evaluation['portfolio'].get('method') if evaluation['portfolio']['passed'] else None)
    release['release']=digest(release)[:32]
    with database(root) as db:
        old=next((r for r in values(db,'releases') if r['experiment']==spec['experiment']),None)
        if old:return old
        _release_valid(root,db,release);put(db,'releases',release['release'],release)
        put(db,'events',release['release']+'|PROMOTE',dict(action='PROMOTE',release=release['release'],experiment=spec['experiment'],at=now().isoformat()))
    refresh_registry(root);return release


def refresh_registry(root):
    root=Path(root);models={};portfolio=None;errors=[]
    with database(root) as db:
        for release in sorted(values(db,'releases'),key=lambda r:r['released_at']):
            try:spec,evaluation=_release_valid(root,db,release)
            except (ValueError,KeyError,OSError) as exc:errors.append(dict(release=release['release'],reason=str(exc)));continue
            for key in release['models']:models[key]=dict(release=release['release'],experiment=release['experiment'],artifact=spec['artifacts'][key],evaluation_sha256=release['evaluation_sha256'])
            if release['portfolio_method']:portfolio=dict(release=release['release'],experiment=release['experiment'],method=release['portfolio_method'],horizon=spec['portfolio_config']['horizon'],universe_sha256=spec['universe_sha256'],portfolio_config=spec['portfolio_config'])
    monitor=None
    if portfolio:
        # A newer prediction-only release must not silently replace the models
        # whose signals validated the active portfolio method.
        with database(root) as db:
            pinned=get(db,'releases',portfolio['release']);frozen=get(db,'specs',pinned['experiment'])
            horizon=portfolio['horizon']
            for item in frozen['universe']:
                key=f"{item['symbol']}/{horizon}"
                models[key]=dict(release=pinned['release'],experiment=pinned['experiment'],artifact=frozen['artifacts'][key],evaluation_sha256=pinned['evaluation_sha256'])
        with database(root) as db:
            checks=[e for e in values(db,'events') if e['action']=='PORTFOLIO_MONITOR' and e['release']==portfolio['release']]
            if checks:monitor=max(checks,key=lambda e:e['date'])
    state=dict(portfolio_monitor=monitor,schema=1,generated_at=now().isoformat(),models=models,portfolio=portfolio,invalid_releases=errors)
    write_json(root/'data/production_registry.json',state);return state


def admission(root,registry=None):
    """Inspect immutable release evidence rather than trusting a JSON active flag."""
    root=Path(root)
    if not (root/'data/market.sqlite3').exists():return dict(status='BLOCKED',formal_model=None,formal_method=None,blockers=['NO_VALIDATED_RELEASE'],submitted_orders=0)
    state=registry or refresh_registry(root);reasons=[]
    if registry is not None:
        with database(root) as db:
            for key,entry in state.get('models',{}).items():
                release=get(db,'releases',entry['release'])
                if not release or key not in release['models']:raise ValueError('REGISTRY_MODEL_NOT_RELEASED')
                _release_valid(root,db,release,full=False)
            if state.get('portfolio'):
                entry=state['portfolio'];release=get(db,'releases',entry['release'])
                if not release or release.get('portfolio_method')!=entry['method']:raise ValueError('REGISTRY_PORTFOLIO_NOT_RELEASED')
                frozen,_=_release_valid(root,db,release,full=False)
                if entry['portfolio_config']!=frozen['portfolio_config'] or entry['universe_sha256']!=frozen['universe_sha256'] or entry['horizon']!=frozen['portfolio_config']['horizon']:raise ValueError('PORTFOLIO_RELEASE_BINDING_CHANGED')
                if any(state['models'].get(f"{item['symbol']}/{entry['horizon']}",{}).get('release')!=entry['release'] for item in frozen['universe']):raise ValueError('MODEL_PORTFOLIO_LINEAGE_MISMATCH')
    if not state['models']:reasons.append('NO_VALIDATED_MODEL_RELEASE')
    if not state['portfolio']:reasons.append('NO_VALIDATED_PORTFOLIO_RELEASE')
    if state.get('portfolio_monitor',{} ) and state['portfolio_monitor']['status']!='VALID':reasons.append('PORTFOLIO_FORWARD_MONITOR_INCOMPLETE')
    if state['portfolio']:
        current=[dict(symbol=r.symbol,name=r.name,sector=getattr(r,'sector','未分类')) for r in read_watchlist(root).itertuples()]
        if digest(current)!=state['portfolio']['universe_sha256']:reasons.append('OBSERVATION_COHORT_CHANGED')
        from .decision import DEFAULTS as D,load_fee_config
        current_config={**D,**json.loads((root/'decision_config.json').read_text())};load_fee_config(root,current_config)
        frozen=state['portfolio']['portfolio_config']
        keys=('horizon','max_stock','max_sector','max_invested','max_turnover','max_cost_fraction','risk_aversion','commission_bps','minimum_commission','transfer_bps','sell_tax_bps','slippage_bps')
        if any(current_config[k]!=frozen[k] for k in keys):reasons.append('PORTFOLIO_POLICY_CHANGED')
    return dict(status='VALIDATED' if not reasons else 'BLOCKED',formal_model='immutable_registered_numeric_artifacts' if state['models'] else None,
        formal_method=state['portfolio']['method'] if state['portfolio'] and not reasons else None,blockers=reasons,models=len(state['models']),submitted_orders=0)


def load_prediction(root,symbol,horizon,features,registry=None):
    from .validation_engine import infer
    from .probability_calibration import predict
    root=Path(root);state=registry or refresh_registry(root);entry=state['models'].get(f'{symbol}/{horizon}')
    if not entry:return None
    with database(root) as db:
        release=get(db,'releases',entry['release']);spec,evaluation=_release_valid(root,db,release,full=False)
        if f'{symbol}/{horizon}' not in release['models']:raise ValueError('MODEL_SLOT_NOT_RELEASED')
        path,artifact=_artifact(root,spec,symbol,horizon);p=float(infer(path,artifact['sha256'],features)[0]);assessment=evaluation['models'][f'{symbol}/{horizon}'];cal=assessment['probability']
        # Calibration-only residuals provide a version-bound interval; test data never refits it.
        sessions=_sessions(root,cutoff(now()));calibration,_,_=_phases(spec,sessions);rows=_joined(db,spec,symbol,horizon,calibration)
        residual=np.array([r['actual']-r['predicted'] for r in rows]);lo,hi=np.quantile(residual,[.1,.9])
        prob=float(predict(cal['model'],np.array([p]))[0]) if cal['independent_validated'] else None
        return dict(model='registered_ridge',predicted_return=p,model_status='VALIDATED_REGISTERED',release_id=release['release'],artifact_sha256=artifact['sha256'],
            up_probability_calibrated=prob,probability_calibration=cal,up_probability_empirical=None,
            probability_method='Independent prospective calibration/test' if prob is not None else 'Unavailable: independent calibration did not pass',
            interval80_empirical=[float(p+lo),float(p+hi)],validation={**assessment,'preliminary_screen_passed':True},formal_qualification=admission(root,state))


def record_live(root,symbol,horizon,origin,target,prediction,version,input_sha256=None):
    at=pd.Timestamp(now());sessions=_sessions(root,origin);lower,upper=_window(pd.Timestamp(origin),sessions)
    if not lower<=at<upper:raise ValueError('MISSED_PRODUCTION_PREOPEN_DEADLINE')
    with database(root) as db:
        key=f"{prediction['release_id']}|{symbol}|{horizon}|{origin}"
        old=get(db,'live',key)
        if old:
            if old['predicted']!=prediction['predicted_return'] or old.get('input_sha256')!=input_sha256:raise ValueError('PRODUCTION_ISSUANCE_ALREADY_SEALED')
            return
        release=get(db,'releases',prediction['release_id'])
        if not release or pd.Timestamp(release['released_at'])>at:raise ValueError('PRODUCTION_RELEASE_NOT_YET_AVAILABLE')
        put(db,'live',key,dict(release=prediction['release_id'],symbol=symbol,horizon=horizon,origin=origin,target=target,issued_at=at.isoformat(),
            predicted=prediction['predicted_return'],baseline_mean=0.,input_version=version,input_sha256=input_sha256))


def monitor(root):
    """Mature production predictions, revoke persistent degradation, fall back only to validated releases."""
    with database(root) as db:
        for row in values(db,'live'):
            key=f"{row['release']}|{row['symbol']}|{row['horizon']}|{row['origin']}"
            if get(db,'live_outcomes',key) or pd.Timestamp(now())<pd.Timestamp(row['target']).tz_localize('Asia/Shanghai')+pd.Timedelta(hours=15):continue
            snap=read_snapshot(Path(root),row['symbol'])
            if snap is None or pd.Timestamp(snap['meta']['fetched_at'])>pd.Timestamp(now()):continue
            f=snap['hfq'].set_index('date')
            if all(pd.Timestamp(d) in f.index for d in (row['origin'],row['target'])):
                actual=float(f.loc[row['target'],'close']/f.loc[row['origin'],'close']-1)
                put(db,'live_outcomes',key,{**row,'actual':actual,'observed_at':now().isoformat(),'outcome_version':snap['meta']['version']})
        for release in values(db,'releases'):
            spec=get(db,'specs',release['experiment']);p=spec['policy'];rows=[r for r in values(db,'live_outcomes') if r['release']==release['release']]
            degraded=[]
            for key in release['models']:
                s,h=key.split('/');g=sorted([r for r in rows if r['symbol']==s and r['horizon']==int(h)],key=lambda r:r['origin']);ind=[];last=pd.Timestamp.min
                for r in g:
                    if pd.Timestamp(r['origin'])>last:ind.append(r);last=pd.Timestamp(r['target'])
                n=p['monitor_independent'];k=p['monitor_windows']
                if len(ind)<n*k:continue
                tail=ind[-n*k:];bad=[]
                for j in range(k):
                    a=np.array([r['actual'] for r in tail[j*n:(j+1)*n]]);b=np.array([r['predicted'] for r in tail[j*n:(j+1)*n]])
                    bad.append(float(abs(a-b).mean())>max(float(abs(a).mean()),1e-12)*(1+p['deterioration']))
                if all(bad):degraded.append(key)
            eventkey=release['release']+'|REVOKE'
            if degraded and get(db,'events',eventkey) is None:put(db,'events',eventkey,dict(action='REVOKE',release=release['release'],at=now().isoformat(),reason='PERSISTENT_PROSPECTIVE_DEGRADATION',models=degraded))
            if release.get('portfolio_method') and get(db,'events',eventkey) is None:
                end=cutoff(now());sessions=_sessions(root,end)
                origins=pd.DatetimeIndex(sorted({r['origin'] for r in values(db,'targets',release['experiment']) if pd.Timestamp(r['issued_at'])>pd.Timestamp(release['released_at'])}))
                # Inspect only closed, post-release signal-to-next-session returns.
                eligible=origins[origins<end]
                if len(eligible)>=p['portfolio_monitor_sessions']:
                    check=_replay(root,db,spec,eligible,end)
                    monitor_key=release['release']+'|PORTFOLIO_MONITOR|'+str(end.date())
                    if get(db,'events',monitor_key) is None:
                        state='VALID' if check['status']=='COMPLETE' else 'UNKNOWN'
                        put(db,'events',monitor_key,dict(action='PORTFOLIO_MONITOR',release=release['release'],at=now().isoformat(),date=str(end.date()),status=state,evidence_sha256=digest(check)))
                    if check['status']=='COMPLETE':
                        result=check['results'][release['portfolio_method']]
                        if result['summary']['max_drawdown'] < -p['maximum_drawdown']:
                            put(db,'events',eventkey,dict(action='REVOKE',release=release['release'],at=now().isoformat(),reason='PORTFOLIO_FORWARD_DRAWDOWN_LIMIT',evidence_sha256=digest(check)))
    return refresh_registry(root)


def run(root):
    root=Path(root);spec=prepare(root);settled=settle(root,spec)
    choice=select_portfolio(root,spec);collected=collect(root,spec);evaluation=evaluate(root,spec);release=promote(root,spec,evaluation)
    previous=refresh_registry(root);monitor_errors=[]
    if previous['portfolio'] and previous['portfolio']['experiment']!=spec['experiment']:
        with database(root) as db:prior=get(db,'specs',previous['portfolio']['experiment'])
        if prior['universe_sha256']==spec['universe_sha256'] and prior['runtime_hash']==runtime_hash():
            try:collect(root,prior);settle(root,prior)
            except (ValueError,OSError) as exc:
                monitor_errors.append(str(exc));date=str(cutoff(now()).date())
                with database(root) as db:
                    key=previous['portfolio']['release']+'|PORTFOLIO_MONITOR|'+date
                    if get(db,'events',key) is None:put(db,'events',key,dict(action='PORTFOLIO_MONITOR',release=previous['portfolio']['release'],date=date,at=now().isoformat(),status='UNKNOWN',reason=str(exc)))
    state=monitor(root)
    report=dict(generated_at=now().isoformat(),experiment=spec['experiment'],frozen_at=spec['frozen_at'],start_session=spec['start_session'],
        monitor_errors=monitor_errors,policy=spec['policy'],collector=collected,settled=settled,selection=choice,evaluation_status=evaluation['status'],release=release,
        admission=admission(root,state),registered_models=len(state['models']),submitted_orders=0)
    write_json(root/'reports/lifecycle/latest.json',report);return report
