"""Module VIII: research targets and conditional plans; no order transport exists."""
from pathlib import Path
from datetime import datetime
import hashlib
import json
import logging
import sqlite3
import uuid
import numpy as np
import pandas as pd
from .core import now, read_snapshot, read_watchlist, write_json, write_csv, atomic_text

METHODS = ('equal_weight', 'risk_parity', 'mean_variance', 'constrained_score')
DEFAULTS = dict(initial_cash=150000., horizon=20, max_stock=.15, max_sector=.25,
                max_invested=.60, max_turnover=.30, max_cost_fraction=.002,
                risk_aversion=8., covariance_days=252, minimum_covariance_days=120,
                minimum_probability=.55, max_uncertainty=.30, max_downside=.25,
                minimum_trade_value=2000., selected_method=None,
                commission_bps=3., minimum_commission=5., transfer_bps=.1,
                exchange_bps=.341, regulatory_bps=.2, sell_tax_bps=5.,
                commission_includes_regulatory=True, commission_includes_transfer=False,
                slippage_bps=5., fees_verified=False, fee_source=None,
                market_environment='unknown', market_exposure={'unknown':0., 'risk_off':0., 'neutral':.4, 'risk_on':.6})
# 核验日期 2026-10-19；范围仅普通沪深主板A股。
# SSE 2026规则: https://www.sse.com.cn/lawandrules/sselawsrules2025/fund/trading/c/c_20260424_10817739.shtml
# SZSE 2026规则: https://docs.static.szse.cn/www/lawrules/rule/trade/current/W020260424690713155663.pdf
# 新股/风险警示/退市及未知涨跌幅制度均由market_blockers阻断。
RULES = {
    'SSE_MAIN_A': dict(prefixes=['600','601','603','605'], buy_lot=100, tick=.01, max_order=1000000, t_plus=1, ordinary_limit=.10),
    'SZSE_MAIN_A': dict(prefixes=['000','001','002','003'], buy_lot=100, tick=.01, max_order=1000000, t_plus=1, ordinary_limit=.10),
}


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and np.isfinite(value)


def validate_config(c):
    for key, value in c.items():
        if isinstance(value, (int,float)) and (not np.isfinite(value) or value < 0):
            raise ValueError(f'配置无效: {key}')
    for k in ('max_stock','max_sector','max_invested','max_turnover','max_cost_fraction','minimum_probability'):
        if not 0 <= c[k] <= 1: raise ValueError(k)
    if not 0 < c['max_stock'] <= c['max_invested'] <= 1 or c['initial_cash'] <= 0 or c['risk_aversion'] <= 0:
        raise ValueError('仓位/资金/风险厌恶参数无效')
    if c['horizon'] not in (1,5,20) or c['minimum_covariance_days'] < 30:
        raise ValueError('预测期限或协方差窗口无效')
    if c['selected_method'] not in (None,*METHODS): raise ValueError('未知方法')
    if c['market_environment'] not in c['market_exposure']: raise ValueError('未知市场环境')
    if any(not finite(v) or not 0 <= v <= 1 for v in c['market_exposure'].values()): raise ValueError('市场暴露上限无效')


def rule_for(symbol):
    if not isinstance(symbol,str) or len(symbol)!=6 or not symbol.isdigit(): return None
    return next((key for key,r in RULES.items() if symbol.startswith(tuple(r['prefixes']))),None)


def load_fee_config(root, c):
    path=Path(root)/'data/connections/fees/latest.json'
    if path.exists():
        fee=json.loads(path.read_text())
        keys=('commission_bps','minimum_commission','transfer_bps','exchange_bps','regulatory_bps','sell_tax_bps','slippage_bps','commission_includes_regulatory','commission_includes_transfer','fees_verified','fee_source','fee_market_overrides')
        c.update({k:fee[k] for k in keys if k in fee})
    return c


def costs(notional, side, c, symbol=None):
    """Total includes an adverse slippage allowance. No double counting of bundled fees."""
    overrides=c.get('fee_market_overrides',{})
    if symbol is not None and overrides:
        market=rule_for(symbol)
        if market not in overrides: raise ValueError('证券没有已确认的费用规则')
        c={**c,**overrides[market]}
    if not finite(notional) or notional < 0 or side not in ('BUY','SELL'): raise ValueError('费用输入无效')
    if not notional: return dict(commission=0., transfer=0., regulatory=0., stamp=0., slippage=0., total=0.)
    parts = dict(commission=max(c['minimum_commission'],notional*c['commission_bps']/10000),
                 transfer=0. if c['commission_includes_transfer'] else notional*c['transfer_bps']/10000,
                 regulatory=0. if c['commission_includes_regulatory'] else notional*(c['exchange_bps']+c['regulatory_bps'])/10000,
                 stamp=notional*c['sell_tax_bps']/10000 if side=='SELL' else 0.,
                 slippage=notional*c['slippage_bps']/10000)
    return {**parts,'total':sum(parts.values())}


