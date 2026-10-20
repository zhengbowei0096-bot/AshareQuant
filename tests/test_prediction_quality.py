"""SYNTHETIC fixtures in temp roots; never independent investment evidence."""
import unittest,tempfile,json,hashlib,sqlite3
from pathlib import Path
from unittest.mock import patch
import pandas as pd
from asharequant import prediction_quality as q

class QualityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.at=pd.Timestamp('2026-01-05T18:20:00+08:00')
        self.payload=dict(generated_at=str(self.at),asof='2026-01-05',next_session='2026-01-06',model_code_sha256='code',data_versions={'600001':'v1'},forecasts=[dict(symbol='600001',name='合成测试',sector='SYNTHETIC',forecasts={'1':dict(model='ridge',model_parameters={'x':1},predicted_return=.02,forecast_target_date='2026-01-06',up_probability_empirical=.7)})])
        self.snap=self.snapshot('v1','2026-01-05T18:10:00+08:00')
        self.p1=patch.object(q,'read_snapshot',side_effect=lambda *a:self.snap);self.p1.start();self.addCleanup(self.p1.stop)
        self.p2=patch('asharequant.connections.market_environment',return_value={'state':'neutral','asof':'2026-01-05'});self.p2.start();self.addCleanup(self.p2.stop)
    def snapshot(self,version,fetched,prices=(10,11)):
        frame=pd.DataFrame({'date':pd.to_datetime(['2026-01-05','2026-01-06']),'close':prices}); hashes={}
        folder=self.root/'data/normalized/600001/versions'/version;folder.mkdir(parents=True,exist_ok=True)
        for k in ('raw','hfq'):
            p=folder/(k+'.csv');frame.to_csv(p,index=False);hashes[k]=hashlib.sha256(p.read_bytes()).hexdigest()
        return dict(meta=dict(version=version,fetched_at=fetched,source='tencent',sha256=hashes),raw=frame,hfq=frame)
    def archive(self):return q.archive(self.root,self.payload,self.at)
    def table(self,name):
        with q.database(self.root) as db:return q.rows(db,name)
    def test_record_fields_and_uncalibrated_null(self):
        self.archive();r=self.table('q9_predictions')[0][1]
        self.assertTrue(r['primary_daily']);self.assertEqual(r['predicted_direction'],'UP');self.assertIsNone(r['probability_up']);self.assertEqual(r['market_state']['state'],'neutral');self.assertTrue(r['input_sha256'])
    def test_retry_idempotent(self):
        self.archive();self.assertEqual(self.archive()['archived'],0)
    def test_changed_same_issue_rejected(self):
        self.archive();self.payload['forecasts'][0]['forecasts']['1']['predicted_return']=.9
        with self.assertRaisesRegex(ValueError,'SEALED'):self.archive()
    def test_reruns_not_duplicate_samples(self):
        self.archive();self.payload['generated_at']=str(self.at+pd.Timedelta(minutes=1));q.archive(self.root,self.payload,self.at+pd.Timedelta(minutes=1))
        self.assertEqual(sum(r['primary_daily'] for _,r in self.table('q9_predictions')),1)
    def test_future_issuance(self):
        with self.assertRaisesRegex(ValueError,'FUTURE'):q.archive(self.root,self.payload,self.at-pd.Timedelta(seconds=1))
    def test_no_backfilled_context(self):
        q.archive(self.root,self.payload,self.at+pd.Timedelta(days=10));r=self.table('q9_predictions')[0][1]
        self.assertEqual(r['market_state']['state'],'unknown');self.assertFalse(r['primary_daily'])
    def test_before_origin_close_rejected(self):
        self.payload['generated_at']='2026-01-05T14:00:00+08:00'
        with self.assertRaisesRegex(ValueError,'BEFORE'):self.archive()
    def test_no_premature_settlement(self):
        self.archive();self.snap=self.snapshot('v2','2026-01-06T18:10:00+08:00')
        self.assertEqual(q.confirm(self.root,'2026-01-06T17:59:00+08:00')['confirmed'],0)
    def test_confirmed_errors_and_no_execution_fabrication(self):
        self.archive();self.snap=self.snapshot('v2','2026-01-06T18:10:00+08:00')
        self.assertEqual(q.confirm(self.root,'2026-01-06T18:20:00+08:00')['confirmed'],1)
        r=self.table('q9_outcomes')[0][1];self.assertAlmostEqual(r['actual_return'],.1);self.assertAlmostEqual(r['prediction_error'],-.08);self.assertFalse(r['direction_error']);self.assertIsNone(r['execution_error']);self.assertIsNone(r['brier_error'])
    def test_revision_cannot_overwrite(self):
        self.archive();self.snap=self.snapshot('v2','2026-01-06T18:10:00+08:00');q.confirm(self.root,'2026-01-06T18:20:00+08:00')
        self.snap=self.snapshot('v3','2026-01-07T18:10:00+08:00',(10,20));q.confirm(self.root,'2026-01-07T18:20:00+08:00');self.assertAlmostEqual(self.table('q9_outcomes')[0][1]['actual_return'],.1)
    def test_synthetic_outcome_rejected(self):
        self.archive();self.snap=self.snapshot('v2','2026-01-06T18:10:00+08:00');self.snap['meta']['synthetic']=True
        self.assertEqual(q.confirm(self.root,'2026-01-06T18:20:00+08:00')['confirmed'],0)
    def test_stale_outcome_rejected(self):
        self.archive();self.assertTrue(q.confirm(self.root,'2026-01-06T18:20:00+08:00')['errors'])
    def test_mutated_archive_rejected(self):
        self.archive();self.snap=self.snapshot('v2','2026-01-06T18:10:00+08:00');(self.root/'data/normalized/600001/versions/v2/hfq.csv').write_text('tampered')
        self.assertEqual(q.confirm(self.root,'2026-01-06T18:20:00+08:00')['confirmed'],0)
    def test_exact_date_required(self):
        self.archive();self.snap=self.snapshot('v2','2026-01-06T18:10:00+08:00');self.snap['hfq']=self.snap['hfq'].iloc[:1]
        self.assertEqual(q.confirm(self.root,'2026-01-06T18:20:00+08:00')['confirmed'],0)
    def test_database_immutable(self):
        self.archive()
        with q.database(self.root) as db:
            with self.assertRaises(sqlite3.IntegrityError):db.execute('DELETE FROM q9_predictions')
    def test_report_no_samples_honest(self):
        self.archive();r=q.report(self.root,self.at)
        self.assertEqual(r['summary']['status'],'NO_CONFIRMED_SAMPLES');self.assertEqual(r['pending']['NOT_MATURED'],1);self.assertIsNone(r['execution_error']['value']);self.assertTrue((self.root/'reports/prediction_quality/latest/index.html').exists())
    def test_calibrated_probability_only(self):
        f=self.payload['forecasts'][0]['forecasts']['1'];f.update(up_probability_calibrated=.8,probability_calibration={'independent_validated':True});self.archive();self.snap=self.snapshot('v2','2026-01-06T18:10:00+08:00');q.confirm(self.root,'2026-01-06T18:20:00+08:00')
        self.assertAlmostEqual(self.table('q9_outcomes')[0][1]['brier_error'],.04)
    def test_invalid_probability_rejected(self):
        self.payload['forecasts'][0]['forecasts']['1'].update(up_probability_calibrated=2,probability_calibration={'independent_validated':True})
        with self.assertRaisesRegex(ValueError,'PROBABILITY'):self.archive()
    def test_missing_calendar_target_pending(self):
        self.payload['forecasts'][0]['forecasts']['1']['forecast_target_date']=None;self.archive();self.assertEqual(q.report(self.root,self.at)['pending']['TARGET_UNKNOWN'],1)
    def test_unknown_input_never_primary(self):
        self.payload['data_versions']['600001']='other';self.archive();self.assertFalse(self.table('q9_predictions')[0][1]['primary_daily'])
    def test_hash_tamper_detected(self):
        self.archive()
        with q.database(self.root) as db:
            db.execute('DROP TRIGGER q9_predictions_update');db.execute("UPDATE q9_predictions SET body='{}'")
        with self.assertRaisesRegex(ValueError,'HASH'):self.table('q9_predictions')
    def test_insufficient_statistics_not_healthy(self):
        rows=[dict(symbol='x',horizon=1,origin='2026-01-05',target='2026-01-06',predicted_return=.02,actual_return=.01,prediction_error=.01,direction_error=False,probability_up=None,brier_error=None)]
        r=q.stats(rows);self.assertEqual(r['degradation'],'INSUFFICIENT_24_ORIGIN_CLUSTERS');self.assertIsNone(r['bias_95pct_cluster_t_interval']);self.assertIsNone(r['ece'])
    def test_overlap_and_persistent_bias(self):
        dates=pd.bdate_range('2025-01-01',periods=100)
        rs=[dict(symbol=s,horizon=5,origin=str(d.date()),target=str((d+pd.offsets.BDay(5)).date()),predicted_return=.03,actual_return=.01,prediction_error=.02,direction_error=False,probability_up=.8,brier_error=.04) for d in dates for s in ('x','y')]
        r=q.stats(rs);self.assertEqual(r['independent_origin_clusters'],20);self.assertEqual(r['persistent_bias'],'POSITIVE');self.assertAlmostEqual(r['ece'],.2)
    def test_after_next_open_not_primary(self):
        self.at=pd.Timestamp('2026-01-06T10:00:00+08:00');self.payload['generated_at']=str(self.at);self.archive();self.assertFalse(self.table('q9_predictions')[0][1]['primary_daily'])
    def test_missing_calendar_not_primary(self):
        self.payload.pop('next_session');self.archive();self.assertFalse(self.table('q9_predictions')[0][1]['primary_daily'])
    def test_groups_separate_horizons_and_research(self):
        self.archive();self.snap=self.snapshot('v2','2026-01-06T18:10:00+08:00');r=q.report(self.root,'2026-01-06T18:20:00+08:00');self.assertIn('600001|T+1|RESEARCH_ONLY',r['groups']['stock'])
    def test_reconcile_real_receipts_not_invented(self):
        evidence={'orders':[dict(order_id='synthetic-only',side='BUY',planned_shares=100,arrival_price=10,arrival_at=str(self.at),estimated_fees=5,fills=[dict(fill_id='f',shares=100,price=10.1,fees=6,filled_at=str(self.at))])],'portfolio_periods':[dict(start_at=str(self.at),end_at=str(self.at+pd.Timedelta(days=1)),start_nav=100,end_nav=102,ex_ante_expected_return=.03)]}
        r=q.reconcile(evidence);self.assertFalse(r['verified']);self.assertAlmostEqual(r['execution'][0]['price_shortfall_amount'],10);self.assertAlmostEqual(r['portfolio'][0]['return_deviation'],-.01)
        q.import_reference(self.root,self.write_reference(evidence));self.assertEqual(self.table('q9_outcomes'),[])
    def write_reference(self,e):
        p=self.root/'reference.json';p.write_text(json.dumps(e));return p
    def test_no_fill_does_not_mean_zero_execution_error(self):
        r=q.reconcile({'orders':[dict(order_id='synthetic',side='SELL',planned_shares=100,arrival_price=10,arrival_at=str(self.at),estimated_fees=5,fills=[])]})
        self.assertIsNone(r['execution'][0]['implementation_shortfall_bps']);self.assertIsNone(r['execution'][0]['price_shortfall_amount'])
    def test_portfolio_flows_not_naively_subtracted(self):
        r=q.reconcile({'portfolio_periods':[dict(start_at=str(self.at),end_at=str(self.at+pd.Timedelta(days=1)),start_nav=100,end_nav=200,ex_ante_expected_return=.1,external_flows=[{'amount':100}])]})
        self.assertIsNone(r['portfolio'][0]['return_deviation'])
    def test_invalid_fill_rejected(self):
        e={'orders':[dict(order_id='synthetic',side='BUY',planned_shares=100,arrival_price=10,arrival_at=str(self.at),estimated_fees=5,fills=[dict(fill_id='f',shares=200,price=10,fees=5,filled_at=str(self.at))])]}
        with self.assertRaisesRegex(ValueError,'MORE_THAN'):q.reconcile(e)
    def test_sql_view_and_future_null(self):
        self.archive()
        with q.database(self.root) as db:
            row=db.execute('SELECT symbol,predicted_return,actual_return,prediction_error FROM q9_prediction_results').fetchone()
        self.assertEqual(row[0],'600001');self.assertEqual(row[1],.02);self.assertIsNone(row[2]);self.assertIsNone(row[3])
