"""User-selected daily research simulation, independent of baseline/formal ledgers."""
from pathlib import Path
import hashlib,html,json,sqlite3
import numpy as np
import pandas as pd
from .core import now,read_snapshot,write_json,atomic_text,write_csv
from .paper_account import get,put,digest,opened,evidence
from .research_policy import POLICY,targets
from .strategy_experiment import configurations,allocation
from .decision import covariance
from .active_execution import replay,simulate

def enabled(root):
    p=Path(root)/'active_research_config.json'
    return p.exists() and json.loads(p.read_text()).get('enabled') is True

def run(root):
    root=Path(root)
    if not enabled(root):return {'status':'DISABLED'}
    selection=json.loads((root/'active_research_config.json').read_text())
    if selection['variant']!='risk1.5_threshold1' or selection['mode']!='RESEARCH_PAPER_ONLY' or selection['formal_promotion']:raise ValueError('RESEARCH_ONLY_AUTHORIZED_VARIANT_REQUIRED')
    source=json.loads((root/'reports/research_paper/latest/report.json').read_text());at=now()
    if source['status']!='ACTIVE_RESEARCH_PAPER' or not source.get('predictions') or not source.get('plan'):raise ValueError('CURRENT_SOURCE_FORECAST_REQUIRED')
    payload=source['predictions'];day=payload['asof'];target=source['plan']['target_session']
    if source['valuation_date']!=day or source['plan']['origin_session']!=day or pd.Timestamp(payload['generated_at'])>at:raise ValueError('STALE_OR_FUTURE_SOURCE')
    cal=pd.DatetimeIndex(pd.to_datetime(pd.read_csv(root/'data/calendar.csv').iloc[:,0])).sort_values()
    if target!=str(cal[cal>pd.Timestamp(day)][0].date()):raise ValueError('NEXT_SESSION_UNVERIFIED')
    settings=json.loads((root/'strategy_experiment_config.json').read_text());base=source['meta']['config'];policy=source['spec'].get('policy',POLICY)
    variant=configurations(base,policy,settings)[selection['variant']];c=variant['config']
    if not (abs(c['max_stock']-.225)<1e-9 and abs(c['max_turnover']-.45)<1e-9 and abs(variant['policy']['exposure']['risk_off']-.45)<1e-9):raise ValueError('AUTHORIZED_CAPS_CHANGED')
    code={n:hashlib.sha256((Path(__file__).parent/n).read_bytes()).hexdigest() for n in ('active_research.py','active_execution.py','strategy_experiment.py','research_policy.py','decision.py','paper_account.py')}
    signature=digest(dict(selection=selection,variant=variant,settings=settings,model=source['spec']['version'],code=code))
    symbols=[x['symbol'] for x in payload['forecasts']]
    histories=pd.concat({s:read_snapshot(root,s)['hfq'].set_index('date').close.pct_change(fill_method=None) for s in symbols},axis=1).loc[:day].dropna().tail(c['covariance_days'])
    cov=covariance(histories.to_numpy(),c['minimum_covariance_days'],c['horizon']);env=source['plan']['environment']
    path=root/'data/active_research.sqlite3';alerts=[]
    with sqlite3.connect(path) as db:
        path.chmod(0o600);db.execute('CREATE TABLE IF NOT EXISTS records(kind TEXT,key TEXT,body TEXT,sha256 TEXT,PRIMARY KEY(kind,key))');db.commit();db.execute('BEGIN IMMEDIATE')
        meta=get(db,'meta').get('account')
        if meta is None:
            if at>=opened(target):raise ValueError('ACTIVATION_AFTER_OPEN_FORBIDDEN')
            meta=dict(signature=signature,selected_variant=selection['variant'],authorization=selection['authorization'],created_at=at.isoformat(),
                anchor_session=day,start_session=target,initial_equity=source['equity'],initial_cash=source['cash'],initial_positions=source['positions'],
                initial_marks={x['symbol']:x['close'] for x in payload['forecasts'] if x['symbol'] in source['positions']},
                original_initial_equity=source['meta']['original_initial_equity'],config=c,source_model=source['spec']['version'],formal_validated=False,code=code)
            put(db,'meta','account',meta)
        if meta['signature']!=signature:raise ValueError('FROZEN_ACTIVE_STRATEGY_CHANGED_REVIEW_REQUIRED')
        plans=get(db,'plans');observations=get(db,'observations')
        for date in cal[(cal>=pd.Timestamp(meta['start_session']))&(cal<=pd.Timestamp(day))]:
            date=str(date.date())
            if date in observations:continue
            if date not in plans:alerts.append('MISSING_PREOPEN_PLAN:'+date);break
            try:o=evidence(root,date,symbols)
            except (ValueError,KeyError,OSError) as exc:alerts.append(str(exc));break
            put(db,'observations',date,o);observations[date]=o
        result=replay(meta,cal,plans,observations)
        for row in result['ledger']:put(db,'nav',row['date'],row)
        for i,fill in enumerate(result['fills']):put(db,'fills',str(i),fill)
        last=result['ledger'][-1] if result['ledger'] else None
        positions=last['positions'] if last else meta['initial_positions'];equity=last['equity'] if last else meta['initial_equity'];cash=last['cash'] if last else meta['initial_cash']
        valuation=last['date'] if last else meta['anchor_session'];peak=max([meta['initial_equity']]+[r['equity'] for r in result['ledger']]);plan=plans.get(target)
        if plan is None and valuation==day and at<opened(target):
            bw,_=targets(payload,positions,equity,cov,base,env,policy);v=np.array([bw[s] for s in symbols]);risk=float(np.sqrt(max(0.,v@cov@v)*252/c['horizon']))
            plan=allocation(payload,positions,equity,cov,variant,env,risk,settings,peak)
            for row in plan['rows']:row['reasons'].append('买入/加仓按最终成交整数股数与实际费用复核净预期优势；不通过则不交易。风险减仓独立执行')
            plan.update(origin_session=day,target_session=target,registered_at=at.isoformat(),source_sha256=digest(payload),policy_version=policy['version'],environment=env,
                selected_variant=selection['variant'],mode='RESEARCH_PAPER_ONLY',formal_validated=False,
                edges={x['symbol']:dict(reference_price=x['close'],predicted_return=x['forecasts'][str(c['horizon'])]['predicted_return'],uncertainty=x['forecasts'][str(c['horizon'])]['model_uncertainty']['std']) for x in payload['forecasts']})
            put(db,'plans',target,plan);put(db,'inputs',day,dict(predictions=payload,covariance=cov.tolist(),environment=env))
        elif plan is None:alerts.append('STALE_VALUATION_OR_MISSED_PREOPEN_DEADLINE')
        if plan and plan['source_sha256']!=digest(payload):raise ValueError('FROZEN_FORECAST_CHANGED')
        db.commit()
    r=dict(status='ACTIVE_RESEARCH_PAPER' if plan and valuation==day and not alerts else 'BLOCKED_DATA',asof=day,generated_at=at.isoformat(),
        meta=meta,spec=source['spec'],plan=plan,predictions=payload,positions=positions,equity=equity,cash=cash,valuation_date=valuation,
        ledger=result['ledger'],fills=result['fills'],execution_blockers=result['blocked'],alerts=alerts,submitted_orders=0,
        daily_return=last['daily_return'] if last else None,research_segment_return=equity/meta['initial_equity']-1,cumulative_from_original=equity/meta['original_initial_equity']-1,
        note='用户指定的日常研究模拟（22.5% / 弱市45% / 换手45%）；费用复核版，尚未通过正式验证，不操作真实资金。原研究模拟继续作为基线对照。')
    if plan and valuation==day:
        try:
            o=evidence(root,day,symbols);prior=cal[cal<pd.Timestamp(day)][-1];origin=str(prior.date())
            bars={s:pd.DataFrame([{**b,'open':b['close']}],index=pd.DatetimeIndex([day])) for s,b in o['bars'].items()}
            preview=simulate(pd.DatetimeIndex([prior,pd.Timestamp(day)]),bars,{origin:plan['weights']},{(s,day):v for s,v in o['states'].items()},c,cash,positions,{s:o['bars'][s]['close'] for s in positions},priorities={origin:plan['risk_priorities']},edges={origin:plan['edges']})
            r['reference_scenario']=dict(basis='仅用当天收盘价替代开盘价的参考撮合，不是次日行情，不入账；实际数量以次日真实数据重算',reference_session=day,target_session=target,
                fills=[dict(symbol=f['symbol'],side=f['side'],shares=f['shares'],reference_price=f['price'],estimated_fee=f['fee'],estimated_cash_after=f['cash_after'],edge_check=f.get('edge_check')) for f in preview['fills']],cash_after=preview['ledger'][-1]['cash'],blocked=preview['blocked'])
        except (ValueError,KeyError,OSError) as exc:r['reference_scenario']=dict(fills=[],blocked=str(exc))
    publish(root,r);return r

