"""Read-only news/disclosure collection; first-seen versioned evidence, never instructions."""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import html,json,re,sqlite3,time
import pandas as pd
import requests
from .core import now,read_watchlist,write_json,run_lock
from .paper_account import digest

def database(root):
 p=Path(root)/'data/adaptive/events.sqlite3';p.parent.mkdir(parents=True,exist_ok=True)
 db=sqlite3.connect(p,timeout=20);p.chmod(0o600)
 db.execute('PRAGMA journal_mode=WAL')
 db.execute('CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, symbol TEXT, first_seen TEXT, body TEXT)')
 db.execute('CREATE TABLE IF NOT EXISTS health(symbol TEXT,kind TEXT,body TEXT,PRIMARY KEY(symbol,kind))');db.commit();return db

def clean(s):return html.unescape(re.sub('<[^>]*>','',str(s or ''))).strip()
def timestamp(value):
 t=pd.Timestamp(value)
 if pd.isna(t):raise ValueError('MISSING_PUBLICATION_TIME')
 return (t.tz_localize('Asia/Shanghai') if t.tzinfo is None else t.tz_convert('Asia/Shanghai')).isoformat()

def news(symbol,at):
 query=dict(uid='',keyword=symbol,type=['cmsArticleWebOld'],client='web',clientType='web',clientVersion='curr',param={'cmsArticleWebOld':dict(searchScope='default',sort='default',pageIndex=1,pageSize=100,preTag='',postTag='')})
 cb='jQuery35101000_'+str(int(time.time()*1000))
 r=requests.get('https://search-api-web.eastmoney.com/search/jsonp',params={'cb':cb,'param':json.dumps(query,ensure_ascii=False),'_':str(int(time.time()*1000))},headers={'Referer':'https://so.eastmoney.com/news/s?keyword='+symbol,'User-Agent':'Mozilla/5.0'},timeout=(5,12));r.raise_for_status()
 text=r.text.strip();match=re.fullmatch(re.escape(cb)+r'\((.*)\);?',text,re.S);raw=json.loads(match.group(1) if match else text)
 rows=raw['result']['cmsArticleWebOld']
 if not isinstance(rows,list):raise ValueError('NEWS_RESPONSE_OR_COVERAGE_UNCERTAIN')
 return [dict(kind='news',symbol=symbol,source='Eastmoney news search',publisher=clean(x.get('mediaName')),source_id=str(x['code']),title=clean(x['title'])[:500],text=clean(x.get('content'))[:6000],published_at=timestamp(x['date']),time_precision='second',url='https://finance.eastmoney.com/a/'+str(x['code'])+'.html') for x in rows]

