"""GARCH(1,1) and causal Gaussian HMM research; never smooth future states."""
from pathlib import Path
import hashlib,json,sqlite3,logging,math
import numpy as np
import pandas as pd
from scipy.special import logsumexp
from .core import now,cutoff,load_config,read_watchlist,read_snapshot,write_json,write_csv
from .data import calendar
from .indices import load_index
from .model_research import sign_pvalue,holm
from .forecast import independent_rows

SPEC={'garch':'zero-mean normal GARCH(1,1)','hmm_states':3,'hmm_iterations':200,'seed':1729,'train_cap':756,'minimum_train':504,'refit_days':21,'validation_days':252,'horizons':[1,5,20],'minimum_independent':12,'minimum_forward_origins':63,'alpha':.05,'cost_proxy':.001,'exposure':{'risk_off':0.,'neutral':.4,'risk_on':.6}}

def fingerprint():
 directory=Path(__file__).parent
 files=['risk_models.py','forecast.py','core.py','data.py','indices.py','model_research.py','model_monitor.py']
 return hashlib.sha256(b''.join(name.encode()+b'\0'+(directory/name).read_bytes() for name in files)).hexdigest()

def research_account(root):
 root=Path(root);path=root/'data/decision_account.json'
 if path.exists():return json.loads(path.read_text()),False
 config=root/'decision_config.json'
 if config.exists() and json.loads(config.read_text()).get('require_real_account'):
  raise ValueError('已确认真实账户但快照缺失，禁止假设现金账户')
 return dict(cash=150000.,available_cash=150000.,positions={}),True
def qlike(actual,variance):
 v=np.maximum(np.asarray(variance,dtype=float),1e-12)
 return np.log(v)+np.asarray(actual)/v

def garch_fit(returns):
 from arch import arch_model
 a=np.asarray(returns,float)
 if len(a)<SPEC['minimum_train'] or not np.isfinite(a).all():raise ValueError('GARCH训练数据不足/缺失')
 res=arch_model(a*100,mean='Zero',vol='GARCH',p=1,q=1,dist='normal',rescale=False).fit(disp='off',show_warning=False,options={'maxiter':500})
 if res.convergence_flag:raise ValueError('GARCH优化未收敛')
 p=res.params;omega=float(p['omega'])/10000;alpha=float(p['alpha[1]']);beta=float(p['beta[1]'])
 if omega<=0 or min(alpha,beta)<0 or alpha+beta>=1:raise ValueError('GARCH非平稳/参数无效')
 return dict(omega=omega,alpha=alpha,beta=beta,last_variance=float(res.conditional_volatility[-1]**2)/10000,last_return=float(a[-1]))

def garch_step(state,observed):
 v=state['omega']+state['alpha']*state['last_return']**2+state['beta']*state['last_variance']
 state={**state,'last_variance':v,'last_return':float(observed)}
 nxt=state['omega']+state['alpha']*observed**2+state['beta']*v
 forecast=[]
 for _ in range(20):
  forecast.append(nxt);nxt=state['omega']+(state['alpha']+state['beta'])*nxt
 return state,np.cumsum(forecast)

def filter_step(prob,transition,means,variances,observation,initial=False):
 prior=prob if initial else prob@transition
 log_density=-.5*np.sum(np.log(2*np.pi*variances)+(np.asarray(observation)-means)**2/variances,axis=1)
 logp=np.log(np.maximum(prior,1e-300))+log_density
 return np.exp(logp-logsumexp(logp))

