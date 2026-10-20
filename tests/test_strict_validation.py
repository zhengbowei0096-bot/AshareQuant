"""All fixtures in this file are synthetic, never market/promotion evidence."""
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
import pandas as pd
from asharequant.strict_validation import chronological_split,walk_forward,point_in_time_join,metrics,formal_gate
from asharequant.scheduler import due
from asharequant.core import TZ,load_config
from asharequant.daily import run
ROOT=Path(__file__).resolve().parents[1]

class StrictTests(unittest.TestCase):
    def frame(self):
        days=pd.bdate_range('2020-01-01',periods=200)
        return pd.DataFrame({'label_end':days+pd.offsets.BDay(5),'x':np.arange(200)},index=days)
    def test_purged_three_part_split(self):
        a,b,c=chronological_split(self.frame(),40,30,5)
        self.assertLess(a.label_end.max(),b.index.min());self.assertLess(b.label_end.max(),c.index.min())
        self.assertEqual(len(c),30)
    def test_no_shuffle(self):
        with self.assertRaises(ValueError):chronological_split(self.frame().sample(frac=1,random_state=1),40,30)
    def test_rolling_expanding_embargo(self):
        for window in (None,60):
            folds=list(walk_forward(self.frame(),40,20,window,5));self.assertGreater(len(folds),2)
            for train,test in folds:
                self.assertLess(train.label_end.max(),test.index.min())
                if window:self.assertLessEqual(len(train),window)
    def test_future_mutation_cannot_change_training(self):
        f=self.frame();a=list(walk_forward(f,40,20,60,5))[0][0]
        f.loc[f.index[100]:,'x']=999999
        pd.testing.assert_frame_equal(a,list(walk_forward(f,40,20,60,5))[0][0])
    def test_disclosure_date_not_fiscal_date(self):
        obs=pd.DataFrame({'symbol':['A','A'],'asof':['2026-01-01','2026-04-02']})
        releases=pd.DataFrame({'symbol':['A'],'available_at':['2026-04-01'],'value':[42]})
        x=point_in_time_join(obs,releases);self.assertTrue(pd.isna(x.value.iloc[0]));self.assertEqual(x.value.iloc[1],42)
    def test_metrics_and_reliability(self):
        f=pd.DataFrame([{'date':str(d),'symbol':str(s),'actual':s/100-.02,'predicted':s/110-.02,'probability':.6} for d in range(10) for s in range(5)])
        m=metrics(f);self.assertEqual(m['samples'],50);self.assertEqual(m['rank_ic_dates'],10);self.assertEqual(len(m['quintile_returns']),5);self.assertIn('brier',m)
    def test_formal_fail_closed(self):
        self.assertEqual(formal_gate(ROOT)['status'],'BLOCKED')
    def test_exchange_holiday_and_timezone(self):
        cfg=load_config(ROOT);sessions=pd.DatetimeIndex(['2026-10-19'])
        self.assertFalse(due(datetime(2026,10,1,19,tzinfo=TZ),{},cfg,sessions))
        self.assertTrue(due(datetime(2026,10,19,19,tzinfo=TZ),{},cfg,sessions))
        self.assertFalse(due(datetime(2026,10,19,19),{},cfg,sessions))
    def test_daily_retry_idempotency_and_archive(self):
        import shutil
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);shutil.copy(ROOT/'config.json',root/'config.json')
            at=datetime(2026,10,19,19,tzinfo=TZ);cal=pd.DatetimeIndex(['2026-10-19'])
            with patch('asharequant.daily.calendar',return_value=(cal,{'source':'SYNTHETIC_TEST'})):
                a=run(root,at,lambda _:2);self.assertEqual(a['status'],'FAILED')
                b=run(root,at,lambda _:0);self.assertEqual(b['attempt'],2);self.assertEqual(b['status'],'SUCCESS')
                c=run(root,at,lambda _:self.fail('duplicate'));self.assertEqual(c['status'],'ALREADY_COMPLETED')
                self.assertTrue((root/'reports/daily/2026-10-19/2/manifest.json').exists())
    def test_daily_calendar_outage(self):
        with patch('asharequant.daily.calendar',side_effect=RuntimeError('offline')):
            with self.assertRaises(RuntimeError):run(ROOT,datetime(2026,10,19,19,tzinfo=TZ),lambda _:self.fail('must not run'))

    def test_uncalibrated_probability_not_trade_output(self):
        from test_decision import row,cfg,account,AT
        from asharequant.decision import build_plan
        result=build_plan([row()],np.array([[.01]]),cfg(),account(),AT)
        self.assertIsNone(result['decisions'][0]['up_probability'])
        self.assertFalse(result['decisions'][0]['executable'])
        self.assertEqual(result['decisions'][0]['estimated_cost']['total'],0.)

    def test_uncertainty_never_reorders_training(self):
        from asharequant.seven_metrics import bootstrap_uncertainty
        train=self.frame()
        def check(sample,test,alpha):
            self.assertTrue(sample.index.is_monotonic_increasing)
            self.assertFalse(sample.index.has_duplicates)
            return np.array([.01]),{}
        with patch('asharequant.forecast.fit_predict',side_effect=check):
            result=bootstrap_uncertainty(train,None,5,10)
            self.assertEqual(result['method'],'chronological_contiguous_window_sensitivity')

    def test_daily_input_mutation_fails_closed(self):
        import shutil
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);shutil.copy(ROOT/'config.json',root/'config.json')
            def mutate(command):
                if command=='run':(root/'watchlist.csv').write_text('changed')
                return 0
            with patch('asharequant.daily.calendar',return_value=(pd.DatetimeIndex(['2026-10-19']),{})):
                result=run(root,datetime(2026,10,19,19,tzinfo=TZ),mutate)
                self.assertEqual(result['status'],'FAILED')
                self.assertEqual(result['error'],'CONFIG_OR_ACCOUNT_CHANGED_DURING_RUN')

    def test_numeric_artifact_loader_rejects_tamper(self):
        import hashlib,json
        from asharequant.validation_engine import infer
        from asharequant.forecast import FEATURES
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'model.json';n=len(FEATURES)
            artifact=dict(kind='ridge_numeric_v1',features=FEATURES,parameters=dict(means=[0]*n,scales=[1]*n,coefficients=[1]*n,intercept=.5))
            path.write_text(json.dumps(artifact));sha=hashlib.sha256(path.read_bytes()).hexdigest()
            np.testing.assert_allclose(infer(path,sha,pd.DataFrame([{c:1 for c in FEATURES}])),[n+.5])
            path.write_text('{}')
            with self.assertRaises(ValueError):infer(path,sha,pd.DataFrame())

    def test_sealed_diagnostic_cannot_be_retuned(self):
        import shutil,json
        from test_forecast import fixture
        from asharequant.validation_engine import run as evaluate
        raw,cal=fixture(1200);snap={'raw':raw,'hfq':raw,'meta':{'version':'SYNTHETIC'}}
        watch=pd.DataFrame({'symbol':['600000']})
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'data').mkdir();shutil.copy(ROOT/'config.json',root/'config.json')
            (root/'forecast_config.json').write_text('{}')
            with patch('asharequant.validation_engine.calendar',return_value=(cal,{})),patch('asharequant.validation_engine.cutoff',return_value=cal.max()),patch('asharequant.validation_engine.read_snapshot',return_value=snap),patch('asharequant.validation_engine.read_watchlist',return_value=watch):
                first=evaluate(root);self.assertEqual(len(first['results']),3);self.assertEqual(first['errors'],[])
                again=evaluate(root);self.assertTrue(all(r['cached_test_result'] for r in again['results']))
                (root/'forecast_config.json').write_text(json.dumps({'ridge_alpha':99}))
                changed=evaluate(root);self.assertEqual(len(changed['results']),0);self.assertEqual(len(changed['errors']),1)

    def test_registry_fingerprint_covers_feature_pipeline(self):
        from asharequant.model_candidates import signature
        original=Path.read_bytes
        before=signature()
        def changed(path):
            content=original(path)
            return content+b'\n# changed feature formula' if path.name=='forecast.py' else content
        with patch.object(Path,'read_bytes',changed):self.assertNotEqual(signature(),before)

    def test_risk_research_missing_account_respects_known_holdings(self):
        from asharequant.risk_models import research_account
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);account,assumed=research_account(root)
            self.assertTrue(assumed);self.assertEqual(account['cash'],150000);self.assertEqual(account['positions'],{})
            (root/'decision_config.json').write_text('{"require_real_account":true}')
            with self.assertRaises(ValueError):research_account(root)

    def test_replay_missing_history_never_optimizes_or_invents_curve(self):
        import json
        from asharequant.execution_replay import run as replay
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);folder=root/'reports/forecasts/latest';folder.mkdir(parents=True)
            (folder/'forecast.json').write_text('{}');(root/'decision_config.json').write_text('{}')
            source=root/'synthetic.csv';source.write_text('SYNTHETIC')
            with patch('asharequant.execution_replay.source_records',return_value=(pd.DataFrame({'horizon':[20]}),source)),patch('asharequant.execution_replay.optimize',side_effect=AssertionError('must not optimize')):
                result=replay(root)
                self.assertEqual(result['status'],'BLOCKED_EXECUTION_HISTORY_MISSING');self.assertEqual(result['results'],{})
                self.assertIsNone(result['formal_method'])
