"""Prospective research-only strategy epoch. Never promotes or touches old ledgers."""
from pathlib import Path
import hashlib,html,json,sqlite3
import numpy as np
import pandas as pd
from .core import now,cutoff,load_config,read_watchlist,read_snapshot,write_json,write_csv,atomic_text,quality,stock_config
from .paper_account import get,put,digest,opened,evidence,engine_version as legacy_engine_version
from .forecast import DEFAULTS as FORECAST_DEFAULTS,FEATURES,features_and_labels,fit_predict,validate_forecast,independent_rows
from .research_execution import replay
from .decision import DEFAULTS,covariance,optimize,load_fee_config,validate_config,finite,costs,rule_for


def enabled(root):
 p=Path(root)/'research_paper_config.json'
 return p.exists() and json.loads(p.read_text()).get('enabled') is True


def implementation():
 # This epoch must stop on changes to any function contributing predictions or fills.
 names=('slot_upgrade.py','research_policy.py','research_execution.py','research_paper.py','forecast.py','seven_metrics.py','probability_calibration.py','decision.py','execution_replay.py','paper_account.py','connections.py')
 return hashlib.sha256(b''.join((Path(__file__).parent/n).read_bytes() for n in names)).hexdigest()


def engine_version():
 return digest({'legacy':legacy_engine_version(),'research_execution':hashlib.sha256((Path(__file__).parent/'research_execution.py').read_bytes()).hexdigest()})


def forecast_candidate(snap,cal,day,h,cfg,candidate):
 cols=candidate['features'];alpha=candidate['alpha']
 f=features_and_labels(snap['hfq'],cal[cal<=pd.Timestamp(day)],h)
 c={**cfg,'ridge_alpha':alpha};oos,validation=validate_forecast(f,h,c,cols)
 train=f.dropna(subset=cols+['target','label_end']);train=train[train.label_end<=pd.Timestamp(day)].tail(cfg['training_cap']);test=f.iloc[[-1]]
 if test[cols].isna().any().any() or len(train)<cfg['minimum_train']:raise ValueError('RESEARCH_FEATURE_OR_TRAINING_MISSING')
 pred,artifact=fit_predict(train,test,alpha,cols);p=float(pred[0]);res=independent_rows(oos);errors=(res.actual-res.predicted).to_numpy()
 lo,hi=np.quantile(errors,[.1,.9]);prob=float((1+np.sum(errors>-p))/(len(errors)+2));vol=float(test.vol20.iloc[0]*np.sqrt(h))
 width=max(60,int(len(train)*.8));sub=[float(fit_predict(train.iloc[i:i+width],test,alpha,cols)[0][0]) for i in np.linspace(0,len(train)-width,24,dtype=int)]
 future=cal[cal>pd.Timestamp(day)]
 if len(future)<h:raise ValueError('RESEARCH_TARGET_CALENDAR_MISSING')
 # Comparator uses the same matured data and clock, never a later refit of old predictions.
 baseline_train=f.dropna(subset=FEATURES+['target','label_end']).tail(cfg['training_cap'])
 baseline=float(fit_predict(baseline_train,test,cfg['ridge_alpha'])[0][0])
 return dict(model='ridge-research-candidate',horizon=h,predicted_return=p,baseline_predicted_return=baseline,
  up_probability_empirical=prob,probability_method='smoothed nonoverlapping historical OOS residual frequency; research only',
  interval80_empirical=[float(p+lo),float(p+hi)],volatility_horizon=vol,downside_es95_normal_proxy=float(min(0,p-2.063*vol)),
  model_uncertainty=dict(status='RESEARCH_ESTIMATE',std=float(np.std(sub,ddof=1)),members=24,mean_prediction_interval80=np.quantile(sub,[.1,.9]).tolist()),
  validation=validation,forecast_target_date=str(future[h-1].date()),model_parameters=artifact,
  final_training_last_label_end=str(train.label_end.max().date()),model_status='UNVALIDATED_RESEARCH_PAPER'),oos


