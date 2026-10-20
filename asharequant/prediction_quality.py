"""Module 09: prospective, immutable prediction/error ledger; never a trading gate."""
from pathlib import Path
from contextlib import contextmanager
import hashlib, json, sqlite3, html, math
import numpy as np
import pandas as pd
from .core import now, cutoff, read_snapshot, write_json


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()


@contextmanager
def database(root):
    path=Path(root)/'data/market.sqlite3';path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path)
    try:
        for table in ('q9_predictions','q9_outcomes'):
            db.execute(f'CREATE TABLE IF NOT EXISTS {table} (id TEXT PRIMARY KEY, body TEXT NOT NULL, sha256 TEXT NOT NULL)')
            for action in ('UPDATE','DELETE'):
                db.execute(f'CREATE TRIGGER IF NOT EXISTS {table}_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,"immutable quality ledger"); END')
        db.execute('''CREATE VIEW IF NOT EXISTS q9_prediction_results AS SELECT p.id,
            json_extract(p.body,'$.origin') prediction_date,json_extract(p.body,'$.symbol') symbol,
            json_extract(p.body,'$.horizon') horizon,json_extract(p.body,'$.target') target_date,
            json_extract(p.body,'$.predicted_return') predicted_return,
            json_extract(p.body,'$.predicted_direction') predicted_direction,
            json_extract(p.body,'$.probability_up') probability_up,
            json_extract(o.body,'$.actual_return') actual_return,
            json_extract(o.body,'$.prediction_error') prediction_error,
            json_extract(p.body,'$.model_version') model_version,
            json_extract(p.body,'$.market_state.state') market_state,
            json_extract(p.body,'$.input_version') data_version,
            json_extract(o.body,'$.confirmed_at') confirmed_at
            FROM q9_predictions p LEFT JOIN q9_outcomes o ON p.id=o.id''')
        yield db
        db.commit()
    finally: db.close()


def rows(db, table):
    result=[]
    for key,body,sha in db.execute(f'SELECT id,body,sha256 FROM {table} ORDER BY id'):
        value=json.loads(body)
        if digest(value)!=sha:raise ValueError('QUALITY_LEDGER_HASH_MISMATCH')
        result.append((key,value))
    return result


def put(db,table,key,value):
    sha=digest(value);old=db.execute(f'SELECT sha256 FROM {table} WHERE id=?',(key,)).fetchone()
    if old:
        if old[0]!=sha:raise ValueError('QUALITY_ISSUANCE_ALREADY_SEALED')
        return False
    db.execute(f'INSERT INTO {table} VALUES(?,?,?)',(key,json.dumps(value,ensure_ascii=False,allow_nan=False),sha));return True


def aware(value):
    t=pd.Timestamp(value)
    if t.tzinfo is None:raise ValueError('TIMESTAMP_REQUIRES_TIMEZONE')
    return t.tz_convert('Asia/Shanghai')


