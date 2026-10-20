"""Synthetic fixtures; no test values are real investment signals."""
import unittest,copy
from asharequant.risk_control import build
from asharequant.decision import DEFAULTS
from asharequant.probability_display import fields,text
class IndependentRiskTests(unittest.TestCase):
 def setUp(self):
  self.p={'600031':dict(shares=10000,sellable=10000),'000681':dict(shares=5000,sellable=5000)}
  self.b={s:dict(close=10.,volume=1000000,source='SYNTHETIC_TEST_ONLY') for s in self.p}
  self.states={s:dict(status='trading',corporate_action_status='none',ordinary_session=True,source='SYNTHETIC_TEST_ONLY',limit_down=9.,limit_up=11.) for s in self.p}
  self.c={**DEFAULTS,'fees_verified':True,'fee_source':'SYNTHETIC_TEST_ONLY'};self.sectors={s:'TEST_SECTOR' for s in self.p}
 def run_plan(self):return build(self.p,500,self.b,self.states,self.sectors,self.c)
 def test_risk_reduces_without_model_probability_or_covariance(self):
  before=copy.deepcopy(self.p);r=self.run_plan();self.assertTrue(any(x['candidate_sell_shares']>0 for x in r['rows']));self.assertEqual(self.p,before);self.assertEqual(r['submitted_orders'],0);self.assertFalse(r['ledger_mutated']);self.assertLessEqual(r['reference_turnover'],self.c['max_turnover']);self.assertLessEqual(r['estimated_fees'],r['equity']*self.c['max_cost_fraction']);self.assertTrue(r['remaining_risk'])
  for x in r['rows']:self.assertLessEqual(x['scenario_remaining_shares'],x['current_shares']);self.assertLessEqual(x['risk_cap_weight'],self.c['max_stock'])
  self.assertLessEqual(sum(x['risk_cap_weight'] for x in r['rows']),self.c['max_sector'])
 def test_suspended_limit_unknown_corporate_and_zero_volume_block(self):
  for change in ('suspension','limit','corporate','volume'):
   self.setUp()
   if change=='suspension':self.states['600031']['status']='suspended'
   if change=='limit':self.states['600031']['limit_down']=10.
   if change=='corporate':self.states['600031']['corporate_action_status']='event'
   if change=='volume':self.b['600031']['volume']=0
   r=self.run_plan();x=next(x for x in r['rows'] if x['symbol']=='600031');self.assertEqual(x['candidate_sell_shares'],0);self.assertTrue(x['blockers'])
 def test_sellable_and_volume_caps(self):
  self.p['600031']['sellable']=100;self.b['000681']['volume']=10000
  for x in self.run_plan()['rows']:self.assertLessEqual(x['candidate_sell_shares'],100)
 def test_missing_price_and_unverified_fees_rejected(self):
  self.b.pop('600031')
  with self.assertRaises(ValueError):self.run_plan()
  self.setUp();self.c['fees_verified']=False
  with self.assertRaises(ValueError):self.run_plan()
 def test_in_cap_holdings_no_forced_trade(self):
  r=build({'600031':dict(shares=100,sellable=100)},100000,self.b,self.states,self.sectors,self.c);self.assertEqual(r['rows'][0]['candidate_sell_shares'],0);self.assertEqual(r['rows'][0]['action'],'HOLD')
 def test_unvalidated_probability_visible_with_reasons(self):
  f=dict(up_probability_empirical=.6,up_probability_calibrated=.52,probability_calibration=dict(independent_validated=False,status='CALIBRATED_NOT_VALIDATED',holdout_blocks=5,blockers=['INDEPENDENT_HOLDOUT_BLOCKS_BELOW_30']))
  r=fields(f);self.assertEqual(r['research_up_probability'],.52);self.assertFalse(r['probability_validated']);self.assertIn('52.00%',text(r));self.assertIn('不足30',text(r))
 def test_missing_or_invalid_probability_not_invented(self):
  for f in ({},{'up_probability_empirical':float('nan')},{'up_probability_empirical':1.5}):self.assertIsNone(fields(f)['research_up_probability'])
 def test_validated_probability_labeled(self):
  r=fields(dict(up_probability_calibrated=.6,probability_calibration={'independent_validated':True,'status':'VALIDATED'}));self.assertTrue(r['probability_validated']);self.assertIn('独立验证通过',text(r))
 def test_current_holdings_not_misreported_as_target(self):
  import numpy as np
  from asharequant.decision import build_plan
  from test_decision import row,cfg,account,AT
  r=build_plan([row()],np.array([[.04]]),cfg(),account({'600000':dict(shares=100,sellable=100)},10000),AT)
  x=r['decisions'][0];self.assertGreater(x['current_weight'],0);self.assertEqual(x['current_amount'],1000);self.assertIsNone(x['target_weight']);self.assertIsNone(x['target_amount']);self.assertEqual(x['action'],'NO_TRADE')
 def test_daily_paper_cli_also_generates_risk_plan(self):
  import tempfile
  from unittest.mock import patch
  from pathlib import Path
  from asharequant.cli import main
  r=dict(status='SETTLED',start_session='2026-10-20',equity=100,cash=100,daily_return=0,cumulative_return=0,alerts=[],submitted_orders=0)
  with tempfile.TemporaryDirectory() as d,patch('asharequant.paper_account.run',return_value=r),patch('asharequant.risk_control.run',return_value={}) as risk:
   self.assertEqual(main(['paper-account','--root',d]),0);risk.assert_called_once_with(Path(d).resolve())
