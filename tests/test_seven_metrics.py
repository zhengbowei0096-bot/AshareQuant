import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
import pandas as pd
from asharequant.seven_metrics import WEIGHTS,combine,financial_score,bootstrap_uncertainty,enrich,export
class SevenTests(unittest.TestCase):
 def test_missing_never_reweighted(self):
  p={k:{'score':100.} for k in WEIGHTS};p['财务质量']['score']=None
  r=combine(p,WEIGHTS);self.assertIsNone(r['score']);self.assertEqual(r['coverage'],.8)
 def test_bad_weights_rejected(self):
  with self.assertRaises(ValueError):combine({}, {**WEIGHTS,'财务质量':.3})
 def test_complete_weighted(self):
  self.assertEqual(combine({k:{'score':40.} for k in WEIGHTS},WEIGHTS)['score'],40)
 def test_financial_sector_rules(self):
  r={'ROEJQ':10,'PARENTNETPROFITTZ':5}
  self.assertIsNotNone(financial_score(r,'银行'));self.assertIsNone(financial_score(r,'制造'))
 def test_bootstrap_deterministic(self):
  train=pd.DataFrame({'target':np.linspace(-.1,.1,120),'label_end':pd.date_range('2025-01-01',periods=120)})
  def fit(a,b,alpha):return np.array([a.target.mean()]),{}
  with patch('asharequant.forecast.fit_predict',side_effect=fit):
   a=bootstrap_uncertainty(train,train.iloc[[-1]],20,10);b=bootstrap_uncertainty(train,train.iloc[[-1]],20,10)
  self.assertEqual(a,b);self.assertGreater(a['std'],0);self.assertEqual(a['members'],24)
 def test_short_sample_missing(self):
  self.assertIsNone(bootstrap_uncertainty(pd.DataFrame(index=range(10)),None,20,10)['std'])
 def test_uniform_and_future_data_missing(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);(root/'data/connections/suspensions').mkdir(parents=True)
   pd.DataFrame([{'symbol':s,'name':n,'quote_time':'20261019150000','market_cap_100m':100} for s,n in [('600031','测试甲'),('600066','测试乙')]]).to_csv(root/'data/quotes.csv',index=False)
   (root/'data/connections/suspensions/latest.json').write_text(json.dumps({'complete':True,'date':'2026-10-19','source':'SYNTHETIC','fetched_at':'2026-10-19T16:00:00+08:00','items':[]}))
   days=pd.bdate_range(end='2026-10-19',periods=300);h=pd.DataFrame({'date':days,'close':100+np.arange(300)+np.sin(np.arange(300)),'turnover':.01,'amount':2e8,'volume':10000})
   watch=pd.DataFrame([{'symbol':s,'name':n,'sector':'制造'} for s,n in [('600031','测试甲'),('600066','测试乙')]])
   fin={'_observation_date':'2026-06-30','NOTICE_DATE':'2026-08-20','ROEJQ':10,'PARENTNETPROFITTZ':5,'NCO_NETPROFIT':1.2,'ZCFZL':40}
   payload={'generated_at':'2026-10-19T18:00:00+08:00','asof':'2026-10-19','config':{},'forecasts':[]}
   with patch('asharequant.core.read_snapshot',return_value={'raw':h,'hfq':h}),patch('asharequant.factor_research.Store') as cls:
    cls.return_value.visible.return_value=(pd.Timestamp('2026-10-19T17:00:00+08:00'),'SYNTHETIC',[fin])
    rows=enrich(root,payload,watch);self.assertEqual(len(rows),6);self.assertTrue(all(r['composite_rank'] for r in rows));self.assertTrue(all(r['expected_return'] is None for r in rows))
    self.assertIn('模型不确定性',export(rows,root))
    fin['NOTICE_DATE']='2026-10-21';rows=enrich(root,payload,watch)
    self.assertTrue(all(r['composite_rank'] is None for r in rows))
 def test_zero_score_valid(self):
  self.assertEqual(combine({k:{'score':0.} for k in WEIGHTS},WEIGHTS)['score'],0.)
