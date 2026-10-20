"""Synthetic fixtures only: never real trading signals."""
import copy,json,tempfile,unittest,sqlite3
from pathlib import Path
from unittest.mock import patch
import pandas as pd
from asharequant import paper_account as pa
from asharequant.execution_replay import simulate
from asharequant.decision import DEFAULTS

CAL=pd.DatetimeIndex(['2026-10-19','2026-10-20','2026-10-21','2026-10-22'])
AT=pd.Timestamp('2026-10-20T08:00:00+08:00').to_pydatetime()
def source():
 return dict(plan_session='2026-10-20',source_forecast_asof='2026-10-19',generated_at='2026-10-19T18:00:00+08:00',plan_calendar_verified=True,formal_method='equal_weight',production_admission={'status':'VALIDATED'},blockers=['ACCOUNT_STALE_OR_UNVERIFIED'],decisions=[dict(symbol='600000',sector='银行',target_weight=.1,probability_calibrated=True,target_kind='RESEARCH_TARGET',blockers=['ACCOUNT_STALE_OR_UNVERIFIED','OUTSIDE_SUPPORTED_CONTINUOUS_SESSION'])])
def evidence(root,day,symbols):
 return dict(session=day,bars={s:dict(open=10.,high=12.,low=9.,close=11.,volume=1000000.,source='SYNTHETIC_TEST_ONLY') for s in symbols},states={s:dict(source='SYNTHETIC_TEST_ONLY',status='trading',corporate_action_status='none',limit_down=9.,limit_up=12.) for s in symbols},proof={'synthetic':True})
class PaperAccountTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);(self.root/'decision_config.json').write_text('{}');(self.root/'reports/decisions/latest').mkdir(parents=True);self.path=self.root/'reports/decisions/latest/decision.json';self.path.write_text(json.dumps(source()))
  (self.root/'data').mkdir();(self.root/'data/decision_account.json').write_text('REAL_ACCOUNT_SENTINEL')
 def tearDown(self):self.tmp.cleanup()
 def run_at(self,at,fail=False):
  def fees(root,c):c.update(fees_verified=True,fee_source='SYNTHETIC_TEST_ONLY');return c
  with patch.object(pa,'now',return_value=pd.Timestamp(at).to_pydatetime()),patch.object(pa,'load_config',return_value={}),patch('asharequant.data.calendar',return_value=(CAL,{})),patch.object(pa,'load_fee_config',side_effect=fees),patch.object(pa,'evidence',side_effect=ValueError('MISSING') if fail else evidence):return pa.run(self.root)
 def test_initialization_does_not_backfill_or_touch_real_account(self):
  r=self.run_at(AT);self.assertEqual(r['cash'],150000);self.assertEqual(r['positions'],{});self.assertEqual(r['ledger'],[]);self.assertIsNone(r['daily_return']);self.assertEqual(r['start_session'],'2026-10-20');self.assertEqual((self.root/'data/decision_account.json').read_text(),'REAL_ACCOUNT_SENTINEL')
 def test_next_close_fill_fees_and_repeat_idempotency(self):
  self.run_at(AT);a=self.run_at('2026-10-20T18:00:00+08:00');b=self.run_at('2026-10-20T18:01:00+08:00')
  self.assertEqual(a['ledger'],b['ledger']);self.assertEqual(len(a['fills']),1);self.assertGreater(a['fills'][0]['fee'],0);self.assertEqual(a['fills'][0]['shares']%100,0);self.assertGreater(a['equity'],150000);self.assertEqual(a['submitted_orders'],0)
 def test_evidence_failure_does_not_invent_zero_return(self):
  self.run_at(AT);r=self.run_at('2026-10-20T18:00:00+08:00',True);self.assertEqual(r['status'],'BLOCKED_DATA');self.assertIsNone(r['daily_return']);self.assertEqual(r['ledger'],[])
 def test_after_open_first_run_begins_next_session(self):
  r=self.run_at('2026-10-20T10:00:00+08:00');self.assertEqual(r['start_session'],'2026-10-21');self.assertEqual(r['plans'],{})
 def test_unqualified_strategy_holds_cash_with_real_zero_return(self):
  s=source();s['formal_method']=None;s['production_admission']['status']='BLOCKED';self.path.write_text(json.dumps(s))
  self.run_at(AT);r=self.run_at('2026-10-20T18:00:00+08:00');self.assertEqual(r['fills'],[]);self.assertEqual(r['daily_return'],0);self.assertEqual(r['equity'],150000)
 def test_frozen_plan_not_rewritten(self):
  a=self.run_at(AT);s=source();s['decisions'][0]['target_weight']=.05;self.path.write_text(json.dumps(s));b=self.run_at('2026-10-20T08:10:00+08:00');self.assertEqual(a['plans'],b['plans'])
 def test_missing_plan_never_backfills_trades(self):
  self.path.unlink();self.run_at(AT);r=self.run_at('2026-10-20T18:00:00+08:00');self.assertEqual(r['fills'],[]);self.assertIn('NO_PREOPEN_PLAN:2026-10-20',r['alerts'])
 def test_tamper_detected(self):
  self.run_at(AT)
  with sqlite3.connect(self.root/'data/paper_account.sqlite3') as db:db.execute("UPDATE records SET body='{}' WHERE kind='plans'")
  with self.assertRaisesRegex(ValueError,'TAMPERED'):self.run_at('2026-10-20T08:10:00+08:00')
 def test_limits_and_daily_range_block_fills(self):
  for change in ('limit','range','volume'):
   o=evidence(None,'2026-10-20',{'600000'})
   if change=='limit':o['states']['600000']['limit_up']=10.
   if change=='range':o['bars']['600000']['high']=10.
   if change=='volume':o['bars']['600000']['volume']=0.
   meta=dict(anchor_session='2026-10-19',start_session='2026-10-20',config={**DEFAULTS,'max_volume_participation':.01,'require_fill_in_range':True})
   p={'2026-10-20':dict(origin_session='2026-10-19',weights={'600000':.1})}
   r=pa.replay(meta,CAL,p,{'2026-10-20':o});self.assertEqual(r['fills'],[]);self.assertTrue(r['blocked'])
 def test_volume_participation_caps_quantity(self):
  o=evidence(None,'2026-10-20',{'600000'});o['bars']['600000']['volume']=10000
  meta=dict(anchor_session='2026-10-19',start_session='2026-10-20',config={**DEFAULTS,'minimum_trade_value':0.,'max_volume_participation':.01,'require_fill_in_range':True})
  p={'2026-10-20':dict(origin_session='2026-10-19',weights={'600000':.1})}
  r=pa.replay(meta,CAL,p,{'2026-10-20':o});self.assertEqual(r['fills'][0]['shares'],100)
 def test_real_holding_not_required_but_other_model_gates_kept(self):
  m={'config':{**DEFAULTS,'fees_verified':True}};s=source();r=pa.freeze_plan(s,AT,CAL,m);self.assertEqual(r['weights'],{'600000':.1})
  s['decisions'][0]['probability_calibrated']=False;r=pa.freeze_plan(s,AT,CAL,m);self.assertEqual(r['weights'],{})

 def test_future_dated_plan_rejected(self):
  s=source();s['generated_at']='2026-10-20T08:05:00+08:00';p=pa.freeze_plan(s,AT,CAL,{'config':{**DEFAULTS,'fees_verified':True}})
  self.assertEqual(p['weights'],{});self.assertIn('FUTURE_SOURCE_PLAN',p['blockers'])
 def test_daily_pipeline_runs_paper_and_stops_on_failure(self):
  from asharequant.daily import run
  import shutil
  shutil.copy(Path(__file__).resolve().parents[1]/'config.json',self.root/'config.json')
  calls=[]
  def execute(command):calls.append(command);return 2 if command=='paper-account' else 0
  with patch('asharequant.daily.calendar',return_value=(CAL,{})):
   r=run(self.root,pd.Timestamp('2026-10-20T18:00:00+08:00').to_pydatetime(),execute)
  self.assertEqual(calls,['history-refresh','run','paper-account']);self.assertEqual(r['status'],'FAILED')
 def test_seeded_holdings_valuation_and_no_fake_purchase(self):
  seed=dict(cash=1000.0,source='SYNTHETIC_TEST_ONLY',positions={'600000':dict(shares=1000,sellable=1000,reference_price=10.)})
  (self.root/'paper_account_seed.json').write_text(json.dumps(seed))
  self.path.unlink();r=self.run_at(AT)
  self.assertEqual(r['equity'],11000.0);self.assertEqual(r['cash'],1000.0);self.assertEqual(r['positions']['600000']['shares'],1000);self.assertEqual(r['fills'],[])
  a=self.run_at('2026-10-20T18:00:00+08:00');b=self.run_at('2026-10-20T18:01:00+08:00')
  self.assertEqual(a['equity'],12000.0);self.assertAlmostEqual(a['daily_return'],1000/11000.0);self.assertEqual(a['fills'],[]);self.assertEqual(a['ledger'],b['ledger'])
 def test_seed_holdings_missing_evidence_blocks_valuation(self):
  (self.root/'paper_account_seed.json').write_text(json.dumps(dict(cash=1000.0,source='SYNTHETIC_TEST_ONLY',positions={'600000':dict(shares=1000,sellable=1000,reference_price=10.)})))
  self.run_at(AT);r=self.run_at('2026-10-20T18:00:00+08:00',True)
  self.assertEqual(r['status'],'BLOCKED_DATA');self.assertIsNone(r['daily_return']);self.assertEqual(r['equity'],11000.0)