def hmm_fit(features):
 from hmmlearn.hmm import GaussianHMM
 a=np.asarray(features,float);mu=a.mean(0);sd=a.std(0);sd[sd<1e-8]=1
 if len(a)<SPEC['minimum_train'] or not np.isfinite(a).all():raise ValueError('HMM训练数据不足/缺失')
 x=(a-mu)/sd;model=GaussianHMM(n_components=3,covariance_type='diag',n_iter=SPEC['hmm_iterations'],tol=1e-3,min_covar=.001,random_state=SPEC['seed']).fit(x)
 if len(model.monitor_.history)<2 or abs(model.monitor_.history[-1]-model.monitor_.history[-2])>.01:raise ValueError('HMM迭代未收敛')
 means=model.means_;var=np.maximum(model._covars_,1e-6);rawmean=means*sd+mu
 high=int(np.argmax(rawmean[:,1]));remaining=[i for i in range(3) if i!=high];on=max(remaining,key=lambda i:rawmean[i,0]);labels=['neutral']*3;labels[high]='risk_off';labels[on]='risk_on'
 p=model.startprob_
 for i,row in enumerate(x):p=filter_step(p,model.transmat_,means,var,row,initial=i==0)
 return {'mu':mu,'sd':sd,'means':means,'variance':var,'transition':model.transmat_,'probability':p,'labels':labels,'return_second_moment':var[:,0]*sd[0]**2+rawmean[:,0]**2}

def hmm_step(state,observation):
 p=filter_step(state['probability'],state['transition'],state['means'],state['variance'],(np.asarray(observation)-state['mu'])/state['sd']);state={**state,'probability':p}
 nxt=p.copy();rv=[]
 for _ in range(20):nxt=nxt@state['transition'];rv.append(float(nxt@state['return_second_moment']))
 exposure=sum(float(p[i])*SPEC['exposure'][label] for i,label in enumerate(state['labels']))
 return state,np.cumsum(rv),exposure

def trend_states(close):
 ma20=close.rolling(20).mean();ma60=close.rolling(60).mean();vol=close.pct_change(fill_method=None).rolling(20).std()*np.sqrt(252)
 return pd.Series(np.where((close<ma60)|(vol>.35),'risk_off',np.where((close>ma20)&(ma20>ma60),'risk_on','neutral')),index=close.index).where(ma60.notna())

def outcomes(returns,h):
 f=pd.concat([returns.shift(-i).rename(i) for i in range(1,h+1)],axis=1)
 return f.pow(2).sum(axis=1,min_count=h),(1+f).prod(axis=1,min_count=h)-1

def evaluate(kind,symbol,close):
 returns=close.pct_change(fill_method=None);features=pd.concat([returns,returns.rolling(20).std()],axis=1);states=trend_states(close)
 allrows=[];latest=[];failures=[];audits=[]
 start=max(SPEC['minimum_train']+25,len(close)-SPEC['validation_days']-20)
 model=None
 for idx in range(start,len(close)):
  day=close.index[idx]
  if (idx-start)%SPEC['refit_days']==0 or model is None:
   # Missing stock sessions are not compressed into invented consecutive returns.
   hist=returns.iloc[max(1,idx-SPEC['train_cap']):idx]
   feat=features.iloc[max(20,idx-SPEC['train_cap']):idx]
   try:
    if kind=='garch':model=garch_fit(hist)
    else:model=hmm_fit(feat)
    audits.append({'origin':str(day.date()),'last_training_date':str(close.index[idx-1].date()),'rows':len(hist if kind=='garch' else feat)})
   except Exception as e:failures.append({'date':str(day.date()),'error':str(e)});model=None;continue
  if not np.isfinite(returns.iloc[idx]) or (kind=='hmm' and features.iloc[idx].isna().any()):model=None;continue
  if kind=='garch':model,pred=garch_step(model,returns.iloc[idx]);exposure=None
  else:model,pred,exposure=hmm_step(model,features.iloc[idx]);
  for h in SPEC['horizons']:
   baseline=float(returns.iloc[:idx+1].tail(20).var()*h)
   if kind=='hmm':
    # Rule-conditioned risk estimate uses only forward labels already matured before this refit.
    fit_end=(idx-start)//SPEC['refit_days']*SPEC['refit_days']+start
    past_rv,_=outcomes(returns.iloc[:fit_end],h);mask=states.iloc[:fit_end]==states.iloc[idx]
    values=past_rv[mask].dropna().tail(SPEC['train_cap'])
    if len(values)>=20:baseline=float(values.mean())
   item={'kind':kind,'symbol':symbol,'horizon':h,'date':day,'candidate_variance':float(pred[h-1]),'baseline_variance':baseline,'candidate_exposure':exposure,'baseline_exposure':SPEC['exposure'].get(states.iloc[idx],0.) if kind=='hmm' else None}
   if idx+h<len(close):
    future=returns.iloc[idx+1:idx+h+1]
    if future.notna().all():allrows.append({**item,'label_end':close.index[idx+h],'actual':float((future**2).sum()),'market_return':float((1+future).prod()-1)})
   if idx==len(close)-1:latest.append({**item,'probabilities':model['probability'].tolist() if kind=='hmm' else None,'state_labels':model['labels'] if kind=='hmm' else None})
 return pd.DataFrame(allrows),latest,failures,audits

