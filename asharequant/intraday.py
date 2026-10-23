"""30-second, read-only vendor monitoring and a separate conditional PAPER ledger.
No broker gateway, credential transport or real order submission exists here.
"""
from pathlib import Path
import copy,html,json,math,sqlite3,subprocess,sys,hashlib
import pandas as pd
from .core import now,write_json,atomic_text,run_lock,read_watchlist
from .paper_account import digest,get,put,evidence,opened
from .decision import costs,sell_quantity,rule_for
from .active_execution import buy_edge
from .intraday_data import days,collect,quality,refresh_reference

def enabled(root):
    p=Path(root)/'intraday_config.json'
    return p.exists() and json.loads(p.read_text()).get('enabled') is True

def session(at,cal):
    at=pd.Timestamp(at).tz_convert('Asia/Shanghai');hm=(at.hour,at.minute,at.second)
    if pd.Timestamp(str(at.date())) not in cal:return 'CLOSED'
    if (9,30,0)<=hm<(11,30,0) or (13,0,0)<=hm<(14,57,0):return 'CONTINUOUS'
    if (9,15,0)<=hm<(9,30,0) or (14,57,0)<=hm<(15,5,0):return 'OBSERVE_ONLY'
    return 'CLOSED'

def configuration(root):
    c=json.loads((Path(root)/'intraday_config.json').read_text())
    if c['real_orders_enabled'] or c['refresh_seconds']!=30 or not 0<c['book_participation']<=.01 or c['maximum_drawdown']>.15 or c['minimum_equity']<120000:raise ValueError('INTRADAY_CONFIG_UNAUTHORIZED')
    return c

def db_open(root):
    path=Path(root)/'data/intraday/account.sqlite3';path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path);path.chmod(0o600)
    db.execute('CREATE TABLE IF NOT EXISTS records(kind TEXT,key TEXT,body TEXT,sha256 TEXT,PRIMARY KEY(kind,key))')
    db.execute('CREATE TABLE IF NOT EXISTS current_state(id INTEGER PRIMARY KEY,body TEXT NOT NULL)');db.commit();return db

def save_state(db,s):db.execute('INSERT OR REPLACE INTO current_state VALUES(1,?)',(json.dumps(s,ensure_ascii=False,allow_nan=False),))
def load_state(db):
    row=db.execute('SELECT body FROM current_state WHERE id=1').fetchone();return json.loads(row[0]) if row else None

def initialize(db,source,cfg):
    code={n:hashlib.sha256((Path(__file__).parent/n).read_bytes()).hexdigest() for n in ('intraday.py','intraday_data.py','active_execution.py','decision.py')}
    meta=get(db,'meta').get('account')
    if meta is None:
        if source['status']!='ACTIVE_RESEARCH_PAPER' or source['valuation_date']!=source['asof']:raise ValueError('CURRENT_VALUED_ANCHOR_REQUIRED')
        meta=dict(code=code,universe={r['symbol']:r['name'] for r in source['predictions']['forecasts']},anchor=source['asof'],start_session=source['plan']['target_session'],initial_equity=source['equity'],original_initial_equity=source['meta']['original_initial_equity'],config=source['meta']['config'],monitor_config=cfg,created_at=now().isoformat(),mode='INTRADAY_PAPER_ONLY')
        s=dict(cash=source['cash'],positions=copy.deepcopy(source['positions']),marks={r['symbol']:r['close'] for r in source['predictions']['forecasts']},equity=source['equity'],previous_close_equity=source['equity'],peak=source['equity'],day=source['asof'],closed_day=source['asof'],turnover=0.,fees=0.,used_quotes={},conditions={},notification_queue=[],fills_count=0,valuation_at=source['valuation_date'],valuation_status='ANCHOR_SNAPSHOT')
        if abs(s['cash']+sum(p['shares']*s['marks'][k] for k,p in s['positions'].items())-s['equity'])>.02:raise ValueError('ANCHOR_RECONCILIATION_FAILED')
        put(db,'meta','account',meta);save_state(db,s)
    if meta.get('code')!=code or meta['config']!=source['meta']['config'] or meta['monitor_config']!=cfg:raise ValueError('FROZEN_PAPER_CONFIG_CHANGED_REVIEW_REQUIRED')
    return meta,load_state(db)