def archive(root,payload,at=None):
    """Archive observed predictions, not retrospective validation rows. Never backdate."""
    root=Path(root);at=aware(at or now());issued=aware(payload['generated_at'])
    if issued>at:raise ValueError('FUTURE_ISSUANCE')
    origin=pd.Timestamp(payload['asof']);close=origin.tz_localize('Asia/Shanghai')+pd.Timedelta(hours=15)
    if issued<close:raise ValueError('ISSUED_BEFORE_ORIGIN_CLOSE')
    from .connections import market_environment
    environment=market_environment(root,payload['asof'])
    # Environment is captured now, never reconstructed for an older archive.
    contemporaneous=at-issued<=pd.Timedelta(minutes=10)
    if not contemporaneous:environment=dict(state='unknown',reason='LATE_ARCHIVE_NO_ISSUANCE_ENVIRONMENT')
    count=0
    with database(root) as db:
        for stock in payload.get('forecasts',[]):
            snap=read_snapshot(root,stock['symbol']);meta=snap['meta'] if snap else {}
            for h,f in stock['forecasts'].items():
                target=f.get('forecast_target_date');pred=float(f['predicted_return'])
                if not math.isfinite(pred):raise ValueError('NONFINITE_PREDICTION')
                if target and pd.Timestamp(target)<=origin:raise ValueError('TARGET_NOT_AFTER_ORIGIN')
                cal=f.get('probability_calibration',{});p=f.get('up_probability_calibrated')
                calibrated=bool(cal.get('independent_validated') and p is not None)
                if calibrated and (not math.isfinite(float(p)) or not 0<=float(p)<=1):raise ValueError('INVALID_PROBABILITY')
                key=digest(dict(symbol=stock['symbol'],horizon=int(h),origin=str(origin.date()),issued_at=str(issued),model=f.get('model'),release=f.get('release_id'),code=payload.get('model_code_sha256')))
                version=payload.get('data_versions',{}).get(stock['symbol'])
                bound=bool(meta.get('version')==version and meta.get('source')=='tencent' and not meta.get('synthetic') and aware(meta['fetched_at'])<=issued)
                if bound:
                    for kind in ('raw','hfq'):
                        path=root/'data/normalized'/stock['symbol']/'versions'/version/(kind+'.csv')
                        if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest()!=meta['sha256'][kind]:bound=False
                next_session=payload.get('next_session')
                timely=bool(next_session and issued<pd.Timestamp(next_session).tz_localize('Asia/Shanghai')+pd.Timedelta(hours=9,minutes=15))
                value=dict(symbol=stock['symbol'],name=stock.get('name'),sector=stock.get('sector','unknown') if contemporaneous else 'unknown',
                    origin=str(origin.date()),horizon=int(h),target=target,issued_at=str(issued),archived_at=str(at),
                    predicted_return=pred,predicted_direction='UP' if pred>0 else 'DOWN' if pred<0 else 'FLAT',
                    probability_up=float(p) if calibrated else None,probability_status='INDEPENDENTLY_CALIBRATED' if calibrated else 'UNAVAILABLE_UNVALIDATED',
                    probability_empirical_unvalidated=f.get('up_probability_empirical'),
                    model=f.get('model'),model_version=digest(dict(code=payload.get('model_code_sha256'),model=f.get('model'),parameters=f.get('model_parameters'),release=f.get('release_id'))),
                    release=f.get('release_id'),model_status=f.get('model_status','RESEARCH_ONLY'),market_state=environment,
                    input_version=version,input_sha256=meta.get('sha256') if bound else None,
                    data_status='BOUND_REAL_SNAPSHOT' if bound else 'UNVERIFIED_INPUT_VERSION',
                    parameters=f.get('model_parameters'),source_generated_at=payload['generated_at'],
                    primary_daily=False,late_archive=not contemporaneous,issuance_before_next_open=timely,
                    note='Close-to-close HFQ research return; not executable next-open return')
                # Exact retries retain the original environment and archive time.
                old=db.execute('SELECT body FROM q9_predictions WHERE id=?',(key,)).fetchone()
                if old:
                    previous=json.loads(old[0])
                    if any(previous[k]!=value[k] for k in ('predicted_return','probability_up','input_version','model_version','target')):raise ValueError('QUALITY_ISSUANCE_ALREADY_SEALED')
                    continue
                # One primary per stock/horizon/day; reruns remain auditable, never inflate samples.
                existing=[v for _,v in rows(db,'q9_predictions') if (v['symbol'],v['horizon'],v['origin'])==(value['symbol'],value['horizon'],value['origin']) and v['primary_daily']]
                value['primary_daily']=not existing and bound and contemporaneous and timely
                count+=put(db,'q9_predictions',key,value)
    return dict(archived=count)


