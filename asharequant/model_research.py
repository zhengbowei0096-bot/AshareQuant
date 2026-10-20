"""Paired purged comparisons and immutable prospective shadow forecasts."""
from pathlib import Path
import json,sqlite3,math,html,logging,importlib.metadata
import numpy as np
import pandas as pd
from .core import now,cutoff,read_watchlist,read_snapshot,load_config,write_json,write_csv
from .data import calendar
from .forecast import DEFAULTS,FEATURES,features_and_labels,fit_predict,independent_rows,validate_forecast
from .model_candidates import NAMES,PARAMS,add_lags,columns,fit,signature

GATE={'minimum_mae_improvement':.02,'minimum_direction_accuracy':.52,'minimum_independent_pairs':12,'minimum_positive_folds':3,'familywise_alpha':.05,'minimum_prospective_origins':63}

def sign_pvalue(delta):
 a=np.asarray(delta);a=a[np.abs(a)>1e-12];n=len(a);wins=int((a>0).sum())
 return sum(math.comb(n,k) for k in range(wins,n+1))/2**n if n else 1.

def holm(values):
 p=np.asarray(values,dtype=float);out=np.ones(len(p));largest=0.
 for rank,index in enumerate(np.argsort(p)):
  largest=max(largest,(len(p)-rank)*p[index]);out[index]=min(1.,largest)
 return out.tolist()

def paired(champion,candidate):
 if not champion.date.equals(candidate.date):raise ValueError('候选和冠军验证日期不同')
 c=candidate.copy();c['champion_predicted']=champion.predicted.to_numpy()
 b=np.abs(c.actual-c.champion_predicted);a=np.abs(c.actual-c.predicted)
 independent=independent_rows(c)
 delta=np.abs(independent.actual-independent.champion_predicted)-np.abs(independent.actual-independent.predicted)
 return dict(relative_mae_improvement=float(1-a.mean()/max(b.mean(),1e-12)),champion_mae=float(b.mean()),candidate_mae=float(a.mean()),
  direction=float(((independent.actual>0)==(independent.predicted>0)).mean()),champion_direction=float(((independent.actual>0)==(independent.champion_predicted>0)).mean()),
  independent_pairs=len(independent),sign_pvalue=sign_pvalue(delta),samples=len(c)),c

def historical_pass(r):
 return bool(r['relative_mae_improvement']>=GATE['minimum_mae_improvement'] and r['direction']>=max(GATE['minimum_direction_accuracy'],r['champion_direction']) and r['independent_pairs']>=GATE['minimum_independent_pairs'] and r['positive_folds']>=GATE['minimum_positive_folds'] and r['holm_pvalue']<=GATE['familywise_alpha'] and r['candidate_simple_baseline_pass'])

def dbconnect(root):
 db=sqlite3.connect(root/'data/market.sqlite3',timeout=30)
 db.execute('''CREATE TABLE IF NOT EXISTS model_shadow(
 spec TEXT,model TEXT,symbol TEXT,horizon INTEGER,origin_date TEXT,target_date TEXT,issued_at TEXT,
 predicted REAL,champion_predicted REAL,input_version TEXT,actual REAL,matured_at TEXT,outcome_version TEXT,
 PRIMARY KEY(spec,model,symbol,horizon,origin_date))''')
 return db

def record_shadow(db,entry):
 issued=pd.Timestamp(entry['issued_at']);origin=pd.Timestamp(entry['origin_date']).tz_localize('Asia/Shanghai')+pd.Timedelta(hours=15)
 target=pd.Timestamp(entry['target_date']).tz_localize('Asia/Shanghai')+pd.Timedelta(hours=15)
 if not origin<=issued<target:raise ValueError('非前瞻预测：必须在起点收盘后、目标收盘前记录')
 db.execute('INSERT OR IGNORE INTO model_shadow VALUES(?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL)',tuple(entry[k] for k in ['spec','model','symbol','horizon','origin_date','target_date','issued_at','predicted','champion_predicted','input_version']))

