"""Persistent, prospective PAPER account. No broker APIs or real-account writes."""
from pathlib import Path
import csv,hashlib,html,inspect,json,sqlite3
import pandas as pd
from .core import now,cutoff,load_config,write_json,atomic_text,read_snapshot
from .decision import DEFAULTS,load_fee_config,rule_for,costs,sell_quantity
from .execution_replay import simulate
from .next_session_plan import EXECUTION_ONLY

INITIAL=150000.
FEE_KEYS=('commission_bps','minimum_commission','transfer_bps','exchange_bps','regulatory_bps','sell_tax_bps','slippage_bps','commission_includes_regulatory','commission_includes_transfer','fee_market_overrides','fees_verified','fee_source')

def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()
def engine_version():return hashlib.sha256((inspect.getsource(simulate)+inspect.getsource(costs)+inspect.getsource(sell_quantity)+inspect.getsource(replay)).encode()).hexdigest()
def opened(date):return pd.Timestamp(date).tz_localize('Asia/Shanghai')+pd.Timedelta(hours=9,minutes=30)

def get(db,kind):
    out={}
    for key,body,sha in db.execute('SELECT key,body,sha256 FROM records WHERE kind=?',(kind,)):
        value=json.loads(body)
        if digest(value)!=sha:raise ValueError('PAPER_RECORD_TAMPERED:'+kind+':'+key)
        out[key]=value
    return out

def put(db,kind,key,value):
    old=db.execute('SELECT sha256 FROM records WHERE kind=? AND key=?',(kind,key)).fetchone()
    sha=digest(value)
    if old:
        if old[0]!=sha:raise ValueError('PAPER_RECORD_IMMUTABLE:'+kind+':'+key)
        return
    db.execute('INSERT INTO records VALUES(?,?,?,?)',(kind,key,json.dumps(value,ensure_ascii=False,allow_nan=False),sha))

def freeze_plan(source,at,cal,meta):
    target=source.get('plan_session');blocked=[];weights={}
    if not target or pd.Timestamp(target) not in cal:return None
    if pd.Timestamp(at)>=opened(target):return None  # Never backfill an already-open session.
    origins=cal[cal<pd.Timestamp(target)]
    if not len(origins) or source.get('source_forecast_asof')!=str(origins[-1].date()):blocked.append('SOURCE_SESSION_MISMATCH')
    try:
        if pd.Timestamp(source['generated_at'])>pd.Timestamp(at):blocked.append('FUTURE_SOURCE_PLAN')
    except (KeyError,ValueError,TypeError):blocked.append('PLAN_TIME_UNKNOWN')
    if not source.get('plan_calendar_verified'):blocked.append('NEXT_SESSION_UNVERIFIED')
    if not source.get('formal_method') or source.get('production_admission',{}).get('status')!='VALIDATED':blocked.append('FORMAL_METHOD_NOT_VALIDATED')
    ignored=EXECUTION_ONLY|{'ACCOUNT_STALE_OR_UNVERIFIED','ACCOUNT_ASSUMED_INITIAL_CASH','CURRENT_HOLDING_ABOVE_STOCK_CAP'}
    blocked.extend(x for x in source.get('blockers',[]) if x not in ignored)
    sectors={};rows=source.get('decisions',[]);c=meta['config']
    if not rows:blocked.append('EMPTY_SOURCE_PLAN')
    for r in rows:
        symbol=r['symbol'];w=r.get('target_weight');flags=[x for x in r.get('blockers',[]) if x not in ignored]
        if not r.get('probability_calibrated'):flags.append('PROBABILITY_NOT_INDEPENDENTLY_VALIDATED')
        if r.get('target_kind')!='RESEARCH_TARGET':flags.append('TARGET_NOT_VALIDATED')
        if not rule_for(symbol):flags.append('UNSUPPORTED_SECURITY')
        if not isinstance(w,(int,float)) or not 0<=w<=c['max_stock']:flags.append('INVALID_WEIGHT')
        if flags:blocked.extend(symbol+':'+f for f in flags)
        else:weights[symbol]=float(w);sectors[r['sector']]=sectors.get(r['sector'],0)+w
    if len({r['symbol'] for r in rows})!=len(rows):blocked.append('DUPLICATE_SYMBOL')
    if sum(weights.values())>c['max_invested']+1e-9 or any(w>c['max_sector']+1e-9 for w in sectors.values()):blocked.append('PORTFOLIO_CAP_EXCEEDED')
    if not c.get('fees_verified'):blocked.append('FEES_UNVERIFIED')
    return dict(target_session=target,origin_session=str(origins[-1].date()) if len(origins) else None,
        registered_at=pd.Timestamp(at).isoformat(),source_generated_at=source.get('generated_at'),source_sha256=digest(source),
        weights={} if blocked else weights,status='HOLD_NO_QUALIFIED_PLAN' if blocked else 'FROZEN_CONDITIONAL_TARGETS',
        blockers=sorted(set(blocked)),fee_config_sha256=digest({k:c.get(k) for k in FEE_KEYS}),mode='PAPER_ONLY')