def sell_quantity(wanted, sellable, total):
    """A residual odd lot may be sold once, including together with round lots."""
    n=min(max(0,int(wanted)),sellable)
    odd=total%100
    if odd and n>=odd and sellable>=odd:
        return (n-odd)//100*100+odd
    return n//100*100


def covariance(returns, minimum=120, horizon=20):
    a=np.asarray(returns,float)
    if a.ndim!=2 or len(a)<minimum or not np.isfinite(a).all(): raise ValueError('协方差缺失/样本不足/非有限数据')
    s=np.atleast_2d(np.cov(a,rowvar=False,ddof=1))*horizon
    # Diagonal shrinkage and eigenvalue flooring control ill-conditioning.
    s=.8*s+.2*np.diag(np.diag(s))
    vals, vec=np.linalg.eigh(s)
    return (vec*np.maximum(vals,1e-8))@vec.T


def cap_weights(w, sectors, c):
    w=np.minimum(np.maximum(np.asarray(w,float),0.),c['max_stock'])
    for sector in set(sectors):
        ids=np.array([s==sector for s in sectors])
        total=w[ids].sum()
        if total>c['max_sector']: w[ids]*=c['max_sector']/total
    if w.sum()>c['max_invested']: w*=c['max_invested']/w.sum()
    return w



def project_weights(v, sectors, c):
    """Euclidean projection onto box, industry and invested-weight convex sets."""
    x=np.asarray(v,float).copy(); corrections=[np.zeros(len(x)) for _ in range(3)]
    for _ in range(300):
        old=x.copy()
        for k in range(3):
            y=x+corrections[k]
            if k==0: z=np.clip(y,0,c['max_stock'])
            elif k==1:
                z=y.copy()
                for sector in set(sectors):
                    ids=np.array([s==sector for s in sectors])
                    excess=z[ids].sum()-c['max_sector']
                    if excess>0: z[ids]-=excess/ids.sum()
            else: z=y-max(0.,(y.sum()-c['max_invested'])/len(y))
            corrections[k]=y-z; x=z
        if np.max(abs(x-old))<1e-12: break
    return cap_weights(x,sectors,c)


def optimize(method, mu, cov, sectors, c, eligible=None):
    """Long-only research weights. Cash absorbs every unallocated constraint budget."""
    if method not in METHODS: raise ValueError(method)
    mu=np.asarray(mu,float); cov=np.asarray(cov,float)
    if cov.shape!=(len(mu),len(mu)) or not np.isfinite(cov).all() or not np.isfinite(mu).all(): raise ValueError('优化输入无效')
    mask=np.ones(len(mu),bool) if eligible is None else np.asarray(eligible,bool)
    ids=np.flatnonzero(mask)
    w=np.zeros(len(mu))
    if not len(ids): return w
    m=mu[ids]; s=cov[np.ix_(ids,ids)]; groups=[sectors[i] for i in ids]
    if method=='equal_weight': x=np.ones(len(ids))/len(ids)*c['max_invested']
    elif method=='risk_parity':
        # Cyclic coordinate descent solves 1/2*x'Sx - sum(b_i*log x_i).
        b=np.ones(len(ids))/len(ids); x=1/np.sqrt(np.diag(s))
        for _ in range(1000):
            old=x.copy()
            for i in range(len(x)):
                cross=s[i]@x-s[i,i]*x[i]
                x[i]=(-cross+np.sqrt(cross*cross+4*s[i,i]*b[i]))/(2*s[i,i])
            if np.max(abs(x-old))<1e-10: break
        x=x/x.sum()*c['max_invested']
    elif method=='constrained_score':
        score=np.maximum(m,0)/np.sqrt(np.diag(s))
        x=score/max(score.sum(),1e-12)*c['max_invested']
    else:
        x=np.zeros(len(ids)); step=1/(c['risk_aversion']*max(np.linalg.eigvalsh(s).max(),1e-8))
        # Feasible descent with conservative cap projection, cash is a zero-return asset.
        for _ in range(2000):
            nxt=project_weights(x+step*(m-c['risk_aversion']*s@x),groups,c)
            if np.max(abs(nxt-x))<1e-10: break
            x=nxt
    w[ids]=cap_weights(x,groups,c)
    return w