def policy_metrics(rows):
 result={}
 for name in ['candidate','baseline']:
  w=rows[name+'_exposure'].to_numpy(float);ret=rows.market_return.to_numpy(float);previous=np.r_[0.,w[:-1]]
  net=w*ret-SPEC['cost_proxy']*np.abs(w-previous)
  if len(net):net[-1]-=SPEC['cost_proxy']*w[-1]
  equity=np.r_[1.,np.cumprod(1+net)];drawdown=1-equity/np.maximum.accumulate(equity)
  result[name+'_net_return']=float(equity[-1]-1);result[name+'_max_drawdown']=float(drawdown.max());result[name+'_turnover']=float(np.abs(w-previous).sum()+w[-1]) if len(w) else 0.
 return result

def metrics(frame):
 f=frame.sort_values('date');ind=independent_rows(f);delta=qlike(ind.actual,ind.baseline_variance)-qlike(ind.actual,ind.candidate_variance)
 base=np.mean(abs(f.actual-f.baseline_variance));cand=np.mean(abs(f.actual-f.candidate_variance))
 m={'samples':len(f),'independent_samples':len(ind),'variance_mae_improvement':float(1-cand/max(base,1e-12)),'qlike_gain':float(np.mean(qlike(f.actual,f.baseline_variance)-qlike(f.actual,f.candidate_variance))),'sign_pvalue':sign_pvalue(delta)}
 if f.kind.iloc[0]=='hmm':m.update(policy_metrics(ind))
 return m

def schema(db):
 db.execute('''CREATE TABLE IF NOT EXISTS risk_shadow(spec TEXT,kind TEXT,symbol TEXT,horizon INTEGER,origin TEXT,target TEXT,issued_at TEXT,candidate_variance REAL,baseline_variance REAL,candidate_exposure REAL,baseline_exposure REAL,actual REAL,market_return REAL,PRIMARY KEY(spec,kind,symbol,horizon,origin))''')