def predict_day(root,cal,day,spec):
 watch=read_watchlist(root);base=load_config(root);rows=[];oos=[];versions={}
 from .slot_upgrade import registry,choice
 upgrades=registry(root)
 if watch.symbol.tolist()!=spec['universe']:raise ValueError('RESEARCH_UNIVERSE_CHANGED_REQUIRES_NEW_EPOCH')
 for item in watch.itertuples():
  snap=read_snapshot(root,item.symbol)
  if snap is None or snap['meta'].get('synthetic') or snap['meta'].get('source')!='tencent':raise ValueError('RESEARCH_UNVERIFIED_DATA:'+item.symbol)
  if pd.Timestamp(snap['meta']['fetched_at'])>pd.Timestamp(now()):raise ValueError('FUTURE_SNAPSHOT')
  q=quality(snap['raw'],cal,stock_config(base,item.symbol),pd.Timestamp(day))
  if not q['fresh'] or not q['calendar_complete']:raise ValueError('RESEARCH_DATA_INCOMPLETE:'+item.symbol)
  forecasts={}
  for h in (1,5,20):
   candidate,upgrade=choice(upgrades,item.symbol,h,spec['candidate'])
   f,o=forecast_candidate(snap,cal,day,h,spec['forecast_config'],candidate)
   f['model_features']=candidate['features']
   if upgrade:
    f['prediction_upgrade']=upgrade
    f['model_status']='RESEARCH_TRIAL' if upgrade['scope']=='RESEARCH_TRIAL_ONLY' else 'PREDICTIVE_PROCEDURE_VALIDATED_RESEARCH_EXECUTION'
   forecasts[str(h)]=f
   o['symbol']=item.symbol;o['horizon']=h;oos.append(o)
  rows.append(dict(symbol=item.symbol,name=item.name,sector=spec['forecast_config']['sector_groups'].get(item.symbol,item.sector),close=float(snap['raw'].close.iloc[-1]),last_date=day,
   price_source=snap['meta']['source'],price_fetched_at=snap['meta']['fetched_at'],forecasts=forecasts,data_eligible=bool(rule_for(item.symbol) and not any(t in item.name.upper() for t in ('ST','退')) and snap['raw'].volume.iloc[-1]>0 and snap['raw'].amount.tail(20).mean()>=base['min_average_amount'])))
  versions[item.symbol]=snap['meta']['version']
 from .probability_calibration import calibrate,predict
 for h in (1,5,20):
  report=calibrate(pd.concat([x for x in oos if x.horizon.iloc[0]==h]),day)
  for row in rows:
   f=row['forecasts'][str(h)];f['probability_calibration']=report
   if report.get('model'):f['up_probability_calibrated']=float(predict(report['model'],np.array([f['predicted_return']]))[0])
 payload=dict(asof=day,generated_at=now().isoformat(),model=spec['version'],config=spec['forecast_config'],data_versions=versions,forecasts=rows,errors=[],mode='RESEARCH_PAPER_ONLY')
 from .seven_metrics import enrich
 enrich(root,payload,watch)
 return payload


