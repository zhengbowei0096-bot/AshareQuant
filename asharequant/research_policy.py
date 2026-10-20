"""Predeclared research allocation policy, never grants formal qualification."""
import numpy as np
from .decision import validate_config,finite,costs,optimize
from .probability_display import fields
POLICY={'version':'individual-risk-budget-v2','exposure':{'risk_off':.30,'neutral':.45,'risk_on':.60},'unvalidated_reliability':.5,'parameter_error_penalty':1.,'no_trade_band':.01,
 'priority_weights':{'stock_excess':3.,'sector_excess':2.,'variance_contribution':1.,'downside':1.,'interval':1.,'parameter':1.,'probability_risk':1.}}

def targets(payload,positions,equity,cov,c,environment,policy=None):
 policy=policy or POLICY;validate_config(c)
 if environment not in policy['exposure']:raise ValueError('MARKET_ENVIRONMENT_UNKNOWN_HOLD_NO_ORDERS')
 rows=payload['forecasts'];n=len(rows);cov=np.asarray(cov,float)
 if cov.shape!=(n,n) or not np.isfinite(cov).all() or not np.allclose(cov,cov.T) or np.linalg.eigvalsh(cov).min()<-1e-10:raise ValueError('COVARIANCE_INVALID_HOLD_NO_ORDERS')
 cap=min(c['max_invested'],policy['exposure'][environment]);config={**c,'max_invested':cap}
 if not finite(equity) or equity<=0 or not c.get('fees_verified'):raise ValueError('ACCOUNT_OR_FEES_UNVERIFIED')
 symbols=[r['symbol'] for r in rows]
 if len(set(symbols))!=n or set(s for s,p in positions.items() if p['shares'])-set(symbols):raise ValueError('UNIVERSE_OR_HOLDING_INPUT_INCOMPLETE')
 current=np.array([positions.get(r['symbol'],{}).get('shares',0)*r['close']/equity for r in rows]);sectors=[r['sector'] for r in rows]
 sector_totals={g:sum(w for w,s in zip(current,sectors) if s==g) for g in set(sectors)}
 variance=max(float(current@cov@current),1e-12);contribution=current*(cov@current)/variance
 utility=[];penalties=[];eligible=[];data=[]
 for i,r in enumerate(rows):
  f=r['forecasts'][str(c['horizon'])];prob=fields(f);p=prob['research_up_probability'];interval=f.get('interval80_empirical');std=f.get('model_uncertainty',{}).get('std');score=r.get('composite_score',{}).get('score')
  required=[r.get('close'),f.get('predicted_return'),f.get('volatility_horizon'),f.get('downside_es95_normal_proxy'),std,p,score]
  if any(not finite(x) for x in required) or r['close']<=0 or std<0 or not 0<=score<=100 or not interval or len(interval)!=2 or any(not finite(v) for v in interval) or interval[1]<interval[0]:raise ValueError('INDIVIDUAL_INPUT_MISSING_HOLD_NO_ORDERS:'+r['symbol'])
  width=interval[1]-interval[0];es=abs(min(0.,f['downside_es95_normal_proxy']));vol=max(f['volatility_horizon'],1e-6)
  ticket=equity*c['max_stock'];fee=(costs(ticket,'BUY',c,r['symbol'])['total']+costs(ticket,'SELL',c,r['symbol'])['total'])/ticket
  reliability=1. if prob['probability_validated'] else policy['unvalidated_reliability']
  net=f['predicted_return']-fee-policy['parameter_error_penalty']*std
  benefit=max(0.,net)*p*reliability*(score/100)
  reasons=[]
  if not r['data_eligible']:reasons.append('名称/流动性或证券适用性不满足配置要求')
  if es>c['max_downside']:reasons.append('个股下行风险超过上限')
  if width>c['max_uncertainty']:reasons.append('个股预测区间超过上限')
  if net<=0:reasons.append('预期收益不足以覆盖费用和参数不确定性')
  if not prob['probability_validated']:reasons.append('概率仅为研究估计，收益贡献按可信度折减')
  allow=r['data_eligible'] and es<=c['max_downside'] and width<=c['max_uncertainty'] and net>0
  components=dict(stock_excess=max(0.,current[i]/c['max_stock']-1),sector_excess=max(0.,sector_totals[r['sector']]/c['max_sector']-1),variance_contribution=max(0.,float(contribution[i])),downside=es/max(c['max_downside'],1e-9),interval=width/max(c['max_uncertainty'],1e-9),parameter=std/vol,probability_risk=1-p)
  priority=sum(policy['priority_weights'][k]*v for k,v in components.items())
  utility.append(benefit);penalties.append(std**2+.25*es**2);eligible.append(allow)
  data.append((prob,reasons,net,priority,components,fee,benefit))
 weights=optimize('mean_variance',utility,cov+np.diag(penalties),sectors,config,eligible)
 # Suppress small uneconomic adjustments only when retaining them remains feasible.
 for i,r in enumerate(rows):
  if abs(weights[i]-current[i])>policy['no_trade_band']:continue
  proposed=weights.copy();proposed[i]=current[i]
  notional=abs(weights[i]-current[i])*equity;side='BUY' if weights[i]>current[i] else 'SELL'
  advantage=abs(weights[i]-current[i])*equity*abs(data[i][2])
  feasible=r['data_eligible'] and data[i][4]['downside']<=1 and data[i][4]['interval']<=1 and current[i]<=c['max_stock'] and proposed.sum()<=cap and all(sum(w for w,s in zip(proposed,sectors) if s==g)<=c['max_sector'] for g in set(sectors))
  if feasible and advantage<=costs(notional,side,c,r['symbol'])['total']:
   weights=proposed;data[i][1].append('小幅调整的预期改善不足以覆盖交易费用，保留仓位')
 # Recheck minimum commissions at the actual incremental buy ticket, not only a maximum-sized allocation.
 for i,r in enumerate(rows):
  if weights[i]<=current[i]:continue
  ticket=(weights[i]-current[i])*equity;f=r['forecasts'][str(c['horizon'])]
  ticket_cost=costs(ticket,'BUY',c,r['symbol'])['total']+costs(ticket,'SELL',c,r['symbol'])['total']
  if ticket<c['minimum_trade_value'] or ticket*max(0.,f['predicted_return']-f['model_uncertainty']['std'])<=ticket_cost:
   weights[i]=current[i];data[i][1].append('实际新增交易金额不足或预期改善未覆盖该笔往返费用，不新增仓位')
 output=[]
 for i,(r,w,cur,d) in enumerate(zip(rows,weights,current,data)):
  prob,reasons,net,priority,components,fee,benefit=d;shares=positions.get(r['symbol'],{}).get('shares',0);target=int(w*equity/r['close'])//100*100
  if w<=1e-12 and eligible[i]:reasons.append('组合配置中净收益优势不足以抵消边际风险，未分配目标仓位')
  output.append(dict(symbol=r['symbol'],name=r['name'],sector=r['sector'],current_shares=shares,current_weight=float(cur),target_weight=float(w),target_amount=float(w*equity),reference_target_shares=target,
   direction='BUY' if target>shares else 'SELL' if target==0 and shares else 'REDUCE' if target<shares else 'HOLD',reasons=[f'市场总仓位预算上限{cap:.0%}，允许闲置现金']+reasons+['结合个股净预期收益、概率可信度、不确定性、下行风险、协方差和行业上限配置'],
   risk_priority=float(priority),risk_components=components,net_expected_return=float(net),effective_utility=float(benefit),roundtrip_cost_fraction=float(fee),reference_price=r['close'],price_date=payload['asof'],price_source=r['price_source'],probability=prob,executable=False))
 return dict(zip(symbols,map(float,weights))),output