def account_state(account, prices, at):
    assumed=account is None
    a=account or dict(cash=150000.,available_cash=150000.,positions={},source='ASSUMED_INITIAL_CASH',asof=at)
    cash=a.get('cash'); available=a.get('available_cash')
    if not finite(cash) or not finite(available) or not 0<=available<=cash: raise ValueError('现金/可用资金无效')
    positions=a.get('positions',{})
    for symbol,p in positions.items():
        for key in ('shares','sellable'):
            if not isinstance(p.get(key),int) or isinstance(p[key],bool) or p[key]<0: raise ValueError('持仓数量须非负整数')
        if p['sellable']>p['shares']: raise ValueError('可卖数量超过持仓')
        if symbol not in prices or not finite(prices[symbol]) or prices[symbol]<=0: raise ValueError(f'持仓缺少估值价格: {symbol}')
    equity=cash+sum(p['shares']*prices[s] for s,p in positions.items())
    if equity<=0: raise ValueError('账户权益无效')
    return a,equity,assumed


def research_blockers(row,c):
    out=[]
    if not row.get('data_eligible'): out.append('DATA_INELIGIBLE')
    f=row.get('forecast',{})
    required=['predicted_return','volatility_horizon','downside_es95_normal_proxy']
    if any(not finite(f.get(k)) for k in required): return out+['FORECAST_FIELDS_MISSING']
    interval=f.get('interval80_empirical',[])
    if len(interval)!=2 or not all(finite(x) for x in interval) or interval[0]>interval[1]: return out+['UNCERTAINTY_MISSING']
    if not f.get('validation',{}).get('preliminary_screen_passed'): out.append('FORECAST_VALIDATION_FAILED')
    probability=f.get('up_probability_calibrated') if f.get('probability_calibration',{}).get('independent_validated') else f.get('up_probability_empirical')
    if not finite(probability): out.append('PROBABILITY_UNCALIBRATED')
    elif not 0<=probability<=1: out.append('PROBABILITY_INVALID')
    elif probability<c['minimum_probability']: out.append('PROBABILITY_LOW')
    if interval[1]-interval[0]>c['max_uncertainty']: out.append('UNCERTAINTY_HIGH')
    if abs(min(0.,f['downside_es95_normal_proxy']))>c['max_downside']: out.append('DOWNSIDE_HIGH')
    ticket=c['initial_cash']*c['max_stock']
    hurdle=(costs(ticket,'BUY',c,row.get('symbol'))['total']+costs(ticket,'SELL',c,row.get('symbol'))['total'])/ticket
    if f['predicted_return']<=hurdle: out.append('RETURN_BELOW_COST')
    if row.get('sector') in (None,'','未分类'): out.append('SECTOR_UNKNOWN')
    return out


def market_blockers(row, at):
    """Historical closes are useful analysis inputs but cannot establish execution state."""
    out=[]; q=row.get('quote',{}); t=pd.Timestamp(at)
    if rule_for(row['symbol'])!=row.get('rule'): out.append('UNSUPPORTED_SECURITY_RULE')
    if not finite(q.get('price')) or q['price']<=0: out.append('PRICE_MISSING')
    if not q.get('source') or not q.get('asof'): out.append('PRICE_PROVENANCE_MISSING')
    try:
        age=(t-pd.Timestamp(q['asof'])).total_seconds()
        if age<0 or age>60 or q.get('kind')!='live': out.append('PRICE_NOT_CURRENT_LIVE')
    except (KeyError,ValueError,TypeError): out.append('PRICE_TIME_INVALID')
    if q.get('status')!='trading' or not q.get('status_source') or q.get('status_asof')!=q.get('asof'): out.append('TRADING_STATUS_UNVERIFIED')
    if q.get('risk_warning') is not False or q.get('ordinary_session') is not True: out.append('SPECIAL_SESSION_OR_RISK_SECURITY')
    if q.get('limit_regime')!='ordinary_10pct': out.append('LIMIT_REGIME_UNVERIFIED')
    lo,hi=q.get('limit_down'),q.get('limit_up')
    if not finite(lo) or not finite(hi) or not 0<lo<hi: out.append('LIMIT_PRICES_MISSING')
    elif finite(q.get('price')) and not lo<q['price']<hi: out.append('LIMIT_LOCK_OR_PRICE_INVALID')
    if finite(q.get('price')) and abs(q['price']*100-round(q['price']*100))>1e-6: out.append('PRICE_TICK_INVALID')
    if q.get('checks') and any('CONFLICT' in x or x=='FUTURE_QUOTE' for x in q['checks']): out.append('MARKET_SOURCE_CONFLICT')
    if not q.get('session_verified') or t.weekday()>=5: out.append('SESSION_UNVERIFIED')
    local=t.tz_convert('Asia/Shanghai') if t.tzinfo else None
    if local is None or not ((9,30)<=(local.hour,local.minute)<(11,30) or (13,0)<=(local.hour,local.minute)<(14,57)):
        out.append('OUTSIDE_SUPPORTED_CONTINUOUS_SESSION')
    return out