def confirm(root,at=None):
    """Use both prices from one newly observed adjustment version, after close buffer."""
    at=aware(at or now());latest=cutoff(at.to_pydatetime());results={};count=0;errors=[]
    with database(root) as db:
        done=dict(rows(db,'q9_outcomes'))
        for key,row in rows(db,'q9_predictions'):
            if key in done or not row['target'] or pd.Timestamp(row['target'])>latest:continue
            if row['data_status']!='BOUND_REAL_SNAPSHOT':continue
            try:
                s=row['symbol']
                if s not in results:results[s]=read_snapshot(Path(root),s)
                snap=results[s]
                if not snap:raise ValueError('NO_REAL_OUTCOME_SNAPSHOT')
                meta=snap['meta'];fetched=aware(meta['fetched_at'])
                end=pd.Timestamp(row['target']).tz_localize('Asia/Shanghai')+pd.Timedelta(hours=18)
                if fetched<end or fetched>at or meta.get('source')!='tencent' or meta.get('synthetic'):raise ValueError('OUTCOME_NOT_CONFIRMED_AFTER_CLOSE')
                # Bind actual archived bytes, reject mutated snapshots.
                version=meta['version'];hashes=meta['sha256']
                for kind in ('raw','hfq'):
                    p=Path(root)/'data/normalized'/s/'versions'/version/(kind+'.csv')
                    if not p.exists() or hashlib.sha256(p.read_bytes()).hexdigest()!=hashes[kind]:raise ValueError('OUTCOME_SOURCE_HASH_MISMATCH')
                f=snap['hfq'].set_index('date');raw=snap['raw'].set_index('date')
                origin=pd.Timestamp(row['origin']);target=pd.Timestamp(row['target'])
                if any(d not in f.index or d not in raw.index for d in (origin,target)):raise ValueError('MISSING_EXACT_ORIGIN_OR_TARGET_BAR')
                prices=np.array([f.loc[origin,'close'],f.loc[target,'close']],dtype=float)
                if not np.isfinite(prices).all() or (prices<=0).any():raise ValueError('INVALID_OUTCOME_PRICE')
                actual=float(prices[1]/prices[0]-1);error=row['predicted_return']-actual
                actual_direction='UP' if actual>0 else 'DOWN' if actual<0 else 'FLAT'
                probability=row['probability_up'];out=dict(actual_return=actual,prediction_error=error,absolute_error=abs(error),squared_error=error**2,
                    actual_direction=actual_direction,direction_error=row['predicted_direction']!=actual_direction,
                    brier_error=(probability-float(actual>0))**2 if probability is not None else None,
                    confirmed_at=str(at),outcome_version=version,outcome_sha256=hashes,
                    outcome_basis='same-version HFQ close-to-close; research proxy; not verified broker total return',
                    trade_status='NO_BROKER_EXECUTION_EVIDENCE',execution_error=None,portfolio_return_deviation=None)
                count+=put(db,'q9_outcomes',key,out)
            except Exception as exc:errors.append(dict(id=key,symbol=row['symbol'],reason=str(exc)))
    return dict(confirmed=count,errors=errors)


def independent(records):
    """Per-symbol/horizon non-overlapping intervals; date-cluster inference handles cross stocks."""
    chosen=[];ends={}
    for r in sorted(records,key=lambda r:(r['origin'],r['symbol'],r['horizon'])):
        k=(r['symbol'],r['horizon'])
        if k not in ends or r['origin']>=ends[k]:chosen.append(r);ends[k]=r['target']
    return chosen


