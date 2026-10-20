import json,sqlite3,tempfile,unittest
from pathlib import Path
import numpy as np
import pandas as pd
from asharequant.forecast import FEATURES,DEFAULTS,features_and_labels,validate_forecast
from asharequant.model_candidates import add_lags,fit,columns,NAMES,selected
from asharequant.model_research import holm,sign_pvalue,historical_pass,record_shadow,dbconnect,prospective


def fixture():
 rng=np.random.default_rng(21);days=pd.bdate_range('2021-01-01',periods=1100);r=rng.normal(0,.01,len(days));c=100*np.exp(np.cumsum(r))
 bars=pd.DataFrame(dict(date=days,open=c,high=c*1.01,low=c*.99,close=c,volume=rng.integers(100,1000,len(c))))
 return add_lags(features_and_labels(bars,days,5))

class CandidateTests(unittest.TestCase):
 def test_lags_are_causal(self):
  f=fixture();changed=f.copy();changed.loc[changed.index[800]:,'r1']=100
  pd.testing.assert_frame_equal(add_lags(f)[columns('ar5')].iloc[:800],add_lags(changed)[columns('ar5')].iloc[:800])
 def test_linear_fit_scaling_and_determinism(self):
  f=fixture().dropna();train=f.iloc[:700];test=f.iloc[700:705]
  for model in ['ar5','lasso']:
   a,meta=fit(model,train,test);changed=test.copy();changed[columns(model)]*=100;b,other=fit(model,train,changed)
   self.assertEqual(meta,other);self.assertTrue(np.isfinite(a).all())
   np.testing.assert_allclose(a,fit(model,train,test)[0])
 def test_all_models_purge_labels_on_same_dates(self):
  frame=fixture().dropna();cfg={**DEFAULTS,'validation_days':63,'refit_days':21}
  baseline,_=validate_forecast(frame,5,cfg)
  for model in NAMES:
   out,metric=validate_forecast(frame,5,cfg,columns(model),model_name=model)
   self.assertTrue(out.date.equals(baseline.date));self.assertTrue(np.isfinite(out.predicted).all())
   for fold in metric['folds']:self.assertLess(fold['training_last_label_end'],fold['test_start'])
 def test_multiple_testing_and_gate(self):
  self.assertEqual(holm([.001,.04,.2]),[.003,.08,.2]);self.assertEqual(sign_pvalue([1]*4),.0625)
  row=dict(relative_mae_improvement=.1,direction=.7,champion_direction=.6,independent_pairs=20,positive_folds=4,holm_pvalue=.01,candidate_simple_baseline_pass=True)
  self.assertTrue(historical_pass(row));self.assertFalse(historical_pass({**row,'holm_pvalue':.1}))
 def test_shadow_is_immutable_and_cannot_be_backdated(self):
  with tempfile.TemporaryDirectory() as tmp:
   r=Path(tmp);(r/'data').mkdir()
   entry=dict(spec='spec',model='lasso',symbol='600001',horizon=1,origin_date='2026-10-19',target_date='2026-10-20',issued_at='2026-10-19T20:00:00+00:00',predicted=.01,champion_predicted=.02,input_version='v1')
   with dbconnect(r) as db:
    record_shadow(db,entry);record_shadow(db,{**entry,'predicted':99})
    self.assertEqual(db.execute('select count(*),predicted from model_shadow').fetchone(),(1,.01))
    self.assertEqual(prospective(db,'spec','lasso','600001',1)['samples'],0)
    with self.assertRaises(ValueError):record_shadow(db,{**entry,'issued_at':'2026-10-21T00:00:00+00:00'})
 def test_missing_or_unqualified_registry_keeps_ridge(self):
  with tempfile.TemporaryDirectory() as tmp:
   r=Path(tmp);(r/'data').mkdir();self.assertEqual(selected(r,'600001',1),'ridge')
   (r/'data/model_registry.json').write_text(json.dumps({'active':{'600001/1':{'model':'lasso','historical_passed':True,'prospective_passed':False}}}))
   self.assertEqual(selected(r,'600001',1),'ridge')

 def test_qualified_registry_checks_config_versions_and_data(self):
  from unittest.mock import patch
  from asharequant.model_candidates import signature
  with tempfile.TemporaryDirectory() as tmp:
   r=Path(tmp);(r/'data').mkdir();p=r/'data/model_registry.json'
   entry={'model':'lasso','historical_passed':True,'prospective_passed':True,'input_version':'v1'}
   state={'spec_id':'test-spec','config':DEFAULTS,'dependencies':{'numpy':np.__version__},'implementation_hash':signature(),'evaluated_asof':'2026-10-19','active':{'600001/1':entry}}
   p.write_text(json.dumps(state))
   self.assertEqual(selected(r,'600001',1,'v1','2026-10-19'),'ridge')
   from asharequant.model_monitor import digest
   (r/'data/model_monitor.json').write_text(json.dumps({'registry_hashes':{'return':digest(state)},'active':['return|test-spec|lasso|600001|1'],'blocked':{}}))
   self.assertEqual(selected(r,'600001',1,'v1','2026-10-19'),'lasso')
   self.assertEqual(selected(r,'600001',1,'v2','2026-10-19'),'ridge')
   self.assertEqual(selected(r,'600001',1,'v1','2026-10-20'),'ridge')
   entry['prospective_passed']=False;p.write_text(json.dumps(state))
   self.assertEqual(selected(r,'600001',1,'v1','2026-10-19'),'ridge')
   entry['prospective_passed']=True;state['implementation_hash']='different';p.write_text(json.dumps(state))
   self.assertEqual(selected(r,'600001',1,'v1','2026-10-19'),'ridge')