def run(root):
 root=Path(root);end=cutoff();start=end-pd.DateOffset(years=5);cal,_=calendar(root,load_config(root),end,offline=True);days=pd.DatetimeIndex(cal);days=days[(days>=start)&(days<=end)]
 watch=read_watchlist(root);account,account_assumed=research_account(root);folder=root/'reports/risk_models'/now().strftime('%Y%m%d-%H%M%S-%f');folder.mkdir(parents=True)
 import importlib.metadata
 spec={**SPEC,'hash':fingerprint(),'universe':watch.symbol.tolist(),'dependencies':{x:importlib.metadata.version(x) for x in ['arch','hmmlearn','numpy','scipy']}}
 write_json(folder/'frozen_specification.json',spec)
 sources={};input_versions={}
 for item in watch.itertuples():
  snap=read_snapshot(root,item.symbol)
  if snap is None or snap['raw'].date.max()!=days.max():raise ValueError('股票数据过期/缺失 '+item.symbol)
  input_versions[item.symbol]=snap['meta']['version']
  c=snap['hfq'].set_index('date').close;sources[('garch',item.symbol)]=c.reindex(days[days>=c.index.min()])
 market=load_index(root,'sh000300',start,end).set_index('date').close.reindex(days)
 if market.isna().any():raise ValueError('市场指数缺失')
 sources[('hmm','sh000300')]=market
 input_versions['sh000300']=hashlib.sha256(market.to_csv().encode()).hexdigest()
 results=[];frames=[];latest=[];failures=[];audits={}
 for (kind,symbol),close in sources.items():
  frame,last,fail,audit=evaluate(kind,symbol,close);latest.extend(last);failures.extend({'kind':kind,'symbol':symbol,**x} for x in fail);audits[kind+'/'+symbol]=audit
  for h in SPEC['horizons']:
   f=frame[frame.horizon==h].tail(SPEC['validation_days']) if not frame.empty else frame
   if len(f)<SPEC['validation_days']:
    results.append({'kind':kind,'symbol':symbol,'horizon':h,'status':'incomplete_validation','samples':len(f),'sign_pvalue':1.});continue
   frames.append(f);results.append({'kind':kind,'symbol':symbol,'horizon':h,'status':'evaluated',**metrics(f)})
  logging.info('风险模型完成 %s %s',kind,symbol)
 for r,p in zip(results,holm([r['sign_pvalue'] for r in results])):
  r['holm_pvalue']=p;r['historical_passed']=bool(r['status']=='evaluated' and r['variance_mae_improvement']>=.02 and r['qlike_gain']>0 and r['independent_samples']>=SPEC['minimum_independent'] and p<=SPEC['alpha'])
  if r['kind']=='hmm' and r['status']=='evaluated':r['historical_passed'] &= r['candidate_net_return']>=r['baseline_net_return'] and r['candidate_max_drawdown']<=r['baseline_max_drawdown']
 specid=hashlib.sha256(json.dumps(spec,sort_keys=True).encode()).hexdigest()
 with sqlite3.connect(root/'data/market.sqlite3') as db:
  schema(db)
  for s,kind,symbol,h,origin,target in db.execute('SELECT spec,kind,symbol,horizon,origin,target FROM risk_shadow WHERE actual IS NULL').fetchall():
   if pd.Timestamp(now())<pd.Timestamp(target).tz_localize('Asia/Shanghai')+pd.Timedelta(hours=15):continue
   c=sources.get((kind,symbol))
   if c is None or pd.Timestamp(origin) not in c.index or pd.Timestamp(target) not in c.index:continue
   rets=c.pct_change(fill_method=None).loc[origin:target].iloc[1:]
   if len(rets)!=h or rets.isna().any():continue
   db.execute('UPDATE risk_shadow SET actual=?,market_return=? WHERE spec=? AND kind=? AND symbol=? AND horizon=? AND origin=?',(float((rets**2).sum()),float((1+rets).prod()-1),s,kind,symbol,h,origin))
  future=pd.DatetimeIndex(cal);future=future[future>days.max()]
  for item in latest:
   if len(future)<item['horizon']:continue
   target=str(future[item['horizon']-1].date());issued=now().isoformat()
   if pd.Timestamp(issued)>=pd.Timestamp(target).tz_localize('Asia/Shanghai')+pd.Timedelta(hours=15):raise ValueError('不能回填前瞻预测')
   db.execute('INSERT OR IGNORE INTO risk_shadow VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,NULL)',(specid,item['kind'],item['symbol'],item['horizon'],str(days.max().date()),target,issued,item['candidate_variance'],item['baseline_variance'],item['candidate_exposure'],item['baseline_exposure']))
  for r in results:
   f=pd.read_sql_query('SELECT kind,origin AS date,target AS label_end,candidate_variance,baseline_variance,candidate_exposure,baseline_exposure,actual,market_return FROM risk_shadow WHERE spec=? AND kind=? AND symbol=? AND horizon=? AND actual IS NOT NULL ORDER BY origin',db,params=(specid,r['kind'],r['symbol'],r['horizon']))
   if not f.empty:f['date']=pd.to_datetime(f.date);f['label_end']=pd.to_datetime(f.label_end)
   r['forward']=metrics(f) if len(f) else {'samples':0,'sign_pvalue':1.}
 for r,p in zip(results,holm([r['forward']['sign_pvalue'] for r in results])):
  f=r['forward'];f['holm_pvalue']=p
  r['forward_passed']=bool(f['samples']>=SPEC['minimum_forward_origins'] and f.get('independent_samples',0)>=SPEC['minimum_independent'] and f.get('variance_mae_improvement',0)>=.02 and f.get('qlike_gain',0)>0 and p<=SPEC['alpha'])
  if r['kind']=='hmm' and r['forward_passed']:r['forward_passed'] &= f['candidate_net_return']>=f['baseline_net_return'] and f['candidate_max_drawdown']<=f['baseline_max_drawdown']
  r['approved']=bool(r['historical_passed'] and r['forward_passed'])
 registry={'spec_id':specid,'input_versions':input_versions,'dependencies':spec['dependencies'],'asof':str(days.max().date()),'implementation_hash':fingerprint(),'approved':[r for r in results if r['approved']],'latest':latest}
 write_json(root/'data/risk_model_registry.json',registry)
 reference=sum(x['shares']*x['reference_price'] for x in account['positions'].values());snapshot_equity=reference+account['cash']
 payload={'asof':str(days.max().date()),'specification':spec,'results':results,'latest':latest,'fit_failures':failures,'fold_audits':audits,'account_snapshot':account,'account_assumed':account_assumed,'account_reference_equity':snapshot_equity,'account_reference_stock_value':reference,'input_versions':input_versions,'approved_count':sum(r['approved'] for r in results),'limitations':['HMM状态无客观真值，不将状态分类准确率冒充验证。','HMM仅前向过滤，状态语义只按训练数据确定；不对全样本平滑。','GARCH对比20日历史方差；HMM对比原趋势规则及训练期状态条件方差。','HMM仓位收益是沪深300收盘代理，含固定换仓成本，不是三只实际持仓的可成交回测。','旧历史已用于研究，正式启用还需后续真实预测验证。','账户截图只有确认日期，不能视作实时持仓。']}
 write_json(folder/'report.json',payload)
 if frames:write_csv(folder/'oos_predictions.csv',pd.concat(frames,ignore_index=True))
 import html
 names=dict(zip(watch['symbol'],watch['name']));names['sh000300']='沪深300'
 rows=''
 for r in results:
  label=f"{names[r['symbol']]}（{r['symbol']}）" if r['symbol'] in names else r['symbol']
  vals=[r['kind'],label,r['horizon'],r['samples'],f"{r.get('variance_mae_improvement',0):.2%}",f"{r.get('qlike_gain',0):.4f}",r['historical_passed'],r['forward']['samples'],r['approved']]
  rows+='<tr>'+''.join('<td>'+html.escape(str(v))+'</td>' for v in vals)+'</tr>'
 page='''<!doctype html><meta charset="utf-8"><title>风险模型与实际持仓</title><style>body{font:16px/1.8 system-ui;margin:40px;color:#17253a}td,th{padding:9px;border-bottom:1px solid #ddd}table{border-collapse:collapse}</style><h1>GARCH与HMM风险模型验证</h1>'''
 page+=('<p>初始账户假设：现金150000元，持仓0；非已连接真实账户。</p>' if account_assumed else '<p>账户来自已导入快照，未重新初始化。</p>')
 page+=f'<p>数据截至 {payload["asof"]}；正式启用 {payload["approved_count"]} 组。可用现金 {account["available_cash"]:,.2f} 元，按快照参考价估值 {snapshot_equity:,.2f} 元。账户金额按上方来源/假设解释。</p><ul>'
 for code,pos in account['positions'].items():page+=f'<li>{html.escape(pos["name"])}（{code}）：{pos["shares"]}股</li>'
 page+='</ul><p>持仓比例及集中度请见实际持仓决策报告；账户不是实时查询。未验证风险模型不会据此直接生成减仓订单。</p><p>正的方差误差改进或QLIKE增益表示候选较好。训练至少504天、每21天重新拟合，参数只用过去数据。逐项252个样本，方向不同的期限使用非重叠样本做显著性检验并进行Holm校正。未收敛、非平稳或缺失数据的窗口不填造预测。</p><table><tr><th>模型</th><th>标的</th><th>期限</th><th>样本</th><th>方差MAE改善</th><th>QLIKE增益</th><th>历史通过</th><th>前瞻到期数</th><th>正式启用</th></tr>'+rows+'</table><ul>'+''.join('<li>'+html.escape(x)+'</li>' for x in payload['limitations'])+'</ul><p><a href="../../decisions/latest/index.html">查看基于实际持仓的决策报告</a></p>'
 page+='<p><a href="../../model_monitor/latest/index.html">预测误差、持续退化与回退记录</a></p>'
 page+='<p>接口参考：<a href="https://arch.readthedocs.io/en/latest/univariate/forecasting.html">ARCH官方预测文档</a>、<a href="https://hmmlearn.readthedocs.io/en/stable/tutorial.html">hmmlearn官方文档</a>。</p>'
 (folder/'index.html').write_text(page);dest=root/'reports/risk_models/latest';dest.mkdir(exist_ok=True)
 for file in ['report.json','index.html','frozen_specification.json']:(dest/file).write_text((folder/file).read_text())
 return payload,folder

