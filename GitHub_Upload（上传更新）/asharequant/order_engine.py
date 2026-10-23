"""Chronological paper OMS. No broker transport exists in this module."""
import copy
import math
import pandas as pd
from .decision import costs, sell_quantity, rule_for
from .active_execution import buy_edge
from .intraday import transition
from .paper_account import digest

ACTIVE = {'ACCEPTED', 'PARTIALLY_FILLED'}

class LiveBrokerUnavailable:
    """Fail-closed boundary: paper fills can never be submitted as broker receipts."""
    def submit(self, *args, **kwargs):
        raise RuntimeError('LIVE_BROKER_NOT_CONNECTED_OR_AUTHORIZED')
    cancel = submit
    sync = submit


def seed(state, at):
    s = copy.deepcopy(state)
    s.update(orders={}, order_events=[], executions=[], alerts=[], quote_seen={}, cooldown={},
             realized_pnl=0., total_fees=0., total_turnover=0., max_drawdown=0.,
             segment_initial_equity=s['equity'], opening_basis={}, basis_kind='MIGRATION_MARK_NOT_BROKER_COST',
             execution_started_at=at.isoformat(), last_cycle_at=None)
    for k, p in s['positions'].items():
        # Existing account has no broker-verified acquisition cost. Do not invent it.
        p['basis'] = p['shares'] * s['marks'][k]
        s['opening_basis'][k] = p['basis']
    return s


def event(s, order, status, at, reason):
    order['status'] = status
    record = dict(order_id=order['id'], at=at.isoformat(), status=status, reason=reason,
                  symbol=order['symbol'], side=order['side'], shares=order['quantity'],
                  filled=order['filled'], rationale=order.get('rationale',[]), mode='PAPER_ONLY')
    s['order_events'].append(record)
    if status in ('ACCEPTED', 'PARTIALLY_FILLED', 'FILLED', 'REJECTED', 'CANCELED'):
        s['alerts'].append(dict(id=digest(record), **record, limit_price=order['limit_price']))


def cancel_all(s, at, reason):
    for o in s['orders'].values():
        if o['status'] in ACTIVE:
            event(s, o, 'CANCELED', at, reason)


def quote_guards(s, quotes, checks, at, cfg):
    checks = copy.deepcopy(checks)
    for k, q in quotes.items():
        flags = checks.setdefault(k, [])
        try:
            t = pd.Timestamp(q['asof'])
            if t.tzinfo is None or not 0 <= (at-t).total_seconds() <= cfg['quote_max_age_seconds'] or t.date() != at.date():
                flags.append('STALE_OR_FUTURE_QUOTE')
            fields = ('price','previous_close','bid','ask','bid_size_lots','ask_size_lots','limit_down','limit_up')
            if not all(isinstance(q.get(f),(int,float)) and math.isfinite(q[f]) and q[f] > 0 for f in fields):
                flags.append('INVALID_QUOTE')
            elif not q['limit_down'] < q['price'] < q['limit_up'] or q['bid'] > q['ask']:
                flags.append('LOCKED_LIMIT_OR_INVALID_BOOK')
            old = s['quote_seen'].get(k)
            if old and old['day'] == str(at.date()):
                if t <= pd.Timestamp(old['asof']): flags.append('DUPLICATE_OR_REORDERED_QUOTE')
                if abs(q['price']/old['price']-1) > cfg['maximum_quote_jump']: flags.append('ABNORMAL_PRICE_JUMP')
                if q.get('volume_shares') is not None and old.get('volume_shares') is not None and q['volume_shares'] < old['volume_shares']:
                    flags.append('CUMULATIVE_VOLUME_REGRESSION')
            if not flags:
                s['quote_seen'][k] = {f:q.get(f) for f in ('asof','price','volume_shares')} | {'day':str(at.date())}
        except (KeyError, ValueError, TypeError): flags.append('QUOTE_PARSE_FAILED')
    return checks


def value(s, quotes, checks, at):
    valid = {k:q for k,q in quotes.items() if not checks.get(k)}
    missing = [k for k,p in s['positions'].items() if p['shares'] and k not in valid]
    if missing:
        s['valuation_status'] = 'STALE_OR_UNVERIFIED'
        return False
    for k,q in valid.items(): s['marks'][k] = q['price']
    s['equity'] = s['cash'] + sum(p['shares']*s['marks'][k] for k,p in s['positions'].items())
    s['peak'] = max(s['peak'], s['equity'])
    s['max_drawdown'] = max(s['max_drawdown'], 1-s['equity']/s['peak'])
    s['valuation_at'] = at.isoformat()
    s['valuation_status'] = 'OBSERVED_QUOTE_PAPER_ESTIMATE'
    return True


def fee(total, side, c, symbol):
    return costs(total, side, {**c,'slippage_bps':0.}, symbol)['total']