def target_shares(weight, equity, price):
    """Floor to board lots, correcting only round-trip floating point noise.

    Four ULPs cover the weight/value/price arithmetic, not a trading tolerance.
    Meaningfully below-boundary targets still round down; risk checks remain intact.
    """
    shares = weight * equity / price
    boundary = round(shares / 100) * 100
    if abs(shares - boundary) <= 4 * math.ulp(shares):
        shares = float(boundary)
    return int(shares) // 100 * 100


def transition(state,plan,quotes,checks,c,settings,at,sectors):
    """Pure deterministic transition; called in one transaction with quote audit evidence."""
    s=copy.deepcopy(state);fills=[];conditions={};date=str(at.date())
    if s['day']!=date:
        if s['closed_day']!=s['day']:return s,[],{'ACCOUNT_UNSETTLED':'上一交易日未结算，暂停模拟成交'},{}
        for p in s['positions'].values():p['sellable']=p['shares']
        s.update(day=date,turnover=0.,fees=0.,used_quotes={},conditions={})
    valid={k:q for k,q in quotes.items() if not checks.get(k)}
    badheld=[k for k,p in s['positions'].items() if p['shares'] and k not in valid]
    if badheld:
        s['valuation_status']='STALE_OR_UNVERIFIED';return s,[],{'ACCOUNT_DATA':'持仓行情或公司行为未核实：'+','.join(badheld)},{}
    for k,q in valid.items():s['marks'][k]=q['price']
    equity=s['cash']+sum(p['shares']*s['marks'][k] for k,p in s['positions'].items());s.update(equity=equity,valuation_at=at.isoformat(),valuation_status='CONDITIONAL_VENDOR_MARK')
    s['peak']=max(s['peak'],equity);stop=equity<=settings['minimum_equity'] or equity/s['peak']-1<=-settings['maximum_drawdown']
    if stop:conditions['PORTFOLIO_STOP']='触发组合风险退出线，模拟减仓仍受成交条件限制'
    for k,q in valid.items():
        move=q['price']/q['previous_close']-1
        if abs(move)>=settings['move_alert_fraction']:conditions['MOVE:'+k]=q['name']+f'较昨收{move:+.2%}'
        if min(abs(q['price']/q['limit_up']-1),abs(q['price']/q['limit_down']-1))<=settings['limit_alert_fraction']:conditions['LIMIT:'+k]=q['name']+'接近涨跌停，注意成交风险'
    budget=min(equity,s['previous_close_equity']);caps={'risk_off':.45,'neutral':.675,'risk_on':.9}
    if plan.get('environment') not in caps:return s,[],conditions|{'REGIME_UNKNOWN':'市场预算未核实，暂停模拟成交'},{}
    target=plan['weights'];priorities=plan.get('risk_priorities',{});blocks={}
    ordered=sorted(set(target)|set(s['positions']),key=lambda k: (0 if stop or target.get(k,0)*equity<s['positions'].get(k,{}).get('shares',0)*s['marks'].get(k,0) else 1,-(priorities.get(k,0)+max(0,s['positions'].get(k,{}).get('shares',0)*s['marks'].get(k,0)/equity/c['max_stock']-1)),k))
    for k in ordered:
        if k not in valid:blocks[k]=checks.get(k,['QUOTE_MISSING']);continue
        q=valid[k];p=s['positions'].setdefault(k,{'shares':0,'sellable':0});weight=0. if stop else target.get(k,0.)
        desired=target_shares(weight,equity,q['price']);delta=desired-p['shares']
        if delta==0:continue
        if s['used_quotes'].get(k)==q['asof']:continue
        side='BUY' if delta>0 else 'SELL'
        book=q['ask'] if side=='BUY' else q['bid'];book_size=q['ask_size_lots'] if side=='BUY' else q['bid_size_lots']
        price=book*(1+c['slippage_bps']/10000*(1 if side=='BUY' else -1));price=(math.ceil(price*100) if side=='BUY' else math.floor(price*100))/100
        if not q['limit_down']<price<q['limit_up']:blocks[k]=['SLIPPAGE_CROSSES_LIMIT'];continue
        qty=min(abs(delta),int(book_size*100*settings['book_participation'])//100*100,int(max(0,c['max_turnover']*budget-s['turnover'])/price)//100*100)
        qty=qty//100*100 if side=='BUY' else sell_quantity(qty,p['sellable'],p['shares'])
        while qty>0:
            n=qty*price;fee=costs(n,side,{**c,'slippage_bps':0.},k)['total']
            stock_value=sum(x['shares']*s['marks'][symbol] for symbol,x in s['positions'].items())
            sector_value=sum(x['shares']*s['marks'][symbol] for symbol,x in s['positions'].items() if sectors.get(symbol)==sectors.get(k))
            buy_ok=side!='BUY' or (n+fee<=s['cash'] and stock_value+n<=equity*min(c['max_invested'],caps[plan['environment']]) and (p['shares']+qty)*price<=equity*c['max_stock'] and sector_value+n<=equity*c['max_sector'])
            if buy_ok and s['fees']+fee<=budget*c['max_cost_fraction']:break
            qty=max(0,qty-100)
            if side=='SELL':qty=sell_quantity(qty,p['sellable'],p['shares'])
        if qty<=0:blocks[k]=['CASH_POSITION_TURNOVER_COST_OR_BOOK_LIMIT'];continue
        n=qty*price;fee=costs(n,side,{**c,'slippage_bps':0.},k)['total']
        if n<c['minimum_trade_value']:blocks[k]=['MINIMUM_TICKET'];continue
        edge=buy_edge(plan.get('edges',{}).get(k),qty,q['price'],price,c,k) if side=='BUY' else None
        if edge and not edge['allowed']:blocks[k]=[edge['reason']];continue
        s['used_quotes'][k]=q['asof'];s['cash']+=(-n-fee) if side=='BUY' else n-fee;p['shares']+=qty if side=='BUY' else -qty
        if side=='SELL':p['sellable']-=qty
        # Bought shares cannot be sold until a subsequent exchange session.
        s['turnover']+=n;s['fees']+=fee;s['fills_count']+=1
        fills.append(dict(at=at.isoformat(),symbol=k,name=q['name'],side=side,shares=int(qty),price=price,fee=fee,cash_after=s['cash'],quote_time=q['asof'],source=q['source'],edge_check=edge,mode='CONDITIONAL_INTRADAY_PAPER',assumption='按一档报价加不利滑点、最多可见一档量1%模拟；不是盘口成交证明'))
        if s['cash']<0 or p['sellable']<0 or p['sellable']>p['shares']:raise AssertionError('PAPER_ACCOUNT_CONSTRAINT_FAILED')
    s['equity']=s['cash']+sum(p['shares']*s['marks'][k] for k,p in s['positions'].items())
    return s,fills,conditions,blocks

def queue_alerts(s,conditions,fills,at):
    old=s.get('conditions',{})
    for key,body in conditions.items():
        if key not in old:s['notification_queue'].append(dict(id=digest([key,at.isoformat()]),at=at.isoformat(),title='AshareQuant 盘中提醒',body=body,status='PENDING'))
    if fills:s['notification_queue'].append(dict(id=digest(fills),at=at.isoformat(),title='AshareQuant 模拟成交（非实盘）',body='；'.join(f"{f['name']} {'买' if f['side']=='BUY' else '卖'}{f['shares']}股" for f in fills),status='PENDING'))
    s['conditions']=conditions

def deliver(root):
    with db_open(root) as db:
        s=load_state(db)
        if s is None:return
        for alert in s['notification_queue']:
            if alert['status']!='PENDING':continue
            # Claim before sending: a crash may lose one notification, never duplicate a trade.
            alert['status']='ATTEMPTED';save_state(db,s);db.commit()
            try:
                proc=subprocess.run(['osascript','-e','display notification '+json.dumps(alert['body'],ensure_ascii=False)+' with title '+json.dumps(alert['title'],ensure_ascii=False)],capture_output=True,timeout=5)
                alert['status']='SUBMITTED_TO_MACOS' if proc.returncode==0 else 'DELIVERY_FAILED'
            except (OSError,subprocess.TimeoutExpired):alert['status']='DELIVERY_FAILED'
            put(db,'alerts',alert['id'],alert)
        save_state(db,s)

def run_once(root):
    root=Path(root)
    if not enabled(root):return {'status':'DISABLED'}
    cfg=configuration(root);at=now();cal=days(root);mode=session(at,cal)
    with run_lock(root/'logs/intraday'):
        source=json.loads((root/'reports/active_research/latest/report.json').read_text())
        with db_open(root) as db:
            db.execute('BEGIN IMMEDIATE');meta,s=initialize(db,source,cfg);db.commit()
        if mode=='CLOSED':return publish(root,meta,s,'MARKET_CLOSED',{},[],at)
        raw,second=collect(read_watchlist(root).symbol.tolist());at=now();mode=session(at,cal)
        rp=root/'data/intraday/reference'/(str(at.date())+'.json');ref=json.loads(rp.read_text()) if rp.exists() else {}
        checks={k:quality(raw.get(k),second.get(k),ref,k,at,cal,meta['anchor'],cfg) for k in read_watchlist(root).symbol.tolist()}
        with db_open(root) as db:
            db.execute('BEGIN IMMEDIATE');s=load_state(db);key=str(int(at.timestamp())//30)
            if db.execute("SELECT 1 FROM records WHERE kind='cycles' AND key=?",(key,)).fetchone():return {'status':'ALREADY_PROCESSED'}
            s['last_quotes']=raw
            before=digest(s);plan=source.get('plan') or {};sectors={r['symbol']:r['sector'] for r in plan.get('rows',[])};conditions={};blocks={};fills=[]
            if any(checks.values()):conditions['DATA_QUALITY']=f"{sum(bool(v) for v in checks.values())}只行情/交易证据不完整，相关模拟暂停"
            ready=plan.get('target_session')==str(at.date()) and pd.Timestamp(plan['registered_at'])<opened(str(at.date())) and source['status']=='ACTIVE_RESEARCH_PAPER'
            if mode=='CONTINUOUS' and ready and set(s['positions'])<=set(checks):
                s,fills,risk,blocks=transition(s,plan,raw,checks,meta['config'],cfg,at,sectors);conditions.update(risk)
            elif mode=='CONTINUOUS':conditions['NO_CURRENT_PLAN']='缺少当日开盘前冻结计划，暂停模拟成交'
            for symbol,flags in checks.items():
                if 'LIMIT_LOCK_OR_CONFLICT' in flags:conditions['LIMIT:'+symbol]=symbol+'触及涨跌停或限制价冲突，暂停模拟成交'
            queue_alerts(s,conditions,fills,at)
            put(db,'references',digest(ref),ref)
            put(db,'cycles',key,dict(at=at.isoformat(),before=before,after=digest(s),quotes=raw,secondary=second,reference_sha=digest(ref),checks=checks,plan_sha=digest(plan),fills=fills,blocks=blocks,mode=mode))
            for i,f in enumerate(fills):put(db,'fills',key+':'+str(i),f)
            save_state(db,s);db.commit()
        if cfg['notification_enabled']:deliver(root)
        return publish(root,meta,s,mode,checks,blocks,at)

def close_day(root,day):
    root=Path(root)
    if not enabled(root):return {'status':'DISABLED'}
    with run_lock(root/'logs/intraday'):
        with db_open(root) as db:
            db.execute('BEGIN IMMEDIATE');meta=get(db,'meta').get('account');s=load_state(db)
            if not meta:return {'status':'NOT_INITIALIZED'}
            if day<=s['closed_day']:return {'status':'ALREADY_SETTLED'}
            at=now()
            if day>str(at.date()) or (day==str(at.date()) and at.hour<15):raise ValueError('CLOSE_NOT_AVAILABLE_YET')
            if str(days(root)[days(root)<pd.Timestamp(day)][-1].date())!=s['closed_day']:raise ValueError('MISSING_PREVIOUS_SETTLEMENT')
            if s['day'] not in (day,s['closed_day']):raise ValueError('ACCOUNT_SESSION_CONFLICT')
            symbols=[k for k,p in s['positions'].items() if p['shares']];o=evidence(root,day,symbols)
            if any(v['corporate_action_status']!='none' for v in o['states'].values()):raise ValueError('CORPORATE_ACTION_RECONCILIATION_REQUIRED')
            for k,b in o['bars'].items():s['marks'][k]=b['close']
            equity=s['cash']+sum(p['shares']*s['marks'][k] for k,p in s['positions'].items());prior=s['previous_close_equity'];s['peak']=max(s['peak'],equity)
            nav=dict(date=day,equity=equity,cash=s['cash'],daily_pnl=equity-prior,daily_return=equity/prior-1,cumulative_pnl=equity-meta['initial_equity'],cumulative_return=equity/meta['initial_equity']-1,drawdown=1-equity/s['peak'],fees=s['fees'] if s['day']==day else 0.,turnover=s['turnover'] if s['day']==day else 0.,mode='INTRADAY_PAPER_ONLY')
            put(db,'nav',day,nav);s.update(equity=equity,previous_close_equity=equity,closed_day=day,day=day,valuation_at=day+'T15:00:00+08:00',valuation_status='VERIFIED_CLOSE')
            save_state(db,s);db.commit()
        return publish(root,meta,s,'SETTLED',{},[],now())

def publish(root,meta,s,status,checks,blocks,at):
    folder=Path(root)/'reports/intraday/latest';folder.mkdir(parents=True,exist_ok=True)
    with db_open(root) as db:nav=list(get(db,'nav').values());fills=list(get(db,'fills').values());alerts=list(get(db,'alerts').values())
    report=dict(status=status,checked_at=at.isoformat(),meta=meta,account=s,nav=nav,fills=fills,alerts=alerts[-100:],checks=checks,blocks=blocks,submitted_orders=0,
        note='独立盘中模拟主账本；旧日线模拟保留对照，两个账户收益不相加。公开行情与一档量假设不代表真实可成交。新买股T+1可卖，过期数据不冒充实时。')
    write_json(folder/'report.json',report)
    pd.DataFrame(nav,columns=['date','equity','cash','daily_pnl','daily_return','cumulative_pnl','cumulative_return','drawdown','fees','turnover','mode']).to_csv(folder/'NAV.csv',index=False,encoding='utf-8-sig')
    pd.DataFrame(fills,columns=['at','symbol','name','side','shares','price','fee','cash_after','quote_time','source','mode']).to_csv(folder/'FILLS.csv',index=False,encoding='utf-8-sig')
    e=lambda v:html.escape(str(v));p=lambda v:f'{v:.2%}'
    page='<html lang="zh"><meta charset="utf-8"><meta http-equiv="refresh" content="30"><title>盘中盯盘与模拟收益</title><style>body{font:15px/1.5 system-ui;margin:20px;color:#183047}table{border-collapse:collapse;width:100%}td,th{padding:6px;border-bottom:1px solid #ddd;text-align:left;vertical-align:top}.scroll{overflow:auto}</style><h1>30只股票盘中盯盘 · 独立模拟主账本</h1><p>'+e(report['note'])+'</p><p>检查时间 '+e(at.isoformat())+'；状态 '+e(status)+'；估值时间 '+e(s['valuation_at'])+'（'+e(s['valuation_status'])+'）。</p><p>模拟权益 '+f"{s['equity']:.2f}"+'元；现金 '+f"{s['cash']:.2f}"+'元；盘中段累计收益 '+p(s['equity']/meta['initial_equity']-1)+'。起点 '+e(meta['anchor'])+'，首个交易日 '+e(meta['start_session'])+'。</p><p>只有收盘验证成功才结算每日收益；盘中估值可能过期。<a href="report.json">来源、限制与全部账本</a></p>'
    page+='<p><a href="NAV.csv">每日及累计收益CSV</a> · <a href="FILLS.csv">模拟成交CSV</a></p><h2>30只股票行情（以来源时间为准）</h2><table><tr><th>股票</th><th>价格</th><th>行情时间</th><th>来源</th><th>限制</th></tr>'
    for symbol,name in meta['universe'].items():
        q=s.get('last_quotes',{}).get(symbol,{})
        page+='<tr>'+''.join('<td>'+e(v)+'</td>' for v in [name+' '+symbol,q.get('price','未接收'),q.get('asof','未接收'),q.get('source','—'),checks.get(symbol,[])])+'</tr>'
    page+='</table>'
    page+='<h2>当前模拟持仓</h2><table><tr><th>代码</th><th>股数</th><th>可卖</th><th>最近估值价格</th></tr>'+''.join('<tr>'+''.join('<td>'+e(v)+'</td>' for v in [k,x['shares'],x['sellable'],s['marks'].get(k)])+'</tr>' for k,x in s['positions'].items() if x['shares'])+'</table>'
    page+='<h2>最近模拟成交</h2><table><tr><th>时间</th><th>股票</th><th>方向</th><th>股数</th><th>价格</th><th>费用</th></tr>'+''.join('<tr>'+''.join('<td>'+e(v)+'</td>' for v in [f['at'],f['name'],f['side'],f['shares'],f['price'],round(f['fee'],2)])+'</tr>' for f in fills[-30:])+'</table>'
    page+='<h2>每日及累计模拟收益</h2><table><tr><th>日期</th><th>日盈亏</th><th>日收益率</th><th>累计收益率</th><th>费用</th></tr>'+''.join('<tr>'+''.join('<td>'+e(v)+'</td>' for v in [x['date'],round(x['daily_pnl'],2),p(x['daily_return']),p(x['cumulative_return']),round(x['fees'],2)])+'</tr>' for x in nav)+'</table>'
    page+='<h2>行情与成交限制</h2><p>'+e(checks)+'</p><p>'+e(blocks)+'</p><h2>提醒记录</h2><ul>'+''.join('<li>'+e(a['at']+' '+a['body']+' ['+a['status']+']')+'</li>' for a in alerts[-30:])+'</ul><p>macOS通知是否显示取决于通知权限和专注模式；报告保存提醒记录。</p></html>'
    atomic_text(folder/'index.html',page);return {'status':status,'equity':s['equity'],'cash':s['cash'],'fills':len(fills),'submitted_orders':0}

def primary(root):
    path=Path(root)/'reports/intraday/latest/report.json'
    if not path.exists():return '<section><h2>盘中盯盘尚未初始化</h2></section>'
    r=json.loads(path.read_text());s=r['account'];e=html.escape
    source=json.loads((Path(root)/'reports/active_research/latest/report.json').read_text());plan=source.get('plan',{})
    rows=''
    for row in plan.get('rows',[]):
        k=row['symbol'];shares=s['positions'].get(k,{}).get('shares',0);weight=plan.get('weights',{}).get(k,0)
        if not shares and not weight:continue
        rows+='<tr>'+''.join('<td>'+e(str(v))+'</td>' for v in [row['name']+' '+k,shares,f'{weight:.2%}', '按当时盘口、费用、可卖数量及累计风险额度计算'])+'</tr>'
    plan_html='<h3>开盘前冻结计划：'+e(str(plan.get('target_session','待生成')))+'（研究模拟）</h3><table><tr><th>股票</th><th>本账户现有股数</th><th>参考目标权重</th><th>盘中执行条件</th></tr>'+rows+'</table><p>目标权重不是已成交仓位；实际模拟买卖股数在盘中成交记录中显示。</p>'
    return '<section>'+plan_html+'<h2>当前主模拟：盘中盯盘与成交</h2><p>最近检查 '+e(r['checked_at'])+'；状态 '+e(r['status'])+'；估值时间 '+e(str(s['valuation_at']))+'。</p><p>权益 '+f"{s['equity']:.2f}"+'元；现金 '+f"{s['cash']:.2f}"+'元；成交 '+str(len(r['fills']))+'笔。日线研究模拟为独立对照，不与本账户收益相加。</p><a href="'+(Path(root)/'reports/intraday/latest/index.html').resolve().as_uri()+'">打开实时更新的盘中持仓、成交与收益报告</a> · <a href="详细报告/active_research/latest/index.html">查看收盘冻结计划参考及日线模拟对照</a></section>'

def main():
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('command',choices=['poll','reference','reference-probe','probe','close']);parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1]);parser.add_argument('--date');args=parser.parse_args()
    try:
        if args.command in ('reference','reference-probe'):
            with run_lock(args.root/'logs/intraday_reference'):result=refresh_reference(args.root,force=args.command=='reference-probe')
        elif args.command=='probe':
            a,b=collect(read_watchlist(args.root).symbol.tolist());result=dict(mode='CONNECTIVITY_PROBE_NO_TRADING',primary_count=len(a),secondary_count=len(b),quote_times=sorted({q['asof'] for q in a.values()}));write_json(args.root/'reports/intraday/connectivity.json',result)
        elif args.command=='close':result=close_day(args.root,args.date)
        else:result=run_once(args.root)
        write_json(args.root/'logs'/('intraday_reference_health.json' if args.command.startswith('reference') else 'intraday_health.json'),dict(at=now().isoformat(),status=result['status'] if 'status' in result else result['mode'],submitted_orders=0))
        print(json.dumps(result,ensure_ascii=False));return 0
    except Exception as exc:
        if isinstance(exc,RuntimeError) and '另一次更新正在运行' in str(exc):return 0
        # Exception text is not echoed: providers may embed credentials or response bodies.
        hp=args.root/'logs'/('intraday_reference_health.json' if args.command.startswith('reference') else 'intraday_health.json')
        old=json.loads(hp.read_text()) if hp.exists() else {}
        failure=dict(at=now().isoformat(),status='FAILED',error_type=type(exc).__name__,submitted_orders=0)
        if old.get('status')!='FAILED':
            try:subprocess.run(['osascript','-e','display notification "盯盘或交易参考数据服务失败，模拟交易可能暂停；请查看日志。" with title "AshareQuant 服务异常"'],timeout=5,capture_output=True)
            except (OSError,subprocess.TimeoutExpired):pass
        write_json(hp,failure);print(json.dumps({'status':'FAILED','error_type':type(exc).__name__}));return 2
if __name__=='__main__':sys.exit(main())
