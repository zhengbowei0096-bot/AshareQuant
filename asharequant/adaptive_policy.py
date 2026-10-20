"""Post-model research overlay. Raw forecasts, scores and ranks are never edited."""
import copy,math
import numpy as np
import pandas as pd
from .intraday_events import classify

def trend(history,symbol,at,cfg):
 points={}
 for cycle in history:
  if pd.Timestamp(cycle['at'])>at or cycle.get('checks',{}).get(symbol):continue
  q=cycle.get('quotes',{}).get(symbol)
  if not q:continue
  t=pd.Timestamp(q['asof']);age=(at-t).total_seconds()
  if str(t.date())!=str(at.date()) or not 0<=age<=cfg['trend_window_seconds']+cfg['trend_max_gap_seconds']:continue
  if not isinstance(q.get('price'),(int,float)) or not math.isfinite(q['price']) or q['price']<=0:continue
  points[t]=q['price']
 times=sorted(points)
 if len(times)<cfg['trend_min_samples'] or (times[-1]-times[0]).total_seconds()<cfg['trend_window_seconds']:return dict(ready=False,reason='TREND_WARMUP',samples=len(times))
 if (at-times[-1]).total_seconds()>45 or any((b-a).total_seconds()>cfg['trend_max_gap_seconds'] for a,b in zip(times,times[1:])):return dict(ready=False,reason='TREND_GAP_OR_STALE',samples=len(times))
 prices=np.array([points[t] for t in times]);returns=np.diff(np.log(prices));vol=float(np.std(returns)*np.sqrt(len(returns)));momentum=float(prices[-1]/prices[0]-1)
 return dict(ready=True,samples=len(times),start=times[0].isoformat(),end=times[-1].isoformat(),momentum=momentum,realized_window_volatility=vol,threshold=max(.002,vol),basis='最近约5分钟已接收到的报价；不是历史回填分钟线')

def adjust(plan,state,quotes,history,snapshot,c,cfg,at):
 p=copy.deepcopy(plan);trends={k:trend(history,k,at,cfg) for k in plan['weights']};valid=[t['momentum'] for t in trends.values() if t['ready']];breadth=float(np.median(valid)) if len(valid)>=20 else None
 budget={'risk_off':.45,'neutral':.675,'risk_on':.9}.get(plan.get('environment'),0.)
 market_factor=.75 if breadth is not None and breadth<-.003 else 1.
 labels={k:[] for k in plan['weights']};names={r['symbol']:r['name'] for r in plan['rows']};sectors={r['symbol']:r['sector'] for r in plan['rows']}
 for event in snapshot['events']:
  if event['symbol'] in labels:labels[event['symbol']].append(classify(event,names[event['symbol']],at))
 audit={};weights={};blocks={}
 for k,base in plan['weights'].items():
  t=trends[k];factor=1.;reasons=[];hold=state['positions'].get(k,{}).get('shares',0)*quotes.get(k,{}).get('price',state['marks'].get(k,0))/state['equity']
  healthy=all(snapshot['health'].get(k+':'+kind,{}).get('fresh') for kind in ('news','announcement'))
  if t['ready']:
   if t['momentum']>t['threshold'] and (breadth is None or t['momentum']>breadth):factor*=cfg['positive_trend_factor'];reasons.append('UPWARD_TREND_CONFIRMATION')
   elif t['momentum']<-t['threshold']:factor*=cfg['negative_trend_factor'];reasons.append('DOWNWARD_TREND_RISK')
  else:reasons.append(t['reason'])
  kinds={x['label'] for x in labels[k]}
  if 'NEGATIVE_DISCLOSURE' in kinds:factor*=cfg['negative_announcement_factor'];reasons.append('NEGATIVE_DISCLOSURE');p.setdefault('risk_priorities',{})[k]=p.get('risk_priorities',{}).get(k,0)+10
  elif 'POSITIVE_DISCLOSURE' in kinds and t['ready'] and t['momentum']>t['threshold']:factor*=cfg['positive_announcement_factor'];reasons.append('DISCLOSURE_AND_TREND_CONFIRMATION')
  if 'NEGATIVE_NEWS_UNCONFIRMED' in kinds:factor*=cfg['negative_news_factor'];reasons.append('UNCONFIRMED_NEWS_BUY_VETO')
  if not healthy:reasons.append('EVENT_FEED_STALE_OR_FAILED')
  if 'REVIEW_REQUIRED' in kinds:reasons.append('EVENT_REQUIRES_REVIEW')
  target=min(c['max_stock'],base*min(factor,cfg['max_weight_multiplier']))
  # No new buy while warming up, disconnected, ambiguous or adverse news.
  if not healthy or not t['ready'] or kinds&{'NEGATIVE_NEWS_UNCONFIRMED','REVIEW_REQUIRED','NEGATIVE_DISCLOSURE'}:
   blocks[k]=['ADAPTIVE_BUY_VETO'];target=min(target,hold)
  weights[k]=max(0.,target);audit[k]=dict(base_weight=base,adjusted_weight=weights[k],trend=t,events=labels[k],reasons=reasons,feed_healthy=healthy,factor=factor)
  if t['ready'] and t['momentum']<0:p.setdefault('risk_priorities',{})[k]=p.get('risk_priorities',{}).get(k,0)+abs(t['momentum'])*100
 for sector in set(sectors.values()):
  keys=[k for k in weights if sectors[k]==sector];total=sum(weights[k] for k in keys)
  if total>c['max_sector']:
   for k in keys:weights[k]*=c['max_sector']/total
 ceiling=min(budget*market_factor,c['max_invested']);total=sum(weights.values())
 if total>ceiling:
  for k in weights:weights[k]*=ceiling/total
 for k in weights:audit[k]['adjusted_weight']=weights[k]
 p['weights']=weights;p['total_target_weight']=sum(weights.values());p['cash_target']=1-p['total_target_weight'];p['adaptive_at']=at.isoformat()
 return p,dict(at=at.isoformat(),market_median_window_return=breadth,market_budget=ceiling,market_factor=market_factor,stocks=audit,buy_blocks=blocks,raw_forecasts_unchanged=True,formal_validated=False)