def evidence(root,day,symbols):
    if not symbols:return dict(session=day,bars={},states={},proof={'reason':'CASH_ONLY_NO_MARKET_VALUATION_NEEDED'})
    from .history_sources import execution_states
    states,proof,errors=execution_states(root,sorted(symbols),pd.DatetimeIndex([day]))
    if errors:raise ValueError('PAPER_EVIDENCE_MISSING:'+','.join(errors))
    bars={};normalized={}
    for symbol in symbols:
        state=states[symbol,day];snap=read_snapshot(root,symbol)
        if snap is None:raise ValueError('PAPER_BAR_MISSING:'+symbol)
        if snap['meta'].get('synthetic') or snap['meta'].get('source')!='tencent' or pd.Timestamp(snap['meta']['fetched_at'])>pd.Timestamp(now()):raise ValueError('PAPER_UNVERIFIED_OR_FUTURE_BAR:'+symbol)
        frame=snap['raw'].set_index('date')
        if pd.Timestamp(day) not in frame.index:raise ValueError('PAPER_CLOSE_MISSING:'+symbol)
        bar=frame.loc[pd.Timestamp(day)];values={k:float(bar[k]) for k in ('open','high','low','close','volume')}
        if any(not pd.notna(v) or abs(v)==float('inf') for v in values.values()) or min(values[k] for k in ('open','high','low','close'))<=0:raise ValueError('PAPER_INVALID_BAR')
        ref=state.get('reference_ohlc')
        if state['status']=='trading' and (not ref or any(abs(values[k]-ref[k])>.011 for k in ('open','high','low','close'))):raise ValueError('PAPER_PROVIDER_PRICE_CONFLICT:'+symbol)
        values.update(source=snap['meta']['source'],fetched_at=snap['meta']['fetched_at'],version=snap['meta']['version'])
        bars[symbol]=values;normalized[symbol]=state
    return dict(session=day,bars=bars,states=normalized,proof=proof)

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
    r=simulate(pd.DatetimeIndex(dates),bars,signals,states,meta['config'],meta.get('initial_cash',INITIAL),meta.get('initial_positions',{}),meta.get('initial_marks',{}))
    r['ledger']=[x for x in r['ledger'] if x['date']>=meta['start_session']]
    previous=initial;peak=initial
    for row in r['ledger']:
        peak=max(peak,row['equity']);row.update(daily_pnl=row['equity']-previous,daily_return=row['equity']/previous-1,cumulative_pnl=row['equity']-initial,cumulative_return=row['equity']/initial-1,drawdown=1-row['equity']/peak,mode='PAPER_ONLY')
        previous=row['equity']
    return r

