"""Time-aligned factor candidates; diagnostic backtests cannot promote a model."""
from pathlib import Path
import json,sqlite3,hashlib,html
import numpy as np
import pandas as pd
from ..core import now,cutoff,read_watchlist,read_snapshot,write_json,write_csv,load_config
from ..data import calendar
from ..forecast import FEATURES,DEFAULTS,features_and_labels,validate_forecast

GROUPS={
 'industry':['industry_r5','industry_r20','industry_vol20','relative_industry20'],
 'valuations':['earnings_yield','book_yield','log_market_cap'],
 'financial':['roe','leverage','revenue_growth','profit_growth'],
 'macro':['cpi_yoy','pmi_deviation','gdp_yoy','m2_yoy','lpr_1y']}

class Store:
 def __init__(self,root):
  self.root=Path(root);self.cache={}
 def events(self,ds,entity):
  key=(ds,entity)
  if key not in self.cache:
   with sqlite3.connect(self.root/'data/market.sqlite3') as db:
    rows=db.execute('SELECT f.fetched_at,f.content_hash,s.rows_json FROM factor_fetches f JOIN factor_snapshots s USING(dataset,entity,content_hash) WHERE f.dataset=? AND f.entity=? ORDER BY f.fetched_at',(ds,entity)).fetchall()
   self.cache[key]=[(pd.Timestamp(t),h,json.loads(v)) for t,h,v in rows]
  return self.cache[key]
 def current(self,ds,entity):
  events=self.events(ds,entity)
  return events[-1][2] if events else []
 def visible(self,ds,entity,point):
  events=self.events(ds,entity)
  eligible=[e for e in events if e[0]<=point]
  return eligible[-1] if eligible else None

def decision_time(day):
 return pd.Timestamp(day).normalize().tz_localize('Asia/Shanghai')+pd.Timedelta(days=1,hours=8)

def released(row,point):
 """Date-only notices conservatively available the following calendar day.
 First observed snapshot is independently required by Store.visible.
 """
 if row.get('_observation_date') and pd.Timestamp(row['_observation_date']).date()>point.date():return False
 for key in ['NOTICE_DATE','UPDATE_DATE','update_time']:
  value=row.get(key)
  if not value:continue
  t=pd.Timestamp(value)
  if t.tzinfo is None:t=t.tz_localize('Asia/Shanghai')
  if t.hour==t.minute==t.second==0:t+=pd.Timedelta(days=1)
  if t>point:return False
 return True

def latest_period(rows,point,max_age):
 visible=[r for r in rows if r.get('_observation_date') and released(r,point)]
 if not visible:return None
 last=max(r['_observation_date'] for r in visible)
 if (point.date()-pd.Timestamp(last).date()).days>max_age:return None
 choices=[r for r in visible if r['_observation_date']==last]
 if len(choices)!=1:raise ValueError('同一报告期有冲突记录，不猜测版本')
 return choices[0]

def num(row,key):
 try:
  value=float(row.get(key));return value if np.isfinite(value) else np.nan
 except (TypeError,ValueError):return np.nan

def daily_frame(rows,days):
 if not rows:return pd.DataFrame(index=days)
 frame=pd.DataFrame(rows);frame.index=pd.to_datetime(frame['_observation_date'])
 if frame.index.has_duplicates:raise ValueError('日频记录日期冲突')
 return frame.reindex(days)

def valuation_features(rows,days):
 f=daily_frame(rows,days);out=pd.DataFrame(index=days,columns=GROUPS['valuations'],dtype=float)
 if f.empty or 'PE(TTM)' not in f:return out
 pe=pd.to_numeric(f['PE(TTM)'],errors='coerce');pb=pd.to_numeric(f['市净率'],errors='coerce');cap=pd.to_numeric(f['总市值'],errors='coerce')
 out['earnings_yield']=1/pe.where(pe!=0);out['book_yield']=1/pb.where(pb!=0);out['log_market_cap']=np.log(cap.where(cap>0))
 return out.replace([np.inf,-np.inf],np.nan)

def industry_features(rows,days,close):
 f=daily_frame(rows,days);out=pd.DataFrame(index=days,columns=GROUPS['industry'],dtype=float)
 if '收盘' not in f:return out
 c=pd.to_numeric(f['收盘'],errors='coerce');r=c.pct_change(fill_method=None)
 # Require a complete intervening window, not merely endpoints.
 out['industry_r5']=(c/c.shift(5)-1).where(c.rolling(6).count()==6)
 out['industry_r20']=(c/c.shift(20)-1).where(c.rolling(21).count()==21)
 out['industry_vol20']=r.rolling(20).std()
 out['relative_industry20']=close/close.shift(20)-1-out.industry_r20
 return out.replace([np.inf,-np.inf],np.nan)

def membership(store,symbol,point):
 indices=store.visible('industry_list','sw_level1',point)
 if not indices:return None
 found=[]
 for item in indices[2]:
  code=str(item['指数代码']).zfill(6);event=store.visible('industry_members',code,point)
  if event and any(str(r['证券代码']).zfill(6)==symbol for r in event[2]):found.append(code)
 if len(found)>1:raise ValueError('行业归属冲突')
 return found[0] if found else None

