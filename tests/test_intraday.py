import copy,json,sqlite3,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
from asharequant.intraday import session,transition,queue_alerts,initialize,db_open,close_day,run_once
from asharequant.intraday_data import quality
from asharequant.decision import DEFAULTS
from asharequant.paper_account import get

class IntradayTests(unittest.TestCase):
 def setUp(self):
  self.at=pd.Timestamp('2026-10-21T10:00:00+08:00');self.cal=pd.DatetimeIndex(['2026-10-20','2026-10-21','2026-10-22'])
  self.cfg=dict(enabled=True,refresh_seconds=30,quote_max_age_seconds=45,book_participation=.01,move_alert_fraction=.05,limit_alert_fraction=.01,notification_enabled=False,maximum_drawdown=.15,minimum_equity=120000,real_orders_enabled=False)
  self.c={**DEFAULTS,'max_stock':.225,'max_sector':.375,'max_turnover':.45,'max_invested':.9,'fees_verified':True}
  self.state=dict(cash=150000.,positions={},marks={},equity=150000.,previous_close_equity=150000.,peak=150000.,day='2026-10-20',closed_day='2026-10-20',turnover=0.,fees=0.,used_quotes={},conditions={},notification_queue=[],fills_count=0)
  self.q=dict(symbol='600031',name='三一重工',source='SYNTHETIC_TEST',price=10.,previous_close=10.,bid=9.99,ask=10.01,bid_size_lots=100000,ask_size_lots=100000,limit_up=11.,limit_down=9.,asof=self.at.isoformat())
  self.plan=dict(environment='risk_off',weights={'600031':.15},edges={'600031':dict(reference_price=10.,predicted_return=.08,uncertainty=.005)},risk_priorities={'600031':1},registered_at='2026-10-20T18:00:00+08:00',target_session='2026-10-21',rows=[{'symbol':'600031','sector':'机械'}])
 def step(self,s=None,p=None,q=None,checks=None,at=None):return transition(s or self.state,p or self.plan,{'600031':q or self.q},checks or {'600031':[]},self.c,self.cfg,at or self.at,{'600031':'机械'})
 def test_beijing_sessions_lunch_auction_holiday(self):
  for time,expected in [('09:29:59','OBSERVE_ONLY'),('09:30:00','CONTINUOUS'),('11:30:00','CLOSED'),('12:30:00','CLOSED'),('13:00:00','CONTINUOUS'),('14:57:00','OBSERVE_ONLY'),('15:05:00','CLOSED')]:self.assertEqual(session(pd.Timestamp('2026-10-21T'+time+'+08:00'),self.cal),expected)
  self.assertEqual(session(pd.Timestamp('2026-10-24T10:00:00+08:00'),self.cal),'CLOSED')
  self.assertEqual(session(self.at.tz_convert('Europe/London'),self.cal),'CONTINUOUS')
 def test_t_plus_one_and_same_quote_no_double_fill(self):
  s,f,_,_=self.step();self.assertTrue(f);self.assertEqual(s['positions']['600031']['sellable'],0)
  s2,f2,_,_=self.step(s);self.assertFalse(f2)
  sell={**self.plan,'weights':{'600031':0}}
  q={**self.q,'asof':'2026-10-21T10:01:00+08:00'}
  _,f3,_,_=self.step(s,sell,q);self.assertFalse(f3)
 def test_next_session_unlock_requires_settlement(self):
  s,_,_,_=self.step();nextday=pd.Timestamp('2026-10-22T10:00:00+08:00');sell={**self.plan,'weights':{'600031':0}}
  _,f,c,_=self.step(s,sell,at=nextday);self.assertFalse(f);self.assertIn('ACCOUNT_UNSETTLED',c)
  s['closed_day']=s['day'];_,f,_,_=self.step(s,sell,q={**self.q,'asof':nextday.isoformat()},at=nextday);self.assertTrue(f)
 def test_no_buy_without_net_advantage(self):
  p=copy.deepcopy(self.plan);p['edges']['600031']['predicted_return']=.001
  _,f,_,b=self.step(p=p);self.assertFalse(f);self.assertIn('BUY_NET_EDGE_BELOW_ACTUAL_COST',b['600031'])
 def test_stale_held_quote_blocks_valuation_and_orders(self):
  s=copy.deepcopy(self.state);s['positions']={'600031':dict(shares=1000,sellable=1000)};s['marks']={'600031':10.};s['cash']=140000
  out,f,c,_=self.step(s,checks={'600031':['QUOTE_STALE_OR_FUTURE']});self.assertFalse(f);self.assertEqual(out['valuation_status'],'STALE_OR_UNVERIFIED');self.assertIn('ACCOUNT_DATA',c)
 def test_visible_book_participation_and_cash(self):
  q={**self.q,'ask_size_lots':100};_,f,_,_=self.step(q=q);self.assertFalse(f) # 100 shares below min ticket
  q['ask_size_lots']=300;s,f,_,_=self.step(q=q);self.assertEqual(f[0]['shares'],300);self.assertGreaterEqual(s['cash'],0)
 def test_turnover_and_fee_budgets_cumulative(self):
  s=copy.deepcopy(self.state);s.update(day='2026-10-21',turnover=150000*.45-1000)
  _,f,_,_=self.step(s);self.assertFalse(f)
  s['turnover']=0;s['fees']=150000*self.c['max_cost_fraction'];_,f,_,_=self.step(s);self.assertFalse(f)
 def test_risk_exit_not_blocked_by_negative_forecast(self):
  s=copy.deepcopy(self.state);s.update(cash=110000.,equity=120000.,positions={'600031':dict(shares=1000,sellable=1000)},marks={'600031':10.})
  _,f,c,_=self.step(s);self.assertTrue(f);self.assertEqual(f[0]['side'],'SELL');self.assertIn('PORTFOLIO_STOP',c)
 def test_notifications_edge_triggered(self):
  s=copy.deepcopy(self.state);queue_alerts(s,{'a':'warning'},[],self.at);queue_alerts(s,{'a':'warning'},[],self.at);self.assertEqual(len(s['notification_queue']),1)
  queue_alerts(s,{},[],self.at);queue_alerts(s,{'a':'warning'},[],self.at);self.assertEqual(len(s['notification_queue']),2)
 def test_quality_rejects_current_source_disagreement_and_actions(self):
  cal=pd.bdate_range('2020-01-01','2026-10-21');ref=dict(date='2026-10-21',symbols={'600031':dict(fetched_at=self.at.isoformat(),limits={'down_limit':9.,'up_limit':11.},basic={'list_date':'20200101','market':'主板'},dividends=[])},suspensions=dict(date='2026-10-21',fetched_at=self.at.isoformat(),complete=True,items=[]))
  self.assertEqual(quality(self.q,self.q,ref,'600031',self.at,cal,'2026-10-20',self.cfg),[])
  self.assertIn('VENDOR_CONFLICT',quality(self.q,{**self.q,'price':10.1},ref,'600031',self.at,cal,'2026-10-20',self.cfg))
  ref['symbols']['600031']['dividends']=[{'ex_date':'20261021'}]
  self.assertIn('CORPORATE_ACTION_RECONCILIATION_REQUIRED',quality(self.q,self.q,ref,'600031',self.at,cal,'2026-10-20',self.cfg))
 def test_poll_idempotence_settlement_and_no_eod_double_trade(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);(root/'data').mkdir();(root/'data/calendar.csv').write_text('date\n2026-10-20\n2026-10-21\n2026-10-22\n');(root/'watchlist.csv').write_text('symbol,name\n600031,三一重工\n')
   (root/'intraday_config.json').write_text(json.dumps(self.cfg));f=root/'reports/active_research/latest';f.mkdir(parents=True)
   source=dict(status='ACTIVE_RESEARCH_PAPER',asof='2026-10-20',valuation_date='2026-10-20',cash=150000.,equity=150000.,positions={},meta=dict(config=self.c,original_initial_equity=150000.),predictions={'forecasts':[dict(symbol='600031',name='三一重工',close=10.)]},plan=self.plan)
   (f/'report.json').write_text(json.dumps(source))
   with patch('asharequant.intraday.now',return_value=self.at),patch('asharequant.intraday.collect',return_value=({'600031':self.q},{'600031':self.q})),patch('asharequant.intraday.quality',return_value=[]),patch('asharequant.intraday.read_watchlist',return_value=pd.DataFrame({'symbol':['600031']})):
    a=run_once(root);b=run_once(root)
   self.assertEqual(b['status'],'ALREADY_PROCESSED');self.assertEqual(a['fills'],1)
   with patch('asharequant.intraday.now',return_value=self.at):
    with self.assertRaisesRegex(ValueError,'CLOSE_NOT_AVAILABLE'):close_day(root,'2026-10-21')
   o=dict(bars={'600031':{'close':10.2}},states={'600031':{'corporate_action_status':'none'}})
   with patch('asharequant.intraday.evidence',return_value=o),patch('asharequant.intraday.now',return_value=pd.Timestamp('2026-10-21T18:00:00+08:00')):
    close_day(root,'2026-10-21');self.assertEqual(close_day(root,'2026-10-21')['status'],'ALREADY_SETTLED')
   with db_open(root) as db:
    self.assertEqual(len(get(db,'fills')),1);nav=get(db,'nav')['2026-10-21'];self.assertGreater(nav['fees'],0);self.assertGreater(nav['daily_pnl'],0)
   self.assertTrue((root/'reports/intraday/latest/NAV.csv').exists())