def targets(payload,positions,equity,cov,c,environment):
 rows=payload['forecasts'];c={**c,'market_environment':environment};validate_config(c)
 c['max_invested']=min(c['max_invested'],c['market_exposure'][environment]);mu=[];allowed=[];reasons=[]
 from .probability_display import fields
 for r in rows:
  f=r['forecasts'][str(c['horizon'])];p=fields(f)['research_up_probability'];reason=[]
  score=r['composite_score']['score'];ticket=equity*c['max_stock'];roundtrip=(costs(ticket,'BUY',c,r['symbol'])['total']+costs(ticket,'SELL',c,r['symbol'])['total'])/ticket
  if environment in ('risk_off','unknown'):reason.append('市场环境偏弱或未确认，既定规则要求现金防守')
  if not r['data_eligible']:reason.append('行情/名称/流动性不满足要求')
  if score is None:reason.append('九因素评分缺项')
  if p is None or p<c['minimum_probability']:reason.append('研究上涨概率不足')
  if not finite(f.get('predicted_return')) or f['predicted_return']<=roundtrip:reason.append('预测收益未覆盖往返费用')
  if not finite(f.get('downside_es95_normal_proxy')) or abs(f['downside_es95_normal_proxy'])>c['max_downside']:reason.append('下行风险超过限制')
  interval=f.get('interval80_empirical')
  if not interval or not all(finite(v) for v in interval) or interval[1]-interval[0]>c['max_uncertainty']:reason.append('预测区间过宽或缺失')
  # Unvalidated probability/forecast remains visible, deliberately not an execution gate in this paper experiment.
  mu.append(max(0.,f['predicted_return']-roundtrip)*((score or 0)/100));allowed.append(not reason);reasons.append(reason)
 weights=optimize('constrained_score',mu,cov,[r['sector'] for r in rows],c,allowed)
 output=[]
 for r,w,reason in zip(rows,weights,reasons):
  current=positions.get(r['symbol'],{}).get('shares',0);target=int(w*equity/r['close'])//100*100
  output.append(dict(symbol=r['symbol'],name=r['name'],sector=r['sector'],current_shares=current,current_weight=current*r['close']/equity,
   target_weight=float(w),target_amount=float(w*equity),reference_target_shares=target,
   direction='BUY' if target>current else 'SELL' if target==0 and current else 'REDUCE' if target<current else 'HOLD',
   reasons=reason or ['研究概率、净收益、风险及九因素评分形成约束目标'],reference_price=r['close'],price_date=payload['asof'],price_source=r['price_source'],
   probability=fields(r['forecasts'][str(c['horizon'])]),executable=False))
 return dict(zip([r['symbol'] for r in rows],map(float,weights))),output


def freeze(payload,at,cal,spec,meta,positions,equity,cov,environment):
 day=payload['asof'];future=cal[cal>pd.Timestamp(day)]
 if not len(future):raise ValueError('NEXT_SESSION_UNKNOWN')
 target=str(future[0].date())
 if pd.Timestamp(at)>=opened(target):raise ValueError('MISSED_PREOPEN_DEADLINE_NO_BACKFILL')
 if pd.Timestamp(payload['generated_at'])>pd.Timestamp(at):raise ValueError('FUTURE_FORECAST')
 c=meta['config']
 if not c.get('fees_verified'):raise ValueError('FEES_UNVERIFIED')
 if spec.get('policy'):
  from .research_policy import targets as adaptive_targets
  w,rows=adaptive_targets(payload,positions,equity,cov,c,environment,spec['policy'])
 else:w,rows=targets(payload,positions,equity,cov,c,environment)
 w={s:v for s,v in w.items() if v>0 or positions.get(s,{}).get('shares',0)>0}
 return dict(target_session=target,origin_session=day,registered_at=pd.Timestamp(at).isoformat(),source_sha256=digest(payload),model_version=spec['version'],
  status='FROZEN_RESEARCH_TARGETS',total_target_weight=sum(w.values()),market_budget=min(c['max_invested'],spec.get('policy',{}).get('exposure',c['market_exposure']).get(environment,0.)),policy_version=spec.get('policy',{}).get('version','v1'),risk_priorities={r['symbol']:r.get('risk_priority',0.) for r in rows},weights=w,rows=rows,blockers=[],mode='RESEARCH_PAPER_ONLY',environment=environment,
  note='目标与本次成交数量分离；次日真实行情结算，按费用/换手/可卖/成交量限制分步执行；未通过正式验证')


def settle_predictions(root,db,predictions,asof):
 existing=get(db,'outcomes')
 for origin,payload in predictions.items():
  for row in payload['forecasts']:
   for h,f in row['forecasts'].items():
    key=f'{origin}/{row["symbol"]}/{h}';end=f['forecast_target_date']
    if key in existing or end>asof:continue
    snap=read_snapshot(root,row['symbol'])
    if snap is None or snap['meta'].get('synthetic') or snap['meta'].get('source')!='tencent':continue
    if snap['meta'].get('fetched_at') and pd.Timestamp(snap['meta']['fetched_at'])>pd.Timestamp(now()):continue
    prices=snap['hfq'].set_index('date').close
    if pd.Timestamp(origin) not in prices or pd.Timestamp(end) not in prices:continue
    actual=float(prices.loc[pd.Timestamp(end)]/prices.loc[pd.Timestamp(origin)]-1)
    from .probability_display import fields
    p=fields(f)['research_up_probability']
    if not finite(actual):continue
    value=dict(origin=origin,target=end,symbol=row['symbol'],horizon=int(h),actual=actual,predicted=f['predicted_return'],baseline=f['baseline_predicted_return'],
      absolute_error=abs(actual-f['predicted_return']),baseline_absolute_error=abs(actual-f['baseline_predicted_return']),brier=(p-float(actual>0))**2 if p is not None else None,
      model_version=payload['model'],source_version=snap['meta']['version'],settled_at=now().isoformat(),formal_validation=False)
    put(db,'outcomes',key,value)