def announcements(symbol,at):
 result=[];pages=1;page=1;seen=set()
 while page<=pages:
  r=requests.get('https://np-anotice-stock.eastmoney.com/api/security/ann',params=dict(stock_list=symbol,sr=-1,page_size=100,page_index=page,ann_type='A',client_source='web',f_node=0,s_node=0,begin_time=str((at-pd.Timedelta(days=3)).date()),end_time=str(at.date())),timeout=(5,12));r.raise_for_status();data=r.json()['data']
  total=int(data['total_hits']);pages=max(1,(total+99)//100)
  if pages>5 or not isinstance(data['list'],list):raise ValueError('ANNOUNCEMENT_COVERAGE_UNCERTAIN')
  for x in data['list']:
   if not any(c.get('stock_code')==symbol for c in x['codes']):raise ValueError('ANNOUNCEMENT_SYMBOL_MISMATCH')
   if x['art_code'] in seen:raise ValueError('ANNOUNCEMENT_DUPLICATE_PAGE')
   seen.add(x['art_code'])
   # notice_date has day precision. Never invent an intraday release timestamp.
   result.append(dict(kind='announcement',symbol=symbol,source='Eastmoney company disclosure mirror',publisher='上市公司公告（东方财富转发）',source_id=x['art_code'],title=clean(x['title'])[:500],text='',published_at=timestamp(str(x['notice_date'])[:10]),time_precision='day',url=f"https://data.eastmoney.com/notices/detail/{symbol}/{x['art_code']}.html"))
  page+=1
 if len(seen)!=total:raise ValueError('ANNOUNCEMENT_PAGINATION_CHANGED')
 return result

def store(db,symbol,kind,rows,at,error=None):
 if not error:
  for row in rows:
   if row['symbol']!=symbol or row['kind']!=kind:raise ValueError('EVENT_IDENTITY_MISMATCH')
   identity=digest(row);body={**row,'id':identity,'first_seen':at.isoformat()}
   db.execute('INSERT OR IGNORE INTO events VALUES(?,?,?,?)',(identity,symbol,at.isoformat(),json.dumps(body,ensure_ascii=False)))
 health=dict(at=at.isoformat(),ok=error is None,error=error,count=len(rows),coverage='recent search results, not guaranteed exhaustive')
 db.execute('INSERT OR REPLACE INTO health VALUES(?,?,?)',(symbol,kind,json.dumps(health,ensure_ascii=False)))

def collect(root,force=False):
 root=Path(root);at=now()
 if not force and not (8<=at.hour<16):return dict(status='OUTSIDE_EVENT_WINDOW')
 with run_lock(root/'logs/adaptive_events'):
  symbols=read_watchlist(root).symbol.tolist();jobs={};errors={}
  with ThreadPoolExecutor(max_workers=6) as pool:
   for s in symbols:
    for kind,fn in [('news',news),('announcement',announcements)]:jobs[s,kind]=pool.submit(fn,s,at)
   with database(root) as db:
    for (s,kind),job in jobs.items():
     try:rows=job.result();store(db,s,kind,rows,now())
     except Exception as exc:errors[s+':'+kind]=type(exc).__name__;store(db,s,kind,[],now(),type(exc).__name__)
  result=dict(status='EVENT_FEEDS_READY' if not errors else 'EVENT_FEEDS_PARTIAL',at=now().isoformat(),queries=len(jobs),success=len(jobs)-len(errors),errors=errors)
  write_json(root/'logs/adaptive_events_health.json',result);return result

def snapshot(root,at,cfg):
 with database(root) as db:
  events=[json.loads(x[0]) for x in db.execute('SELECT body FROM events WHERE first_seen<=?',(at.isoformat(),))]
  health={}
  for s,kind,body in db.execute('SELECT symbol,kind,body FROM health'):
   h=json.loads(body);age=(at-pd.Timestamp(h['at'])).total_seconds();health[s+':'+kind]=dict(**h,fresh=bool(h['ok'] and 0<=age<=cfg['feed_max_age_seconds']))
 eligible=[]
 for e in events:
  age=(at-pd.Timestamp(e['published_at'])).total_seconds()
  if 0<=age<=cfg['event_max_age_hours']*3600 and pd.Timestamp(e['first_seen'])<=at:eligible.append(e)
 return dict(at=at.isoformat(),events=eligible,health=health)

NEGATIVE=('立案调查','退市风险警示','债务逾期','业绩预亏','重大诉讼','业绩预减')
POSITIVE=('业绩预增','回购股份实施完成','中标重大项目')
AMBIGUOUS=('未发现','不存在','不涉及','未收到','未被','无重大','澄清','传闻','辟谣','否认','不构成','解除','撤销','终结','尚未','拟','可能')
def classify(e,name,at):
 title=e['title'];text=e.get('text','');result=dict(id=e['id'],symbol=e['symbol'],kind=e['kind'],title=title,url=e['url'],source=e['source'],published_at=e['published_at'],first_seen=e['first_seen'],time_precision=e['time_precision'],label='NEUTRAL',basis='标题/摘要规则研究标签，不是全文理解或已验证情绪模型')
 if pd.Timestamp(e['first_seen'])>at or pd.Timestamp(e['published_at'])>at:return {**result,'label':'UNAVAILABLE'}
 if e['kind']=='news' and name not in title and e['symbol'] not in title:return {**result,'label':'UNRELATED_OR_AMBIGUOUS'}
 if any(w in title for w in NEGATIVE+POSITIVE) and any(w in title+' '+text for w in AMBIGUOUS):return {**result,'label':'REVIEW_REQUIRED'}
 if any(w in title for w in NEGATIVE):result['label']='NEGATIVE_DISCLOSURE' if e['kind']=='announcement' else 'NEGATIVE_NEWS_UNCONFIRMED'
 elif e['kind']=='announcement' and any(w in title for w in POSITIVE):result['label']='POSITIVE_DISCLOSURE'
 return result