def run(root):
    root=Path(root);at=now();end=cutoff(at)
    from .data import calendar
    cal,_=calendar(root,load_config(root),end,offline=True);cal=cal.sort_values();eligible=cal[cal<=end]
    if not len(eligible):raise ValueError('PAPER_CALENDAR_MISSING')
    asof=str(eligible[-1].date());path=root/'data/paper_account.sqlite3';path.parent.mkdir(exist_ok=True)
    c={**DEFAULTS,**json.loads((root/'decision_config.json').read_text())};load_fee_config(root,c)
    c.update(max_volume_participation=.01,require_fill_in_range=True)
    alerts=[]
    with sqlite3.connect(path) as db:
        path.chmod(0o600)
        db.execute('CREATE TABLE IF NOT EXISTS records(kind TEXT,key TEXT,body TEXT,sha256 TEXT,PRIMARY KEY(kind,key))');db.commit();db.execute('BEGIN IMMEDIATE')
        metas=get(db,'meta')
        if not metas:
            future=[d for d in cal if opened(str(d.date()))>pd.Timestamp(at)]
            if not future:raise ValueError('PAPER_FUTURE_CALENDAR_MISSING')
            start=future[0];prior=cal[cal<start]
            if not len(prior):raise ValueError('PAPER_ANCHOR_MISSING')
            meta=dict(start_session=str(start.date()),anchor_session=str(prior[-1].date()),created_at=at.isoformat(),initial_cash=INITIAL,initial_positions={},config=c,engine_version=engine_version(),mode='PAPER_ONLY')
            seed_path=root/'paper_account_seed.json'
            if seed_path.exists():
                seed=json.loads(seed_path.read_text());positions=seed['positions'];cash=seed['cash']
                if not seed.get('source') or not isinstance(cash,(int,float)) or cash<0:raise ValueError('INVALID_PAPER_SEED')
                for symbol,p in positions.items():
                    if not rule_for(symbol) or type(p['shares']) is not int or p['shares']<0 or not 0<=p['sellable']<=p['shares'] or not 0<p['reference_price']<float('inf'):raise ValueError('INVALID_PAPER_SEED')
                marks={s:p['reference_price'] for s,p in positions.items()}
                meta.update(initial_cash=cash,initial_positions={s:dict(shares=p['shares'],sellable=p['sellable']) for s,p in positions.items()},initial_marks=marks,initial_equity=cash+sum(p['shares']*marks[s] for s,p in positions.items()),seed=seed,seed_sha256=digest(seed))
            else:meta['initial_equity']=INITIAL
            put(db,'meta','account',meta)
        else:meta=metas['account']
        if meta['engine_version']!=engine_version():raise ValueError('PAPER_ENGINE_CHANGED_REQUIRES_REVIEW')
        fee_changed=any(c.get(k)!=meta['config'].get(k) for k in FEE_KEYS)
        if fee_changed:alerts.append('FEE_CONFIG_CHANGED_REQUIRES_REVIEW')
        plans=get(db,'plans');observations=get(db,'observations')
        for day in cal[(cal>=pd.Timestamp(meta['start_session']))&(cal<=pd.Timestamp(asof))]:
            date=str(day.date())
            if date in observations:continue
            if fee_changed:break
            # Preserve marks and event evidence for every security ever targeted.
            symbols=set(meta['initial_positions'])|{s for target,p in plans.items() if target<=date for s,w in p['weights'].items() if w>0}
            if date in plans:symbols.update(plans[date]['weights'])
            try:o=evidence(root,date,symbols)
            except (ValueError,KeyError,OSError) as exc:alerts.append(str(exc));break
            if date not in plans:alerts.append('NO_PREOPEN_PLAN:'+date)
            put(db,'observations',date,o);observations[date]=o
        source=root/'reports/decisions/latest/decision.json'
        if source.exists() and not fee_changed:
            candidate=freeze_plan(json.loads(source.read_text()),at,cal,meta)
            if candidate and candidate['target_session']>=meta['start_session'] and candidate['target_session'] not in plans:
                put(db,'plans',candidate['target_session'],candidate);plans[candidate['target_session']]=candidate
        result=replay(meta,cal,plans,observations)
        # Recomputed NAV must exactly reproduce previously finalized records.
        for row in result['ledger']:put(db,'nav',row['date'],row)
        for i,fill in enumerate(result['fills']):put(db,'fills',str(i),fill)
        db.commit()
    last=result['ledger'][-1] if result['ledger'] else None
    current=last and last['date']==asof
    report=dict(mode='PAPER_ONLY',generated_at=at.isoformat(),market_asof=asof,start_session=meta['start_session'],
        status='BLOCKED_DATA' if alerts and not current and asof>=meta['start_session'] else 'INITIALIZED_WAITING_FIRST_CLOSE' if not last else 'SETTLED',
        initial_cash=meta['initial_cash'],initial_equity=meta['initial_equity'],initial_seed=meta.get('seed'),cash=last['cash'] if last else meta['initial_cash'],positions=last['positions'] if last else meta['initial_positions'],
        last_valuation_date=last['date'] if last else None,equity=last['equity'] if last else meta['initial_equity'],
        daily_return=last['daily_return'] if current else None,cumulative_return=last['cumulative_return'] if last else 0.,
        max_drawdown=result['summary']['max_drawdown'],ledger=result['ledger'],fills=result['fills'],blocked_orders=result['blocked'],
        plans=plans,alerts=alerts,config=meta['config'],submitted_orders=0,
        limitations=['独立模拟账户：以用户授权的起始快照为基准，后续只模拟记账，不同步或操作真实账户。','计划必须在目标交易日开盘前冻结；不回填错过的交易，不修改已结算净值。','按日线开盘价格加不利滑点模拟，价格须在当日高低区间及涨跌停内，数量不超过日成交量1%；不能证明开盘时能实际成交。','未知停牌、公司行为或估值数据时暂停结算并标注旧估值，不虚构收益。','未通过正式策略/预测验证时保留原模拟持仓与现金；模拟结果不授予正式模型资格。'])
    publish(root,report);return report

