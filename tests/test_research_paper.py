import copy,json,sqlite3,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
import pandas as pd
from asharequant.research_paper import targets,freeze,enabled,settle_predictions
from asharequant.paper_account import put,get,replay,engine_version
from asharequant.decision import DEFAULTS

class ResearchPaperTests(unittest.TestCase):
 def setUp(self):
  self.c={**DEFAULTS,'fees_verified':True,'fee_source':'test','horizon':1,'market_environment':'neutral','max_uncertainty':.3}
  self.f=dict(predicted_return=.06,baseline_predicted_return=.01,up_probability_empirical=.7,downside_es95_normal_proxy=-.03,interval80_empirical=[-.04,.1],probability_calibration={'independent_validated':False,'status':'MISSING'},validation={'preliminary_screen_passed':False})
  self.payload=dict(asof='2026-10-20',generated_at='2026-10-20T18:00:00+08:00',model='test',forecasts=[dict(symbol='600031',name='三一重工',sector='机械',close=10.,price_source='test',data_eligible=True,composite_score={'score':80},forecasts={'1':self.f})])
  self.cal=pd.DatetimeIndex(['2026-10-19','2026-10-20','2026-10-21','2026-10-22'])
 def test_unvalidated_model_can_form_research_target(self):
  w,rows=targets(self.payload,{},150000,np.array([[.01]]),self.c,'neutral')
  self.assertGreater(w['600031'],0);self.assertLessEqual(w['600031'],.15);self.assertFalse(rows[0]['executable']);self.assertFalse(rows[0]['probability']['probability_validated'])
 def test_risk_reduction_not_blocked_by_probability_failure(self):
  self.f['up_probability_empirical']=.4
  w,r=targets(self.payload,{'600031':{'shares':5000}},150000,np.array([[.01]]),self.c,'neutral')
  self.assertEqual(w['600031'],0);self.assertEqual(r[0]['direction'],'SELL')
 def test_missing_score_and_risk_off_allow_cash(self):
  for env in ['unknown','risk_off']:
   w,r=targets(self.payload,{},150000,np.array([[.01]]),self.c,env);self.assertEqual(w['600031'],0)
  self.payload['forecasts'][0]['composite_score']['score']=None
  self.assertEqual(targets(self.payload,{},150000,np.array([[.01]]),self.c,'neutral')[0]['600031'],0)
 def test_deadline_and_fees_not_bypassed(self):
  args=(self.cal,{'version':'test'},{'config':self.c},{},150000,np.array([[.01]]),'neutral')
  with self.assertRaisesRegex(ValueError,'MISSED_PREOPEN'):freeze(self.payload,'2026-10-21T09:30:00+08:00',*args)
  self.c['fees_verified']=False
  with self.assertRaisesRegex(ValueError,'FEES_UNVERIFIED'):freeze(self.payload,'2026-10-20T18:01:00+08:00',*args)
 def test_frozen_plan_idempotent_and_cannot_replace(self):
  plan=freeze(self.payload,'2026-10-20T18:01:00+08:00',self.cal,{'version':'test'},{'config':self.c},{},150000,np.array([[.01]]),'neutral')
  with sqlite3.connect(':memory:') as db:
   db.execute('CREATE TABLE records(kind TEXT,key TEXT,body TEXT,sha256 TEXT,PRIMARY KEY(kind,key))')
   put(db,'plans','2026-10-21',plan);put(db,'plans','2026-10-21',plan)
   with self.assertRaisesRegex(ValueError,'IMMUTABLE'):put(db,'plans','2026-10-21',{**plan,'weights':{}})
 def test_real_next_day_execution_and_cash_constraints(self):
  meta=dict(anchor_session='2026-10-20',start_session='2026-10-21',initial_cash=100000,initial_equity=150000,initial_positions={'600031':{'shares':5000,'sellable':5000}},initial_marks={'600031':10},config={**self.c,'max_volume_participation':.01,'require_fill_in_range':True})
  plan={'2026-10-21':dict(origin_session='2026-10-20',weights={'600031':.1})}
  obs={'2026-10-21':dict(bars={'600031':dict(open=10.,close=10.1,high=10.5,low=9.5,volume=1000000)},states={'600031':dict(status='trading',source='TEST_SYNTHETIC',corporate_action_status='none',limit_down=9.,limit_up=11.)})}
  r=replay(meta,self.cal,plan,obs);self.assertEqual(r['fills'][0]['side'],'SELL');self.assertGreater(r['fills'][0]['fee'],0);self.assertLessEqual(r['ledger'][0]['turnover'],.3)
  obs['2026-10-21']['states']['600031']['status']='suspended';self.assertEqual(replay(meta,self.cal,plan,obs)['fills'],[])
 def test_future_outcome_not_settled_and_prediction_is_immutable(self):
  f={**self.f,'forecast_target_date':'2026-10-21'};payload=copy.deepcopy(self.payload);payload['forecasts'][0]['forecasts']={'1':f}
  snap={'hfq':pd.DataFrame({'date':pd.to_datetime(['2026-10-20','2026-10-21']),'close':[10,11]}),'meta':{'source':'tencent','version':'test'}}
  with sqlite3.connect(':memory:') as db,patch('asharequant.research_paper.read_snapshot',return_value=snap):
   db.execute('CREATE TABLE records(kind TEXT,key TEXT,body TEXT,sha256 TEXT,PRIMARY KEY(kind,key))')
   settle_predictions(Path('.'),db,{'2026-10-20':payload},'2026-10-20');self.assertEqual(get(db,'outcomes'),{})
   settle_predictions(Path('.'),db,{'2026-10-20':payload},'2026-10-21');out=list(get(db,'outcomes').values())[0];self.assertAlmostEqual(out['actual'],.1);self.assertFalse(out['formal_validation'])
   settle_predictions(Path('.'),db,{'2026-10-20':payload},'2026-10-21');self.assertEqual(len(get(db,'outcomes')),1)
 def test_daily_cli_runs_enabled_research_and_reports_failures(self):
  from asharequant.cli import main
  base=dict(status='SETTLED',start_session='2026-10-20',equity=150000,cash=100,daily_return=0,cumulative_return=0,alerts=[],submitted_orders=0)
  with tempfile.TemporaryDirectory() as d,patch('asharequant.paper_account.run',return_value=base),patch('asharequant.risk_control.run'),patch('asharequant.research_paper.enabled',return_value=True),patch('asharequant.research_paper.run',return_value={'status':'BLOCKED_DATA','alerts':['missing']}) as r:
   self.assertEqual(main(['paper-account','--root',d]),2);r.assert_called_once()
 def test_two_day_run_freezes_settles_and_preserves_control(self):
  from asharequant.research_paper import run
  from asharequant.forecast import FEATURES
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);(root/'data').mkdir();(root/'reports/iteration').mkdir(parents=True)
   (root/'research_paper_config.json').write_text('{"enabled":true}')
   c={**self.c,'market_environment':'unknown','max_volume_participation':.01,'require_fill_in_range':True}
   (root/'decision_config.json').write_text(json.dumps(c));(root/'forecast_config.json').write_text('{}')
   (root/'reports/iteration/latest.json').write_text(json.dumps({'asof':'2026-10-20','studies':[{'candidate':{'alpha':100.,'features':FEATURES},'mean_gain':.01,'errors':[]}]}))
   position={'600031':{'shares':5000,'sellable':5000}};bar=dict(open=10.,high=10.5,low=9.5,close=10.,volume=1000000)
   old=root/'data/paper_account.sqlite3'
   with sqlite3.connect(old) as db:
    db.execute('CREATE TABLE records(kind TEXT,key TEXT,body TEXT,sha256 TEXT,PRIMARY KEY(kind,key))')
    put(db,'meta','account',{'config':c,'initial_equity':150000})
    put(db,'nav','2026-10-20',dict(date='2026-10-20',positions=position,cash=100000,equity=150000,daily_return=0))
    put(db,'observations','2026-10-20',dict(bars={'600031':bar}))
   original=old.read_bytes();watch=pd.DataFrame([{'symbol':'600031','name':'三一重工','sector':'机械'}])
   snap={'hfq':pd.DataFrame({'date':self.cal,'close':[10.,10.,10.,10.]})}
   def prediction(root,cal,day,spec):
    payload=copy.deepcopy(self.payload);payload.update(asof=day,generated_at=day+'T18:00:00+08:00',model=spec['version'],seven_metrics=[])
    payload['forecasts'][0]['forecasts']['1']['forecast_target_date']='2026-10-22'
    return payload
   obs=lambda root,day,symbols:dict(session=day,bars={'600031':bar},states={'600031':dict(status='trading',source='SYNTHETIC_TEST',corporate_action_status='none',limit_down=9.,limit_up=11.)})
   with patch('asharequant.research_paper.now',return_value=pd.Timestamp('2026-10-20T18:01:00+08:00')),patch('asharequant.research_paper.cutoff',return_value=pd.Timestamp('2026-10-20')),patch('asharequant.research_paper.load_config',return_value={}),patch('asharequant.data.calendar',return_value=(self.cal,{})),patch('asharequant.research_paper.read_watchlist',return_value=watch),patch('asharequant.research_paper.predict_day',side_effect=prediction),patch('asharequant.research_paper.read_snapshot',return_value=snap),patch('asharequant.research_paper.covariance',return_value=np.array([[.01]])),patch('asharequant.connections.market_environment',return_value={'state':'neutral'}),patch('asharequant.research_paper.evidence',side_effect=obs),patch('asharequant.research_paper.publish'):
    first=run(root);self.assertEqual(first['status'],'ACTIVE_RESEARCH_PAPER');self.assertEqual(first['fills'],[])
    again=run(root);self.assertEqual(first['plan'],again['plan'])
    with patch('asharequant.research_paper.now',return_value=pd.Timestamp('2026-10-21T18:01:00+08:00')),patch('asharequant.research_paper.cutoff',return_value=pd.Timestamp('2026-10-21')):
     second=run(root);self.assertEqual(second['status'],'ACTIVE_RESEARCH_PAPER');self.assertTrue(second['fills']);self.assertEqual(second['fills'][0]['date'],'2026-10-21')
     third=run(root);self.assertEqual(second['ledger'],third['ledger']);self.assertEqual(second['fills'],third['fills'])
   self.assertEqual(old.read_bytes(),original)