def build_plan(rows, cov, c, account=None, at=None, input_errors=None, formal=None):
    validate_config(c); at=at or now().isoformat()
    if len({r['symbol'] for r in rows})!=len(rows): raise ValueError('重复证券')
    prices={r['symbol']:r.get('quote',{}).get('price') for r in rows}
    a,equity,assumed=account_state(account,prices,at)
    if assumed and c['initial_cash']!=150000.: raise ValueError('初始账户固定15万元；其他账户请明确传入')
    positions=a['positions']; sectors=[r.get('sector','未分类') for r in rows]
    current=np.array([positions.get(r['symbol'],{}).get('shares',0)*prices[r['symbol']]/equity if finite(prices[r['symbol']]) else 0. for r in rows])
    blockers=[research_blockers(r,c) for r in rows]
    mu=np.array([r.get('forecast',{}).get('predicted_return',0.) or 0. for r in rows],float)
    mu=np.nan_to_num(mu)
    comparisons={}; global_flags=list(input_errors or [])
    for method in METHODS:
        if cov is None:
            comparisons[method]=dict(status='BLOCKED_COVARIANCE',weights=None); continue
        w=optimize(method,mu,cov,sectors,c,[not b for b in blockers])
        exposure=min(c['max_invested'],c['market_exposure'][c['market_environment']])
        w*=min(1.,exposure/max(w.sum(),1e-12))
        turnover=float(abs(w-current).sum())
        estimated_cost=sum(costs(abs(w[i]-current[i])*equity,'BUY' if w[i]>current[i] else 'SELL',c,rows[i]['symbol'])['total'] for i in range(len(rows)))
        comparisons[method]=dict(status='RESEARCH_ONLY',weights={r['symbol']:float(w[i]) for i,r in enumerate(rows)},cash_weight=float(1-w.sum()),
                                expected_return=float(w@mu),volatility=float(np.sqrt(w@cov@w)),turnover=turnover,
                                estimated_cost=estimated_cost,within_turnover=bool(turnover<=c['max_turnover']),within_cost=bool(estimated_cost<=equity*c['max_cost_fraction']),
                                risk_contributions=(w*(cov@w)).tolist(),note='约束后的风险平价不保证严格等风险贡献；均值方差使用凸目标投影梯度迭代，保留现金')
    # No research score or single flag can silently promote a strategy.
    approved=bool(formal and formal.get('status')=='VALIDATED' and formal.get('formal_method')==c['selected_method'])
    if not approved: global_flags.append('FORMAL_METHOD_NOT_VALIDATED')
    if cov is None: global_flags.append('COVARIANCE_UNAVAILABLE')
    if c['market_environment']=='unknown': global_flags.append('MARKET_ENVIRONMENT_UNKNOWN')
    if not c['fees_verified'] or not c['fee_source']: global_flags.append('BROKER_FEES_UNVERIFIED')
    if assumed: global_flags.append('ACCOUNT_ASSUMED_INITIAL_CASH')
    if not assumed:
        try:
            age=(pd.Timestamp(at)-pd.Timestamp(a['asof'])).total_seconds()
            if pd.isna(age) or age<0 or age>60 or not a.get('source'): global_flags.append('ACCOUNT_STALE_OR_UNVERIFIED')
        except (KeyError,TypeError,ValueError): global_flags.append('ACCOUNT_STALE_OR_UNVERIFIED')
    selected=c['selected_method']
    analytical=comparisons.get(selected,{}) if selected else {}
    weights=analytical.get('weights')
    decisions=[]
    for i,row in enumerate(rows):
        p=positions.get(row['symbol'],dict(shares=0,sellable=0)); price=prices[row['symbol']]
        w=weights[row['symbol']] if weights else float(current[i])
        flags=list(dict.fromkeys(global_flags+blockers[i]+market_blockers(row,at)))
        f=row.get('forecast',{})
        decisions.append(dict(symbol=row['symbol'],name=row['name'],sector=sectors[i],rule=row.get('rule'),
            price=price,price_source=row.get('quote',{}).get('source'),price_asof=row.get('quote',{}).get('asof'),
            predicted_return=f.get('predicted_return'),up_probability=f.get('up_probability_calibrated') if f.get('probability_calibration',{}).get('independent_validated') else None,
            estimated_cost=costs(0.,'BUY',c,row['symbol']),
            calibrated_probability_estimate=f.get('up_probability_calibrated'),
            probability_basis=f.get('probability_method'),probability_calibrated=bool(f.get('probability_calibration',{}).get('independent_validated')),
            calibration_status=f.get('probability_calibration',{}).get('status','MISSING'),
            uncertainty=f.get('interval80_empirical'),downside_risk=f.get('downside_es95_normal_proxy'),
            risk_level='UNKNOWN' if blockers[i] else 'HIGH' if f.get('volatility_horizon',0)>.15 else 'MEDIUM',
            current_shares=p['shares'],sellable_shares=p['sellable'],current_weight=float(current[i]),current_amount=round(float(current[i])*equity,2),target_weight=w if weights else None,target_amount=round(w*equity,2) if weights else None,
            target_kind='RESEARCH_TARGET' if weights else 'PRESERVE_CURRENT_PENDING_VALIDATION',
            action='NO_TRADE',suggested_shares=0,cash_after=round(a['cash'],2),available_cash_after=round(a['available_cash'],2),
            executable=False,blockers=flags,reasons=['未选定经验证正式方法；目标配置与交易指令分离','次日价格及状态须重新核验，限价不保证成交']))
    from .probability_display import fields as probability_fields
    for d,row in zip(decisions,rows):d.update(probability_fields(row.get('forecast',{})))
    conditional=None
    if approved and weights and not global_flags:
        conditional=candidate_plan(rows,weights,a,c,at)
        updates={r['symbol']:r for r in conditional['rows']}
        for d in decisions:
            update=updates[d['symbol']]
            d.update({k:update[k] for k in ('action','suggested_shares','cash_after','available_cash_after','estimated_cost')})
            d['blockers']=update['blockers'];d['conditional']=True;d['reasons']=[update['condition']]
    return dict(schema_version=1,generated_at=at,mode='PLAN_ONLY',account=a,account_assumed=assumed,equity=equity,
                formal_method=c['selected_method'] if approved else None,requested_research_method=selected,comparisons=comparisons,decisions=decisions,
                cash_after=conditional['cash_after'] if conditional else a['cash'],available_cash_after=conditional['available_cash_after'] if conditional else a['available_cash'],candidate_orders=conditional['rows'] if conditional else [],submitted_orders=0,
                blockers=list(dict.fromkeys(global_flags)),config=c)