def publish(root,r):
    folder=Path(root)/'reports/paper_account/latest';folder.mkdir(parents=True,exist_ok=True)
    write_json(folder/'report.json',r)
    for name,rows,columns in [('每日及累计模拟收益.csv',r['ledger'],['date','cash','equity','daily_pnl','daily_return','cumulative_pnl','cumulative_return','drawdown','fees']),('模拟成交.csv',r['fills'],['date','symbol','side','shares','price','fee','cash_after','signal_date'])]:
        with (folder/name).open('w',encoding='utf-8-sig',newline='') as f:
            w=csv.DictWriter(f,fieldnames=columns,extrasaction='ignore');w.writeheader();w.writerows(rows)
    e=html.escape;pct=lambda v:'尚未结算' if v is None else f'{v:.2%}'
    rows=''.join('<tr>'+''.join('<td>'+e(str(v))+'</td>' for v in [x['date'],f"{x['equity']:,.2f}",f"{x['daily_pnl']:,.2f}",pct(x['daily_return']),pct(x['cumulative_return']),pct(x['drawdown']),f"{x['fees']:,.2f}"])+'</tr>' for x in reversed(r['ledger']))
    future=''.join('<li>'+e(day)+'：'+('无合格计划，保留模拟持仓与现金' if not p['weights'] else '已冻结条件性目标，待真实行情结算')+'；阻止原因 '+e('、'.join(p['blockers'][:6]))+'</li>' for day,p in sorted(r['plans'].items(),reverse=True)[:5])
    chart=''
    if len(r['ledger'])>=2:
        values=[r['initial_equity']]+[x['equity'] for x in r['ledger']];lo=min(values);span=max(values)-lo or 1
        points=' '.join(f'{20+i*760/(len(values)-1):.1f},{180-(v-lo)/span*150:.1f}' for i,v in enumerate(values))
        chart='<h2>模拟净值轨迹</h2><svg viewBox="0 0 800 210" role="img" aria-label="模拟权益走势"><polyline points="'+points+'" fill="none" stroke="#087f8c" stroke-width="2"/></svg><p>仅显示已结算记录；不是实际账户收益。</p>'
    page='<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>持续模拟账户</title><style>body{font:15px/1.5 system-ui;margin:20px;color:#17253a}td,th{padding:6px 10px;border-bottom:1px solid #ddd;text-align:left}table{border-collapse:collapse}svg{max-width:850px;width:100%}</style><h1>持续模拟账户 · 不使用真实资金</h1>'
    page+=f'<p>开始交易日：{e(r["start_session"])}；状态：{e(r["status"])}；估值日期：{e(str(r["last_valuation_date"] or "初始资金，尚未收盘结算"))}</p><p>模拟权益：{r["equity"]:,.2f}元；现金：{r["cash"]:,.2f}元；日收益：{pct(r["daily_return"])}；累计收益：{pct(r["cumulative_return"])}；最大回撤：{pct(r["max_drawdown"])}；模拟成交：{len(r["fills"])}笔。</p>'
    page+='<ul>'+''.join('<li>'+e(x)+'</li>' for x in r['limitations'])+'</ul><p>'+e('；'.join(r['alerts']))+'</p>'+chart+'<h2>已冻结的计划</h2><ul>'+future+'</ul><h2>模拟收益单</h2><p><a href="每日及累计模拟收益.csv">收益CSV</a> · <a href="模拟成交.csv">成交CSV</a> · <a href="report.json">完整来源与记录</a></p><table><tr><th>日期</th><th>权益</th><th>日盈亏</th><th>日收益率</th><th>累计收益率</th><th>回撤</th><th>费用</th></tr>'+rows+'</table></html>'
    atomic_text(folder/'index.html',page)
