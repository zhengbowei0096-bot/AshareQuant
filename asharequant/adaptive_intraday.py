"""Independent research account; original intraday account remains an untouched control."""
from pathlib import Path
import copy,hashlib,html,json,sqlite3,subprocess,sys
import pandas as pd
from .core import now,run_lock,write_json,atomic_text
from .paper_account import get,put,digest,evidence,opened
from .intraday import session,transition,queue_alerts,load_state,save_state
from .intraday_data import days
from .intraday_events import snapshot,collect as collect_events
from .adaptive_policy import adjust

def enabled(root):
 p=Path(root)/'adaptive_config.json';return p.exists() and json.loads(p.read_text()).get('enabled') is True

def db_open(root):
 path=Path(root)/'data/adaptive/account.sqlite3';path.parent.mkdir(parents=True,exist_ok=True);db=sqlite3.connect(path,timeout=20);path.chmod(0o600)
 db.execute('CREATE TABLE IF NOT EXISTS records(kind TEXT,key TEXT,body TEXT,sha256 TEXT,PRIMARY KEY(kind,key))');db.execute('CREATE TABLE IF NOT EXISTS current_state(id INTEGER PRIMARY KEY,body TEXT NOT NULL)');db.commit();return db

def source_db(root):return sqlite3.connect('file:'+str(Path(root)/'data/intraday/account.sqlite3')+'?mode=ro',uri=True,timeout=10)

def initialize(root,db):
 cfg=json.loads((root/'adaptive_config.json').read_text())
 if cfg['real_orders_enabled'] or cfg['mode']!='RESEARCH_PAPER_ONLY':raise ValueError('PAPER_ONLY_REQUIRED')
 signature={n:hashlib.sha256((Path(__file__).parent/n).read_bytes()).hexdigest() for n in ('adaptive_intraday.py','adaptive_policy.py','intraday_events.py','intraday.py','intraday_data.py','active_execution.py','decision.py')}
 meta=get(db,'meta').get('account')
 if not meta:
  with source_db(root) as control:
   control.execute('BEGIN');base=get(control,'meta')['account'];state=load_state(control)
  state=copy.deepcopy(state)
  if state['valuation_status'] not in ('ANCHOR_SNAPSHOT','VERIFIED_CLOSE'):raise ValueError('START_FROM_VERIFIED_CLOSE_REQUIRED')
  meta=dict(created_at=now().isoformat(),code=signature,adaptive_config=cfg,config=base['config'],monitor_config=base['monitor_config'],universe=base['universe'],anchor=state['closed_day'],start_session=base['start_session'],initial_equity=state['equity'],control_anchor_equity=state['equity'],mode='ADAPTIVE_RESEARCH_PAPER_ONLY')
  state.update(turnover=0.,fees=0.,used_quotes={},conditions={},notification_queue=[],fills_count=0,peak=state['equity']);put(db,'meta','account',meta);save_state(db,state)
 if meta['code']!=signature or meta['adaptive_config']!=cfg:raise ValueError('ADAPTIVE_FROZEN_CONFIG_CHANGED')
 return meta,load_state(db)

def alerts(root):
 with db_open(root) as db:
  s=load_state(db)
  for alert in s['notification_queue']:
   if alert['status']!='PENDING':continue
   alert['status']='ATTEMPTED';save_state(db,s);db.commit()
   try:
    proc=subprocess.run(['osascript','-e','display notification '+json.dumps(alert['body'],ensure_ascii=False)+' with title "AshareQuant 动态研究模拟"'],timeout=5,capture_output=True)
    alert['status']='SUBMITTED_TO_MACOS' if proc.returncode==0 else 'DELIVERY_FAILED'
   except (OSError,subprocess.TimeoutExpired):alert['status']='DELIVERY_FAILED'
   put(db,'alerts',alert['id'],alert)
  save_state(db,s)