def run(root):
 root=Path(root)
 if not enabled(root):return {'status':'DISABLED'}
 from .data import calendar
 at=now();cal,_=calendar(root,load_config(root),cutoff(at),offline=True);cal=cal.sort_values();day=str(cal[cal<=cutoff(at)][-1].date())
 path=root/'data/research_paper.sqlite3';alerts=[]
 with sqlite3.connect(path) as db:
  path.chmod(0o600);db.execute('CREATE TABLE IF NOT EXISTS records(kind TEXT,key TEXT,body TEXT,sha256 TEXT,PRIMARY KEY(kind,key))');db.commit();db.execute('BEGIN IMMEDIATE')
  specs=get(db,'spec');metas=get(db,'meta')
  if not specs:
   control=root/'data/paper_account.sqlite3'
   if not control.exists():raise ValueError('SETTLED_PAPER_ANCHOR_REQUIRED')
   with sqlite3.connect('file:'+str(control)+'?mode=ro',uri=True) as old:
    nav=get(old,'nav');oldmeta=get(old,'meta')['account'];oldobs=get(old,'observations')
   if day not in nav or day not in oldobs:raise ValueError('SAME_DAY_SETTLED_PAPER_ANCHOR_REQUIRED')
   latest=nav[day];study=json.loads((root/'reports/iteration/latest.json').read_text());studies=[x for x in study['studies'] if not x.get('errors') and finite(x.get('mean_gain'))]
   if study['asof']!=day:raise ValueError('CURRENT_CANDIDATE_RESEARCH_REQUIRED')
   if study.get('version')=='slot-v2':
    candidate=dict(alpha=FORECAST_DEFAULTS['ridge_alpha'],features=FEATURES)
    chosen=dict(candidate=candidate,mean_gain=None,basis='BASELINE_WITH_SEPARATE_SLOT_TRIALS')
   else:
    if not studies:raise ValueError('CURRENT_CANDIDATE_RESEARCH_REQUIRED')
    chosen=max(studies,key=lambda x:x['mean_gain']);candidate=chosen['candidate']
   if candidate['alpha'] not in (1.,10.,100.) or not candidate['features'] or not set(candidate['features'])<=set(FEATURES):raise ValueError('UNKNOWN_RESEARCH_CANDIDATE')
   cfg={**FORECAST_DEFAULTS,**json.loads((root/'forecast_config.json').read_text())}
   spec=dict(candidate=candidate,selected_at=at.isoformat(),selected_asof=day,selection_basis='用户授权研究模拟；历史搜索最高改善候选，仅探索，未通过正式筛选',historical_study=chosen,
    implementation=implementation(),forecast_config=cfg,universe=read_watchlist(root).symbol.tolist(),method='constrained_score',formal_validated=False)
   spec['version']='research-'+digest(spec)[:16]
   future=cal[cal>pd.Timestamp(day)]
   if not len(future) or at>=opened(str(future[0].date())):raise ValueError('NO_FUTURE_START_SESSION')
   marks={s:oldobs[day]['bars'][s]['close'] for s in latest['positions']}
   meta=dict(start_session=str(future[0].date()),anchor_session=day,created_at=at.isoformat(),initial_cash=latest['cash'],initial_positions=latest['positions'],initial_marks=marks,initial_equity=latest['equity'],
    original_initial_equity=oldmeta['initial_equity'],prior_ledger=[nav[k] for k in sorted(nav)],config=oldmeta['config'],engine_version=engine_version(),model_version=spec['version'],mode='RESEARCH_PAPER_ONLY')
   put(db,'spec','active',spec);put(db,'meta','account',meta)
  else:spec=get(db,'spec_revisions').get('v2',specs['active']);meta=get(db,'meta_revisions').get('v2',metas['account'])
  if reviewed_implementation(db,spec)!=implementation() or meta['engine_version']!=engine_version():raise ValueError('RESEARCH_IMPLEMENTATION_CHANGED_REQUIRES_REVIEW')
  current={**DEFAULTS,**json.loads((root/'decision_config.json').read_text())};load_fee_config(root,current)
  for k,v in meta['config'].items():
   if k in current and current[k]!=v:raise ValueError('RESEARCH_FROZEN_CONFIG_CHANGED:'+k)
  plans={**get(db,'plans'),**get(db,'plan_revisions')};observations=get(db,'observations');predictions=get(db,'predictions')
  for date in cal[(cal>=pd.Timestamp(meta['start_session']))&(cal<=pd.Timestamp(day))]:
   date=str(date.date())
   if date in observations:continue
   symbols=set(meta['initial_positions'])|{s for d,p in plans.items() if d<=date for s,w in p['weights'].items() if w>0}
   try:o=evidence(root,date,symbols)
   except (ValueError,KeyError,OSError) as exc:alerts.append(str(exc));break
   if date not in plans:alerts.append('NO_PREOPEN_PLAN:'+date)
   put(db,'observations',date,o);observations[date]=o
  result=replay(meta,cal,plans,observations)
  for row in result['ledger']:put(db,'nav',row['date'],row)
  for i,fill in enumerate(result['fills']):put(db,'fills',str(i),fill)
  settle_predictions(root,db,predictions,day)
  last=result['ledger'][-1] if result['ledger'] else None
  positions=last['positions'] if last else meta['initial_positions'];cash=last['cash'] if last else meta['initial_cash'];equity=last['equity'] if last else meta['initial_equity']
  current_nav=(last and last['date']==day) or day==meta['anchor_session']
  # Commit mature outcomes even if today's new inputs fail; never invent a plan.
  db.commit();db.execute('BEGIN IMMEDIATE')
  payload=predictions.get(day);plan=None
  try:
   if not current_nav:raise ValueError('RESEARCH_ACCOUNT_VALUATION_STALE')
   if payload is None:
    payload=predict_day(root,cal,day,spec)
    nextday=str(cal[cal>pd.Timestamp(day)][0].date())
    if at>=opened(nextday):raise ValueError('MISSED_PREOPEN_DEADLINE_NO_BACKFILL')
    put(db,'predictions',day,payload)
   future=cal[cal>pd.Timestamp(day)];target=str(future[0].date())
   if target in plans:plan=plans[target]
   else:
    histories=pd.concat({s:read_snapshot(root,s)['hfq'].set_index('date').close.pct_change(fill_method=None) for s in spec['universe']},axis=1).loc[:day].dropna().tail(meta['config']['covariance_days'])
    cov=covariance(histories.to_numpy(),meta['config']['minimum_covariance_days'],meta['config']['horizon'])
    from .connections import market_environment
    environment_evidence=market_environment(root,day);environment=environment_evidence['state']
    plan=freeze(payload,now(),cal,spec,meta,positions,equity,cov,environment)
    plan['market_environment_evidence']=environment_evidence
    put(db,'plans',target,plan);plans[target]=plan
  except (ValueError,KeyError,OSError,IndexError) as exc:alerts.append('RESEARCH_PLAN_BLOCKED:'+str(exc))
  db.commit();outcomes=list(get(db,'outcomes').values())
 report=dict(status='ACTIVE_RESEARCH_PAPER' if plan and current_nav else 'BLOCKED_DATA',generated_at=now().isoformat(),asof=day,spec=spec,meta=meta,equity=equity,cash=cash,positions=positions,
  valuation_date=last['date'] if last else meta['anchor_session'],daily_return=(last['daily_return'] if last else meta['prior_ledger'][-1]['daily_return']) if current_nav else None,
  ledger=result['ledger'],fills=result['fills'],execution_blockers=result['blocked'],alerts=alerts,plan=plan,predictions=payload,outcomes=outcomes,submitted_orders=0,
  cumulative_from_original=equity/meta['original_initial_equity']-1,research_segment_return=equity/meta['initial_equity']-1,
  note='研究模型用于独立模拟，未获正式资格；旧账本保留作对照。新计划开盘前冻结，次日实际行情到齐后模拟结算；绝无真实下单。')
 # Reference-only preview uses today's verified bar with open replaced by close, explicitly not a future observation or ledger fill.
 if plan and current_nav:
  try:
   symbols=set(positions)|{s for s,w in plan['weights'].items() if w>0};o=evidence(root,day,symbols)
   from .research_execution import simulate
   prior=cal[cal<pd.Timestamp(day)][-1];bars={s:pd.DataFrame([{**b,'open':b['close']}],index=pd.DatetimeIndex([day])) for s,b in o['bars'].items()}
   preview=simulate(pd.DatetimeIndex([prior,pd.Timestamp(day)]),bars,{str(prior.date()):plan['weights']},{(s,day):v for s,v in o['states'].items()},meta['config'],cash,positions,{s:o['bars'][s]['close'] for s in positions},priorities={str(prior.date()):plan.get('risk_priorities',{})})
   report['reference_scenario']=dict(basis='仅以今日收盘作为模拟开盘参考的情景，不是明日行情，不入账；实际数量以明日真实开盘重新计算',reference_session=day,target_session=plan['target_session'],fills=[dict(symbol=x['symbol'],side=x['side'],shares=x['shares'],reference_price=x['price'],estimated_fee=x['fee'],estimated_cash_after=x['cash_after']) for x in preview['fills']],cash_after=preview['ledger'][-1]['cash'],blocked=preview['blocked'])
  except (ValueError,KeyError,OSError) as exc:report['reference_scenario']={'blocked':str(exc),'fills':[]}
 publish(root,report);return report


