import copy,json,sqlite3,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
import pandas as pd
from asharequant.active_execution import buy_edge,simulate,replay
from asharequant.decision import DEFAULTS
from asharequant.active_research import run
from asharequant.research_policy import POLICY
from asharequant.paper_account import get

class ActiveResearchTests(unittest.TestCase):
 def setUp(self):
  self.c={**DEFAULTS,'fees_verified':True,'commission_bps':1.51,'minimum_commission':5.,'slippage_bps':5.,'max_volume_participation':.01,'require_fill_in_range':True}
  self.edge=dict(reference_price=10.,predicted_return=.02,uncertainty=.005)
 def test_actual_small_ticket_fee_can_block(self):
  edge={**self.edge,'predicted_return':.008,'uncertainty':0.}
  self.assertFalse(buy_edge(edge,100,10.,10.01,self.c,'600031')['allowed'])
  self.assertTrue(buy_edge(edge,1000,10.,10.01,self.c,'600031')['allowed'])
 def test_gap_up_consumes_expected_advantage(self):
  self.assertTrue(buy_edge(self.edge,1000,10.,10.01,self.c,'600031')['allowed'])
  self.assertFalse(buy_edge(self.edge,1000,10.3,10.31,self.c,'600031')['allowed'])
 def test_missing_uncertainty_blocks_buys(self):
  self.assertFalse(buy_edge(None,1000,10.,10.01,self.c,'600031')['allowed'])
 def sim(self,edge=None,sell=False,halt=False):
  days=pd.DatetimeIndex(['2026-10-20','2026-10-21']);bars={'600031':pd.DataFrame([dict(open=10.,close=10.,high=11.,low=9.,volume=1000000)],index=days[1:])}
  states={('600031','2026-10-21'):dict(source='SYNTHETIC_TEST',status='suspended' if halt else 'trading',corporate_action_status='none',limit_down=9.,limit_up=11.)}
  return simulate(days,bars,{'2026-10-20':{'600031':0. if sell else .15}},states,self.c,140000. if sell else 150000.,{'600031':dict(shares=1000,sellable=1000)} if sell else {},{'600031':10.},edges={'2026-10-20':{'600031':edge}})
 def test_execution_rechecks_quantity_and_records_gate(self):
  good=self.sim(self.edge);self.assertTrue(good['fills']);self.assertTrue(good['fills'][0]['edge_check']['allowed'])
  bad=self.sim({**self.edge,'predicted_return':.001});self.assertFalse(bad['fills']);self.assertIn('BUY_NET_EDGE_BELOW_ACTUAL_COST',bad['blocked'])
 def test_risk_sell_not_blocked_by_negative_edge(self):
  r=self.sim(None,True);self.assertTrue(r['fills']);self.assertEqual(r['fills'][0]['side'],'SELL')
 def test_halt_still_blocks(self):self.assertFalse(self.sim(self.edge,False,True)['fills'])
 def test_original_execution_unchanged_when_gate_passes(self):
  from asharequant.research_execution import simulate as original
  days=pd.DatetimeIndex(['2026-10-20','2026-10-21']);bars={'600031':pd.DataFrame([dict(open=10.,close=10.,high=11.,low=9.,volume=1000000)],index=days[1:])};states={('600031','2026-10-21'):dict(source='SYNTHETIC_TEST',status='trading',corporate_action_status='none',limit_down=9.,limit_up=11.)}
  old=original(days,bars,{'2026-10-20':{'600031':.15}},states,self.c,150000.,{}, {'600031':10.})
  new=self.sim(self.edge);self.assertEqual(new['ledger'],old['ledger'])
  self.assertEqual([{k:v for k,v in f.items() if k!='edge_check'} for f in new['fills']],old['fills'])
 def test_active_two_day_freeze_settle_idempotence_and_ui(self):
  settings=dict(enabled=True,mode='SHADOW_PAPER_ONLY',risk_multipliers=[1.,1.25,1.5],threshold_multipliers=[1.,.9,.8],maximum_drawdown=.15,minimum_equity=120000,minimum_sessions=252,rolling_window=63,minimum_trades=20,auto_promote=False)
  f=dict(predicted_return=.08,volatility_horizon=.08,downside_es95_normal_proxy=-.08,model_uncertainty={'std':.005},interval80_empirical=[-.05,.15],up_probability_empirical=.7)
  payload=dict(model='SYNTHETIC_TEST',asof='2026-10-20',generated_at='2026-10-20T16:00:00+08:00',seven_metrics=[dict(symbol='600031',horizon=20,composite_rank=1)],forecasts=[dict(symbol='600031',name='测试',sector='机械',close=10.,price_source='SYNTHETIC_TEST',composite_score={'score':80},data_eligible=True,forecasts={'20':f})])
  dates=pd.bdate_range(end='2026-10-20',periods=260);hist=pd.DataFrame({'date':dates,'close':10+np.sin(np.arange(260))*.05})
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory);(root/'data').mkdir();folder=root/'reports/research_paper/latest';folder.mkdir(parents=True)
   (root/'active_research_config.json').write_text(json.dumps(dict(enabled=True,variant='risk1.5_threshold1',mode='RESEARCH_PAPER_ONLY',formal_promotion=False,authorization='SYNTHETIC_TEST')))
   (root/'strategy_experiment_config.json').write_text(json.dumps(settings));(root/'data/calendar.csv').write_text('date\n2026-10-19\n2026-10-20\n2026-10-21\n2026-10-22\n')
   source=dict(status='ACTIVE_RESEARCH_PAPER',asof='2026-10-20',valuation_date='2026-10-20',positions={'600031':{'shares':1000,'sellable':1000}},equity=150000.,cash=140000.,meta={'config':self.c,'original_initial_equity':150000.},spec={'version':'SYNTHETIC_TEST','policy':POLICY},predictions=payload,plan={'target_session':'2026-10-21','origin_session':'2026-10-20','environment':'risk_off'})
   (folder/'report.json').write_text(json.dumps(source));(folder/'index.html').write_text('<h1>baseline</h1>')
   observation=dict(bars={'600031':dict(open=10.,high=11.,low=9.,close=10.,volume=1000000)},states={'600031':dict(status='trading',source='SYNTHETIC_TEST',corporate_action_status='none',limit_down=9.,limit_up=11.)})
   with patch('asharequant.active_research.read_snapshot',return_value={'hfq':hist}),patch('asharequant.active_research.now',return_value=pd.Timestamp('2026-10-20T18:00:00+08:00')),patch('asharequant.active_research.evidence',return_value=observation):
    first=run(root);self.assertEqual(first['fills'],[]);self.assertAlmostEqual(first['meta']['config']['max_stock'],.225);self.assertAlmostEqual(first['meta']['config']['max_turnover'],.45)
    self.assertEqual(first['predictions'],payload)
   from asharequant.report_navigation import primary_section
   self.assertIn('active_research/latest',primary_section(root));self.assertIn('对照', (folder/'index.html').read_text())
   source['asof']=source['valuation_date']=payload['asof']='2026-10-21';payload['generated_at']='2026-10-21T16:00:00+08:00';source['plan'].update(origin_session='2026-10-21',target_session='2026-10-22');(folder/'report.json').write_text(json.dumps(source))
   with patch('asharequant.active_research.read_snapshot',return_value={'hfq':hist}),patch('asharequant.active_research.now',return_value=pd.Timestamp('2026-10-21T18:00:00+08:00')),patch('asharequant.active_research.evidence',return_value=observation):
    second=run(root)
    with sqlite3.connect(root/'data/active_research.sqlite3') as db:before=db.execute('select * from records order by kind,key').fetchall()
    run(root)
    with sqlite3.connect(root/'data/active_research.sqlite3') as db:after=db.execute('select * from records order by kind,key').fetchall()
   self.assertEqual(before,after);self.assertEqual(len(second['ledger']),1);self.assertTrue(second['fills']);self.assertTrue(second['fills'][0]['edge_check']['allowed'])
   self.assertEqual(second['submitted_orders'],0)
