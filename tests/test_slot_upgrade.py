"""Synthetic fixtures only; never production acceptance evidence."""
import tempfile,unittest,sqlite3
from pathlib import Path
from unittest.mock import patch
import numpy as np
import pandas as pd
from asharequant import slot_upgrade as su,lifecycle as lc
from asharequant.forecast import FEATURES,DEFAULTS,features_and_labels
from asharequant.core import write_json

class SlotTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  self.days=pd.bdate_range('2024-01-01',periods=900);self.at=pd.Timestamp('2024-01-01T18:00:00+08:00')
  p=patch.object(su,'fingerprint',return_value='SYNTHETIC_PROTOCOL');p.start();self.addCleanup(p.stop)
  self.candidate={'alpha':100.,'features':[c for c in FEATURES if c!='volume_ratio']};self.champion={'alpha':10.,'features':FEATURES};self.cfg=su.config(self.root)
  self.winner=dict(candidate=self.candidate,eligible_for_future_test=True,mean_gain=.009,recent_gain=.008,minimum_positive_folds=4,recent_direction=.6,champion_recent_direction=.5)
  self.study=dict(asof='2024-01-01',generated_at=self.at.isoformat(),code=su.fingerprint(),outcome_ids=['one'],studies=[dict(slot='600000/1',name='纯测试',champion=self.champion,winner=self.winner,comparisons=[self.winner],candidate_count=1)])
 def freeze(self,h=1,symbol='600000'):
  with su.database(self.root) as db:
   su.put(db,'studies','2024-01-01',self.study)
   return su.freeze(self.root,db,symbol,h,self.winner,self.champion,'2024-01-01',self.at,self.days,self.cfg,lc.digest(self.study))
 def evidence(self,spec,missing=False):
  origins,targets,earliest=su.timetable(spec,self.days)
  with su.database(self.root) as db:
   for i,origin in enumerate(origins):
    if missing and i==1:continue
    a=.01 if i%4 in (0,1) else -.01;p=a*.99;c=a*.98
    def fit(v,cols):return dict(parameters=dict(means=[0.]*len(cols),scales=[1.]*len(cols),coefficients=[0.]*len(cols),intercept=v),features={c:0. for c in cols},train_last_label_end=origin,predicted=v)
    row=dict(experiment=spec['id'],symbol=spec['symbol'],horizon=spec['horizon'],origin=origin,target=targets[origin],issued_at=origin+'T18:00:00+08:00',predicted=p,champion_predicted=c,baseline_mean=0.,candidate_fit=fit(p,spec['candidate']['features']),champion_fit=fit(c,spec['champion']['features']))
    out=dict(experiment=spec['id'],symbol=spec['symbol'],origin=origin,target=targets[origin],actual=a,observed_at=targets[origin]+'T18:00:00+08:00')
    su.put(db,'issues',spec['id']+'|'+origin,row);su.put(db,'outcomes',spec['id']+'|'+origin,out)
  return pd.Timestamp(earliest)
 def test_horizon_windows_and_repeat_budget(self):
  self.assertEqual([su.window(h,1)['test_origins'] for h in (1,5,20)],[60,90,253])
  self.assertLess(su.window(20,2)['alpha'],su.window(20,1)['alpha']);self.assertGreater(su.window(20,2)['test_origins'],253)
  self.assertLessEqual(sum(su.window(1,k)['alpha'] for k in range(1,10000))*150,.05)
 def test_subtwo_percent_gain_and_separate_slots(self):
  dates=pd.bdate_range('2020-01-01',periods=80)
  def validation(frame,h,cfg,cols):
   error=.991 if cfg['ridge_alpha']==100. and frame=='good' else 1.
   return pd.DataFrame(dict(date=dates,actual=np.ones(80)*2,predicted=np.ones(80)*(2-error))),{}
  with patch.object(su,'validate_forecast',side_effect=validation):
   _,good=su.search('good',1,{},self.champion);_,bad=su.search('bad',1,{},self.champion)
  self.assertAlmostEqual(good['mean_gain'],.009);self.assertIsNone(bad)
  import json
  json.dumps(good,allow_nan=False)
 def test_recent_deterioration_blocks_trial(self):
  dates=pd.bdate_range('2020-01-01',periods=80)
  def validation(frame,h,cfg,cols):
   error=np.ones(80)
   if cfg['ridge_alpha']==100.:error[:60]=.5;error[-20:]=1.1
   return pd.DataFrame(dict(date=dates,actual=np.ones(80)*2,predicted=2-error)),{}
  with patch.object(su,'validate_forecast',side_effect=validation):self.assertIsNone(su.search(None,1,{},self.champion)[1])
 def test_trial_available_without_formal_qualification(self):
  self.freeze();state=su.registry(self.root,self.at+pd.Timedelta(minutes=1))
  self.assertEqual(state['models'],{});self.assertEqual(state['errors'],[])
  candidate,proof=su.choice(state,'600000',1,self.champion)
  self.assertEqual(candidate,self.candidate);self.assertEqual(proof['scope'],'RESEARCH_TRIAL_ONLY')
  self.assertEqual(su.choice(state,'600000',5,self.champion),(self.champion,None))
 def test_unknown_calendar_extends_but_cannot_rewrite_known_dates(self):
  with su.database(self.root) as db:s=su.freeze(self.root,db,'600000',20,self.winner,self.champion,'2024-01-01',self.at,self.days[:20],self.cfg,'fixture')
  self.assertIsNone(su.timetable(s,self.days[:20])[2]);self.assertIsNotNone(su.timetable(s,self.days)[2])
  with self.assertRaisesRegex(ValueError,'CALENDAR_CHANGED'):su.timetable(s,self.days.delete(3))
 def test_pending_slot_not_retuned(self):
  spec=self.freeze()
  with su.database(self.root) as db:
   b=su.freeze(self.root,db,'600000',1,dict(candidate=self.champion),self.candidate,'2024-01-02',self.at+pd.Timedelta(days=1),self.days,self.cfg,'later')
   self.assertEqual(b,spec);self.assertEqual(len(su.values(db,'specs')),1)
 def test_no_early_peeking_and_maturity_required(self):
  s=self.freeze();end=self.evidence(s)
  with su.database(self.root) as db,patch.object(su,'assess',side_effect=AssertionError('no early score')):
   out=su.evaluate(self.root,db,s,end-pd.Timedelta(seconds=1),self.days)
   self.assertEqual(out['status'],'WAITING');self.assertEqual(su.values(db,'evaluations'),[])
 def test_accept_once_separate_prediction_admission(self):
  s=self.freeze();end=self.evidence(s)
  with su.database(self.root) as db,patch.object(lc,'_source_integrity',return_value=True):
   out=su.evaluate(self.root,db,s,end,self.days);self.assertEqual(out['status'],'ACCEPTED');self.assertFalse(out['portfolio_admission'])
   with patch.object(su,'assess',side_effect=AssertionError('no repeated score')):self.assertEqual(su.evaluate(self.root,db,s,end,self.days),out)
  with patch.object(lc,'_source_integrity',return_value=True),patch.object(lc,'_sessions',return_value=self.days):state=su.registry(self.root,end)
  self.assertEqual(list(state['models']),['600000/1']);self.assertEqual(state['research_models'],{});self.assertEqual(state['errors'],[])
  self.assertFalse((self.root/'data/production_registry.json').exists())
 def test_missing_record_rejects_trial_preserving_evidence(self):
  s=self.freeze();end=self.evidence(s,missing=True)
  with su.database(self.root) as db,patch.object(lc,'_source_integrity',return_value=True):
   old=su.values(db,'issues');out=su.evaluate(self.root,db,s,end,self.days)
   self.assertEqual(out['status'],'REJECTED');self.assertEqual(old,su.values(db,'issues'))
  self.assertEqual(su.registry(self.root,end)['research_models'],{})
 def test_immutable_updates_deletes(self):
  self.freeze()
  with su.database(self.root) as db:
   with self.assertRaises(sqlite3.IntegrityError):db.execute("UPDATE su_specs SET body='{}'")
   with self.assertRaises(sqlite3.IntegrityError):db.execute('DELETE FROM su_specs')
 def test_changed_code_not_silent_reset(self):
  self.freeze()
  with patch.object(su,'fingerprint',return_value='changed'):state=su.registry(self.root,self.at+pd.Timedelta(minutes=1))
  self.assertEqual(state['research_models'],{});self.assertTrue(state['errors'])
 def test_future_or_backdated_evidence_rejected(self):
  s=self.freeze();end=self.evidence(s)
  with su.database(self.root) as db:rows=su.joined(db,s)
  rows[0]['issued_at']='2020-01-01T18:00:00+08:00'
  with patch.object(lc,'_source_integrity',return_value=True):
   with self.assertRaisesRegex(ValueError,'BACKDATED'):su.verify_rows(self.root,s,rows,end,self.days)
  with su.database(self.root) as db:rows=su.joined(db,s)
  rows[0]['outcome']['observed_at']=(end+pd.Timedelta(days=1)).isoformat()
  with patch.object(lc,'_source_integrity',return_value=True):
   with self.assertRaisesRegex(ValueError,'FUTURE_OUTCOME'):su.verify_rows(self.root,s,rows,end,self.days)
 def test_fake_registry_and_hash_tampering(self):
  write_json(self.root/'data/production_registry.json',dict(models={'600000/1':{'passed':True}}))
  self.assertEqual(su.registry(self.root,self.at)['models'],{});self.freeze()
  with su.database(self.root) as db:
   db.execute('DROP TRIGGER su_specs_update');db.execute("UPDATE su_specs SET body='{}'")
  with self.assertRaisesRegex(ValueError,'HASH'):su.registry(self.root,self.at)
 def test_original_fit_never_uses_unmatured_labels(self):
  from test_forecast import fixture
  raw,days=fixture(1100);frame=features_and_labels(raw,days,20);day=str(days[-1].date())
  result=su.point_prediction(frame,day,20,DEFAULTS,self.candidate);self.assertLessEqual(pd.Timestamp(result['train_last_label_end']),days[-1])
  altered=frame.copy();altered.loc[altered.label_end>days[-21],'target']=1e9;earlier=str(days[-21].date())
  self.assertEqual(su.point_prediction(frame,earlier,20,DEFAULTS,self.candidate),su.point_prediction(altered,earlier,20,DEFAULTS,self.candidate))
 def test_report_marks_trial_separately_and_no_empty_table(self):
  from asharequant.reporting.iteration_status import render
  r=dict(version='slot-v2',asof='2024-01-01',generated_at=self.at.isoformat(),research_trial_slots=['600000/1'],accepted_slots=[],slot_progress=[dict(slot='600000/1',status='WAITING',confirmed=0,required=60,earliest=None)],research_progress={'status':'DAILY_MATURED_OUTCOMES','last_slot_study':self.study})
  text=render(self.root,r,{},'2024-01-01')
  for v in ('可研究试用 1 组','独立预测验证通过 0 组','不再要求统一改善2%','0 / 60','日历尚未齐全','0.90%'):self.assertIn(v,text)
  self.assertNotIn('WAITING',text)


 def test_daily_checkpoint_retries_do_not_rescan_or_reset_experiment(self):
  from contextlib import ExitStack
  from asharequant import daily_iteration as daily
  with ExitStack() as stack:
   stack.enter_context(patch.object(su,'now',return_value=self.at))
   stack.enter_context(patch.object(daily,'load_config',return_value={}))
   stack.enter_context(patch('asharequant.data.calendar',return_value=(self.days,{})))
   stack.enter_context(patch.object(daily,'inputs',return_value=({('600000',1):object()},[],False,[])))
   stack.enter_context(patch.object(daily,'outcome_evidence',return_value=({'confirmed':1,'primary_confirmed':1,'settlement':{'errors':[]}},['one'])))
   stack.enter_context(patch.object(su,'read_watchlist',return_value=pd.DataFrame([dict(symbol='600000',name='纯测试')])) )
   search=stack.enter_context(patch.object(su,'search',return_value=([self.winner],self.winner)))
   a=su.run(self.root);b=su.run(self.root)
  self.assertEqual(search.call_count,1);self.assertEqual(a['research_trial_slots'],['600000/1'])
  self.assertEqual(a['accepted_slots'],[]);self.assertEqual(b['research_progress']['status'],'ALREADY_RESEARCHED_SESSION')
  with su.database(self.root) as db:self.assertEqual(len(su.values(db,'specs')),1)
 def test_actual_issue_collects_once_and_wont_backfill(self):
  from test_forecast import fixture
  raw,days=fixture(1100);cal=pd.bdate_range(days[0],periods=1500)
  at=pd.Timestamp(days[-1],tz='Asia/Shanghai')+pd.Timedelta(hours=18);asof=str(days[-1].date())
  with su.database(self.root) as db:s=su.freeze(self.root,db,'600000',1,self.winner,self.champion,asof,at,cal,self.cfg,'fixture')
  raw2,days2=fixture(1101);d=str(days2[-1].date());at2=pd.Timestamp(days2[-1],tz='Asia/Shanghai')+pd.Timedelta(hours=18)
  snap=dict(raw=raw2,hfq=raw2,meta=dict(source='tencent',fetched_at=at2.isoformat(),version='a'*32,sha256={'raw':'fixture','hfq':'fixture'}))
  with patch.object(su,'read_snapshot',return_value=snap),su.database(self.root) as db:
   su.issue(self.root,db,s,d,at2,cal);su.issue(self.root,db,s,d,at2,cal)
   self.assertEqual(len(su.values(db,'issues')),1)
   nextday=str(cal[cal>days2[-1]][0].date())
   with self.assertRaisesRegex(ValueError,'DEADLINE'):su.issue(self.root,db,s,nextday,at2+pd.Timedelta(days=10),cal)