def stats(records):
    if not records:return dict(status='NO_CONFIRMED_SAMPLES',samples=0)
    a=np.array([r['actual_return'] for r in records]);p=np.array([r['predicted_return'] for r in records]);e=p-a
    blocks=independent(records);dates={}
    for r in blocks:dates.setdefault(r['origin'],[]).append(r['prediction_error'])
    # Greedy global date clusters exclude overlapping holding intervals across stocks too.
    all_dates=sorted(dates);kept=[];end=None
    for day in all_dates:
        if end is None or day>=end:
            kept.append(day);end=max(r['target'] for r in blocks if r['origin']==day)
    dates={d:dates[d] for d in kept}
    cluster=np.array([np.mean(v) for v in dates.values()]);n=len(cluster)
    # Approximate CI on nonoverlap date-cluster means, not an iid stock-row CI.
    ci=None
    if n>=12:
        from scipy.stats import t
        half=float(t.ppf(.975,n-1)*cluster.std(ddof=1)/np.sqrt(n));ci=[float(cluster.mean()-half),float(cluster.mean()+half)]
    direction=float(np.mean([not r['direction_error'] for r in records]));prob=[r for r in records if r['probability_up'] is not None]
    bins=[];ece=None;brier=None
    if prob:
        brier=float(np.mean([r['brier_error'] for r in prob]));ece=0.
        for bin_index in range(10):
            low=bin_index/10
            b=[r for r in prob if min(9,int(r['probability_up']*10))==bin_index]
            if not b:continue
            predicted=float(np.mean([r['probability_up'] for r in b]));observed=float(np.mean([r['actual_return']>0 for r in b]));ece+=len(b)/len(prob)*abs(predicted-observed)
            bins.append(dict(low=float(low),count=len(b),mean_probability=predicted,observed_up_rate=observed))
    # Prespecified two disjoint windows, only independent errors. Insufficient is not healthy.
    ordered=sorted(blocks,key=lambda r:(r['origin'],r['symbol']));window_dates=sorted(dates)
    windows=[]
    if len(window_dates)>=24:
        for ds in (window_dates[-24:-12],window_dates[-12:]):
            part=[r for r in ordered if r['origin'] in ds];err=np.array([r['prediction_error'] for r in part]);base=np.mean([abs(r['actual_return']) for r in part])
            windows.append(dict(samples=len(part),origin_dates=len(ds),mae=float(abs(err).mean()),bias=float(err.mean()),zero_return_baseline_mae=float(base),degraded=bool(abs(err).mean()>1.2*max(base,1e-12))))
    lag=None
    if n>=12 and cluster[:-1].std()>1e-12 and cluster[1:].std()>1e-12:lag=float(np.corrcoef(cluster[:-1],cluster[1:])[0,1])
    return dict(status='DESCRIPTIVE_ONLY',samples=len(records),independent_samples=len(blocks),independent_origin_clusters=n,
        mae=float(abs(e).mean()),rmse=float(np.sqrt(np.mean(e**2))),bias=float(e.mean()),direction_accuracy=direction,
        direction_errors=int(sum(r['direction_error'] for r in records)),zero_return_baseline_mae=float(abs(a).mean()),
        bias_95pct_cluster_t_interval=ci,bias_interval_status='APPROXIMATE_DATE_CLUSTER_CI' if ci else 'INSUFFICIENT_12_CLUSTERS',
        persistent_bias='POSITIVE' if ci and ci[0]>0 else 'NEGATIVE' if ci and ci[1]<0 else 'NOT_ESTABLISHED',
        error_lag1_correlation=lag,probability_samples=len(prob),brier=brier,ece=ece,reliability_bins=bins,
        probability_status='DESCRIPTIVE_CALIBRATED_SUBSET' if prob else 'NO_VALIDATED_PROBABILITIES',
        degradation='DEGRADED_RELATIVE_TO_ZERO_BASELINE' if windows and all(w['degraded'] for w in windows) else 'NO_PERSISTENT_TRIGGER' if windows else 'INSUFFICIENT_24_ORIGIN_CLUSTERS',windows=windows)