def publish(root,r):
 folder=Path(root)/'reports/research_paper/latest';folder.mkdir(parents=True,exist_ok=True);write_json(folder/'report.json',r)
 archive=Path(root)/'reports/research_paper'/now().strftime('%Y%m%d-%H%M%S-%f');write_json(archive/'report.json',r)
 write_csv(folder/'模拟成交.csv',pd.DataFrame(r['fills']))
 combined=[]
 for x in r['meta']['prior_ledger']+r['ledger']:
  combined.append({**x,'cumulative_return':x['equity']/r['meta']['original_initial_equity']-1,'strategy_segment':'原模拟' if x['date']<=r['meta']['anchor_session'] else r['spec']['version']})
 write_csv(folder/'每日及累计模拟收益.csv',pd.DataFrame(combined));write_csv(folder/'预测到期对照.csv',pd.DataFrame(r['outcomes']))
 e=html.escape
 page='<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>研究模型模拟交易</title><style>body{font:15px/1.5 system-ui;margin:24px;color:#18334a}table{border-collapse:collapse;width:100%}td,th{padding:6px 8px;text-align:left;border-bottom:1px solid #ddd;vertical-align:top}.scroll{overflow:auto}</style><h1>当前研究模型 · 每日预测与模拟交易</h1>'
 page+=f'<p>{e(r["note"])}</p><p>行情日期 {r["asof"]}；状态 {r["status"]}；版本 {r["spec"]["version"]}；固定候选参数 {r["spec"]["candidate"]["alpha"]}，特征数 {len(r["spec"]["candidate"]["features"])}。每天用成熟数据重训，参数方案不随短期盈亏调整。</p>'
 page+=f'<p>切换日 {r["meta"]["anchor_session"]}；新策略首个模拟交易日 {r["meta"]["start_session"]}；模拟权益 {r["equity"]:,.2f} 元；现金 {r["cash"]:,.2f} 元；接续原始基准累计收益 {r["cumulative_from_original"]:.2%}；新模型段收益 {r["research_segment_return"]:.2%}；新段模拟成交 {len(r["fills"])} 笔。</p>'
 page+='<p>'+e('；'.join(r['alerts']))+'</p><p><a href="每日及累计模拟收益.csv">连续模拟收益单</a> · <a href="模拟成交.csv">新模型成交记录</a> · <a href="预测到期对照.csv">到期误差对照</a> · <a href="report.json">完整版本与来源</a></p>'
 if r['spec'].get('policy'):
  page+='<p>研究规则v2：偏弱/中性/偏强总仓位上限30%/45%/60%；个股按净收益、概率可信度、不确定性、下行风险、协方差及行业上限分别配置；不要求配满。未知市场或缺少输入时不生成新交易；减仓按冻结的风险评分降序，代码仅用于同分时稳定排序。</p>'
 plan=r.get('plan');preview=r.get('reference_scenario',{});by={x['symbol']:x for x in preview.get('fills',[])}
 if plan:
  if plan.get('policy_version')=='individual-risk-budget-v2':page+=f'<p>市场股票仓位预算上限：{plan["market_budget"]:.2%}；个股配置合计研究目标：{plan["total_target_weight"]:.2%}；其余目标为现金。目标配置不是本次成交后仓位，初始超额持仓受换手等约束分步调整。</p>'
  page+=f'<h2>{plan["target_session"]} 条件性模拟计划</h2><p>方法：市场风险预算与个股收益/风险约束配置；市场环境 {e(plan["environment"])}。不称为最优组合。研究收益目标可触发减仓，单只/行业/总仓位限制同时生效。目标需分步实现，不能把参考数量当作已成交。</p><p>'+e(preview.get('basis','参考数量暂不可计算'))+f'；参考现金 {preview.get("cash_after",r["cash"]):,.2f} 元。</p>'
  page+='<div class="scroll"><table><tr><th>股票</th><th>当前仓位</th><th>研究目标</th><th>目标方向</th><th>本次参考动作/股数</th><th>价格/日期/来源</th><th>理由</th></tr>'
  for x in plan['rows']:
   fill=by.get(x['symbol']);action=fill['side'] if fill else 'HOLD' if x['direction']=='HOLD' else 'NO_TRADE'
   page+='<tr>'+''.join('<td>'+e(str(v))+'</td>' for v in [x['name']+' '+x['symbol'],f'{x["current_weight"]:.2%}',f'{x["target_weight"]:.2%}',x['direction'],f'{action} / {fill["shares"] if fill else 0}',f'{x["reference_price"]:.2f} / {x["price_date"]} / {x["price_source"]}','；'.join(x['reasons'])+('；本次参考数量受换手/费用/资金或成交条件限制' if not fill and x['direction']!='HOLD' else '')])+ '</tr>'
  page+='</table></div><p>执行限制与未成交原因：'+e(str(preview.get('blocked',{})))+'</p>'
 page+='<h2>每天如何更新与改进</h2><p>收盘更新真实输入 → 固定候选重训并保存90组预测 → 冻结次日模拟计划 → 次日核验行情及规则后模拟记账 → 1/5/20日到期后记录收益误差、概率误差与原模型对照。每日有建议，不要求每日成交；模拟业绩不自动授予正式资格。</p>'
 page+=f'<p>已到期预测 {len(r["outcomes"])} 组；未到期不计算命中率。候选历史搜索及模拟结果均为研究证据，不代替冻结的正式独立验证。</p>'
 if r['outcomes']:
  mae=float(np.mean([x['absolute_error'] for x in r['outcomes']]));base=float(np.mean([x['baseline_absolute_error'] for x in r['outcomes']]))
  page+=f'<p>已到期收益绝对误差均值：{mae:.4%}；同期原模型对照：{base:.4%}。不同期限混合仅作诊断，不能作为独立验收。</p>'
 if r.get('predictions'):
  from .seven_metrics import export
  page+=export(r['predictions']['seven_metrics'],folder)
 atomic_text(folder/'index.html',page+'</html>')