def publish(root,r):
    folder=Path(root)/'reports/active_research/latest';folder.mkdir(parents=True,exist_ok=True);write_json(folder/'report.json',r)
    archive=Path(root)/'reports/active_research'/now().strftime('%Y%m%d-%H%M%S-%f');write_json(archive/'report.json',r)
    e=lambda x:html.escape(str(x));pct=lambda x:'待结算' if x is None else f'{x:.2%}'
    from .holding_summary import render
    page='<html lang="zh"><meta charset="utf-8"><title>当前日常研究模拟计划 · 22.5% / 45% / 45%</title><style>body{font:15px/1.5 system-ui;margin:20px;color:#183047}table{border-collapse:collapse;width:100%}td,th{padding:6px 9px;border-bottom:1px solid #ddd;vertical-align:top;text-align:left;white-space:nowrap}td:nth-child(6){white-space:normal;min-width:320px}.scroll{overflow:auto}summary{cursor:pointer}h2{font-size:20px}</style><h1>当前日常研究模拟 · 积极配置与实际费用复核</h1>'
    page+='<p>'+e(r['note'])+'</p>'+render(r)
    page+='<p>数据日 '+e(r['asof'])+'；估值日期 '+e(r['valuation_date'])+'；状态 '+e(r['status'])+'；模拟权益 '+e(r['equity'])+'元；现金 '+e(r['cash'])+'元。</p><p>本阶段日收益 '+pct(r['daily_return'])+'；本阶段累计 '+pct(r['research_segment_return'])+'；沿原账户起点累计 '+pct(r['cumulative_from_original'])+'。新方案起点 '+e(r['meta']['anchor_session'])+'，首次计划 '+e(r['meta']['start_session'])+'。</p>'
    page+='<p><a href="../../research_paper/latest/index.html">原研究策略对照（非当前方案）</a> · <a href="../../strategy_experiment/latest/index.html">九组配置比较</a> · <a href="report.json">全部预测、来源、计划与账本记录</a></p>'
    page+='<h2>30只股票的目标与条件计划</h2><p>目标股数不是明天必然成交股数。预测和概率对应原20交易日期限；正式模型未升级。</p><div class="scroll"><table><tr><th>股票</th><th>原排名 / 原收益预测</th><th>当前股数</th><th>目标权重 / 股数</th><th>目标方向</th><th>依据和执行条件</th></tr>'
    for x in (r.get('plan') or {}).get('rows',[]):
        vals=[x['name']+' '+x['symbol'],str(x['original_rank'])+' / '+pct(x['original_prediction']),x['current_shares'],pct(x['target_weight'])+' / '+str(x['reference_target_shares']),x['decision'],'；'.join(x['reasons'])+'；'+x['execution_conditions']]
        page+='<tr>'+''.join('<td>'+e(v)+'</td>' for v in vals)+'</tr>'
    page+='</table></div><h2>本阶段模拟收益与成交</h2><p>模拟成交 '+str(len(r['fills']))+'笔；下一交易日真实数据齐全后结算。预览股数不会计入收益。</p><div class="scroll"><table><tr><th>日期</th><th>权益</th><th>日收益</th><th>累计收益</th><th>费用</th></tr>'
    for x in r['ledger']:page+='<tr>'+''.join('<td>'+e(v)+'</td>' for v in [x['date'],round(x['equity'],2),pct(x['daily_return']),pct(x['cumulative_return']),round(x['fees'],2)])+'</tr>'
    page+='</table></div><p>限制：'+e(r['alerts'])+'；参考撮合限制：'+e(r.get('reference_scenario',{}).get('blocked',{}))+'</p></html>'
    atomic_text(folder/'index.html',page)
    from .report_navigation import prepare
    prepare(root)