def rescale_covariance(cov,symbols,variances):
 c=np.asarray(cov,float);d=np.ones(len(symbols))
 for i,s in enumerate(symbols):
  if s in variances:
   if variances[s]<=0 or not np.isfinite(variances[s]) or c[i,i]<=0:raise ValueError('无效候选方差')
   d[i]=np.sqrt(variances[s]/c[i,i])
 return c*np.outer(d,d)

def apply(root,asof,horizon,cov,symbols,environment,config,input_versions=None):
 status={'used':[],'reason':'没有通过历史及前瞻验证的风险模型'};p=Path(root)/'data/risk_model_registry.json'
 if not p.exists():return cov,environment,status
 r=json.loads(p.read_text())
 if r['asof']!=asof or r['implementation_hash']!=fingerprint():return cov,environment,{'used':[],'reason':'风险模型版本或日期不匹配'}
 import importlib.metadata
 if any(importlib.metadata.version(k)!=v for k,v in r.get('dependencies',{}).items()):return cov,environment,{'used':[],'reason':'依赖版本改变'}
 if input_versions is not None and any(r.get('input_versions',{}).get(s)!=input_versions.get(s) for s in symbols):return cov,environment,{'used':[],'reason':'行情版本改变'}
 approved={(a['kind'],a['symbol'],a['horizon']) for a in r['approved'] if a.get('historical_passed') and a.get('forward_passed')}
 var={}
 from .model_monitor import permitted
 for row in r['latest']:
  identity=f"risk|{r.get('spec_id','unknown')}|{row['kind']}|{row['symbol']}|{row['horizon']}"
  if not permitted(root,'risk',r,identity):continue
  if (row['kind'],row['symbol'],row['horizon']) not in approved or row['horizon']!=horizon:continue
  if row['kind']=='garch':var[row['symbol']]=row['candidate_variance']
  else:
   config['market_exposure']=dict(config['market_exposure']);config['market_exposure']['hmm_validated']=row['candidate_exposure'];environment={**environment,'state':'hmm_validated','method':'validated_filtered_hmm','exposure_cap':row['candidate_exposure']};status['used'].append('hmm')
 if cov is not None and var:cov=rescale_covariance(cov,symbols,var);status['used'].append('garch')
 if status['used']:status['reason']='已通过历史及前瞻验证，仅用于模拟配置'
 return cov,environment,status