def candidate_plan(rows, weights, account, c, at):
    """Explicit research/testing API: conditional quantities only, never executable orders.

    Not called by production CLI until a validated promotion workflow is supplied.
    Sell proceeds are deliberately NOT reused to fund buys, avoiding fill dependencies.
    """
    validate_config(c)
    if set(weights)!={r['symbol'] for r in rows}: raise ValueError('目标代码与行情不匹配')
    if any(not finite(w) or w<0 or w>c['max_stock'] for w in weights.values()) or sum(weights.values())>c['max_invested']+1e-10: raise ValueError('目标仓位越界')
    groups={}
    for r in rows: groups[r['sector']]=groups.get(r['sector'],0)+weights[r['symbol']]
    if any(w>c['max_sector']+1e-10 for w in groups.values()): raise ValueError('行业上限')
    a,equity,assumed=account_state(account,{r['symbol']:r['quote']['price'] for r in rows},at)
    if assumed: raise ValueError('候选数量需要显式账户')
    remaining=a['available_cash']; cash=a['cash']; turnover=spent=0.; result=[]
    for row in sorted(rows,key=lambda r:r['symbol']):
        symbol=row['symbol']; price=row['quote']['price']; p=a['positions'].get(symbol,dict(shares=0,sellable=0))
        flags=market_blockers(row,at); target=int(weights[symbol]*equity/price)//100*100
        delta=target-p['shares']; side='BUY' if delta>0 else 'SELL'
        if not c['fees_verified'] or not c['fee_source']: flags.append('BROKER_FEES_UNVERIFIED')
        try:
            age=(pd.Timestamp(at)-pd.Timestamp(a['asof'])).total_seconds()
            if pd.isna(age) or age<0 or age>60 or not a.get('source'): flags.append('ACCOUNT_STALE_OR_UNVERIFIED')
        except (KeyError,ValueError,TypeError): flags.append('ACCOUNT_STALE_OR_UNVERIFIED')
        if not row.get('synthetic',False):
            flags.extend(research_blockers(row,c))
            if not row.get('forecast',{}).get('probability_calibration',{}).get('independent_validated'): flags.append('PROBABILITY_UNCALIBRATED')
        max_qty=int(max(0,c['max_turnover']*equity-turnover)/price)
        qty=min(abs(delta),1000000,max_qty)
        qty=qty//100*100 if delta>0 else sell_quantity(qty,p['sellable'],p['shares'])
        if delta<0 and abs(delta)>p['sellable']: flags.append('T_PLUS_1_OR_SELLABLE_CAP')
        # Sellable cap may permit a partial sell; keep its reason without blocking it.
        hard=[f for f in flags if f!='T_PLUS_1_OR_SELLABLE_CAP']
        if hard: qty=0
        while qty>0:
            n=qty*price; fee=costs(n,side,c,symbol)['total']
            reserve=equity*(1-min(c['max_invested'],c['market_exposure'][c['market_environment']]))
            if (spent+fee<=equity*c['max_cost_fraction'] and (side=='SELL' or (n+fee<=remaining and cash-n-fee>=reserve))): break
            qty=qty-100 if qty>=100 else 0
            if side=='SELL': qty=sell_quantity(qty,p['sellable'],p['shares'])
        n=qty*price; fee=costs(n,side,c,symbol)
        if qty and n<c['minimum_trade_value']: qty=0; n=0.; fee=costs(0,side,c,symbol); flags.append('MINIMUM_TRADE_VALUE')
        if qty and side=='BUY' and not row.get('synthetic',False):
            if n*row['forecast']['predicted_return']<=fee['total']+costs(n,'SELL',c,symbol)['total']:
                qty=0; n=0.; fee=costs(0,side,c,symbol); flags.append('ACTUAL_TICKET_RETURN_BELOW_COST')
        if qty:
            cash+=(-n-fee['total']) if side=='BUY' else n-fee['total']
            if side=='BUY': remaining-=n+fee['total']
            turnover+=n; spent+=fee['total']
        action=('BUY' if side=='BUY' else 'SELL' if qty==p['shares'] else 'REDUCE') if qty else ('HOLD' if delta==0 and not flags else 'NO_TRADE')
        result.append(dict(symbol=symbol,action=action,suggested_shares=qty,signed_shares=qty if side=='BUY' else -qty,
            target_weight=weights[symbol],target_amount=weights[symbol]*equity,reference_price=price,price_source=row['quote'].get('source'),price_asof=row['quote'].get('asof'),
            cash_after=round(cash,2),available_cash_after=round(remaining,2),estimated_cost=fee,blockers=flags,
            executable=False,conditional=True,synthetic=row.get('synthetic',False),condition='重新核验价格、有效申报范围、盘口、账户、费用；成交不保证；合成数据仅测试'))
    return dict(mode='CONDITIONAL_RESEARCH_PLAN',rows=result,cash_after=cash,available_cash_after=remaining,turnover=turnover/equity,cost=spent,submitted_orders=0)


