"""Active research execution: original research engine plus actual-ticket buy edge gate.
Original research_execution and formal execution_replay remain untouched.
"""
import numpy as np
import pandas as pd
from collections import Counter
from .decision import costs,sell_quantity
INITIAL=150000.
def simulate(sessions,bars,signals,statuses,c,initial_cash=150000.,initial_positions=None,initial_marks=None,priorities=None,edges=None):
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
        def priority_key(s):
            buying=pending[s]*equity>=positions.get(s,{}).get('shares',0)*opens.get(s,0.)
            priority=(priorities or {}).get(pending_date,{}).get(s,0.)
            if not np.isfinite(priority):raise ValueError('INVALID_FROZEN_RISK_PRIORITY')
            return (buying,0. if buying else -priority,s)
        ordered=sorted(pending,key=priority_key)
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
                if budget_ok and daily_cost+fee<=equity*c['max_cost_fraction'] and daily_turn+n<=equity*c['max_turnover']+1e-9:break
                qty=max(0,qty-100)
                if side=='SELL':qty=sell_quantity(qty,p['sellable'],p['shares'])
            if not qty: continue
            n=qty*fill_price;fee=costs(n,side,{**c,'slippage_bps':0.},s)['total']
            if n<c['minimum_trade_value']: continue
            edge_check=None
            if side=='BUY':
                edge_check=buy_edge((edges or {}).get(pending_date,{}).get(s),qty,price,fill_price,c,s)
                if not edge_check['allowed']:
                    blocked[edge_check['reason']]+=1;continue
            if side=='BUY':cash-=n+fee;starting_available-=n+fee;p['shares']+=qty;bought[s]=qty
            else:cash+=n-fee;p['shares']-=qty;p['sellable']-=qty
            daily_cost+=fee;daily_turn+=n
            fills.append(dict(date=date,symbol=s,side=side,shares=int(qty),price=float(fill_price),fee=float(fee),cash_after=float(cash),signal_date=pending_date,edge_check=edge_check))
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


def replay(meta,cal,plans,observations):
    anchor=pd.Timestamp(meta['anchor_session']);dates=[anchor]+[pd.Timestamp(x) for x in sorted(observations)]
    symbols=sorted({s for o in observations.values() for s in o['bars']});bars={};states={};signals={}
    for symbol in symbols:
        entries=[{'date':pd.Timestamp(day),**o['bars'][symbol]} for day,o in sorted(observations.items()) if symbol in o['bars']]
        bars[symbol]=pd.DataFrame(entries).set_index('date')
    for day,o in observations.items():
        for s,v in o['states'].items():states[s,day]=v
    for target,p in plans.items():
        if target in observations:signals[p['origin_session']]=p['weights']
    initial=meta.get('initial_equity',INITIAL)
    r=simulate(pd.DatetimeIndex(dates),bars,signals,states,meta['config'],meta.get('initial_cash',INITIAL),meta.get('initial_positions',{}),meta.get('initial_marks',{}),priorities={p['origin_session']:p.get('risk_priorities',{}) for p in plans.values()},edges={p['origin_session']:p.get('edges',{}) for p in plans.values()})
    r['ledger']=[x for x in r['ledger'] if x['date']>=meta['start_session']]
    previous=initial;peak=initial
    for row in r['ledger']:
        peak=max(peak,row['equity']);row.update(daily_pnl=row['equity']-previous,daily_return=row['equity']/previous-1,cumulative_pnl=row['equity']-initial,cumulative_return=row['equity']/initial-1,drawdown=1-row['equity']/peak,mode='PAPER_ONLY')
        previous=row['equity']
    return r


def buy_edge(edge,quantity,open_price,buy_price,c,symbol):
    """Unchanged forecast terminal estimate, adjusted for current gap, cents and actual fees."""
    if not edge or any(not isinstance(edge.get(k),(int,float)) or not np.isfinite(edge[k]) for k in ('reference_price','predicted_return','uncertainty')) or edge['reference_price']<=0 or edge['uncertainty']<0:
        return dict(allowed=False,reason='BUY_EDGE_INPUT_MISSING')
    terminal=edge['reference_price']*(1+edge['predicted_return']-edge['uncertainty'])
    sell_price=np.floor(terminal*(1-c['slippage_bps']/10000)*100)/100
    if quantity<=0 or sell_price<=0:return dict(allowed=False,reason='BUY_NET_EDGE_NONPOSITIVE')
    fee_cfg={**c,'slippage_bps':0.}
    entry=costs(quantity*buy_price,'BUY',fee_cfg,symbol)['total']
    exit_fee=costs(quantity*sell_price,'SELL',fee_cfg,symbol)['total']
    net=quantity*(sell_price-buy_price)-entry-exit_fee
    return dict(allowed=bool(net>0),reason='POSITIVE_NET_EDGE' if net>0 else 'BUY_NET_EDGE_BELOW_ACTUAL_COST',
        shares=int(quantity),buy_fee=float(entry),estimated_exit_fee=float(exit_fee),
        estimated_net_advantage_cny=float(net),estimated_exit_price=float(sell_price),
        open_price=float(open_price),actual_buy_price=float(buy_price),
        note='买入实际费用及按预测终值估计的退出费用；预测已扣参数不确定性，不能保证未来收益')