def aligned(store,symbol,days,close,group):
 out=pd.DataFrame(np.nan,index=days,columns=GROUPS[group]);computed={}
 for day in days:
  point=min(decision_time(day),pd.Timestamp(now()).tz_convert('Asia/Shanghai'))
  if group in ('industry','valuations'):
   entity=membership(store,symbol,point) if group=='industry' else symbol
   if entity is None:continue
   ds='industry_bars' if group=='industry' else 'valuations';event=store.visible(ds,entity,point)
   if event is None:continue
   key=(entity,event[1])
   if key not in computed:computed[key]=industry_features(event[2],days,close) if group=='industry' else valuation_features(event[2],days)
   out.loc[day]=computed[key].loc[day]
  elif group=='financial':
   event=store.visible('financial_main',symbol,point)
   row=latest_period(event[2],point,550) if event else None
   if row:out.loc[day]=[num(row,k)/100 for k in ['ROEJQ','ZCFZL','TOTALOPERATEREVETZ','PARENTNETPROFITTZ']]
  else:
   for col,ds,key,age,scale,offset in [
    ('cpi_yoy','macro_cpi','全国-同比增长',100,100,0),('pmi_deviation','macro_pmi','制造业-指数',100,1,50),
    ('gdp_yoy','macro_gdp','国内生产总值-同比增长',185,100,0),('m2_yoy','macro_money','货币和准货币(M2)-同比增长',100,100,0),
    ('lpr_1y','macro_lpr','LPR1Y',62,100,0)]:
    event=store.visible(ds,'china',point);row=latest_period(event[2],point,age) if event else None
    if row:out.loc[day,col]=(num(row,key)-offset)/scale
 return out

def promotion_allowed(strict_metrics,prospective_verified=False):
 # Retrospective diagnostics, even if profitable, cannot pass this gate.
 return bool(strict_metrics and prospective_verified and strict_metrics.get('preliminary_screen_passed',False))