def poll(root):
 root=Path(root)
 if not enabled(root):return {'status':'DISABLED'}
 with run_lock(root/'logs/adaptive'):
  with db_open(root) as db:meta,s=initialize(root,db)
  at=now();mode=session(at,days(root))
  if mode=='CLOSED':return publish(root,meta,s,'MARKET_CLOSED',None)
  # Use the exact audited quotes seen by the frozen-plan control. No second fill-price feed.
  with source_db(root) as control:
   records=control.execute("SELECT key,body FROM records WHERE kind='cycles' ORDER BY CAST(key AS INTEGER) DESC LIMIT 16").fetchall()
  history=[json.loads(row[1]) for row in reversed(records)]
  if not history or not 0<=(at-pd.Timestamp(history[-1]['at'])).total_seconds()<=45:return publish(root,meta,s,'WAITING_FRESH_CONTROL_QUOTES',None)
  key=records[0][0];cycle=history[-1];quotes=cycle['quotes'];checks=copy.deepcopy(cycle['checks'])
  for k,q in quotes.items():
   age=(at-pd.Timestamp(q['asof'])).total_seconds()
   if not 0<=age<=45:checks.setdefault(k,[]).append('QUOTE_STALE_OR_FUTURE')
  source=json.loads((root/'reports/active_research/latest/report.json').read_text());plan=source['plan']
  if plan.get('target_session')!=str(at.date()) or pd.Timestamp(plan['registered_at'])>=opened(str(at.date())):return publish(root,meta,s,'WAITING_FROZEN_PLAN',None)
  events=snapshot(root,at,meta['adaptive_config'])
  with db_open(root) as db:
   db.execute('BEGIN IMMEDIATE')
   if db.execute("SELECT 1 FROM records WHERE kind='cycles' AND key=?",(key,)).fetchone():return {'status':'ALREADY_PROCESSED'}
   s=load_state(db);before=digest(s);s['last_quotes']=quotes
   dynamic,audit=adjust(plan,s,quotes,history,events,meta['config'],meta['adaptive_config'],at)
   # Preserve raw source edges, but explicitly veto new exposure where evidence is incomplete.
   for k in audit['buy_blocks']:dynamic.setdefault('edges',{})[k]=None
   conditions={};fills=[];blocks={}
   if mode=='CONTINUOUS':s,fills,conditions,blocks=transition(s,dynamic,quotes,checks,meta['config'],meta['monitor_config'],at,{r['symbol']:r['sector'] for r in plan['rows']})
   for k,x in audit['stocks'].items():
    for event in x['events']:
     if event['label'] in ('NEGATIVE_DISCLOSURE','NEGATIVE_NEWS_UNCONFIRMED','POSITIVE_DISCLOSURE','REVIEW_REQUIRED'):conditions['EVENT:'+event['id']]=meta['universe'][k]+'：'+event['title']+'（'+event['label']+'）'
   if audit['buy_blocks']:conditions['ADAPTIVE_BUY_LIMITS']=f"{len(audit['buy_blocks'])}只股票处于趋势预热、事件待核实或资讯连接限制，禁止新增买入"
   queue_alerts(s,conditions,fills,at)
   put(db,'events',digest(events),events)
   put(db,'cycles',key,dict(at=at.isoformat(),control_cycle=key,control_quotes_sha=digest(quotes),frozen_plan_sha=digest(plan),event_snapshot_sha=digest(events),audit=audit,before=before,after=digest(s),fills=fills,blocks=blocks,checks=checks))
   for i,f in enumerate(fills):put(db,'fills',key+':'+str(i),f)
   save_state(db,s);db.commit()
  if meta['monitor_config']['notification_enabled']:alerts(root)
  return publish(root,meta,s,mode,audit)

def close_day(root,day):
 root=Path(root)
 if not enabled(root):return {'status':'DISABLED'}
 with run_lock(root/'logs/adaptive'):
  with db_open(root) as db:
   db.execute('BEGIN IMMEDIATE');meta=get(db,'meta').get('account');s=load_state(db)
   if not meta:return {'status':'NOT_INITIALIZED'}
   if day<=s['closed_day']:return {'status':'ALREADY_SETTLED'}
   at=now()
   if day>str(at.date()) or (day==str(at.date()) and at.hour<15):raise ValueError('CLOSE_NOT_AVAILABLE_YET')
   cal=days(root)
   if str(cal[cal<pd.Timestamp(day)][-1].date())!=s['closed_day']:raise ValueError('MISSING_PREVIOUS_SETTLEMENT')
   if s['day'] not in (day,s['closed_day']):raise ValueError('ACCOUNT_SESSION_CONFLICT')
   held=[k for k,p in s['positions'].items() if p['shares']];obs=evidence(root,day,held)
   if any(x['corporate_action_status']!='none' for x in obs['states'].values()):raise ValueError('CORPORATE_ACTION_RECONCILIATION_REQUIRED')
   if set(obs['bars'])!=set(held):raise ValueError('CLOSE_COVERAGE_INCOMPLETE')
   for k,b in obs['bars'].items():s['marks'][k]=b['close']
   equity=s['cash']+sum(p['shares']*s['marks'][k] for k,p in s['positions'].items());prior=s['previous_close_equity'];s['peak']=max(s['peak'],equity)
   nav=dict(date=day,equity=equity,cash=s['cash'],daily_pnl=equity-prior,daily_return=equity/prior-1,cumulative_pnl=equity-meta['initial_equity'],cumulative_return=equity/meta['initial_equity']-1,drawdown=1-equity/s['peak'],fees=s['fees'] if s['day']==day else 0.,turnover=s['turnover'] if s['day']==day else 0.)
   put(db,'close_evidence',day,obs);put(db,'nav',day,nav);s.update(equity=equity,previous_close_equity=equity,closed_day=day,day=day,valuation_at=day+'T15:00:00+08:00',valuation_status='VERIFIED_CLOSE');save_state(db,s);db.commit()
  return publish(root,meta,s,'SETTLED',None)

