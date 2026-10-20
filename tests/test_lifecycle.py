"""SYNTHETIC provider doubles only; no test evidence enters the real project."""
import copy,hashlib,tempfile,unittest,sqlite3
from pathlib import Path
from unittest.mock import patch
import numpy as np
import pandas as pd
from asharequant import lifecycle as lc
from asharequant.forecast import FEATURES
from asharequant.decision import DEFAULTS as D
from asharequant.core import write_json

class Fixture:
 def __init__(self,root):
  self.root=root;self.days=pd.bdate_range('2020-01-02',periods=250);self.clock=pd.Timestamp(self.days[-1],tz='Asia/Shanghai')+pd.Timedelta(hours=18)
  universe=[dict(symbol='600000',name='测试股票',sector='银行')]
  self.spec=dict(experiment='fixture',frozen_at='2020-01-01T12:00:00+08:00',start_session='2020-01-02',training_asof='2019-12-31',runtime_hash='SYNTHETIC_RUNTIME',universe=universe,horizons=[1],universe_sha256=lc.digest(universe),policy={**lc.POLICY,'calibration_origins':90,'test_origins':130},portfolio_config={**D,'horizon':1},artifacts={})
  artifact=dict(kind='ridge_numeric_v1',features=FEATURES,parameters=dict(means=[0]*9,scales=[1]*9,coefficients=[1]+[0]*8,intercept=0),baseline_mean=0.,symbol='600000',horizon=1,train_last_label_end='2019-12-31')
  ap=lc.folder(root,'fixture')/'models/600000-1.json';write_json(ap,artifact)
  self.spec['artifacts']['600000/1']=dict(path=str(ap.relative_to(root)),sha256=hashlib.sha256(ap.read_bytes()).hexdigest())
  (root/'watchlist.csv').write_text('symbol,name,sector,enabled\n600000,测试股票,银行,1\n');write_json(root/'decision_config.json',{'horizon':1})
  with lc.database(root) as db:lc.put(db,'specs','fixture',self.spec)
 def evidence(self):
  cal,gap,test=lc._phases(self.spec,self.days)
  with lc.database(self.root) as db:
   for i,day in enumerate(list(cal)+list(test)):
    date=str(day.date());target=str(self.days[self.days.get_loc(day)+1].date());p=.015 if i%7 in (0,1,3) else -.012
    issue=dict(experiment='fixture',symbol='600000',horizon=1,origin=date,target=target,issued_at=date+'T18:00:00+08:00',predicted=p,baseline_mean=0.,baseline_zero=0.,artifact_sha256=self.spec['artifacts']['600000/1']['sha256'],features={c:p if j==0 else 0. for j,c in enumerate(FEATURES)},input_version='a'*32,input_sha256={},source='SYNTHETIC_PROVIDER_DOUBLE')
    actual=dict(experiment='fixture',symbol='600000',horizon=1,origin=date,target=target,actual=p,observed_at=target+'T18:00:00+08:00',input_version='b'*32,input_sha256={},source='SYNTHETIC_PROVIDER_DOUBLE')
    key=f'fixture|600000|1|{date}';lc.put(db,'issues',key,issue);lc.put(db,'outcomes',key,actual)
    for method in ('equal_weight','risk_parity','mean_variance','constrained_score'):lc.put(db,'targets',f'fixture|{method}|{date}',dict(experiment='fixture',method=method,origin=date,issued_at=date+'T18:00:00+08:00',weights={'600000':.15 if p>0 else 0.},sensitivity=.01))
   lc.put(db,'selections','fixture',dict(experiment='fixture',status='LOCKED',method='equal_weight',decided_at=str(test[0].date())+'T18:00:00+08:00',validation_last_date=str(cal[-1].date()),first_test_origin=str(test[0].date())))
  return cal,test
 def replay(self,*args,**kwargs):
  manifest=dict(references=[],fixture='SYNTHETIC_SOURCE_DOUBLE');sha=lc.digest(manifest);path=self.root/'data/history/manifests'/(sha+'.json');write_json(path,manifest)
  method=dict(coverage=1.,stability=.01,summary=dict(sessions=131,trades=50,net_return=.12,max_drawdown=-.02,sharpe_zero_rf=1.2),cost_stress=dict(net_return=.08))
  return dict(status='COMPLETE',history=dict(manifest_path=str(path.relative_to(self.root)),manifest_sha256=sha),results={m:copy.deepcopy(method) for m in ('equal_weight','risk_parity','mean_variance','constrained_score')})

class LifecycleTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.f=Fixture(self.root)
  self.mocks=[patch('asharequant.lifecycle.now',side_effect=lambda:self.f.clock.to_pydatetime()),patch('asharequant.lifecycle.runtime_hash',return_value='SYNTHETIC_RUNTIME'),patch('asharequant.lifecycle._sessions',side_effect=lambda *args:self.f.days)]
  for m in self.mocks:m.start()
 def tearDown(self):
  for m in reversed(self.mocks):m.stop()
  self.tmp.cleanup()
 def accepted(self):
  self.f.evidence()
  with patch('asharequant.lifecycle._replay',side_effect=self.f.replay),patch('asharequant.lifecycle._source_integrity',return_value=True):
   result=lc.evaluate(self.root,self.f.spec);release=lc.promote(self.root,self.f.spec,result)
  return result,release
 def test_cold_registry_and_json_flags_cannot_grant_admission(self):
  write_json(self.root/'data/production_registry.json',dict(active=True,validated=True,models={'600000/1':{}},portfolio={'method':'equal_weight'}))
  self.assertEqual(lc.admission(self.root)['status'],'BLOCKED')
 def test_immutable_sql_update_delete_and_conflict(self):
  with lc.database(self.root) as db:
   lc.put(db,'events','one',{'value':1});self.assertFalse(lc.put(db,'events','one',{'value':1}))
   with self.assertRaises(ValueError):lc.put(db,'events','one',{'value':2})
   with self.assertRaises(sqlite3.IntegrityError):db.execute("UPDATE lc_events SET body='{}'")
   with self.assertRaises(sqlite3.IntegrityError):db.execute('DELETE FROM lc_events')
 def test_no_early_test_peeking(self):
  self.f.evidence();self.f.clock=pd.Timestamp(self.f.days[60],tz='Asia/Shanghai')+pd.Timedelta(hours=18)
  self.assertEqual(lc.evaluate(self.root,self.f.spec)['status'],'WAITING_FOR_UNTOUCHED_TEST')
  with lc.database(self.root) as db:self.assertEqual(lc.values(db,'evaluations'),[])
 def test_label_maturity(self):
  self.f.evidence();_,_,test=lc._phases(self.f.spec,self.f.days);self.f.clock=pd.Timestamp(test[-1],tz='Asia/Shanghai')+pd.Timedelta(hours=18)
  self.assertEqual(lc.evaluate(self.root,self.f.spec)['status'],'WAITING_FOR_LABEL_MATURITY')
 def test_acceptance_loading_and_once_only_test(self):
  result,release=self.accepted();self.assertTrue(result['models']['600000/1']['passed']);self.assertIsNotNone(release)
  with patch('asharequant.lifecycle._source_integrity',return_value=True):
   registry=lc.refresh_registry(self.root);self.assertEqual(lc.admission(self.root,registry)['status'],'VALIDATED')
   frame=pd.DataFrame([{c:.02 if j==0 else 0 for j,c in enumerate(FEATURES)}]);pred=lc.load_prediction(self.root,'600000',1,frame,registry)
   self.assertAlmostEqual(pred['predicted_return'],.02);self.assertEqual(pred['model_status'],'VALIDATED_REGISTERED')
  with patch('asharequant.lifecycle._replay',side_effect=AssertionError('no test reuse')):self.assertEqual(lc.evaluate(self.root,self.f.spec),result)
 def test_artifact_tamper_disables_release(self):
  self.accepted();p,_=lc._artifact(self.root,self.f.spec,'600000',1);p.write_text('{}');self.assertEqual(lc.refresh_registry(self.root)['models'],{})
 def test_runtime_change_disables_release(self):
  self.accepted()
  with patch('asharequant.lifecycle.runtime_hash',return_value='CHANGED'):self.assertEqual(lc.refresh_registry(self.root)['models'],{})
 def test_no_source_proof_cannot_promote(self):
  self.f.evidence()
  with patch('asharequant.lifecycle._replay',side_effect=self.f.replay):
   result=lc.evaluate(self.root,self.f.spec)
   with self.assertRaises(ValueError):lc.promote(self.root,self.f.spec,result)
 def test_missing_member_not_dropped(self):
  self.f.evidence();changed=copy.deepcopy(self.f.spec);changed['universe'].append(dict(symbol='600001',name='缺失或退市',sector='银行'));changed['artifacts']['600001/1']=changed['artifacts']['600000/1']
  with patch('asharequant.lifecycle._replay',side_effect=self.f.replay):result=lc.evaluate(self.root,changed)
  self.assertFalse(result['models']['600001/1']['passed']);self.assertNotEqual(result['status'],'ACCEPTED')
 def test_selection_deadline(self):
  _,_,test=lc._phases(self.f.spec,self.f.days);self.f.clock=pd.Timestamp(test[3],tz='Asia/Shanghai')+pd.Timedelta(hours=18)
  self.assertIn('SELECTION_DEADLINE_MISSED',lc.select_portfolio(self.root,self.f.spec)['blockers'])
 def test_live_prediction_cannot_be_backdated_after_open(self):
  self.f.clock=pd.Timestamp(self.f.days[20],tz='Asia/Shanghai')+pd.Timedelta(hours=10)
  with self.assertRaises(ValueError):lc.record_live(self.root,'600000',1,str(self.f.days[19].date()),str(self.f.days[20].date()),{'release_id':'fake','predicted_return':.1},'a'*32)
 def test_source_path_validation(self):
  with self.assertRaises(ValueError):lc._source_integrity(self.root,{'symbol':'600000','input_version':'../../etc','input_sha256':{}})
 def test_deterioration_revokes_no_research_fallback(self):
  _,release=self.accepted()
  self.f.days=pd.bdate_range('2020-01-02',periods=400);self.f.clock=pd.Timestamp(self.f.days[330],tz='Asia/Shanghai')+pd.Timedelta(hours=18)
  with lc.database(self.root) as db:
   for day in self.f.days[260:310:2]:
    date=str(day.date());target=str(self.f.days[self.f.days.get_loc(day)+1].date());row=dict(release=release['release'],symbol='600000',horizon=1,origin=date,target=target,actual=-.01,predicted=.1,baseline_mean=0.)
    lc.put(db,'live_outcomes',release['release']+'|'+date,row)
  with patch('asharequant.lifecycle._source_integrity',return_value=True):
   state=lc.monitor(self.root);self.assertEqual(state['models'],{});self.assertIsNone(state['portfolio'])
 def test_cohort_and_policy_changes_block_plan(self):
  self.accepted()
  with patch('asharequant.lifecycle._source_integrity',return_value=True):
   registry=lc.refresh_registry(self.root);write_json(self.root/'decision_config.json',{'max_stock':.1})
   self.assertIn('PORTFOLIO_POLICY_CHANGED',lc.admission(self.root,registry)['blockers'])
   (self.root/'watchlist.csv').write_text('symbol,name,sector,enabled\n600001,变更股票,银行,1\n')
   self.assertIn('OBSERVATION_COHORT_CHANGED',lc.admission(self.root,registry)['blockers'])

 def test_validated_plan_has_conditional_quantities_but_no_order_transport(self):
  from test_decision import row,cfg,account,AT
  from asharequant.decision import build_plan
  c={**cfg(),'selected_method':'equal_weight'}
  result=build_plan([row()],np.array([[.01]]),c,account(),AT,formal={'status':'VALIDATED','formal_method':'equal_weight'})
  self.assertEqual(result['formal_method'],'equal_weight');self.assertGreater(result['decisions'][0]['suggested_shares'],0)
  self.assertFalse(result['decisions'][0]['executable']);self.assertEqual(result['submitted_orders'],0)
 def test_stale_account_still_blocks_validated_plan(self):
  from test_decision import row,cfg,account,AT
  from asharequant.decision import build_plan
  a=account();a['asof']='2020-01-01T10:00:00+08:00';c={**cfg(),'selected_method':'equal_weight'}
  result=build_plan([row()],np.array([[.01]]),c,a,AT,formal={'status':'VALIDATED','formal_method':'equal_weight'})
  self.assertEqual(result['decisions'][0]['suggested_shares'],0);self.assertIn('ACCOUNT_STALE_OR_UNVERIFIED',result['blockers'])

 def test_portfolio_forward_drawdown_revokes_release(self):
  _,release=self.accepted();self.f.days=pd.bdate_range('2020-01-02',periods=400);self.f.clock=pd.Timestamp(self.f.days[330],tz='Asia/Shanghai')+pd.Timedelta(hours=18)
  with lc.database(self.root) as db:
   for day in self.f.days[260:290]:
    date=str(day.date());lc.put(db,'targets','post-'+date,dict(experiment='fixture',method='equal_weight',origin=date,issued_at=date+'T18:00:00+08:00',weights={'600000':.15}))
  def loss(*args):
   result=self.f.replay();result['results']['equal_weight']['summary']['max_drawdown']=-.2;return result
  with patch('asharequant.lifecycle._source_integrity',return_value=True),patch('asharequant.lifecycle._replay',side_effect=loss):
   state=lc.monitor(self.root);self.assertEqual(state['models'],{});self.assertIsNone(state['portfolio'])
 def test_missing_forward_monitor_data_suspends_orders(self):
  _,release=self.accepted();self.f.days=pd.bdate_range('2020-01-02',periods=400);self.f.clock=pd.Timestamp(self.f.days[330],tz='Asia/Shanghai')+pd.Timedelta(hours=18)
  with lc.database(self.root) as db:
   for day in self.f.days[260:290]:
    date=str(day.date());lc.put(db,'targets','post-'+date,dict(experiment='fixture',method='equal_weight',origin=date,issued_at=date+'T18:00:00+08:00',weights={'600000':.15}))
  with patch('asharequant.lifecycle._source_integrity',return_value=True),patch('asharequant.lifecycle._replay',return_value={'status':'BLOCKED','blockers':['DATA_GAP']}):
   state=lc.monitor(self.root);self.assertIn('PORTFOLIO_FORWARD_MONITOR_INCOMPLETE',lc.admission(self.root,state)['blockers'])

 def test_qualified_loader_never_refits(self):
  self.accepted()
  with patch('asharequant.lifecycle._source_integrity',return_value=True):
   registry=lc.refresh_registry(self.root)
   with patch('asharequant.forecast.fit_predict',side_effect=AssertionError('registered inference must not train')):
    result=lc.load_prediction(self.root,'600000',1,pd.DataFrame([{c:.01 for c in FEATURES}]),registry)
    self.assertEqual(result['model_status'],'VALIDATED_REGISTERED')
 def test_prepare_collect_mature_and_idempotency_real_workflow(self):
  import shutil
  from test_forecast import fixture
  from asharequant.core import now as real_now
  source=Path(__file__).resolve().parents[1];shutil.copy(source/'config.json',self.root/'config.json');write_json(self.root/'forecast_config.json',{})
  raw,days=fixture(1100);raw['amount']=2e8;raw['turnover']=.01
  self.f.days=pd.bdate_range(days[0],periods=1500);self.f.clock=pd.Timestamp(days[-1],tz='Asia/Shanghai')+pd.Timedelta(hours=18)
  snapshot={'raw':raw,'hfq':raw,'meta':{'version':'a'*32,'source':'tencent','fetched_at':self.f.clock.isoformat(),'sha256':{'raw':'fixture_raw','hfq':'fixture_hfq'}}}
  with patch('asharequant.lifecycle.read_snapshot',side_effect=lambda *args:snapshot),patch('asharequant.connections.market_environment',return_value={'state':'risk_on'}):
   frozen=lc.prepare(self.root);self.assertEqual(lc.prepare(self.root)['experiment'],frozen['experiment'])
   self.assertEqual(lc.collect(self.root,frozen)['issued'],0)
   next_raw,next_days=fixture(1101);next_raw['amount']=2e8;next_raw['turnover']=.01
   self.f.clock=pd.Timestamp(next_days[-1],tz='Asia/Shanghai')+pd.Timedelta(hours=18);snapshot.update(raw=next_raw,hfq=next_raw)
   snapshot['meta']['fetched_at']=self.f.clock.isoformat()
   first=lc.collect(self.root,frozen);self.assertEqual(first['issued'],3);self.assertEqual(lc.collect(self.root,frozen)['issued'],0)
   with lc.database(self.root) as db:
    rows=lc.values(db,'issues',frozen['experiment']);self.assertEqual(len(rows),3)
    self.assertTrue(all(pd.Timestamp(r['issued_at'])>=pd.Timestamp(frozen['frozen_at']) for r in rows))
   target_raw,target_days=fixture(1102);target_raw['amount']=2e8;target_raw['turnover']=.01
   self.f.clock=pd.Timestamp(target_days[-1],tz='Asia/Shanghai')+pd.Timedelta(hours=18);snapshot.update(raw=target_raw,hfq=target_raw);snapshot['meta']['fetched_at']=self.f.clock.isoformat()
   self.assertEqual(lc.settle(self.root,frozen),1);self.assertEqual(lc.settle(self.root,frozen),0)
 def test_prepare_refuses_synthetic_market_input(self):
  import shutil
  from test_forecast import fixture
  source=Path(__file__).resolve().parents[1];shutil.copy(source/'config.json',self.root/'config.json');write_json(self.root/'forecast_config.json',{})
  raw,days=fixture(1100);self.f.days=pd.bdate_range(days[0],periods=1500);self.f.clock=pd.Timestamp(days[-1],tz='Asia/Shanghai')+pd.Timedelta(hours=18)
  snapshot={'raw':raw,'hfq':raw,'meta':{'synthetic':True,'source':'tencent'}}
  with patch('asharequant.lifecycle.read_snapshot',return_value=snapshot):
   with self.assertRaisesRegex(ValueError,'SYNTHETIC'):lc.prepare(self.root)

 def test_prediction_only_update_cannot_change_portfolio_models(self):
  _,release=self.accepted();self.f.clock+=pd.Timedelta(minutes=1)
  newer={**release,'release':'newer-prediction-only','released_at':self.f.clock.isoformat(),'portfolio_method':None}
  with lc.database(self.root) as db:lc.put(db,'releases',newer['release'],newer)
  with patch('asharequant.lifecycle._source_integrity',return_value=True):
   state=lc.refresh_registry(self.root);self.assertEqual(state['models']['600000/1']['release'],state['portfolio']['release'])
 def test_forged_model_portfolio_lineage_is_rejected(self):
  _,release=self.accepted()
  with patch('asharequant.lifecycle._source_integrity',return_value=True):
   state=lc.refresh_registry(self.root);state['portfolio']['portfolio_config']={**state['portfolio']['portfolio_config'],'max_stock':.99}
   with self.assertRaisesRegex(ValueError,'BINDING_CHANGED'):lc.admission(self.root,state)