def settle(root,db):
 stamp=pd.Timestamp(now());cache={}
 for spec,model,symbol,h,origin,target,issued in db.execute('SELECT spec,model,symbol,horizon,origin_date,target_date,issued_at FROM model_shadow WHERE actual IS NULL').fetchall():
  if stamp<pd.Timestamp(target).tz_localize('Asia/Shanghai')+pd.Timedelta(hours=15):continue
  if symbol not in cache:cache[symbol]=read_snapshot(root,symbol)
  snap=cache[symbol]
  if snap is None:continue
  c=snap['hfq'].set_index('date').close
  if pd.Timestamp(origin) not in c.index or pd.Timestamp(target) not in c.index:continue
  actual=float(c.loc[target]/c.loc[origin]-1)
  db.execute('UPDATE model_shadow SET actual=?,matured_at=?,outcome_version=? WHERE spec=? AND model=? AND symbol=? AND horizon=? AND origin_date=?',(actual,str(stamp),snap['meta']['version'],spec,model,symbol,h,origin))

def prospective(db,spec,model,symbol,h):
 rows=pd.read_sql_query('SELECT origin_date AS date,target_date AS label_end,actual,predicted,champion_predicted FROM model_shadow WHERE spec=? AND model=? AND symbol=? AND horizon=? AND actual IS NOT NULL ORDER BY origin_date',db,params=(spec,model,symbol,h))
 if rows.empty:return dict(samples=0,independent_pairs=0,sign_pvalue=1.,relative_mae_improvement=0.,direction=0.,champion_direction=0.)
 rows['date']=pd.to_datetime(rows.date);rows['label_end']=pd.to_datetime(rows.label_end)
 champ=rows.copy();champ['predicted']=rows.champion_predicted
 metrics,_=paired(champ,rows);return metrics

