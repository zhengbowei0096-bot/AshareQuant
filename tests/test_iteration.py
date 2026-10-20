import json,tempfile,unittest,subprocess
from pathlib import Path
from unittest.mock import patch
import numpy as np
import pandas as pd
from asharequant import iteration as it, lifecycle as lc, code_guard as cg

class IterationTests(unittest.TestCase):
 def test_constant_and_shift_drift(self):
  self.assertEqual(it.drift(np.ones(252),np.ones(60))['psi'],0.)
  d=it.drift(np.ones(252),np.ones(60)*2);self.assertGreater(d['psi'],.25);self.assertLess(d['pvalue'],.01)
 def test_invalid_data_not_healthy(self):
  self.assertEqual(it.drift([1]*200,[1]*60)['status'],'INSUFFICIENT_OR_INVALID')
  self.assertEqual(it.drift([np.nan]*252,[1]*60)['status'],'INSUFFICIENT_OR_INVALID')
 def test_trigger_cooldown(self):
  prior={'at':'2026-01-01T18:00:00+08:00'}
  self.assertIsNone(it.should_research('2026-01-05T18:00:00+08:00',prior,True))
  self.assertEqual(it.should_research('2026-02-05T18:00:00+08:00',prior,True),'PERSISTENT_DRIFT_OR_DEGRADATION')
  self.assertEqual(it.should_research('2026-04-05T18:00:00+08:00',prior,False),'SCHEDULED_90_DAYS')
 def test_empty_and_failed_search_no_candidate(self):
  self.assertIsNone(it.select_candidate({},{} )[1])
  with patch('asharequant.iteration.validate_forecast',side_effect=ValueError('missing')):
   self.assertIsNone(it.select_candidate({('x',1):pd.DataFrame()},{} )[1])
 def test_partial_cannot_promote(self):
  with tempfile.TemporaryDirectory() as d:
   self.assertIsNone(lc.promote(Path(d),{},dict(status='REJECTED_OR_PARTIAL',models={'x':{'passed':True}},portfolio={'passed':False})))
 def test_candidate_must_beat_champion(self):
  rows=[]
  for i in range(40):
   origin=pd.Timestamp('2020-01-01')+pd.Timedelta(days=i*3);actual=.01 if i%2 else -.01
   rows.append(dict(origin=str(origin.date()),target=str((origin+pd.Timedelta(days=1)).date()),actual=actual,predicted=actual*.9,baseline_mean=0.,champion_predicted=actual))
  result=lc.predictive_metrics(rows,lc.POLICY)
  self.assertFalse(result['passed']);self.assertIn('CHAMPION_IMPROVEMENT_NOT_PROVEN',result['blockers'])
 def test_gates_not_lowered(self):
  self.assertGreaterEqual(lc.POLICY['test_origins'],672)
  self.assertGreaterEqual(lc.POLICY['minimum_mae_improvement'],.02)
  self.assertGreaterEqual(lc.POLICY['minimum_portfolio_sessions'],120)

class CodeGuardTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
  (self.root/'asharequant').mkdir();(self.root/'asharequant/demo.py').write_text('VALUE=1\n')
  (self.root/'tests').mkdir();(self.root/'tests/test_demo.py').write_text('import unittest\nclass Test(unittest.TestCase):\n def test_value(self):\n  from asharequant.demo import VALUE\n  self.assertGreater(VALUE,0)\n')
  (self.root/'config.json').write_text('{}')
 def tearDown(self):self.tmp.cleanup()
 def branch(self):
  return Path(cg.create(self.root,'trial')['branch'])
 def test_isolated_branch(self):
  b=self.branch();(b/'asharequant/demo.py').write_text('VALUE=2\n')
  self.assertIn('1',(self.root/'asharequant/demo.py').read_text());self.assertEqual(cg.git(b,'branch','--show-current'),'experiment/trial')
 def test_failed_tests_block_publish(self):
  b=self.branch();(b/'asharequant/demo.py').write_text('VALUE=-1\n');self.assertFalse(cg.validate(self.root,'trial')['passed'])
  with self.assertRaises(ValueError):cg.publish(self.root,'trial')
 def test_test_changes_rejected(self):
  b=self.branch();(b/'tests/test_demo.py').write_text('')
  with self.assertRaises(ValueError):cg.validate(self.root,'trial')
 def test_publish_rollback_and_posttest_tamper(self):
  b=self.branch();(b/'asharequant/demo.py').write_text('VALUE=2\n');self.assertTrue(cg.validate(self.root,'trial')['passed'])
  (b/'asharequant/demo.py').write_text('VALUE=3\n')
  with self.assertRaises(ValueError):cg.publish(self.root,'trial')
  (b/'asharequant/demo.py').write_text('VALUE=2\n');cg.publish(self.root,'trial')
  self.assertEqual((self.root/'asharequant/demo.py').read_text(),'VALUE=2\n');cg.rollback(self.root,'trial')
  self.assertEqual((self.root/'asharequant/demo.py').read_text(),'VALUE=1\n')
 def test_production_change_blocks(self):
  self.branch();(self.root/'asharequant/demo.py').write_text('VALUE=8\n')
  with self.assertRaises(ValueError):cg.validate(self.root,'trial')
 def test_path_escape_rejected(self):
  with self.assertRaises(ValueError):cg.create(self.root,'../escape')

class CandidateFreezeTests(unittest.TestCase):
 def test_selected_features_and_champion_freeze(self):
  from asharequant.core import write_json
  from asharequant.forecast import FEATURES,fit_predict,features_and_labels,DEFAULTS
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);days=pd.bdate_range('2020-01-01',periods=1000)
   at=(days[899]+pd.Timedelta(hours=18)).tz_localize('Asia/Shanghai');prices=100+np.arange(900)*.01+np.sin(np.arange(900)/8)
   bars=pd.DataFrame(dict(date=days[:900],open=prices,close=prices,high=prices+1,low=prices-1,volume=np.ones(900)*1e6))
   snap=dict(raw=bars,hfq=bars,meta=dict(source='tencent',fetched_at=at.isoformat(),version='a'*32))
   write_json(root/'forecast_config.json',{});write_json(root/'decision_config.json',{})
   watch=pd.DataFrame([dict(symbol='600000',name='纯测试',sector='银行')])
   candidate=dict(alpha=100.,features=FEATURES[:-1],reason='SYNTHETIC_TEST_ONLY')
   with patch.object(lc,'now',return_value=at.to_pydatetime()),patch.object(lc,'_sessions',return_value=days),patch.object(lc,'read_watchlist',return_value=watch),patch.object(lc,'read_snapshot',return_value=snap):
    spec=lc.prepare(root,candidate)
    self.assertGreater(pd.Timestamp(spec['start_session']),days[899])
    self.assertFalse((root/'data/lifecycle/current.json').exists())
    artifact=json.loads((root/spec['artifacts']['600000/1']['path']).read_text())
    self.assertEqual(artifact['parameters']['coefficients'][-1],0.)
    self.assertEqual(spec['champions']['600000/1']['status'],'RESEARCH_BASELINE_NO_CHAMPION')
    self.assertEqual(lc.prepare(root,candidate)['experiment'],spec['experiment'])