def activate_policy(root,expected_implementation,authorization):
 """Explicit reviewed one-time pre-open revision. Never called by daily jobs."""
 from .research_policy import POLICY
 from .data import calendar
 from .connections import market_environment
 root=Path(root);at=now();cal,_=calendar(root,load_config(root),cutoff(at),offline=True)
 with sqlite3.connect(root/'data/research_paper.sqlite3') as db:
  db.execute('BEGIN IMMEDIATE');old=get(db,'spec')['active'];meta=get(db,'meta')['account']
  if get(db,'spec_revisions'):raise ValueError('REVISION_ALREADY_REGISTERED')
  if old['implementation']!=expected_implementation or not authorization:raise ValueError('REVISION_AUTHORIZATION_OR_BASE_MISMATCH')
  if get(db,'observations') or get(db,'nav') or get(db,'fills'):raise ValueError('CANNOT_REVISE_SETTLED_RESEARCH_EPOCH')
  if at>=opened(meta['start_session']):raise ValueError('CANNOT_REVISE_AFTER_OPEN')
  day=str(cal[cal<=cutoff(at)][-1].date())
  if day!=meta['anchor_session']:raise ValueError('REVISION_ANCHOR_STALE')
  payload=get(db,'predictions').get(day)
  if payload is None:raise ValueError('ORIGINAL_PREDICTIONS_REQUIRED')
  new={**old,'policy':POLICY,'implementation':implementation(),'previous_strategy_version':old['version'],'revision_at':at.isoformat(),'revision_reason':authorization,'forecast_parameters_unchanged':True}
  new['version']='research-policy-v2-'+digest(new)[:12]
  revised_meta={**meta,'engine_version':engine_version(),'model_version':new['version']}
  histories=pd.concat({s:read_snapshot(root,s)['hfq'].set_index('date').close.pct_change(fill_method=None) for s in new['universe']},axis=1).loc[:day].dropna().tail(meta['config']['covariance_days'])
  cov=covariance(histories.to_numpy(),meta['config']['minimum_covariance_days'],meta['config']['horizon']);environment=market_environment(root,day)
  plan=freeze(payload,at,cal,new,revised_meta,meta['initial_positions'],meta['initial_equity'],cov,environment['state']);plan['market_environment_evidence']=environment
  previous=get(db,'plans').get(plan['target_session']);plan['revision']=dict(previous_sha256=digest(previous) if previous else None,reason=authorization,registered_at=at.isoformat(),prior_plan_preserved=True)
  put(db,'spec_revisions','v2',new);put(db,'meta_revisions','v2',revised_meta);put(db,'plan_revisions',plan['target_session'],plan)
  put(db,'revision_audit','v2',dict(at=at.isoformat(),authorization=authorization,old_spec_sha=digest(old),old_meta_sha=digest(meta),new_spec_sha=digest(new),new_plan_sha=digest(plan),unchanged_prediction_sha=digest(payload),effective_session=plan['target_session']))
  db.commit()
 return {'version':new['version'],'effective_session':plan['target_session'],'old_plan_preserved':True,'formal_model_changed':False}


