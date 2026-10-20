"""Post-prediction, prospectively frozen strategy comparison. Never trains/promotes models.
Uses the unchanged research allocation and execution engines; original payloads are hashed.
Historical snapshots cannot be reconstructed from today's scores or risk estimates.
"""
from pathlib import Path
import copy, hashlib, html, json, sqlite3
import numpy as np
import pandas as pd
from .core import now, read_snapshot, atomic_text, write_json
from .paper_account import digest, get, put, opened, evidence
from .research_policy import targets, POLICY
from .research_execution import replay
from .decision import covariance

METRICS=('annualized_return','sharpe_zero_rf','maximum_drawdown','annualized_volatility','trades','turnover','fees','net_return','capital_utilization','worst_day','tail_mean_5pct')

def configurations(base, policy, settings):
    if settings['mode']!='SHADOW_PAPER_ONLY' or settings['auto_promote']:raise ValueError('SHADOW_ONLY_NO_PROMOTION')
    if not 0<settings['maximum_drawdown']<=.15 or settings['minimum_equity']<120000:raise ValueError('LOSS_LIMIT_MUST_NOT_BE_RELAXED')
    if settings['risk_multipliers']!=[1.,1.25,1.5] or settings['threshold_multipliers']!=[1.,.9,.8]:raise ValueError('PREDECLARED_GRID_REQUIRED')
    out={}
    for risk in settings['risk_multipliers']:
        for threshold in settings['threshold_multipliers']:
            c=copy.deepcopy(base);p=copy.deepcopy(policy)
            for k in ('max_stock','max_sector','max_invested','max_turnover'):
                c[k]=min(1.,base[k]*risk)
            c['risk_aversion']=base['risk_aversion']/risk
            # Actual fees, slippage, cost ceiling, individual tail limits and liquidity are unchanged.
            p['exposure']={k:min(c['max_invested'],v*risk) for k,v in policy['exposure'].items()}
            c['minimum_trade_value']=base['minimum_trade_value']*threshold
            p['no_trade_band']=policy['no_trade_band']*threshold
            name='baseline' if risk==threshold==1 else f'risk{risk:g}_threshold{threshold:g}'
            out[name]=dict(config=c,policy=p,risk_multiplier=risk,threshold_multiplier=threshold,
                differences={k:{'old':base[k],'new':v} for k,v in c.items() if v!=base.get(k)},
                policy_differences={k:{'old':policy[k],'new':v} for k,v in p.items() if v!=policy.get(k)})
    return out

def action(current,target):
    if target>current:return 'ADD' if current else 'BUY'
    if target<current:return 'SELL' if target==0 else 'REDUCE'
    return 'HOLD' if current else 'WAIT'

def measurements(ledger,fills,initial):
    if not ledger:return {k:None for k in METRICS}|{'sessions':0,'slippage':'已包含在成交价格中，手续费单列'}
    curve=np.array([initial]+[r['equity'] for r in ledger]);returns=curve[1:]/curve[:-1]-1;n=len(returns)
    std=float(np.std(returns,ddof=1)) if n>1 else 0.
    # Annualization on short live runs is deliberately withheld.
    return dict(sessions=n,annualized_return=float((curve[-1]/initial)**(252/n)-1) if n>=252 else None,
        sharpe_zero_rf=float(returns.mean()/std*np.sqrt(252)) if n>=252 and std>0 else None,
        maximum_drawdown=float((curve/np.maximum.accumulate(curve)-1).min()),
        annualized_volatility=std*np.sqrt(252) if n>=252 else None,trades=len(fills),
        turnover=sum(r['turnover'] for r in ledger),fees=sum(r['fees'] for r in ledger),
        net_return=float(curve[-1]/initial-1),capital_utilization=float(np.mean([1-r['cash']/r['equity'] for r in ledger])),
        worst_day=float(returns.min()),tail_mean_5pct=float(np.sort(returns)[:max(1,int(np.ceil(n*.05)))].mean()),
        slippage='已包含在成交价格中，手续费单列')

def validation(result,initial,settings):
    ledger=result['ledger'];fills=result['fills'];windows=[];size=settings['rolling_window']
    for start in range(0,len(ledger)-size+1,size):
        sample=ledger[start:start+size];dates={r['date'] for r in sample}
        windows.append(dict(start=sample[0]['date'],end=sample[-1]['date'],metrics=measurements(sample,[f for f in fills if f['date'] in dates],initial if start==0 else ledger[start-1]['equity'])))
    # No candidate selection on its own holdout. Final promotion remains a separate review.
    reasons=[]
    if len(ledger)<settings['minimum_sessions']:reasons.append('前瞻样本不足252个交易日')
    if len(fills)<settings['minimum_trades']:reasons.append('模拟成交不足20笔')
    if result.get('blocked'):reasons.append('存在执行数据或成交限制，需逐项审查')
    reasons.append('需独立留出期、费用压力和相对基线尾部风险审核；禁止自动升级')
    return dict(status='NOT_VALIDATED',reasons=reasons,rolling_windows=windows,auto_promote=False)