def run(root,stage='all'):
 root=Path(root);models=NAMES[:2] if stage=='linear' else NAMES[2:] if stage=='trees' else NAMES
 cfg={**DEFAULTS,**json.loads((root/'forecast_config.json').read_text())};end=cutoff();start=end-pd.DateOffset(years=5)
 cal,_=calendar(root,load_config(root),end,offline=True);days=pd.DatetimeIndex(cal);days=days[(days>=start)&(days<=end)]
 folder=root/'reports/model_comparison'/now().strftime('%Y%m%d-%H%M%S-%f');folder.mkdir(parents=True)
 deps={x:importlib.metadata.version(x) for x in ['numpy','pandas','scikit-learn','lightgbm','xgboost']}
 watch=read_watchlist(root);family_size=len(watch)*3*len(NAMES)
 spec={'universe':watch.symbol.tolist(),'family_size':family_size,'implementation_hash':signature(),'parameters':PARAMS,'gate':GATE,'config':cfg,'dependencies':deps,'features':FEATURES,'time_series_features':columns('ar5'),'comparison':'same observations and purged folds; fixed parameters; previously inspected historical period; forward evidence required'}
 specid=__import__('hashlib').sha256(json.dumps(spec,sort_keys=True).encode()).hexdigest();spec['id']=specid
 write_json(folder/'frozen_specification.json',spec)
 results=[];predictions=[];latest=[];errors=[];versions={}
 with dbconnect(root) as db:
  settle(root,db)
  for item in watch.itertuples():
   snap=read_snapshot(root,item.symbol)
   if snap is None or snap['raw'].date.max()!=days.max():raise ValueError('股票快照缺失或过期：'+item.symbol)
   versions[item.symbol]=snap['meta']['version'];bars=snap['hfq'];bars=bars[bars.date.between(start,end)];sd=days[days>=bars.date.min()]
   for h in [1,5,20]:
    frame=add_lags(features_and_labels(bars,sd,h));common=frame.dropna(subset=FEATURES+columns('ar5')+['target','label_end'])
    champion,cm=validate_forecast(common,h,cfg)
    train=common[common.label_end<=days.max()].tail(cfg['training_cap']);test=frame.iloc[[-1]]
    ridge_now,_=fit_predict(train,test,cfg['ridge_alpha'])
    future=pd.DatetimeIndex(cal);future=future[future>days.max()];target=str(future[h-1].date()) if len(future)>=h else None
    for model in models:
     try:
      cand,metrics=validate_forecast(common,h,cfg,columns(model),model_name=model)
      comparison,joined=paired(champion,cand)
      positive=0
      for fold in metrics['folds']:
       mask=joined.date.between(fold['test_start'],fold['test_end']);part=joined[mask]
       positive+=int((part.actual-part.predicted).abs().mean()<(part.actual-part.champion_predicted).abs().mean())
      comparison.update(model=model,symbol=item.symbol,name=item.name,horizon=h,positive_folds=positive,candidate_simple_baseline_pass=metrics['preliminary_screen_passed'],folds=metrics['folds'])
      results.append(comparison);joined['model']=model;joined['symbol']=item.symbol;joined['horizon']=h;predictions.append(joined)
      pred,meta=fit(model,train,test,folder/'models'/f'{item.symbol}-{h}-{model}')
      residual=independent_rows(cand);err=(residual.actual-residual.predicted).to_numpy();lo,hi=np.quantile(err,[.1,.9])
      latest.append(dict(symbol=item.symbol,model=model,horizon=h,predicted_return=float(pred[0]),ridge_return=float(ridge_now[0]),probability_up_empirical=float((1+np.sum(err>-pred[0]))/(len(err)+2)),interval80=[float(pred[0]+lo),float(pred[0]+hi)],target_date=target,mode='shadow_not_allocation'))
      if target:record_shadow(db,dict(spec=specid,model=model,symbol=item.symbol,horizon=h,origin_date=str(days.max().date()),target_date=target,issued_at=now().isoformat(),predicted=float(pred[0]),champion_predicted=float(ridge_now[0]),input_version=snap['meta']['version']))
     except Exception as e:
      errors.append(dict(symbol=item.symbol,horizon=h,model=model,error=str(e)));logging.exception('候选模型失败 %s %s T+%s',item.symbol,model,h)
   logging.info('模型对照完成 %s %s',item.symbol,','.join(models))
  # Fixed family contains all 20 stocks x 3 horizons x 4 challengers, even for staged runs.
  adjusted=holm([r['sign_pvalue'] for r in results]+[1.]*(family_size-len(results)))
  for i,r in enumerate(results):r['holm_pvalue']=adjusted[i];r['historical_passed']=historical_pass(r);r['prospective']=prospective(db,specid,r['model'],r['symbol'],r['horizon'])
  adjusted_forward=holm([r['prospective']['sign_pvalue'] for r in results]+[1.]*(family_size-len(results)))
  active={}
  for i,r in enumerate(results):
   p=r['prospective'];p['holm_pvalue']=adjusted_forward[i]
   r['prospective_passed']=bool(p['samples']>=GATE['minimum_prospective_origins'] and p['independent_pairs']>=GATE['minimum_independent_pairs'] and p['relative_mae_improvement']>=GATE['minimum_mae_improvement'] and p['direction']>=max(GATE['minimum_direction_accuracy'],p['champion_direction']) and p['holm_pvalue']<=GATE['familywise_alpha'])
   r['promoted']=r['historical_passed'] and r['prospective_passed']
   if r['promoted']:
    key=f'{r["symbol"]}/{r["horizon"]}'
    if key not in active or p['relative_mae_improvement']>active[key]['prospective_improvement']:active[key]={'model':r['model'],'historical_passed':True,'prospective_passed':True,'prospective_improvement':p['relative_mae_improvement'],'input_version':versions[r['symbol']]}
  summary=[]
  for model in models:
   for h in [1,5,20]:
    subset=[r for r in results if r['model']==model and r['horizon']==h]
    summary.append(dict(model=model,horizon=h,stocks=len(subset),improved=sum(r['relative_mae_improvement']>0 for r in subset),median_mae_improvement=float(np.median([r['relative_mae_improvement'] for r in subset])) if subset else None,historical_passed=sum(r['historical_passed'] for r in subset),prospective_passed=sum(r['prospective_passed'] for r in subset),promoted=sum(r['promoted'] for r in subset)))
  registry={'config':cfg,'dependencies':deps,'implementation_hash':signature(),'spec_id':specid,'evaluated_asof':str(days.max().date()),'active':active,'report':str(folder/'index.html')}
  # Stage runs never replace the production registry.
  if stage=='all' and not errors:write_json(root/'data/model_registry.json',registry)
 payload=dict(generated_at=now().isoformat(),asof=str(days.max().date()),stage=stage,specification=spec,summary=summary,results=results,shadow_forecasts=latest,errors=errors,data_versions=versions,active=active,production_changed=bool(active) and stage=='all' and not errors,
  limitations=['这是当前启用观察池条件下的历史研究，未重建历史股票池。','这段历史已经被查看，不能冒充从未使用的独立测试集。','历史改善必须经过多重检验和后续真实预测跟踪，才能用于模型晋升。','候选预测为收盘到未来收盘代理，不是扣除实际成交成本后的收益。','候选概率与区间是经验残差估计，不是已校准的交易概率。'])
 write_json(folder/'comparison.json',payload);write_csv(folder/'paired_predictions.csv',pd.concat(predictions,ignore_index=True) if predictions else pd.DataFrame());write_csv(folder/'shadow_forecasts.csv',pd.DataFrame(latest))
 table=''
 for x in summary:
  imp=x['median_mae_improvement'];vals=[x['model'],f'T+{x["horizon"]}',f'{x["improved"]}/{x["stocks"]}',f'{imp:.2%}' if imp is not None else '—',x['historical_passed'],x['prospective_passed'],x['promoted']]
  table+='<tr>'+''.join('<td>'+html.escape(str(v))+'</td>' for v in vals)+'</tr>'
 page='''<!doctype html><meta charset="utf-8"><title>预测模型样本外对照</title><style>body{font:16px/1.8 system-ui;margin:40px;color:#17253a}td,th{padding:12px;border-bottom:1px solid #ddd}table{border-collapse:collapse}pre{white-space:pre-wrap}</style><h1>AR(5)、Lasso、LightGBM、XGBoost 与岭回归对照</h1>'''
 page+=f'<p>数据截至 {payload["asof"]}。阶段：{stage}。正式选择 {len(active)} 个股票/期限组合；错误 {len(errors)} 项。</p><p>AR(5)直接多步收益回归使用最近5个已观察的日收益率；其余模型使用同样9项行情特征。固定参数、不用外层测试调参。252个样本分4折，各折训练只使用已到期标签；均值与尺度只来自训练集。所有比较使用相同日期。</p><table><tr><th>模型</th><th>期限</th><th>误差改善股票</th><th>MAE改善中位数</th><th>历史门槛通过</th><th>前瞻验证通过</th><th>正式选择</th></tr>{table}</table>'
 page+='<p>正数表示候选误差更低。历史准入要求至少2%误差改善、方向不差于岭回归且至少52%、至少12个非重叠样本、4折至少3折改善、相对简单基准通过，并按本次观察池全部股票×3期限×4候选模型进行Holm校正。正式加入还要求至少63条已到期前瞻预测、至少12个非重叠样本及相同的误差/方向/多重检验门槛。首次运行不会伪造已到期前瞻记录。</p>'
 page+='<ul>'+''.join('<li>'+html.escape(x)+'</li>' for x in payload['limitations'])+'</ul><p><a href="shadow_forecasts.csv">下载候选预测（不用于下单）</a> · <a href="../../forecasts/latest/index.html">查看正式预测与资金规划</a></p><pre>'+html.escape(json.dumps(errors,ensure_ascii=False,indent=2))+'</pre>'
 page+='<p><a href="../../model_monitor/latest/index.html">预测误差、持续退化与回退记录</a></p>'
 page+='<p>实现参考：<a href="https://scikit-learn.org/stable/modules/generated/sklearn.linear_model.Lasso.html">Lasso官方文档</a>、<a href="https://lightgbm.readthedocs.io/en/latest/pythonapi/lightgbm.LGBMRegressor.html">LightGBM官方文档</a>、<a href="https://xgboost.readthedocs.io/en/stable/python/python_api.html">XGBoost官方文档</a>。</p>'
 (folder/'index.html').write_text(page)
 dest=root/'reports/model_comparison'/('latest' if stage=='all' else 'latest-'+stage);dest.mkdir(exist_ok=True)
 for name in ['index.html','comparison.json','frozen_specification.json','shadow_forecasts.csv']:(dest/name).write_text((folder/name).read_text())
 return payload,folder