def reviewed_implementation(db,spec):
 current=spec['implementation']
 for key,review in sorted(get(db,'implementation_reviews').items()):
  if review['from']!=current or review['spec_sha']!=digest(spec):raise ValueError('RESEARCH_REVIEW_CHAIN_INVALID')
  current=review['to']
 return current


def approve_implementation(root,expected,validation_sha,reason):
 """Explicit maintenance review; no frozen predictions, plans or ledger rewrites."""
 import re
 from .core import run_lock
 if not reason.strip() or not re.fullmatch('[a-f0-9]{64}',validation_sha):raise ValueError('MAINTENANCE_PROOF_REQUIRED')
 root=Path(root)
 with run_lock(root),sqlite3.connect(root/'data/research_paper.sqlite3') as db:
  db.execute('BEGIN IMMEDIATE')
  spec=get(db,'spec_revisions').get('v2',get(db,'spec')['active'])
  current=reviewed_implementation(db,spec)
  if current!=expected:raise ValueError('RESEARCH_REVIEW_BASE_MISMATCH')
  if current==implementation():return {'status':'ALREADY_REVIEWED'}
  key=f"{len(get(db,'implementation_reviews'))+1:012d}"
  review=dict(at=now().isoformat(),from_code=current,to_code=implementation(),spec_sha=digest(spec),validation_sha=validation_sha,reason=reason)
  review['from']=review.pop('from_code');review['to']=review.pop('to_code')
  put(db,'implementation_reviews',key,review)
 return dict(status='REVIEW_RECORDED',revision=key)
