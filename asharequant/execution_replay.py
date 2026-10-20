"""Daily execution replay. Strict status coverage; no invented fills at limits.

Signals are built at close and eligible for next-session open. Unknown corporate
operations or exchange status block fills, and invalidate promotion evidence.
"""
from pathlib import Path
import json
import hashlib
from collections import Counter
import numpy as np
import pandas as pd
from .core import read_snapshot, write_json, now
from .decision import DEFAULTS,METHODS,covariance,optimize,costs,sell_quantity,load_fee_config
from .connections import save_record,digest
from .probability_calibration import source_records


def import_history(root,path):
    data=json.loads(Path(path).read_text()); rows=data['records']; seen=set()
    for r in rows:
        key=(r['symbol'],r['date'])
        if key in seen: raise ValueError('历史状态重复')
        seen.add(key)
        if not r.get('source') or r.get('status') not in ('trading','suspended'): raise ValueError('缺少可靠历史状态来源')
        if r.get('corporate_action_status') not in ('none','event'): raise ValueError('公司行为状态缺失')
        if r['status']=='trading':
            if not all(isinstance(r.get(k),(int,float)) and np.isfinite(r[k]) and r[k]>0 for k in ('limit_down','limit_up')) or r['limit_down']>=r['limit_up']:
                raise ValueError('历史涨跌停价无效；无价格限制日不支持模拟成交')
        if r['corporate_action_status']=='event' and (not r.get('action') or not r['action'].get('source')): raise ValueError('公司行为缺少详情/来源')
    write_json(Path(root)/'data/execution_history.json',data)
    return dict(records=len(rows),input_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest())