def load_inputs(root,c):
    path=root/'reports/forecasts/latest/forecast.json'
    payload=json.loads(path.read_text()) if path.exists() else {'forecasts':[],'errors':['第七模块预测文件缺失']}
    by_symbol={r['symbol']:r for r in payload['forecasts']}; rows=[]; histories={}; errors=[]
    if payload.get('errors'): errors.append('MODULE7_INCOMPLETE')
    try:
        from .core import cutoff, load_config
        from .data import calendar
        end=cutoff()
        sessions,_=calendar(root,load_config(root),end,offline=True)
        latest=pd.DatetimeIndex(sessions); latest=latest[latest<=end].max()
        if pd.isna(latest) or payload.get('asof')!=str(latest.date()): errors.append('FORECAST_STALE')
        if pd.Timestamp(payload['generated_at'])>pd.Timestamp(now()): errors.append('FORECAST_GENERATED_IN_FUTURE')
    except (ValueError,KeyError,FileNotFoundError) as exc:
        errors.append('FORECAST_CALENDAR_UNVERIFIED:'+str(exc))
    for item in read_watchlist(root).itertuples():
        source=by_symbol.get(item.symbol,{}); f=source.get('forecasts',{}).get(str(c['horizon']),{})
        snap=read_snapshot(root,item.symbol); q={}
        if snap is not None:
            raw=snap['raw']; raw=raw[raw.date<=pd.Timestamp(payload.get('asof','1900-01-01'))]
            if not raw.empty:
                day=str(raw.date.iloc[-1].date()); q=dict(price=float(raw.close.iloc[-1]),asof=day+'T15:00:00+08:00',source=snap['meta']['source'],kind='historical_close',status='unknown')
                hfq=snap['hfq']; hfq=hfq[hfq.date<=pd.Timestamp(payload['asof'])]
                histories[item.symbol]=hfq.set_index('date').close.pct_change(fill_method=None).rename(item.symbol)
            if payload.get('data_versions',{}).get(item.symbol)!=snap['meta']['version']: errors.append('SNAPSHOT_FORECAST_VERSION_MISMATCH:'+item.symbol)
        if not source: errors.append('FORECAST_MISSING:'+item.symbol)
        rows.append(dict(symbol=item.symbol,name=item.name,sector=source.get('sector',getattr(item,'sector','未分类')),
                         data_eligible=source.get('data_eligible',False),rule=rule_for(item.symbol),quote=q,forecast=f))
    cov=None; covariance_meta={}
    if len(histories)==len(rows):
        frame=pd.concat([histories[r['symbol']] for r in rows],axis=1).dropna().tail(c['covariance_days'])
        try:
            cov=covariance(frame.to_numpy(),c['minimum_covariance_days'],c['horizon'])
            covariance_meta=dict(symbols=list(frame.columns),observations=len(frame),first=str(frame.index.min().date()),last=str(frame.index.max().date()),horizon=c['horizon'],method='20% diagonal shrinkage + eigenvalue floor',matrix=cov.tolist())
        except ValueError as exc: errors.append(str(exc))
    return rows,cov,payload,errors,covariance_meta