def allocation(payload,positions,equity,cov,variant,env,baseline_risk,settings,peak):
    before=digest(payload);c=variant['config'];w,rows=targets(payload,positions,equity,cov,c,env,variant['policy'])
    symbols=[r['symbol'] for r in payload['forecasts']];v=np.array([w[s] for s in symbols]);sigma=float(np.sqrt(max(0.,v@cov@v)*252/c['horizon']))
    is_base=variant['risk_multiplier']==variant['threshold_multiplier']==1
    # Relative risk envelope comes from the unchanged baseline allocation, not an invented volatility value.
    ceiling=baseline_risk*variant['risk_multiplier'];scale=1.;flags=[]
    if not is_base and sigma>ceiling+1e-12:
        scale=ceiling/sigma;flags.append('组合预测波动率超过相对基线预算，按比例缩减')
    if not is_base and (equity<=settings['minimum_equity'] or equity/peak-1<=-settings['maximum_drawdown']):
        scale=0.;flags.append('触发最大回撤或账户最低权益：下一交易日条件性退出；不能保证成交或限定实际损失')
    ranks={r['symbol']:r.get('composite_rank') for r in payload.get('seven_metrics',[]) if r['horizon']==c['horizon']}
    original={r['symbol']:r for r in payload['forecasts']}
    for r in rows:
        s=r['symbol'];r['target_weight']*=scale;w[s]=r['target_weight'];r['target_amount']=w[s]*equity
        r['reference_target_shares']=int(r['target_amount']/r['reference_price'])//100*100
        r['decision']=action(r['current_shares'],r['reference_target_shares']);r['direction']=r['decision']
        r['original_rank']=ranks.get(s);r['original_prediction']=original[s]['forecasts'][str(c['horizon'])]['predicted_return']
        r['reasons']+=flags;r['executable']=False;r['execution_conditions']='仅模拟目标；次日真实开盘、停牌/涨跌停、T+1、交易单位、量能、资金、费用及换手限制核验后才模拟成交'
    if digest(payload)!=before:raise AssertionError('RAW_FORECAST_CHANGED')
    return dict(weights=w,rows=rows,risk_priorities={r['symbol']:r['risk_priority'] for r in rows},
        predicted_annual_volatility=sigma*scale,relative_volatility_ceiling=ceiling,
        total_target_weight=sum(w.values()),cash_target=1-sum(w.values()),risk_flags=flags)