def run(root):
 root=Path(root);cfg={**DEFAULTS,**json.loads((root/'forecast_config.json').read_text())};end=cutoff();start=end-pd.DateOffset(years=5)
 cal,_=calendar(root,load_config(root),end,offline=True);days=pd.DatetimeIndex(cal);days=days[(days>=start)&(days<=end)]
 folder=root/'reports/factor_research'/now().strftime('%Y%m%d-%H%M%S-%f');folder.mkdir(parents=True)
 spec=dict(groups=GROUPS,decision_time='following calendar day 08:00 Asia/Shanghai, capped at actual execution time for the current forecast',availability='latest snapshot actually observed by decision time; notices and updates separately gated; no backward fill',
  strict_promotion_requires='causal versions, sufficient purged samples, independent prospective evidence and predeclared validation gate',
  diagnostics_only=['industry with current membership','historical valuation snapshot'],config=cfg,
  validation_warning='The historical validation period has already been inspected; no untouched holdout is claimed.')
 write_json(folder/'frozen_specification.json',spec)
 store=Store(root);results=[];coverage=[];samples=[];strict_frames=[];inputs={}
 for item in read_watchlist(root).itertuples():
  snap=read_snapshot(root,item.symbol)
  if snap is None or snap['raw'].date.max()!=days.max():raise ValueError('股票日线缺失或过期：'+item.symbol)
  inputs[item.symbol]=snap['meta']['version'];bars=snap['hfq'];bars=bars[bars.date.between(start,end)];sd=days[days>=bars.date.min()];close=bars.set_index('date').close.reindex(sd)
  groups={g:aligned(store,item.symbol,sd,close,g) for g in GROUPS}
  combined=pd.concat(groups.values(),axis=1);combined['symbol']=item.symbol;combined.index.name='date';strict_frames.append(combined.reset_index())
  for g,f in groups.items():coverage.append(dict(symbol=item.symbol,group=g,total_sessions=len(sd),complete_strict_sessions=int(f.notna().all(axis=1).sum()),latest_complete=bool(f.iloc[-1].notna().all())))
  latest_point=pd.Timestamp(now()).tz_convert('Asia/Shanghai');code=membership(store,item.symbol,latest_point)
  diagnostic={'valuations':valuation_features(store.current('valuations',item.symbol),sd)}
  if code:diagnostic['industry']=industry_features(store.current('industry_bars',code),sd,close)
  for h in [1,5,20]:
   base=features_and_labels(bars,sd,h)
   for g,f in groups.items():
    joined=base.join(f);cols=FEATURES+GROUPS[g]
    row=dict(symbol=item.symbol,name=item.name,horizon=h,group=g,strict_complete_matured=int(joined.dropna(subset=cols+['target','label_end']).shape[0]),promoted=False)
    try:
     strict,metric=validate_forecast(joined,h,cfg,cols)
     row.update(strict_metrics=metric,status='awaiting_independent_prospective_validation')
    except ValueError as e:row.update(status='blocked_insufficient_point_in_time_history',reason=str(e))
    if g in diagnostic:
     common=base.join(diagnostic[g]).dropna(subset=cols+['target','label_end'])
     try:
      baseline,bm=validate_forecast(common,h,cfg);candidate,cm=validate_forecast(common,h,cfg,cols)
      if not baseline.date.equals(candidate.date):raise ValueError('对照日期不一致')
      row['diagnostic_only']=dict(relative_mae_improvement=1-cm['mae']/bm['mae'],baseline_mae=bm['mae'],candidate_mae=cm['mae'],direction_accuracy=cm['direction_accuracy_nonoverlap'],baseline_direction_accuracy=bm['direction_accuracy_nonoverlap'],samples=len(candidate),folds=cm['folds'],eligible_for_promotion=False)
      candidate['champion_predicted']=baseline.predicted;candidate['symbol']=item.symbol;candidate['horizon']=h;candidate['group']=g;samples.append(candidate)
     except ValueError as e:row['diagnostic_error']=str(e)
    results.append(row)
 summary=[]
 for g in GROUPS:
  for h in [1,5,20]:
   subset=[r for r in results if r['group']==g and r['horizon']==h];diag=[r['diagnostic_only'] for r in subset if 'diagnostic_only' in r]
   summary.append(dict(group=g,horizon=h,strict_evaluable=sum('strict_metrics' in r for r in subset),total=len(subset),diagnostic_evaluable=len(diag),diagnostic_improved=sum(d['relative_mae_improvement']>0 for d in diag),diagnostic_median_mae_improvement=float(np.median([d['relative_mae_improvement'] for d in diag])) if diag else None,promoted=0))
 factor_versions={f'{ds}/{ent}': [{'fetched_at':str(t),'hash':h} for t,h,_ in events] for (ds,ent),events in store.cache.items()}
 payload=dict(generated_at=now().isoformat(),asof=str(days.max().date()),start=str(start.date()),summary=summary,coverage=coverage,results=results,stock_versions=inputs,factor_versions=factor_versions,specification=spec,promoted_groups=[],production_model='ridge-v1-fixed-no-search',status='no_factor_passed_strict_admission',
  limitations=['行业诊断使用当前行业归属，不是历史成分重建。','估值诊断使用当前历史快照，无法排除历史修订。','财务及宏观没有足够历史版本，不伪造发布日期或按固定滞后回填。','历史诊断期间已被查看，不作为独立的新测试集。','缺失行业交易日及受影响滚动窗口保留为空，不插值。','本次没有任何新增因子获得正式模型准入。'])
 write_json(folder/'validation.json',payload);write_csv(folder/'strict_aligned_features.csv',pd.concat(strict_frames,ignore_index=True))
 if samples:write_csv(folder/'diagnostic_predictions.csv',pd.concat(samples,ignore_index=True))
 names={'industry':'行业','valuations':'估值','financial':'财务','macro':'宏观'}
 table=''
 for r in summary:
  imp=r['diagnostic_median_mae_improvement'];values=[names[r['group']],f'T+{r["horizon"]}',f'{r["strict_evaluable"]}/{r["total"]}',f'{r["diagnostic_improved"]}/{r["diagnostic_evaluable"]}' if r['diagnostic_evaluable'] else '未进行不可靠回填',f'{imp:.2%}' if imp is not None else '—','未加入']
  table+='<tr>'+''.join('<td>'+html.escape(str(v))+'</td>' for v in values)+'</tr>'
 page='''<!doctype html><meta charset="utf-8"><title>新增因子时间对齐与验证</title><style>body{font:16px/1.8 system-ui;margin:40px;color:#17253a}table{border-collapse:collapse}td,th{padding:12px;border-bottom:1px solid #ddd}</style><h1>新增因子时间对齐与验证</h1>'''
 page+=f'<p>数据截至 {payload["asof"]}。正式新增因子：0。由于历史可见版本不足，保留原预测模型。</p><p>每个交易日收盘后至次日08:00作为数据截止，当前预测不超过实际运行时刻，只读取截止前已归档版本。预测仍是收盘至未来收盘代理，并非次日开盘可成交收益。财务公告及修订日期另设限制；低频值只向后沿用且限制最大陈旧期。未知发布时间不会推定为统计期末。</p><table><tr><th>数据</th><th>期限</th><th>严格验证可用股票</th><th>诊断误差改善股票</th><th>诊断误差改进中位数</th><th>正式采用</th></tr>{table}</table>'
 page+='<p>诊断只比较同一批日期、同一岭回归参数。训练标签必须在每折测试开始前到期，标准化仅在训练集拟合。诊断改进不等于通过严格准入，也不证明净交易收益。</p><ul>'+''.join('<li>'+html.escape(x)+'</li>' for x in payload['limitations'])+'</ul><p><a href="../../forecasts/latest/index.html">查看更新后的预测与15万元规划</a></p>'
 (folder/'index.html').write_text(page)
 latest=root/'reports/factor_research/latest';latest.mkdir(exist_ok=True)
 for name in ['index.html','validation.json','frozen_specification.json']:(latest/name).write_text((folder/name).read_text())
 write_json(root/'reports/factor_validation_latest.json',dict(folder=str(folder),asof=payload['asof'],summary=summary,promoted_groups=[],status=payload['status']))
 return payload,folder
