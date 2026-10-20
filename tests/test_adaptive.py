import copy,json,sqlite3,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
from asharequant.paper_account import put,get,digest
from asharequant.intraday import save_state
from asharequant.adaptive_policy import adjust,trend
from asharequant.intraday_events import database,store,snapshot,classify,news,announcements
from asharequant.adaptive_intraday import poll,close_day,db_open
import test_intraday

class AdaptiveTests(unittest.TestCase):
 def setUp(self):
  fixture=test_intraday.IntradayTests();fixture.setUp();self.f=fixture
  self.cfg=json.loads(Path('adaptive_config.json').read_text());self.at=pd.Timestamp('2026-10-21T10:06:00+08:00')
  self.plan={**fixture.plan,'rows':[dict(symbol='600031',name='三一重工',sector='机械')]}
  self.history=[]
  for i in range(7):
   t=self.at-pd.Timedelta(minutes=6-i);q={**fixture.q,'price':10+i*.01,'asof':t.isoformat()}
   self.history.append(dict(at=t.isoformat(),quotes={'600031':q},checks={'600031':[]}))
  self.events=dict(events=[],health={f'600031:{kind}':{'fresh':True} for kind in ('news','announcement')})
 def change(self,p=None,s=None,ev=None,h=None):return adjust(p or self.plan,s or self.f.state,self.history[-1]['quotes'],h or self.history,ev or self.events,self.f.c,self.cfg,self.at)
 def event(self,title='业绩预增公告',kind='announcement'):
  return dict(id='event1',symbol='600031',kind=kind,title=title,text='',source='SYNTHETIC_TEST',url='https://example.invalid',published_at='2026-10-21T10:00:00+08:00',first_seen='2026-10-21T10:01:00+08:00',time_precision='second')
 def test_raw_plan_immutable_and_trend_can_change_weight(self):
  before=digest(self.plan);p,a=self.change();self.assertGreater(p['weights']['600031'],self.plan['weights']['600031']);self.assertEqual(digest(self.plan),before);self.assertEqual(p['edges'],self.plan['edges']);self.assertTrue(a['raw_forecasts_unchanged'])
 def test_no_future_or_lunch_backfilled_trend(self):
  h=copy.deepcopy(self.history);h[0]['at']=(self.at+pd.Timedelta(hours=1)).isoformat();self.assertEqual(trend(h,'600031',self.at,self.cfg)['samples'],6)
  h=copy.deepcopy(self.history);h[3]['quotes']['600031']['asof']='2026-10-20T10:03:00+08:00';self.assertFalse(trend(h,'600031',self.at,self.cfg)['ready'])
 def test_warmup_and_failed_events_no_new_exposure(self):
  p,a=self.change(h=self.history[-2:]);self.assertEqual(p['weights']['600031'],0);self.assertIn('600031',a['buy_blocks'])
  ev=copy.deepcopy(self.events);ev['health']['600031:news']['fresh']=False;p,a=self.change(ev=ev);self.assertEqual(p['weights']['600031'],0)
 def test_negative_announcement_and_news_differ(self):
  s=copy.deepcopy(self.f.state);s['positions']={'600031':{'shares':3000,'sellable':3000}};s['cash']=120000;s['marks']={'600031':10}
  ev={**self.events,'events':[self.event('关于被立案调查的公告')]};p,a=self.change(s=s,ev=ev);self.assertLess(p['weights']['600031'],self.plan['weights']['600031']);self.assertGreater(p['risk_priorities']['600031'],10)
  label=classify(self.event('三一重工被立案调查','news'),'三一重工',self.at);self.assertEqual(label['label'],'NEGATIVE_NEWS_UNCONFIRMED')
 def test_ambiguous_and_irrelevant_news_not_bullish(self):
  self.assertEqual(classify(self.event('关于不存在重大诉讼的澄清公告'),'三一重工',self.at)['label'],'REVIEW_REQUIRED')
  self.assertEqual(classify(self.event('其他公司业绩预增','news'),'三一重工',self.at)['label'],'UNRELATED_OR_AMBIGUOUS')
 def test_routine_notice_is_not_a_risk_veto(self):
  self.assertEqual(classify(self.event('关于拟召开股东大会的公告'),'三一重工',self.at)['label'],'NEUTRAL')
 def test_positive_event_requires_confirming_trend_and_caps(self):
  ev={**self.events,'events':[self.event()]};p,a=self.change(ev=ev);self.assertLessEqual(p['weights']['600031'],self.plan['weights']['600031']*1.25)
  plan=copy.deepcopy(self.plan);plan['weights']['600031']=.224;p,a=self.change(p=plan,ev=ev);self.assertLessEqual(p['weights']['600031'],.225)
 def test_first_seen_dedup_revisions_and_failure_visible(self):
  with tempfile.TemporaryDirectory() as d:
   e=self.event();[e.pop(k) for k in ['id','first_seen']];early=self.at-pd.Timedelta(minutes=2)
   with database(d) as db:store(db,'600031','announcement',[e],early);store(db,'600031','announcement',[e],self.at)
   snap=snapshot(d,self.at,self.cfg);self.assertEqual(len(snap['events']),1);self.assertEqual(snap['events'][0]['first_seen'],early.isoformat())
   self.assertEqual(snapshot(d,early-pd.Timedelta(seconds=1),self.cfg)['events'],[])
   changed={**e,'title':'更正公告'}
   with database(d) as db:store(db,'600031','announcement',[changed],self.at);store(db,'600031','announcement',[],self.at,'Timeout')
   snap=snapshot(d,self.at,self.cfg);self.assertEqual(len(snap['events']),2);self.assertFalse(snap['health']['600031:announcement']['fresh'])
 def test_future_publication_not_eligible(self):
  with tempfile.TemporaryDirectory() as d:
   e=self.event();e['published_at']='2026-10-22T00:00:00+08:00'
   with database(d) as db:store(db,'600031','announcement',[e],self.at)
   self.assertFalse(snapshot(d,self.at,self.cfg)['events'])
 def test_news_parser_and_announcement_identity_failure(self):
  class Response:
   text=json.dumps({'result':{'cmsArticleWebOld':[dict(code='abc',title='<em>三一重工</em>业绩',content='摘要',date='2026-10-21 10:00:00',mediaName='TEST')]}})
   def raise_for_status(self):pass
   def json(self):return {'data':{'total_hits':1,'list':[{'codes':[{'stock_code':'000001'}]}]}}
  with patch('asharequant.intraday_events.requests.get',return_value=Response()):
   self.assertEqual(news('600031',self.at)[0]['title'],'三一重工业绩')
   with self.assertRaisesRegex(ValueError,'SYMBOL_MISMATCH'):announcements('600031',self.at)
 def test_paired_cycle_idempotent_no_control_mutation_and_close(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);(root/'data/intraday').mkdir(parents=True);(root/'data/calendar.csv').write_text('date\n2026-10-20\n2026-10-21\n2026-10-22\n');(root/'adaptive_config.json').write_text(json.dumps(self.cfg))
   base_state={**copy.deepcopy(self.f.state),'valuation_status':'VERIFIED_CLOSE','valuation_at':'2026-10-20T15:00:00+08:00'}
   with sqlite3.connect(root/'data/intraday/account.sqlite3') as db:
    db.execute('CREATE TABLE records(kind TEXT,key TEXT,body TEXT,sha256 TEXT,PRIMARY KEY(kind,key))');db.execute('CREATE TABLE current_state(id INTEGER PRIMARY KEY,body TEXT)')
    put(db,'meta','account',dict(config=self.f.c,monitor_config=self.f.cfg,universe={'600031':'三一重工'},start_session='2026-10-21'));save_state(db,base_state)
    for i,c in enumerate(self.history):put(db,'cycles',str(i),c)
    before=db.execute('SELECT * FROM records').fetchall()
   source=root/'reports/active_research/latest';source.mkdir(parents=True);(source/'report.json').write_text(json.dumps({'plan':self.plan}))
   with patch('asharequant.adaptive_intraday.now',return_value=self.at),patch('asharequant.adaptive_intraday.snapshot',return_value=self.events):
    a=poll(root);b=poll(root)
   self.assertEqual(a['fills'],1);self.assertEqual(b['status'],'ALREADY_PROCESSED')
   with sqlite3.connect(root/'data/intraday/account.sqlite3') as db:self.assertEqual(before,db.execute('SELECT * FROM records').fetchall())
   with patch('asharequant.adaptive_intraday.now',return_value=self.at):
    with self.assertRaisesRegex(ValueError,'CLOSE_NOT_AVAILABLE'):close_day(root,'2026-10-21')
   obs=dict(bars={'600031':{'close':10.2}},states={'600031':{'corporate_action_status':'none'}})
   with patch('asharequant.adaptive_intraday.now',return_value=pd.Timestamp('2026-10-21T18:00:00+08:00')),patch('asharequant.adaptive_intraday.evidence',return_value=obs):
    close_day(root,'2026-10-21');self.assertEqual(close_day(root,'2026-10-21')['status'],'ALREADY_SETTLED')
   with db_open(root) as db:self.assertEqual(len(get(db,'fills')),1);self.assertEqual(len(get(db,'nav')),1)