def run(root):
    root=Path(root);cp=root/'strategy_experiment_config.json'
    if not cp.exists() or not json.loads(cp.read_text()).get('enabled'):return {'status':'DISABLED'}
    settings=json.loads(cp.read_text());source=json.loads((root/'reports/research_paper/latest/report.json').read_text())
    if source['status']!='ACTIVE_RESEARCH_PAPER' or not source.get('plan'):raise ValueError('CURRENT_RESEARCH_PLAN_REQUIRED')
    payload=source['predictions'];day=payload['asof'];target=source['plan']['target_session'];at=now()
    if source['valuation_date']!=day or source['plan']['origin_session']!=day:raise ValueError('STALE_SOURCE')
    if pd.Timestamp(payload['generated_at'])>at:raise ValueError('FUTURE_PAYLOAD')
    cal=pd.DatetimeIndex(pd.to_datetime(pd.read_csv(root/'data/calendar.csv').iloc[:,0])).sort_values()
    if target!=str(cal[cal>pd.Timestamp(day)][0].date()):raise ValueError('TARGET_CALENDAR_MISMATCH')
    variants=configurations(source['meta']['config'],source['spec'].get('policy',POLICY),settings)
    symbols=[r['symbol'] for r in payload['forecasts']]
    histories=pd.concat({s:read_snapshot(root,s)['hfq'].set_index('date').close.pct_change(fill_method=None) for s in symbols},axis=1).loc[:day].dropna().tail(source['meta']['config']['covariance_days'])
    cov=covariance(histories.to_numpy(),source['meta']['config']['minimum_covariance_days'],source['meta']['config']['horizon'])
    inputs=dict(predictions=payload,covariance=cov.tolist(),environment=source['plan']['environment'],target_session=target)
    code={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(__file__).parent.glob('*.py') if p.name in ('strategy_experiment.py','research_policy.py','research_execution.py','decision.py','paper_account.py')}
    folder=root/'reports/strategy_experiment/latest';folder.mkdir(parents=True,exist_ok=True)
    path=root/'data/strategy_experiment.sqlite3';results={};alerts=[]
    with sqlite3.connect(path) as db:
        path.chmod(0o600);db.execute('CREATE TABLE IF NOT EXISTS records(kind TEXT,key TEXT,body TEXT,sha256 TEXT,PRIMARY KEY(kind,key))');db.commit();db.execute('BEGIN IMMEDIATE')
        metas=get(db,'meta')
        signature=digest(dict(settings=settings,variants=variants,code=code,model=source['spec']['version']))
        if not metas:
            if at>=opened(target):raise ValueError('MISSED_PREOPEN_DEADLINE')
            meta=dict(signature=signature,settings=settings,variants=variants,code=code,start_session=target,anchor_session=day,
                initial_equity=source['equity'],initial_cash=source['cash'],initial_positions=source['positions'],
                initial_marks={s:r['close'] for s,r in ((r['symbol'],r) for r in payload['forecasts']) if s in source['positions']},created_at=at.isoformat())
            put(db,'meta','experiment',meta)
        else:meta=metas['experiment']
        if meta['signature']!=signature:raise ValueError('FROZEN_EXPERIMENT_CHANGED_START_REVIEWED_NEW_EXPERIMENT')
        obs=get(db,'observations')
        for date in cal[(cal>=pd.Timestamp(meta['start_session']))&(cal<=pd.Timestamp(day))]:
            date=str(date.date())
            if date in obs:continue
            # A skipped run cannot be silently bridged or backfilled with a new plan.
            if any(date not in get(db,'plans:'+name) for name in variants):alerts.append('MISSING_PREOPEN_PLAN:'+date);break
            try:obs[date]=evidence(root,date,symbols)
            except (ValueError,KeyError,OSError) as exc:alerts.append(str(exc));break
            put(db,'observations',date,obs[date])
        baseline_risk=None
        for name,variant in variants.items():
            plans=get(db,'plans:'+name);m={**meta,'config':variant['config']};result=replay(m,cal,plans,obs)
            last=result['ledger'][-1] if result['ledger'] else None
            positions=last['positions'] if last else meta['initial_positions'];equity=last['equity'] if last else meta['initial_equity']
            peak=max([meta['initial_equity']]+[r['equity'] for r in result['ledger']]);current=(last['date'] if last else meta['anchor_session'])==day
            for row in result['ledger']:put(db,'nav:'+name,row['date'],row)
            for i,fill in enumerate(result['fills']):put(db,'fills:'+name,str(i),fill)
            if name=='baseline':
                bw,_=targets(payload,positions,equity,cov,variant['config'],inputs['environment'],variant['policy']);vec=np.array([bw[s] for s in symbols]);baseline_risk=float(np.sqrt(max(0.,vec@cov@vec)*252/variant['config']['horizon']))
            plan=plans.get(target)
            if plan is None and current and at<opened(target):
                plan=allocation(payload,positions,equity,cov,variant,inputs['environment'],baseline_risk,settings,peak)
                plan.update(origin_session=day,target_session=target,registered_at=at.isoformat(),source_sha256=digest(payload),input_sha256=digest(inputs),mode='SHADOW_PAPER_ONLY')
                put(db,'plans:'+name,target,plan)
            elif plan is None:alerts.append(name+':STALE_OR_MISSED_DEADLINE_NO_NEW_PLAN')
            if plan and plan['source_sha256']!=digest(payload):raise ValueError('FROZEN_FORECAST_CHANGED')
            stressed_config={**variant['config'],'commission_bps':variant['config']['commission_bps']*2,'minimum_commission':variant['config']['minimum_commission']*2,'slippage_bps':variant['config']['slippage_bps']*3}
            stress=replay({**meta,'config':stressed_config},cal,plans,obs)
            results[name]=dict(cost_stress=measurements(stress['ledger'],stress['fills'],meta['initial_equity']),cost_stress_basis='同一冻结计划、佣金含最低收费乘2、滑点乘3；不重新调参',configuration=variant,plan=plan,metrics=measurements(result['ledger'],result['fills'],meta['initial_equity']),
                validation=validation(result,meta['initial_equity'],settings),ledger=result['ledger'],fills=result['fills'],execution_blockers=result['blocked'])
        if any(r['plan'] for r in results.values()):put(db,'inputs',day,inputs)
        db.commit();count=len(get(db,'inputs'))
    report=dict(status='SHADOW_COMPARISON_ACTIVE' if not alerts else 'BLOCKED_DATA',asof=day,target_session=target,generated_at=at.isoformat(),
        raw_prediction_sha256=digest(payload),original_model=payload['model'],snapshot_count=count,results=results,alerts=alerts,submitted_orders=0,
        active_strategy='原有研究模拟策略保持不变；九组方案为独立影子模拟，不替换正式模型',
        historical_validation='未重训或重建历史预测；当前完整快照不足以提供历史/滚动样本外收益结论。每日先冻结计划，再用次日真实行情结算。',
        risk_limits={'maximum_drawdown':settings['maximum_drawdown'],'minimum_equity':settings['minimum_equity'],'basis':'沿用正式准入15%回撤门槛、原配置120000元风险审查线；积极候选达到时提出退出，基线原规则不改；跳空、停牌可能使实际损失超限'},
        limitations=['原始排名仅展示；仓位优化沿用原评分与收益效用，不另造排名信号','阈值降低仅作用于最小交易金额和小幅不调整区间；净预期收益必须覆盖成本','未进行独立历史留出及极端行情验证，禁止宣称积极方案更优或自动升级'])
    write_json(folder/'report.json',report);publish(folder,report);return report

