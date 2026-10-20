import copy,json,sqlite3,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
import pandas as pd
from asharequant.research_policy import POLICY,targets
from asharequant.research_execution import simulate
from asharequant.decision import DEFAULTS
from asharequant.paper_account import put,get

class ResearchPolicyTests(unittest.TestCase):
 def setUp(self):
  self.c={**DEFAULTS,'fees_verified':True,'fee_source':'SYNTHETIC_TEST','max_volume_participation':.01,'require_fill_in_range':True}
  f=dict(predicted_return=.08,volatility_horizon=.08,downside_es95_normal_proxy=-.08,model_uncertainty={'std':.005},interval80_empirical=[-.05,.15],up_probability_empirical=.7)
  self.rows=[dict(symbol=s,name=s,sector=str(i),close=10.,price_source='SYNTHETIC_TEST',composite_score={'score':80},data_eligible=True,forecasts={'20':copy.deepcopy(f)}) for i,s in enumerate(['000681','600031'])]
  self.payload={'asof':'2026-10-20','forecasts':self.rows};self.cov=np.diag([.0064,.0064])
 def test_weak_market_does_not_force_all_cash(self):
  w,rows=targets(self.payload,{},150000,self.cov,self.c,'risk_off');self.assertGreater(sum(w.values()),0);self.assertLessEqual(sum(w.values()),.30000001);self.assertTrue(all(x<=.15 for x in w.values()))
 def test_exposure_order_and_individual_risk(self):
  totals=[sum(targets(self.payload,{},150000,self.cov,self.c,e)[0].values()) for e in ['risk_off','neutral','risk_on']]
  self.assertEqual(totals,sorted(totals))
  self.rows[0]['forecasts']['20']['downside_es95_normal_proxy']=-.4
  w,r=targets(self.payload,{},150000,self.cov,self.c,'risk_off');self.assertEqual(w['000681'],0);self.assertGreater(w['600031'],0)
 def test_unknown_environment_never_produces_liquidation(self):
  with self.assertRaisesRegex(ValueError,'UNKNOWN_HOLD'):targets(self.payload,{'000681':{'shares':1000}},150000,self.cov,self.c,'unknown')
 def test_missing_uncertainty_blocks_plan_not_liquidates(self):
  self.rows[0]['forecasts']['20']['model_uncertainty']['std']=None
  with self.assertRaisesRegex(ValueError,'MISSING_HOLD'):targets(self.payload,{},150000,self.cov,self.c,'neutral')
 def test_cost_and_uncertainty_can_justify_cash(self):
  for row in self.rows:row['forecasts']['20']['predicted_return']=.0001
  self.assertEqual(sum(targets(self.payload,{},150000,self.cov,self.c,'risk_on')[0].values()),0)
 def test_lower_probability_reduces_utility(self):
  w,a=targets(self.payload,{},150000,self.cov,self.c,'neutral');self.rows[0]['forecasts']['20']['up_probability_empirical']=.3
  _,b=targets(self.payload,{},150000,self.cov,self.c,'neutral');self.assertLess(b[0]['effective_utility'],a[0]['effective_utility'])
 def test_covariance_and_sector_constraints(self):
  self.rows[1]['sector']='0';w,r=targets(self.payload,{},150000,self.cov,self.c,'risk_on');self.assertLessEqual(sum(w.values()),.25000001)
  with self.assertRaisesRegex(ValueError,'COVARIANCE'):targets(self.payload,{},150000,np.array([[1,2],[2,1]]),self.c,'neutral')
 def test_risk_priority_records_concentration_and_downside(self):
  pos={'600031':{'shares':5000},'000681':{'shares':1000}}
  _,r=targets(self.payload,pos,150000,self.cov,self.c,'neutral');self.assertGreater(r[1]['risk_priority'],r[0]['risk_priority']);self.assertGreater(r[1]['risk_components']['stock_excess'],0)
 def execute(self,halt=False):
  days=pd.DatetimeIndex(['2026-10-20','2026-10-21']);bars={s:pd.DataFrame([{'open':10.,'close':10.,'high':10.5,'low':9.5,'volume':1000000}],index=days[1:]) for s in ['000681','600031']}
  states={(s,'2026-10-21'):dict(source='SYNTHETIC_TEST',status='trading',corporate_action_status='none',limit_down=9.,limit_up=11.) for s in bars}
  if halt:states['600031','2026-10-21']['status']='suspended'
  return simulate(days,bars,{'2026-10-20':{'000681':0,'600031':0}},states,self.c,0,{s:{'shares':1000,'sellable':1000} for s in bars},{s:10 for s in bars},priorities={'2026-10-20':{'600031':10,'000681':1}})
 def test_actual_fill_is_risk_first_not_code_first(self):
  r=self.execute();self.assertEqual(r['fills'][0]['symbol'],'600031');self.assertLessEqual(r['ledger'][-1]['turnover'],.3);self.assertGreater(r['fills'][0]['fee'],0)
 def test_halt_still_blocks_highest_priority(self):
  r=self.execute(True);self.assertEqual(r['fills'][0]['symbol'],'000681');self.assertTrue(r['blocked'])
 def test_preopen_revision_rejects_after_open_and_settled_data(self):
  from asharequant.research_paper import activate_policy
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);(root/'data').mkdir()
   with sqlite3.connect(root/'data/research_paper.sqlite3') as db:
    db.execute('CREATE TABLE records(kind TEXT,key TEXT,body TEXT,sha256 TEXT,PRIMARY KEY(kind,key))')
    put(db,'spec','active',{'implementation':'old'});put(db,'meta','account',{'start_session':'2026-10-21','anchor_session':'2026-10-20'})
   with patch('asharequant.research_paper.now',return_value=pd.Timestamp('2026-10-21T10:00:00+08:00')),patch('asharequant.research_paper.load_config',return_value={}),patch('asharequant.data.calendar',return_value=(pd.DatetimeIndex(['2026-10-20','2026-10-21']),{})):
    with self.assertRaisesRegex(ValueError,'AFTER_OPEN'):activate_policy(root,'old','user test authorization')
   with sqlite3.connect(root/'data/research_paper.sqlite3') as db:
    self.assertEqual(get(db,'spec_revisions'),{});put(db,'nav','2026-10-21',{'equity':1})
   with patch('asharequant.research_paper.load_config',return_value={}),patch('asharequant.data.calendar',return_value=(pd.DatetimeIndex(['2026-10-20']),{})):
    with self.assertRaisesRegex(ValueError,'SETTLED'):activate_policy(root,'old','user test authorization')
 def test_replay_uses_frozen_priority_and_reproduces_nav(self):
  from asharequant.research_execution import replay
  days=pd.DatetimeIndex(['2026-10-20','2026-10-21']);symbols=['000681','600031']
  meta=dict(anchor_session='2026-10-20',start_session='2026-10-21',initial_cash=0.,initial_equity=20000.,initial_positions={s:{'shares':1000,'sellable':1000} for s in symbols},initial_marks={s:10. for s in symbols},config=self.c)
  plans={'2026-10-21':{'origin_session':'2026-10-20','weights':{s:0. for s in symbols},'risk_priorities':{'000681':1.,'600031':9.}}}
  observations={'2026-10-21':{'bars':{s:dict(open=10.,high=10.5,low=9.5,close=10.,volume=1000000) for s in symbols},'states':{s:dict(status='trading',source='SYNTHETIC_TEST',corporate_action_status='none',limit_down=9.,limit_up=11.) for s in symbols}}}
  a=replay(meta,days,plans,observations);b=replay(meta,days,plans,observations)
  self.assertEqual(a,b);self.assertEqual(a['fills'][0]['symbol'],'600031')
 def test_actual_buy_cost_cannot_overrun_turnover_after_slippage(self):
  days=pd.DatetimeIndex(['2026-10-20','2026-10-21']);bars={'600031':pd.DataFrame([dict(open=10.,high=11.,low=9.,close=10.,volume=1000000)],index=days[1:])}
  states={('600031','2026-10-21'):dict(status='trading',source='SYNTHETIC_TEST',corporate_action_status='none',limit_down=9.,limit_up=11.)}
  r=simulate(days,bars,{'2026-10-20':{'600031':.15}},states,{**self.c,'max_turnover':.15},100000.)
  self.assertTrue(r['fills']);self.assertLessEqual(r['ledger'][-1]['turnover'],.15)