def publish(root,meta,s,status,audit):
 folder=Path(root)/'reports/adaptive/latest';folder.mkdir(parents=True,exist_ok=True)
 with db_open(root) as db:
  nav=list(get(db,'nav').values());fills=list(get(db,'fills').values());notifications=list(get(db,'alerts').values())
  if audit is None:
   row=db.execute("SELECT body FROM records WHERE kind='cycles' ORDER BY CAST(key AS INTEGER) DESC LIMIT 1").fetchone();audit=json.loads(row[0])['audit'] if row else None
 with source_db(root) as control:
  base=load_state(control);base_nav=get(control,'nav')
 paired=[]
 for n in nav:
  if n['date'] in base_nav:
   b=base_nav[n['date']];paired.append(dict(date=n['date'],adaptive_return=n['equity']/meta['initial_equity']-1,control_return=b['equity']/meta['control_anchor_equity']-1,excess_pnl=(n['equity']-meta['initial_equity'])-(b['equity']-meta['control_anchor_equity']),adaptive_fees=n['fees'],control_fees=b['fees']))
 report=dict(status=status,checked_at=now().isoformat(),meta=meta,account=s,audit=audit,nav=nav,fills=fills,alerts=notifications[-100:],comparison=paired,submitted_orders=0,limitations=['仅研究模拟，未通过独立效果验证','新闻为公开检索/公告转发，非交易所授权低延迟完整资讯流','标题与摘要规则标签，不读取公告PDF全文，不声称语义理解全部内容','行情30秒、资讯120秒轮询；首次接收之前不使用，缺项禁止新增买入','原收益预测及概率不因本层改变；目标仓位可调整，原策略独立对照','收益从共同锚点计算，两个账户不相加；撮合仍是盘口条件假设'])
 write_json(folder/'report.json',report)
 pd.DataFrame(nav,columns=['date','equity','cash','daily_pnl','daily_return','cumulative_pnl','cumulative_return','drawdown','fees','turnover']).to_csv(folder/'NAV.csv',index=False,encoding='utf-8-sig')
 pd.DataFrame(fills,columns=['at','symbol','name','side','shares','price','fee','cash_after']).to_csv(folder/'FILLS.csv',index=False,encoding='utf-8-sig');pd.DataFrame(paired,columns=['date','adaptive_return','control_return','excess_pnl','adaptive_fees','control_fees']).to_csv(folder/'COMPARE.csv',index=False,encoding='utf-8-sig')
 e=lambda x:html.escape(str(x))
 page='<html lang="zh"><meta charset="utf-8"><meta http-equiv="refresh" content="30"><title>动态盘中研究模拟</title><style>body{font:15px/1.5 system-ui;color:#183047;margin:20px}table{border-collapse:collapse;width:100%}th,td{padding:6px;border-bottom:1px solid #ddd;vertical-align:top;text-align:left}.scroll{overflow:auto}td{min-width:90px}</style><h1>盘中走势＋新闻公告 · 研究模拟</h1><p>状态 '+e(status)+'；检查 '+e(report['checked_at'])+'；估值 '+e(s['valuation_at'])+'（'+e(s['valuation_status'])+'）</p><p>权益 '+f"{s['equity']:.2f}"+'元；现金 '+f"{s['cash']:.2f}"+'元；模拟成交 '+str(len(fills))+'笔。</p><p><a href="NAV.csv">收益</a> · <a href="FILLS.csv">成交</a> · <a href="COMPARE.csv">与原策略比较</a> · <a href="report.json">全部证据与限制</a></p><ul>'+''.join('<li>'+e(x)+'</li>' for x in report['limitations'])+'</ul>'
 page+='<h2>动态仓位与判断原因</h2><p>决策时间 '+e(audit['at'] if audit else '尚未产生盘中决策')+'</p><div class="scroll"><table><tr><th>股票</th><th>原目标</th><th>动态目标</th><th>当前股数</th><th>走势</th><th>调整依据</th></tr>'
 if audit:
  for k,x in audit['stocks'].items():page+='<tr>'+''.join('<td>'+e(v)+'</td>' for v in [meta['universe'][k]+' '+k,f"{x['base_weight']:.2%}",f"{x['adjusted_weight']:.2%}",s['positions'].get(k,{}).get('shares',0),f"{x['trend']['momentum']:+.2%}" if x['trend']['ready'] else '样本不足/不连续','、'.join(x['reasons']) or '无额外调整'])+'</tr>'
 else:page+='<tr><td colspan="6">等待交易时段约5分钟连续报价及当日资讯；不会用历史回填冒充盘中观察。</td></tr>'
 page+='</table></div><h2>新闻公告证据</h2><ul>'
 if audit:
  for k,x in audit['stocks'].items():
   for event in x['events']:
    page+='<li>'+e(meta['universe'][k]+'：'+event['title']+'；标签 '+event['label']+'；发布 '+event['published_at']+'（'+event['time_precision']+'）；首次接收 '+event['first_seen'])+' <a href="'+e(event['url'])+'">来源</a></li>'
 page+='</ul><h2>模拟成交</h2><table><tr><th>时间</th><th>股票</th><th>方向</th><th>股数</th><th>价格</th><th>费用</th></tr>'+''.join('<tr>'+''.join('<td>'+e(v)+'</td>' for v in [f['at'],f['name'],f['side'],f['shares'],f['price'],round(f['fee'],2)])+'</tr>' for f in fills[-50:])+'</table><h2>相同起点的收盘对照</h2><p>'+e(paired if paired else '尚无已完成的共同交易日，不声称新策略优于旧策略。')+'</p></html>'
 atomic_text(folder/'index.html',page);return dict(status=status,equity=s['equity'],fills=len(fills),submitted_orders=0)