def publish(folder,r):
    e=lambda x:html.escape(str(x));pct=lambda x:'待积累' if x is None else f'{x:.2%}'
    page='<html lang="zh"><meta charset="utf-8"><title>风险预算同源对比</title><style>body{font:15px system-ui;margin:24px;color:#183047}table{border-collapse:collapse;width:100%}th,td{padding:7px 10px;border-bottom:1px solid #ddd;text-align:left;vertical-align:top;white-space:nowrap}.scroll{overflow:auto}details{margin:12px 0}p{line-height:1.6}</style><h1>基线与积极型策略：同源预测、独立影子模拟</h1>'
    page+='<p>'+e(r['asof']+' → '+r['target_session']+'；'+r['active_strategy'])+'</p><p>'+e(r['historical_validation'])+'</p><p>'+e(r['risk_limits'])+'</p><p><a href="report.json">完整配置差异、原始预测哈希、逐股原因和模拟记录</a></p>'
    page+='<div class="scroll"><table><tr><th>方案</th><th>目标总仓位</th><th>目标现金</th><th>预测年化波动率</th><th>累计净收益</th><th>最大回撤</th><th>成交笔数</th><th>验证</th></tr>'
    for name,item in r['results'].items():
        p=item['plan'];m=item['metrics'];page+='<tr>'+''.join('<td>'+e(x)+'</td>' for x in [name,pct(p['total_target_weight']) if p else '无新计划',pct(p['cash_target']) if p else '—',pct(p['predicted_annual_volatility']) if p else '—',pct(m['net_return']),pct(m['maximum_drawdown']),m['trades'] if m['trades'] is not None else '待结算','未通过；不自动启用'])+'</tr>'
    page+='</table></div>'
    for name,item in r['results'].items():
        page+='<details><summary>'+e(name)+'：配置差异与30只股票条件计划</summary><p>'+e(item['configuration']['differences'])+'</p><p>'+e(item['configuration']['policy_differences'])+'</p><div class="scroll"><table><tr><th>股票</th><th>原排名</th><th>原预测</th><th>当前股数</th><th>目标权重</th><th>参考目标股数</th><th>决策</th><th>原因</th></tr>'
        for x in (item['plan'] or {}).get('rows',[]):
            page+='<tr>'+''.join('<td>'+e(v)+'</td>' for v in [x['name']+' '+x['symbol'],x['original_rank'],pct(x['original_prediction']),x['current_shares'],pct(x['target_weight']),x['reference_target_shares'],x['decision']])+'<td style="white-space:normal;min-width:300px">'+e('；'.join(x['reasons']))+'</td></tr>'
        page+='</table></div><h3>净收益、风险、成本与资金使用（待结算时留空）</h3><div class="scroll"><table><tr><th>指标</th><th>原费用</th><th>费用压力情景</th></tr>'
        labels=['年化收益','零无风险利率夏普','最大回撤','年化波动率','成交笔数','累计单边换手','手续费（元）','扣费后净收益','平均资金利用率','最差单日收益','最差5%交易日平均收益']
        for key,label in zip(METRICS,labels):
            values=[item['metrics'].get(key),item['cost_stress'].get(key)]
            fmt=lambda v:'待积累' if v is None else str(round(v,4)) if key in ('sharpe_zero_rf','trades','fees') else pct(v)
            page+='<tr><td>'+e(label)+'</td>'+''.join('<td>'+e(fmt(v))+'</td>' for v in values)+'</tr>'
        page+='</table></div><p>目标股数不等于实际可成交股数；实际模拟按原执行引擎核验。'+e(item['validation']['reasons'])+'</p></details>'
    page+='<p>'+e('；'.join(r['limitations']+r['alerts']))+'</p></html>';atomic_text(folder/'index.html',page)