def report(root,at=None):
    root=Path(root);at=aware(at or now());settled=confirm(root,at)
    with database(root) as db:
        predictions=rows(db,'q9_predictions');outcomes=dict(rows(db,'q9_outcomes'))
    records=[];joined=[];pending={}
    for key,r in predictions:
        value=dict(id=key,**r,**outcomes.get(key,{}))
        if key not in outcomes:
            status='TARGET_UNKNOWN' if not r['target'] else 'NOT_MATURED' if pd.Timestamp(r['target'])>cutoff(at.to_pydatetime()) else 'AWAITING_CONFIRMED_REAL_DATA'
            pending[status]=pending.get(status,0)+1;value['evaluation_status']=status
        else:
            value['evaluation_status']='CONFIRMED'
            if r['primary_daily']:records.append(value)
        joined.append(value)
    groups={}
    for name,fn in [('stock',lambda r:r['symbol']),('sector',lambda r:r['sector']),('market_state',lambda r:r['market_state']['state']),('model_version',lambda r:r['model_version']),('model_family',lambda r:r['model']),('model_status',lambda r:r['model_status']),('horizon',lambda r:str(r['horizon']))]:
        buckets={}
        for r in records:buckets.setdefault(f"{fn(r)}|T+{r['horizon']}|{r['model_status']}",[]).append(r)
        groups[name]={key:stats(value) for key,value in sorted(buckets.items())}
    rankings={}
    for dimension in ('stock','sector','market_state','model_version','model_family'):
        eligible=[dict(group=k,**v) for k,v in groups[dimension].items() if v.get('independent_origin_clusters',0)>=24]
        rankings[dimension]=dict(status='DESCRIPTIVE_NOT_CAUSAL' if eligible else 'INSUFFICIENT_MATCHED_SAMPLES',
            by_mae=sorted(eligible,key=lambda v:v['mae']),note='Compare same horizon/period and coverage; aggregate differences are not causal or promotion evidence')
    references=[]
    for ref in sorted((root/'data/prediction_quality/reference').glob('*/result.json')):
        original=(ref.parent/'input.json').read_bytes()
        if hashlib.sha256(original).hexdigest()!=ref.parent.name:raise ValueError('REFERENCE_HASH_MISMATCH')
        computed=reconcile(json.loads(original))
        if computed!=json.loads(ref.read_text()):raise ValueError('REFERENCE_RESULT_CHANGED')
        references.append(dict(sha256=ref.parent.name,result=computed))
    payload=dict(module=9,generated_at=str(at),asof=str(cutoff(at.to_pydatetime()).date()),predictions=len(predictions),confirmed=len(outcomes),
        primary_confirmed=len(records),unverified_reconciliation_references=references,pending=pending,settlement=settled,summary=stats(records),groups=groups,rankings=rankings,
        portfolio_deviation=dict(status='NO_VERIFIED_ACCOUNT_NAV_AND_MATCHED_EX_ANTE_PORTFOLIO',value=None),
        execution_error=dict(status='NO_VERIFIED_BROKER_ORDER_FILL_AND_ARRIVAL_PRICE',value=None),
        trading_effect='REPORT_ONLY_NO_PROMOTION_OR_TRADING_AUTHORITY',
        limitations=['First daily issuance only in pooled metrics; all reruns retained.',
          'Research candidates and registered releases must be compared separately, by horizon and matched periods.',
          'Intervals are approximate, descriptive date-cluster t intervals; cross-horizon overlap and regime selection limit inference.',
          'Actual returns use first confirmed same-version HFQ prices; later vendor revisions do not overwrite outcomes.',
          'Sector/state captured when archived; old missing point-in-time attributes remain unknown.',
          'No real fills or verified NAV: execution error and portfolio deviation stay null, never zero.'])
    folder=root/'reports/prediction_quality'/at.strftime('%Y%m%d-%H%M%S-%f');folder.mkdir(parents=True)
    write_json(folder/'report.json',payload);write_json(folder/'predictions.json',joined)
    fields=['id','origin','symbol','horizon','target','predicted_return','predicted_direction','probability_up','actual_return','prediction_error','direction_error','brier_error','model_version','input_version','evaluation_status']
    import csv
    with (folder/'predictions.csv').open('w',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');writer.writeheader();writer.writerows(joined)
    page='<meta charset="utf-8"><title>每日预测质量报告</title><style>body{font:16px/1.7 system-ui;margin:32px}td,th{padding:8px;border:1px solid #ddd}pre{white-space:pre-wrap}</style><h1>每日预测质量报告</h1>'
    page+=f'<p>截至 {payload["asof"]}；封存预测 {len(predictions)} 条，确认结果 {len(outcomes)} 条，纳入首次日预测统计 {len(records)} 条。未到期与待确认结果不计误差。</p>'
    page+='<p>研究统计不授予交易资格。缺真实账户净值或券商成交证据：组合收益偏差、交易执行误差均为不可评估，不是零。</p>'
    def esc(value):return html.escape(str(value))
    def number(value):return '—' if value is None else f'{value:.6f}' if isinstance(value,float) else esc(value)
    labels={'NOT_MATURED':'预测期限尚未结束','TARGET_UNKNOWN':'缺可靠目标交易日','AWAITING_CONFIRMED_REAL_DATA':'等待确认真实行情'}
    page+='<h2>待评估记录</h2><ul>'+''.join('<li>'+labels.get(k,k)+'：'+str(v)+'</li>' for k,v in pending.items())+'</ul>'
    if not records:page+='<p>目前没有已确认、符合首次日预测统计条件的到期样本。准确率、误差、偏差和稳定性结论均暂不可评估。</p>'
    def metric_table(items):
        cols=[('samples','确认数'),('independent_origin_clusters','独立起点块'),('mae','MAE'),('rmse','RMSE'),('bias','平均偏差'),('direction_accuracy','方向准确率'),('brier','Brier'),('ece','ECE')]
        text='<table><tr><th>分组</th>'+''.join('<th>'+title+'</th>' for _,title in cols)+'</tr>'
        for key,value in items.items():
            text+='<tr><td>'+esc(key)+'</td>'+''.join('<td>'+number(value.get(field))+'</td>' for field,_ in cols)+'</tr>'
        return text+'</table>'
    page+='<h2>总体概览</h2>'+metric_table({'全部首次日预测':payload['summary']})
    titles={'stock':'股票','sector':'预测时点行业','market_state':'预测时点市场环境','model_version':'模型版本','model_family':'模型家族','model_status':'研究或正式状态','horizon':'预测期限'}
    for dimension,buckets in groups.items():
        page+='<h2>'+titles[dimension]+'分组</h2>'
        page+=metric_table(buckets) if buckets else '<p>暂无合格到期样本。</p>'
        for key,value in buckets.items():
            warning='样本不足，尚不能判断持续退化' if not value['windows'] else '连续窗口相对基准退化' if value['degradation']=='DEGRADED_RELATIVE_TO_ZERO_BASELINE' else '未触发连续窗口退化条件'
            bias={'POSITIVE':'高估偏差区间高于零','NEGATIVE':'低估偏差区间低于零','NOT_ESTABLISHED':'尚未建立持续性偏差结论'}[value['persistent_bias']]
            page+='<p>'+esc(key)+'：'+warning+'；'+bias+'。近似偏差95%区间：'+esc(value['bias_95pct_cluster_t_interval'] or '样本不足')+'</p>'
            if value['reliability_bins']:
                page+='<table><tr><th>概率桶下界</th><th>样本</th><th>平均预测概率</th><th>实际上涨比例</th></tr>'+''.join('<tr>'+''.join('<td>'+number(b[k])+'</td>' for k in ('low','count','mean_probability','observed_up_rate'))+'</tr>' for b in value['reliability_bins'])+'</table>'
    page+='<h2>组合与执行误差</h2><p>缺可信净值/券商成交连接，正式结果不可评估。未成交机会成本不能填写为零；人工导入只能作为未核验参考。</p>'
    if references:page+='<p>已保存 '+str(len(references))+' 份未核验对账参考。参考计算和来源哈希见JSON，不参与正式质量结论或交易准入。</p>'
    page+='<p>排序至少需要24个非重叠起点块；请匹配期限和期间比较。分组仅是统计描述，不构成因果解释或晋升证据。完整排序、偏差持续性与误差滞后相关见指标文件。</p>'
    page+='<p><a href="predictions.csv">统一预测与到期误差CSV</a> · <a href="report.json">质量指标JSON</a> · <a href="predictions.json">完整版本与时点快照</a></p>'
    (folder/'index.html').write_text(page)
    latest=root/'reports/prediction_quality/latest';latest.mkdir(parents=True,exist_ok=True)
    import shutil
    for name in ('report.json','predictions.json','predictions.csv','index.html'):shutil.copy2(folder/name,latest/name)
    return payload


def run(root):
    root=Path(root);p=root/'reports/forecasts/latest/forecast.json'
    if p.exists():archive(root,json.loads(p.read_text()))
    return report(root)


def reconcile(evidence):
    """Compute reference attribution for supplied records; never certify a broker source.

    Intake schema deliberately cannot set verified=True. A broker/NAV connector is
    still required before these become independently confirmed execution evidence.
    """
    def finite(value):
        x=float(value)
        if not math.isfinite(x):raise ValueError('NONFINITE_RECONCILIATION_VALUE')
        return x
    result=dict(status='USER_SUPPLIED_UNVERIFIED_REFERENCE_ONLY',verified=False,execution=[],portfolio=[])
    seen=set()
    for order in evidence.get('orders',[]):
        key=str(order['order_id'])
        if key in seen:raise ValueError('DUPLICATE_ORDER_ID')
        seen.add(key);side=order['side']
        if side not in ('BUY','SELL'):raise ValueError('INVALID_SIDE')
        qty=finite(order['planned_shares']);arrival=finite(order['arrival_price']);estimate=finite(order['estimated_fees'])
        if qty<=0 or qty!=int(qty) or arrival<=0 or estimate<0:raise ValueError('INVALID_ORDER_VALUES')
        aware(order['arrival_at']);total=0.;notional=0.;fees=0.;ids=set()
        for fill in order.get('fills',[]):
            if fill['fill_id'] in ids:raise ValueError('DUPLICATE_FILL_ID')
            ids.add(fill['fill_id'])
            if aware(fill['filled_at'])<aware(order['arrival_at']):raise ValueError('FILL_BEFORE_ARRIVAL')
            n=finite(fill['shares']);price=finite(fill['price']);fee=finite(fill['fees'])
            if n<=0 or n!=int(n) or price<=0 or fee<0:raise ValueError('INVALID_FILL_VALUES')
            total+=n;notional+=n*price;fees+=fee
        if total>qty:raise ValueError('FILLED_MORE_THAN_PLANNED')
        sign=1 if side=='BUY' else -1
        result['execution'].append(dict(order_id=key,fill_ratio=total/qty,unfilled_shares=qty-total,
            price_shortfall_amount=sign*(notional-total*arrival) if total else None,
            fee_deviation=fees-estimate if total==qty else None,
            implementation_shortfall_bps=10000*(sign*(notional-total*arrival)+fees-estimate)/(qty*arrival) if total==qty else None,
            note='Positive shortfall means worse execution; partial/unfilled opportunity cost unknown, not zero'))
    for period in evidence.get('portfolio_periods',[]):
        start=aware(period['start_at']);end=aware(period['end_at'])
        if end<=start:raise ValueError('INVALID_PORTFOLIO_PERIOD')
        initial=finite(period['start_nav']);final=finite(period['end_nav']);expected=finite(period['ex_ante_expected_return'])
        if initial<=0 or final<0:raise ValueError('INVALID_NAV')
        # No convenient cash-flow subtraction: intraperiod flows need proper unit NAV/TWR.
        if period.get('external_flows'):
            result['portfolio'].append(dict(start_at=str(start),end_at=str(end),status='CASH_FLOW_TIMING_REQUIRES_UNIT_NAV',return_deviation=None));continue
        actual=final/initial-1
        result['portfolio'].append(dict(start_at=str(start),end_at=str(end),status='UNVERIFIED_REFERENCE',actual_net_return=actual,
            ex_ante_expected_return=expected,return_deviation=actual-expected,
            note='Includes market/model/weight/execution differences; not causal execution attribution'))
    return result


def import_reference(root,path):
    raw=Path(path).read_bytes();value=json.loads(raw);computed=reconcile(value)
    # Store original bytes and digest; imported reference cannot amend confirmed q9 returns.
    folder=Path(root)/'data/prediction_quality/reference'/hashlib.sha256(raw).hexdigest();folder.mkdir(parents=True,exist_ok=True)
    (folder/'input.json').write_bytes(raw);(folder/'input.json').chmod(0o600)
    write_json(folder/'result.json',computed)
    return computed