def primary(root):
 path=Path(root)/'reports/adaptive/latest/report.json'
 if not path.exists():return '<section><h2>动态研究模拟待初始化</h2></section>'
 r=json.loads(path.read_text());s=r['account'];link=(path.parent/'index.html').resolve().as_uri()
 source=json.loads((Path(root)/'reports/active_research/latest/report.json').read_text());plan=source['plan'];audit=r.get('audit');rows=''
 if audit and str(pd.Timestamp(audit['at']).date())!=plan.get('target_session'):audit=None
 for row in plan['rows']:
  k=row['symbol'];quantity=s['positions'].get(k,{}).get('shares',0);weight=audit['stocks'][k]['adjusted_weight'] if audit else plan['weights'].get(k,0)
  if not quantity and not weight:continue
  values=[row['name']+' '+k,quantity,f'{weight:.2%}', '盘中动态目标' if audit else '收盘参考目标；等待盘中证据', '按实时费用、盘口、T+1及风险额度模拟']
  rows+='<tr>'+''.join('<td>'+html.escape(str(v))+'</td>' for v in values)+'</tr>'
 summary='<table><tr><th>股票</th><th>模拟持仓股数</th><th>参考权重</th><th>状态</th><th>执行条件</th></tr>'+rows+'</table>'
 return '<section><h2>当前主模拟：盘中走势＋新闻公告</h2><p>仅研究；原模型和原盘中策略保留对照。权益 '+f"{s['equity']:.2f}"+'元，现金 '+f"{s['cash']:.2f}"+'元，估值时间 '+html.escape(str(s['valuation_at']))+'。</p>'+summary+'<p><a href="'+link+'">查看动态目标、调整原因、事件证据及成交收益</a> · <a href="详细报告/intraday/latest/index.html">原冻结预测盘中策略对照</a></p></section>'

def main():
 import argparse
 parser=argparse.ArgumentParser();parser.add_argument('command',choices=['poll','events','events-probe','close']);parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1]);parser.add_argument('--date');a=parser.parse_args()
 try:
  result=collect_events(a.root,force=a.command=='events-probe') if a.command.startswith('events') else close_day(a.root,a.date) if a.command=='close' else poll(a.root)
  write_json(a.root/'logs'/('adaptive_events_service.json' if a.command.startswith('events') else 'adaptive_health.json'),dict(checked_at=now().isoformat(),**result));print(json.dumps(result,ensure_ascii=False));return 0
 except Exception as exc:
  if isinstance(exc,RuntimeError) and '另一次更新正在运行' in str(exc):return 0
  path=a.root/'logs'/('adaptive_events_service.json' if a.command.startswith('events') else 'adaptive_health.json');old=json.loads(path.read_text()) if path.exists() else {}
  result=dict(at=now().isoformat(),status='FAILED',error_type=type(exc).__name__,submitted_orders=0);write_json(path,result)
  if old.get('status')!='FAILED':
   try:subprocess.run(['osascript','-e','display notification "动态研究模拟服务失败，请检查日志；不会提交真实订单。" with title "AshareQuant"'],capture_output=True,timeout=5)
   except (OSError,subprocess.TimeoutExpired):pass
  print(json.dumps(result));return 2
if __name__=='__main__':sys.exit(main())