def generate(root, selected_method=None):
    root=Path(root); c={**DEFAULTS}
    config_path=root/'decision_config.json'
    if config_path.exists(): c.update(json.loads(config_path.read_text()))
    if selected_method: c['selected_method']=selected_method
    from .connections import market_environment, readiness
    load_fee_config(root,c)
    validate_config(c)
    rows,cov,forecast,errors,cov_meta=load_inputs(root,c)
    environment=market_environment(root,forecast.get('asof'))
    from .risk_models import apply as apply_risk_models
    frozen_cov=cov.copy() if cov is not None else None;frozen_environment=environment.copy()
    cov,environment,risk_models=apply_risk_models(root,forecast.get('asof'),c['horizon'],cov,cov_meta.get('symbols',[]),environment,c,input_versions=forecast.get('data_versions'))
    c['market_environment']=environment['state']
    from .probability_calibration import attach
    registered_probabilities={r['symbol']:r['forecast'].copy() for r in rows if r['forecast'].get('model_status')=='VALIDATED_REGISTERED'}
    calibration=attach(root,rows,forecast,c['horizon'])
    # Legacy pooled research calibration cannot override release-bound calibration.
    for r in rows:
        if r['symbol'] in registered_probabilities:r['forecast']=registered_probabilities[r['symbol']]
    market_path=root/'data/connections/market/latest.json'
    market=json.loads(market_path.read_text()) if market_path.exists() else {}
    for row in rows:
        q=market.get('quotes',{}).get(row['symbol'])
        if q and q.get('name')==row['name'] and q.get('price') and pd.Timestamp(q['asof'])>=pd.Timestamp(row.get('quote',{}).get('asof','1900-01-01T00:00:00+08:00')):
            row['historical_quote']=row['quote']
            row['quote']=q
    at=now().isoformat()
    account_path=root/'data/decision_account.json'
    account=json.loads(account_path.read_text()) if account_path.exists() else None
    if c.get('require_real_account') and account is None: raise ValueError('用户已确认有真实持仓，必须导入华福账户，不能假设全现金')
    from .lifecycle import refresh_registry,admission
    production=refresh_registry(root);formal=admission(root,production)
    if formal['status']=='VALIDATED' and selected_method in (None,formal['formal_method']):
        for row in rows:
            if row['forecast'].get('release_id')!=production['portfolio']['release']:errors.append('MODEL_PORTFOLIO_RELEASE_MISMATCH:'+row['symbol'])
        c['selected_method']=formal['formal_method'];cov=frozen_cov;environment=frozen_environment;c['market_environment']=environment['state']
        risk_models={'used':[],'reason':'正式实验使用冻结的收缩协方差与趋势环境规则；其他风险模型仅作诊断'}
    result=build_plan(rows,cov,c,account,at,errors,formal)
    result['production_admission']=formal
    result.update(source_forecast_asof=forecast.get('asof'),source_forecast_generated_at=forecast.get('generated_at'),covariance=cov_meta,security_rules=RULES,rule_scope='沪深普通主板A股；ST/新股无涨跌幅/退市及其他证券禁止候选交易')
    result['fee_config']={k:c[k] for k in ('commission_bps','minimum_commission','commission_includes_regulatory','commission_includes_transfer','fee_market_overrides','fees_verified','fee_source','slippage_bps') if k in c}
    result['account_snapshot_date']=account.get('snapshot_date') if account else None
    result['seven_metrics']=[r for r in forecast.get('seven_metrics',[]) if r['horizon']==c['horizon']]
    result['connections']=readiness(root)
    result['market_environment']=environment
    result['risk_models']=risk_models
    if risk_models['used']:
        result['covariance']={**cov_meta,'matrix':cov.tolist() if cov is not None else None,'risk_model_adjustments':risk_models['used']}
    observed_path=root/'data/connections/fee_observations/latest.json'
    result['actual_fee_observation']=json.loads(observed_path.read_text()) if observed_path.exists() else None
    result['current_exposure']=dict(stock_weight=1-account['cash']/result['equity'] if account else 0.,max_stock_weight=c['max_stock'],max_total_stock_weight=c['max_invested'])
    for item in result['decisions']:
        if item['current_shares'] and item['price'] and item['current_shares']*item['price']/result['equity']>c['max_stock']:
            item['blockers'].append('CURRENT_HOLDING_ABOVE_STOCK_CAP')
    from .next_session_plan import annotate
    from .data import calendar
    from .core import load_config
    next_session=forecast.get('next_session');plan_calendar_verified=False
    try:
        sessions,_=calendar(root,load_config(root),pd.Timestamp(forecast['asof']),offline=True)
        future=sessions[sessions>pd.Timestamp(forecast['asof'])]
        plan_calendar_verified=bool(len(future) and str(future.min().date())==next_session)
    except (ValueError,KeyError,OSError):
        pass
    annotate(result,next_session,plan_calendar_verified)
    result['probability_calibration_summary']={k:v for k,v in calibration.items() if k!='model'}
    result['account_reported_equity_note']='按本次参考行情估值；截图日期经用户确认，手机时区及实时账户状态未核实，非实时券商资产查询'
    result['input_sha256']=hashlib.sha256(json.dumps(dict(rows=rows,account=account,config=c),sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    stamp=now().strftime('%Y%m%d-%H%M%S-')+uuid.uuid4().hex[:8]
    result['run_id']=stamp
    folder=root/'reports/decisions'/stamp; folder.mkdir(parents=True)
    write_json(folder/'decision.json',result)
    write_csv(folder/'decisions.csv',pd.DataFrame(result['decisions']))
    write_json(folder/'comparison.json',result['comparisons'])
    from html import escape
    html='<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>交易决策系统</title><style>body{font:15px/1.6 system-ui;margin:32px;color:#18334a}table{border-collapse:collapse}td,th{border:1px solid #ddd;padding:10px} .scroll{overflow:auto}</style><h1>Trading Decision Engine · 第八模块</h1>'
    html+='<p>仅生成条件计划（PLAN_ONLY）；正式资格见准入记录，所有订单不可直接执行，未连接券商。</p>'
    html+='<p>'+escape(f"账户假设={result['account_assumed']}；现金 {result['cash_after']:,.2f} 元；行情日 {forecast.get('asof')}；生成时间 {at}")+'</p>'
    from .next_session_plan import render as render_next_session
    html+=render_next_session(result)
    html+='<p><a href="../../risk_control/latest/index.html">独立风控模拟调仓方案（使用模拟账本，仅减仓，不依赖收益模型准入）</a></p>'
    from .seven_metrics import export as export_seven
    html+=export_seven(result.get('seven_metrics',[]),folder)
    html+='<h2>新增风险模型</h2><p>'+escape(risk_models['reason'])+'</p><p><a href="../../risk_models/latest/index.html">GARCH与HMM样本外验证报告</a></p>'
    html+='<h2>接入状态</h2><pre>'+escape(json.dumps(result['connections'],ensure_ascii=False,indent=2))+'</pre>'
    html+='<h2>四种方法比较（研究）</h2><pre>'+escape(json.dumps(result['comparisons'],ensure_ascii=False,indent=2))+'</pre></html>'
    atomic_text(folder/'index.html',html)
    with sqlite3.connect(root/'data/market.sqlite3') as db:
        db.execute('CREATE TABLE IF NOT EXISTS decision_runs (run_id TEXT PRIMARY KEY, generated_at TEXT NOT NULL, input_sha256 TEXT NOT NULL, payload TEXT NOT NULL)')
        db.execute('INSERT INTO decision_runs VALUES (?,?,?,?)',(stamp,at,result['input_sha256'],json.dumps(result,ensure_ascii=False,allow_nan=False)))
    for name in ('decision.json','decisions.csv','comparison.json','index.html','seven_metrics.csv'):
        atomic_text(root/'reports/decisions/latest'/name,(folder/name).read_text(encoding='utf-8'))
    logging.info('Decision plan %s: rows=%s orders=0 blockers=%s',stamp,len(rows),result['blockers'])
    return result,folder
