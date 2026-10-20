import unittest, tempfile, json, sqlite3
from pathlib import Path
from asharequant.model_monitor import evaluate, DEFAULTS, run, permitted, digest

class MonitorTests(unittest.TestCase):
 def rows(self,n=24,pred=3.,baseline=1.):
  import pandas as pd
  days=pd.date_range('2020-01-01',periods=n+1)
  return [dict(origin=str(days[i].date()),target=str(days[i+1].date()),actual=0.,prediction=pred,baseline=baseline) for i in range(n)]
 def test_two_windows_required(self):
  self.assertEqual(evaluate(self.rows(23),False,DEFAULTS)['status'],'insufficient')
  self.assertEqual(evaluate(self.rows(),False,DEFAULTS)['status'],'degraded')
 def test_one_bad_window_not_sustained(self):
  r=self.rows()
  for x in r[:12]:x['prediction']=.1
  self.assertEqual(evaluate(r,False,DEFAULTS)['status'],'healthy')
 def test_overlapping_horizons_do_not_inflate_evidence(self):
  r=self.rows()
  for x in r:x['target']='2021-01-01'
  self.assertEqual(evaluate(r,False,DEFAULTS)['independent_samples'],1)
 def test_bias_and_direction(self):
  m=evaluate(self.rows(),False,DEFAULTS)['metrics']
  self.assertEqual(m['bias'],3.);self.assertEqual(m['direction_accuracy'],0.)
 def test_invalid_risk(self):
  self.assertEqual(evaluate(self.rows(pred=-1),True,DEFAULTS)['status'],'invalid')
 def test_empty(self):self.assertEqual(evaluate([],False,DEFAULTS)['samples'],0)
 def test_qlike_detects_underestimated_risk(self):
  r=self.rows(pred=.01,baseline=1.)
  for x in r:x['actual']=1.
  self.assertEqual(evaluate(r,True,DEFAULTS)['status'],'degraded')
 def fixture(self,root,pred=.01):
  (root/'data').mkdir();db=sqlite3.connect(root/'data/market.sqlite3')
  db.execute('CREATE TABLE model_shadow(spec,model,symbol,horizon,origin_date,target_date,issued_at,predicted,champion_predicted,actual)')
  for x in self.rows(pred=pred):db.execute('INSERT INTO model_shadow VALUES(?,?,?,?,?,?,?,?,?,?)',('s','lasso','600031',1,x['origin'],x['target'],x['origin']+'T16:00:00+08:00',pred,1.,0.))
  db.commit();db.close()
  r={'spec_id':'s','evaluated_asof':'2020-02-01','active':{'600031/1':{'model':'lasso','historical_passed':True,'prospective_passed':True}}}
  (root/'data/model_registry.json').write_text(json.dumps(r));return r
 def test_activate_rollback_quarantine_idempotent(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);r=self.fixture(root);identity='return|s|lasso|600031|1'
   a=run(root);self.assertTrue(permitted(root,'return',r,identity));self.assertEqual(len(a['events']),1)
   db=sqlite3.connect(root/'data/market.sqlite3');db.execute('UPDATE model_shadow SET predicted=3');db.commit();db.close()
   b=run(root);self.assertIn(identity,b['blocked']);self.assertFalse(permitted(root,'return',r,identity))
   self.assertEqual(len(run(root)['events']),len(b['events']))
   db=sqlite3.connect(root/'data/market.sqlite3');db.execute('UPDATE model_shadow SET predicted=.01');db.commit();db.close()
   self.assertNotIn(identity,run(root)['active'])
 def test_future_and_late_issued_excluded(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);self.fixture(root)
   db=sqlite3.connect(root/'data/market.sqlite3');db.execute("UPDATE model_shadow SET issued_at=target_date||'T16:00:00+08:00'");db.commit();db.close()
   a=run(root);self.assertEqual(next(iter(a['groups'].values()))['samples'],0);self.assertEqual(a['active'],[])
 def test_registry_change_fails_closed(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);r=self.fixture(root);run(root);r['evaluated_asof']='2020-02-02'
   self.assertFalse(permitted(root,'return',r,'return|s|lasso|600031|1'))
 def test_original_gates_cannot_be_bypassed(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);r=self.fixture(root);r['active']['600031/1']['prospective_passed']=False
   (root/'data/model_registry.json').write_text(json.dumps(r));self.assertEqual(run(root)['active'],[])
 def test_risk_spec_identity_and_application_gate(self):
  from asharequant.model_monitor import entries
  from asharequant.risk_models import apply, fingerprint
  r={'spec_id':'risk-spec','implementation_hash':fingerprint(),'asof':'2020-02-01','approved':[{'kind':'garch','symbol':'600031','horizon':1,'historical_passed':True,'forward_passed':True}],'latest':[{'kind':'garch','symbol':'600031','horizon':1,'candidate_variance':.16}]}
  identity=entries('risk',r)[0][0]
  self.assertEqual(identity,'risk|risk-spec|garch|600031|1')
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);(root/'data').mkdir();(root/'data/risk_model_registry.json').write_text(json.dumps(r))
   cov,_,status=apply(root,'2020-02-01',1,[[.04]],['600031'],{},{});self.assertEqual(status['used'],[])
   (root/'data/model_monitor.json').write_text(json.dumps({'registry_hashes':{'risk':digest(r)},'active':[identity],'blocked':{}}))
   cov,_,status=apply(root,'2020-02-01',1,[[.04]],['600031'],{},{});self.assertAlmostEqual(cov[0,0],.16);self.assertEqual(status['used'],['garch'])