def simulate(sessions,bars,signals,statuses,c,initial_cash=150000.,initial_positions=None,initial_marks=None):
    cash=float(initial_cash);entitlements={};tax_reserved=0.; positions={s:dict(p) for s,p in (initial_positions or {}).items()}; marks=dict(initial_marks or {}); pending={}; ledger=[]; fills=[]; blocked=Counter(); valid=total=0
    initial_equity=cash+sum(p['shares']*marks[s] for s,p in positions.items())
    sessions=pd.DatetimeIndex(sessions).sort_values(); symbol_set=sorted(set(bars)|set(positions))
    for day in sessions:
        date=str(day.date()); daily_turn=daily_cost=0.; bought={}
        # Apply explicit cash/dividend/split events before session valuation and trading.
        for s,p in positions.items():
            state=statuses.get((s,date),{})
            if state.get('corporate_action_status')=='event' and 'events' not in state.get('action',{}):
                action=state['action']; ratio=action.get('share_multiplier',1.); dividend=action.get('cash_per_share',0.)
                if ratio<=0 or dividend<0 or abs(p['shares']*ratio-round(p['shares']*ratio))>1e-8: raise ValueError('不支持分数股/负公司行为')
                cash+=p['shares']*dividend;p['shares']=int(round(p['shares']*ratio));p['sellable']=p['shares']
                if s in marks: marks[s]/=ratio
            else: p['sellable']=p['shares']
        # Entitlements are fixed at record-date close. Cash is receivable at
        # ex-date and becomes spendable only AFTER payment-date close. Bonus
        # shares are valued from ex-date but cannot be sold before listing.
        for event in entitlements.values():
            ex=pd.Timestamp(event['ex_date']);quantity=event['eligible_shares'];symbol=event['symbol']
            if day>=ex and not event.get('ex_applied'):
                bonus=quantity*event['bonus_ratio']
                if abs(bonus-round(bonus))>1e-8:raise ValueError('分数送转股分配规则缺失，拒绝模拟')
                event['bonus_shares']=int(round(bonus));event['net_cash']=quantity*event['cash_per_share_gross']*(1-event['tax_reserve_rate'])
                tax_reserved+=quantity*event['cash_per_share_gross']*event['tax_reserve_rate'];event['ex_applied']=True
            if event.get('ex_applied') and event['bonus_ratio'] and not event.get('shares_delivered') and day>=pd.Timestamp(event['listing_date']):
                p=positions.setdefault(symbol,dict(shares=0,sellable=0));p['shares']+=event['bonus_shares'];p['sellable']+=event['bonus_shares'];event['shares_delivered']=True
        def receivable_value(prices):
            return sum((0. if e.get('cash_paid') else e.get('net_cash',0.))+(0. if e.get('shares_delivered') else e.get('bonus_shares',0)*prices.get(e['symbol'],marks.get(e['symbol'],0.))) for e in entitlements.values())
        def locked_shares(symbol):
            return sum(e.get('bonus_shares',0) for e in entitlements.values() if e['symbol']==symbol and not e.get('shares_delivered'))
        opens={}
        for s in symbol_set:
            if s in bars and day in bars[s].index:
                bar=bars[s].loc[day]; opens[s]=float(bar.open)
                if s not in marks: marks[s]=float(bar.open)
        equity=cash+sum(p['shares']*opens.get(s,marks.get(s,0.)) for s,p in positions.items())+receivable_value(opens)
        starting_available=cash
        # Sell then buy; buys use starting cash, never presumed sell proceeds.
        ordered=sorted(pending,key=lambda s:(pending[s]*equity>=positions.get(s,{}).get('shares',0)*opens.get(s,0.),s))
        for s in ordered:
            state=statuses.get((s,date));total+=1
            if not state or not state.get('source') or state.get('corporate_action_status') not in ('none','event'):
                blocked['HISTORICAL_STATUS_OR_ACTION_UNKNOWN']+=1;continue
            valid+=1
            if state.get('status')!='trading' or s not in opens: blocked['SUSPENDED_OR_BAR_MISSING']+=1;continue
            price=opens[s];lo=state.get('limit_down');hi=state.get('limit_up')
            if lo is None or hi is None or not lo<price<hi: blocked['LIMIT_LOCK_OR_LIMIT_UNKNOWN']+=1;continue
            p=positions.setdefault(s,dict(shares=0,sellable=0));target=int(pending[s]*equity/price)//100*100;delta=target-p['shares']-locked_shares(s)
            side='BUY' if delta>0 else 'SELL'; qty=min(abs(delta),1000000,int(max(0,c['max_turnover']*equity-daily_turn)/price))
            qty=qty//100*100 if side=='BUY' else sell_quantity(qty,p['sellable'],p['shares'])
            if c.get('max_volume_participation') is not None:
                volume=float(bars[s].loc[day].get('volume',0.))
                if not np.isfinite(volume) or volume<=0:blocked['VOLUME_UNVERIFIED']+=1;continue
                qty=min(qty,int(volume*c['max_volume_participation'])//100*100)
            fill_price=price*(1+c['slippage_bps']/10000*(1 if side=='BUY' else -1))
            fill_price=np.ceil(fill_price*100)/100 if side=='BUY' else np.floor(fill_price*100)/100
            if not lo<fill_price<hi: blocked['SLIPPAGE_CROSSES_LIMIT']+=1;continue
            if c.get('require_fill_in_range') and not float(bars[s].loc[day,'low'])<=fill_price<=float(bars[s].loc[day,'high']):blocked['FILL_OUTSIDE_DAILY_RANGE']+=1;continue
            while qty:
                n=qty*fill_price;fee=costs(n,side,{**c,'slippage_bps':0.},s)['total']
                budget_ok=side=='SELL' or (n+fee<=starting_available and cash-n-fee>=equity*(1-c['max_invested']))
                if budget_ok and daily_cost+fee<=equity*c['max_cost_fraction']:break
                qty=max(0,qty-100)
                if side=='SELL':qty=sell_quantity(qty,p['sellable'],p['shares'])
            if not qty: continue
            n=qty*fill_price;fee=costs(n,side,{**c,'slippage_bps':0.},s)['total']
            if n<c['minimum_trade_value']: continue
            if side=='BUY':cash-=n+fee;starting_available-=n+fee;p['shares']+=qty;bought[s]=qty
            else:cash+=n-fee;p['shares']-=qty;p['sellable']-=qty
            daily_cost+=fee;daily_turn+=n
            fills.append(dict(date=date,symbol=s,side=side,shares=int(qty),price=float(fill_price),fee=float(fee),cash_after=float(cash),signal_date=pending_date))
            if cash<-.001 or p['shares']<0 or p['sellable']<0: raise AssertionError('回测账户约束失效')
        for s in symbol_set:
            if s in bars and day in bars[s].index:marks[s]=float(bars[s].loc[day,'close'])
        for symbol in symbol_set:
            state=statuses.get((symbol,date),{})
            for event in state.get('action',{}).get('events',[]):
                if pd.Timestamp(event['record_date'])==day and event['event_id'] not in entitlements:
                    shares=positions.get(symbol,{}).get('shares',0)
                    if shares:entitlements[event['event_id']]={**event,'symbol':symbol,'eligible_shares':shares}
        for event in entitlements.values():
            if event.get('ex_applied') and event['cash_per_share_gross'] and not event.get('cash_paid') and day>=pd.Timestamp(event['pay_date']):
                cash+=event['net_cash'];event['cash_paid']=True
        nav=cash+sum(p['shares']*marks[s] for s,p in positions.items())+receivable_value(marks)
        ledger.append(dict(dividend_receivable=float(sum(e.get('net_cash',0.) for e in entitlements.values() if not e.get('cash_paid'))),locked_bonus_shares={s:locked_shares(s) for s in symbol_set if locked_shares(s)},date=date,cash=float(cash),equity=float(nav),turnover=float(daily_turn/max(equity,1e-8)),fees=float(daily_cost),
                           positions={s:dict(p) for s,p in positions.items() if p['shares']}))
        pending=signals.get(date,{})
        pending_date=date
    curve=np.array([initial_equity]+[x['equity'] for x in ledger]); draw=curve/np.maximum.accumulate(curve)-1
    returns=curve[1:]/curve[:-1]-1
    std=float(np.std(returns,ddof=1)) if len(returns)>1 else 0.
    sharpe=float(np.mean(returns)/std*np.sqrt(252)) if std>1e-12 else None
    return dict(ledger=ledger,fills=fills,coverage=valid/total if total else 0.,checked_orders=total,blocked=dict(blocked),
        summary=dict(dividend_tax_reserved_conservative=float(tax_reserved),sessions=len(ledger),trades=len(fills),net_return=float(curve[-1]/initial_equity-1),max_drawdown=float(draw.min()),final_equity=float(curve[-1]),sharpe_zero_rf=sharpe,mean_daily_turnover=float(np.mean([x['turnover'] for x in ledger])) if ledger else None,benchmark_cash_return=0.),
        limitations=['日线开盘成交假设不能重现盘口排队及部分成交','现金分红按20%保守税款预留，到账日收盘后可用；并非真实券商税单。送转股上市前仅记权益，不可卖出','策略评估须另有独立留出集和动态观察池证据'])


def run(root):
    root=Path(root); payload=json.loads((root/'reports/forecasts/latest/forecast.json').read_text());records,path=source_records(root,payload)
    c={**DEFAULTS,**json.loads((root/'decision_config.json').read_text())}; load_fee_config(root,c); records=records[records.horizon==c['horizon']].copy()
    history_path=root/'data/execution_history.json'
    history=json.loads(history_path.read_text()) if history_path.exists() else {'records':[]}
    if not history.get('records'):
        # No zero-trade equity curve may stand in for missing execution evidence.
        result=dict(status='BLOCKED_EXECUTION_HISTORY_MISSING',formal_method=None,results={},
            blockers=['VERIFIED_HISTORICAL_STATUS_AND_CORPORATE_ACTIONS_MISSING','UNTOUCHED_HOLDOUT_AND_POINT_IN_TIME_UNIVERSE_REQUIRED'],
            forecast_generated_at=payload.get('generated_at'),model=payload.get('model'),
            config_sha256=digest(c),history_sha256=digest(history),source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            note='缺少历史交易状态与公司行为，未运行优化或生成收益曲线；必须接入数据后重新验证')
        write_json(root/'reports/decisions/execution_replay.json',result)
        return save_record(root,'replay',result)
    records['date']=pd.to_datetime(records.date);symbols=sorted(records.symbol.unique());bars={};hist={};sectors={r['symbol']:r['sector'] for r in payload['forecasts']}
    for s in symbols:
        snap=read_snapshot(root,s)
        if snap['meta']['version']!=payload['data_versions'][s]: raise ValueError('回测快照与预测版本不一致')
        bars[s]=snap['raw'].set_index('date');hist[s]=snap['hfq'].set_index('date').close.pct_change(fill_method=None)
    from .data import calendar
    from .core import load_config
    sessions,_=calendar(root,load_config(root),pd.Timestamp(payload['asof']),offline=True)
    sessions=sessions[(sessions>=records.date.min())&(sessions<=pd.Timestamp(payload['asof']))]
    statuses={(r['symbol'],r['date']):r for r in history['records']}
    targets={m:{} for m in METHODS}; stability={m:0. for m in METHODS}
    for day,g in records.groupby('date',sort=True):
        if set(g.symbol)!=set(symbols):continue
        past=pd.concat([hist[s].loc[:day].rename(s) for s in symbols],axis=1).dropna().tail(c['covariance_days'])
        try:cov=covariance(past.to_numpy(),c['minimum_covariance_days'],c['horizon'])
        except ValueError:continue
        mu=g.set_index('symbol').reindex(symbols).predicted.to_numpy();groups=[sectors[s] for s in symbols]
        for m in METHODS:
            w=optimize(m,mu,cov,groups,c,mu>0);targets[m][str(day.date())]=dict(zip(symbols,map(float,w)))
            if len(targets[m])%20==1:
                w2=optimize(m,mu*1.01,cov*1.05,groups,c,mu>0);stability[m]=max(stability[m],float(abs(w-w2).sum()))
    results={m:simulate(sessions,bars,targets[m],statuses,c) for m in METHODS}
    for m,r in results.items():
        r['stability_max_weight_l1']=stability[m]
        r['cost_stress']=simulate(sessions,bars,targets[m],statuses,{**c,'commission_bps':c['commission_bps']*2,'slippage_bps':c['slippage_bps']*3})['summary']
    # No selection from a reused validation set: code is connected, evidence may fail.
    missing=[]
    if not history['records']: missing.append('VERIFIED_HISTORICAL_STATUS_AND_CORPORATE_ACTIONS_MISSING')
    missing.append('UNTOUCHED_HOLDOUT_AND_POINT_IN_TIME_UNIVERSE_REQUIRED')
    result=dict(status='EXECUTION_REPLAY_COMPLETE_EVIDENCE_INCOMPLETE',formal_method=None,results=results,blockers=missing,
        forecast_generated_at=payload['generated_at'],model=payload['model'],config_sha256=digest(c),history_sha256=digest(history),source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        promotion_criteria=dict(minimum_sessions=120,minimum_trades=20,minimum_coverage=.99,maximum_drawdown=.15,maximum_stability_l1=.05,independent_holdout_required=True),
        note='无历史状态的日子拒绝模拟成交；零交易零收益不等于策略安全或验证通过')
    write_json(root/'reports/decisions/execution_replay.json',result)
    return save_record(root,'replay',result)
