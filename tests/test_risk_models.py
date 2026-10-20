import unittest,numpy as np,pandas as pd
from asharequant.risk_models import garch_step,filter_step,rescale_covariance,outcomes,trend_states,policy_metrics,qlike

class RiskTests(unittest.TestCase):
 def test_garch_units_and_multistep_recurrence(self):
  s={'omega':.00001,'alpha':.1,'beta':.8,'last_variance':.0001,'last_return':.01}
  state,v=garch_step(s,.02)
  self.assertAlmostEqual(state['last_variance'],.0001)
  self.assertAlmostEqual(v[0],.00013)
  self.assertTrue(np.all(np.diff(v)>0))
  self.assertAlmostEqual(v[1],.00013+.00001+.9*.00013)
 def test_forward_filter_causality_and_probability(self):
  means=np.array([[-1.,0],[1.,1]]);variance=np.ones((2,2));trans=np.array([[.9,.1],[.2,.8]])
  def run(obs):
   out=[];p=np.array([.5,.5])
   for o in obs:p=filter_step(p,trans,means,variance,o);out.append(p)
   return np.array(out)
  x=np.random.default_rng(3).normal(size=(100,2));a=run(x);x[60:]*=100;b=run(x)
  np.testing.assert_allclose(a[:60],b[:60]);np.testing.assert_allclose(a.sum(1),1)
 def test_covariance_scaling_preserves_psd_and_correlation(self):
  c=np.array([[.04,.01],[.01,.09]]);a=rescale_covariance(c,['a','b'],{'a':.16})
  self.assertAlmostEqual(a[0,0],.16);self.assertAlmostEqual(a[0,1],.02);self.assertGreater(np.linalg.eigvalsh(a).min(),0)
  with self.assertRaises(ValueError):rescale_covariance(c,['a','b'],{'a':-1})
 def test_outcome_is_future_only_and_missing_is_not_filled(self):
  r=pd.Series([.1,.2,np.nan,.4,.5]);v,ret=outcomes(r,2)
  self.assertTrue(np.isnan(v.iloc[0]));self.assertAlmostEqual(v.iloc[2],.41)
  self.assertAlmostEqual(ret.iloc[2],1.4*1.5-1)
 def test_qlike_and_policy_cost(self):
  self.assertLess(qlike(.04,.04),qlike(.04,.08))
  f=pd.DataFrame({'candidate_exposure':[.6,.6],'baseline_exposure':[0.,0.],'market_return':[0.,0.]})
  m=policy_metrics(f);self.assertLess(m['candidate_net_return'],0);self.assertEqual(m['baseline_net_return'],0)
 def test_trend_uses_no_future(self):
  c=pd.Series(np.arange(100)+100.);a=trend_states(c);c.iloc[80:]=1
  pd.testing.assert_series_equal(a.iloc[:80],trend_states(c).iloc[:80])

 def test_imported_account_is_not_treated_as_all_cash(self):
  import tempfile,json
  from pathlib import Path
  from asharequant.forecast import account_snapshot_plan
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);(root/'data').mkdir();a={'cash':1000.0,'available_cash':1000.0,'positions':{'000681':{'name':'视觉中国','shares':1000,'reference_price':10.0}}}
   (root/'data/decision_account.json').write_text(json.dumps(a))
   account,plan=account_snapshot_plan(root,[])
   self.assertEqual(plan['cash'],1000.0);self.assertEqual(plan['current_holdings'][0]['shares'],1000)
   self.assertEqual(plan['positions'],[]);self.assertAlmostEqual(plan['equity'],1000.0+1000*10.0)