def fill_order(s, o, q, plan, c, cfg, at, sectors):
    """Recheck risk at subsequent quote, then post partial fill atomically with caller."""
    if pd.Timestamp(q['asof']) <= pd.Timestamp(o['created_at']) or (at-pd.Timestamp(o['created_at'])).total_seconds() < cfg['minimum_latency_seconds']:
        return
    k=o['symbol'];side=o['side'];p=s['positions'].setdefault(k,dict(shares=0,sellable=0,basis=0.))
    remaining=o['quantity']-o['filled'];equity=s['equity'];budget=min(equity,s['previous_close_equity'])
    stop=equity<=cfg['minimum_equity'] or equity/s['peak']-1<=-cfg['maximum_drawdown']
    weight=0. if stop else plan['weights'].get(k,0.)
    desired=int(weight*equity/q['price'])//100*100
    delta=desired-p['shares']
    if (side=='BUY' and delta<=0) or (side=='SELL' and delta>=0):
        event(s,o,'CANCELED',at,'SIGNAL_REVERSED_OR_TARGET_REACHED');return
    book=q['ask'] if side=='BUY' else q['bid']
    price=book*(1+c['slippage_bps']/10000*(1 if side=='BUY' else -1))
    price=(math.ceil(price*100) if side=='BUY' else math.floor(price*100))/100
    if not q['limit_down']<price<q['limit_up']:return
    if side=='BUY' and price>o['limit_price'] or side=='SELL' and price<o['limit_price']:return
    book_shares=q['ask_size_lots' if side=='BUY' else 'bid_size_lots']*100
    if q.get('volume_shares') is None or o.get('last_volume') is None:return
    traded=max(0,q['volume_shares']-o['last_volume'])
    quantity=min(remaining,abs(delta),int(traded*cfg['book_participation'])//100*100,int(book_shares*cfg['book_participation'])//100*100,
                 int(max(0,c['max_turnover']*budget-s['turnover'])/price)//100*100)
    quantity=quantity//100*100 if side=='BUY' else sell_quantity(quantity,p['sellable'],p['shares'])
    cap=min(c['max_invested'],{'risk_off':.45,'neutral':.675,'risk_on':.9}.get(plan.get('environment'),0.))
    other_buy=sum((x['quantity']-x['filled'])*x['limit_price']+fee((x['quantity']-x['filled'])*x['limit_price'],'BUY',c,x['symbol']) for x in s['orders'].values() if x['id']!=o['id'] and x['status'] in ACTIVE and x['side']=='BUY')
    while quantity:
        n=quantity*price;charge=fee(o['notional']+n,side,c,k)-o['fees']
        stock_value=sum(v['shares']*s['marks'][sym] for sym,v in s['positions'].items())
        sector_value=sum(v['shares']*s['marks'][sym] for sym,v in s['positions'].items() if sectors.get(sym)==sectors.get(k))
        okay=side=='SELL' or (not stop and n+charge+other_buy<=s['cash'] and stock_value+n<=equity*cap and (p['shares']+quantity)*price<=equity*c['max_stock'] and sector_value+n<=equity*c['max_sector'])
        if okay and s['fees']+charge<=budget*c['max_cost_fraction']:break
        quantity=max(0,quantity-100)
        if side=='SELL':quantity=sell_quantity(quantity,p['sellable'],p['shares'])
    if not quantity:return
    if side=='BUY' and not buy_edge(plan.get('edges',{}).get(k),quantity,q['price'],price,c,k)['allowed']:
        event(s,o,'CANCELED',at,'NET_EDGE_NO_LONGER_COVERS_COST');return
    n=quantity*price;charge=fee(o['notional']+n,side,c,k)-o['fees']
    realized=0.
    if side=='BUY':
        s['cash']-=n+charge;p['basis']+=n+charge;p['shares']+=quantity
    else:
        removed=p['basis']*quantity/p['shares'];realized=n-charge-removed
        s['cash']+=n-charge;p['basis']-=removed;p['shares']-=quantity;p['sellable']-=quantity
    o['filled']+=quantity;o['notional']+=n;o['fees']+=charge;o['last_volume']=q['volume_shares']
    s['realized_pnl']+=realized;s['fees']+=charge;s['total_fees']+=charge;s['turnover']+=n;s['total_turnover']+=n
    f=dict(id=digest([o['id'],o['filled']]),order_id=o['id'],at=at.isoformat(),quote_time=q['asof'],source=q['source'],symbol=k,name=q['name'],side=side,shares=quantity,price=price,fee=charge,realized_pnl=realized,cash_after=s['cash'],mode='PAPER_ONLY',assumption='后续报价的一档量最多1%，含不利滑点；不证明真实排队成交')
    s['executions'].append(f);s['alerts'].append(dict(status='PAPER_FILL',**f))
    event(s,o,'FILLED' if o['filled']==o['quantity'] else 'PARTIALLY_FILLED',at,'LATER_OBSERVED_QUOTE')
    if s['cash'] < -1e-8 or not 0<=p['sellable']<=p['shares']:raise AssertionError('LEDGER_INVARIANT')


def step(state, plan, quotes, checks, c, cfg, at, sectors, continuous=True):
    s=copy.deepcopy(state)
    if s['last_cycle_at'] and at<=pd.Timestamp(s['last_cycle_at']):return s,{'status':'DUPLICATE_OR_REORDERED_CYCLE'}
    s['last_cycle_at']=at.isoformat();day=str(at.date())
    if s['day']!=day:
        cancel_all(s,at,'SESSION_CHANGED')
        if s['closed_day']!=s['day']:return s,{'status':'PREVIOUS_SESSION_UNSETTLED'}
        for p in s['positions'].values():p['sellable']=p['shares']
        s.update(day=day,turnover=0.,fees=0.,used_quotes={})
    checks=quote_guards(s,quotes,checks,at,cfg)
    if not continuous:
        cancel_all(s,at,'OUTSIDE_SUPPORTED_CONTINUOUS_SESSION');return s,{'status':'OBSERVE_ONLY','checks':checks}
    if not value(s,quotes,checks,at):
        cancel_all(s,at,'HELD_QUOTE_UNVERIFIED');return s,{'status':'STALE_VALUATION','checks':checks}
    for o in list(s['orders'].values()):
        if o['status'] not in ACTIVE:continue
        k=o['symbol']
        if (at-pd.Timestamp(o['created_at'])).total_seconds()>=cfg['order_ttl_seconds']:
            event(s,o,'CANCELED',at,'EXPIRED_REASSESS_WITH_NEW_DECISION');continue
        if k not in quotes or checks.get(k):
            event(s,o,'CANCELED',at,'QUOTE_UNVERIFIED');continue
        fill_order(s,o,quotes[k],plan,c,cfg,at,sectors)
        value(s,quotes,checks,at)
    # Existing decision/risk logic is reused only to propose, never to post a fill.
    _,proposals,conditions,blocks=transition(s,plan,quotes,checks,c,cfg,at,sectors)
    reserved=sum((o['quantity']-o['filled'])*o['limit_price']+fee((o['quantity']-o['filled'])*o['limit_price'],'BUY',c,o['symbol']) for o in s['orders'].values() if o['status'] in ACTIVE and o['side']=='BUY')
    for f in proposals:
        k=f['symbol']
        if any(o['symbol']==k and o['status'] in ACTIVE for o in s['orders'].values()):continue
        if k in s['cooldown'] and (at-pd.Timestamp(s['cooldown'][k])).total_seconds()<cfg['decision_cooldown_seconds']:continue
        if not rule_for(k):continue
        o=dict(id=digest([at.isoformat(),k,f['side'],f['shares']]),symbol=k,name=f['name'],side=f['side'],quantity=f['shares'],filled=0,notional=0.,fees=0.,limit_price=f['price'],created_at=at.isoformat(),signal_sha=digest(plan),status='CREATED',last_volume=quotes[k].get('volume_shares'),rationale=next((r.get('reasons',[]) for r in plan.get('rows',[]) if r['symbol']==k),['独立仓位/回撤风控']),quote_time=quotes[k]['asof'],quote_source=quotes[k]['source'])
        s['orders'][o['id']]=o;s['cooldown'][k]=at.isoformat()
        required=f['shares']*f['price']+fee(f['shares']*f['price'],'BUY',c,k)
        if f['side']=='BUY' and required+reserved>s['cash']:
            event(s,o,'REJECTED',at,'AVAILABLE_CASH_EXCLUDES_UNFILLED_SELLS');continue
        if f['side']=='SELL' and f['shares']>s['positions'][k]['sellable']:
            event(s,o,'REJECTED',at,'T_PLUS_ONE');continue
        if f['side']=='BUY':reserved+=required
        event(s,o,'ACCEPTED',at,'CONDITIONAL_PAPER_LIMIT_ORDER_WAIT_LATER_QUOTE')
    value(s,quotes,checks,at)
    s['unrealized_pnl']=sum(p['shares']*s['marks'][k]-p['basis'] for k,p in s['positions'].items())
    if abs(s['equity']-s['segment_initial_equity']-s['realized_pnl']-s['unrealized_pnl'])>.02:raise AssertionError('PNL_RECONCILIATION')
    return s,dict(status='RUNNING_PAPER_ONLY',checks=checks,conditions=conditions,blocks=blocks)


def risk_only_plan(state, quotes, c, environment=None):
    """Independent concentration/stop reduction; no new exposure or predicted advantage."""
    equity=state['cash']+sum(p['shares']*quotes.get(k,{}).get('price',state['marks'].get(k,0)) for k,p in state['positions'].items())
    weights={k:min(c['max_stock'],p['shares']*quotes.get(k,{}).get('price',state['marks'].get(k,0))/equity) for k,p in state['positions'].items()}
    cap=min(c['max_invested'],{'risk_off':.45,'neutral':.675,'risk_on':.9}.get(environment,c['max_invested']))
    total=sum(weights.values())
    if total>cap:weights={k:w*cap/total for k,w in weights.items()}
    return dict(environment=environment if environment in ('risk_off','neutral','risk_on') else 'risk_on',weights=weights,edges={},risk_priorities={k:max(0,p['shares']*state['marks'].get(k,0)/equity-c['max_stock']) for k,p in state['positions'].items()},rows=[],mode='RISK_ONLY_NO_NEW_BUYS')
