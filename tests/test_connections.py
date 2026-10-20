import json
import tempfile
from pathlib import Path
import unittest
import numpy as np
import pandas as pd
from asharequant.connections import merge_quote,validate_account,import_account,import_fee_observation,market_environment
from asharequant.probability_calibration import calibrate
from asharequant.execution_replay import simulate
from asharequant.decision import DEFAULTS,build_plan
AT='2026-10-19T10:00:00+08:00'
def tq():
    return dict(symbol='600031',name='三一重工',price=10.,previous_close=10.,limit_up=11.,limit_down=9.,asof=AT,bid=9.99,ask=10.01,source='SYNTHETIC')
class ConnectionTests(unittest.TestCase):
    def test_fresh_two_vendor_quote(self):
        t=tq();q=merge_quote(t,t.copy(),dict(complete=True,date='2026-10-19',items=[]),'2026-10-19',AT,True)
        self.assertEqual(q['status'],'trading');self.assertEqual(q['limit_regime'],'ordinary_10pct')
    def test_stale_quotes_not_relabelled_live(self):
        t=tq();q=merge_quote(t,t.copy(),dict(complete=True,date='2026-10-19',items=[]),'2026-10-19','2026-10-19T11:00:00+08:00',True)
        self.assertEqual(q['status'],'unknown');self.assertEqual(q['asof'],AT)
    def test_disagreement_and_missing_status_block(self):
        t=tq();q=merge_quote(t,{**t,'price':10.2},dict(complete=True,date='2026-10-19',items=[]),'2026-10-19',AT,True)
        self.assertNotEqual(q['status'],'trading');self.assertIn('SOURCE_CONFLICT:price',q['checks'])
        self.assertNotEqual(merge_quote(t,t,None,'2026-10-19',AT,True)['status'],'trading')
    def test_suspension_positive_evidence(self):
        t=tq();s=dict(complete=True,date='2026-10-19',source='SYNTHETIC',fetched_at=AT,items=[dict(SECURITY_CODE='600031')])
        self.assertEqual(merge_quote(t,t,s,'2026-10-19',AT,True)['status'],'suspended_or_event')
    def test_special_names_and_unseasoned_not_ordinary(self):
        t={**tq(),'name':'ST测试'};self.assertFalse(merge_quote(t,t,None,'2026-10-19',AT,True)['ordinary_session'])
        self.assertIsNone(merge_quote(tq(),tq(),None,'2026-10-19',AT,False)['ordinary_session'])
    def test_account_snapshot_not_live_and_private_fields_removed(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'data').mkdir();p=root/'input.json'
            a=dict(broker='华福证券',cash=525.0,available_cash=525.0,positions={'600031':dict(shares=3000,sellable=3000)},source='SYNTHETIC',asof=None,account_number='DO_NOT_PERSIST')
            p.write_text(json.dumps(a));r=import_account(root,p)
            self.assertFalse(r['live_connection']);self.assertNotIn('account_number',r);self.assertEqual(r['positions']['600031']['shares'],3000)
    def test_bad_account_rejected(self):
        with self.assertRaises(ValueError):validate_account(dict(broker='华福证券',cash=10,available_cash=11,positions={}))
    def test_observed_fee_not_inferred_contract(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'data').mkdir();p=root/'fee.json'
            d=dict(side='SELL',symbol='600031',notional=28240.,net_cash=28220.60,commission=5.,stamp=14.12,transfer=.28,source='SYNTHETIC',trade_date=None)
            p.write_text(json.dumps(d));r=import_fee_observation(root,p)
            self.assertFalse(r['fees_verified']);self.assertTrue(r['commission_rate_unknown']);self.assertAlmostEqual(r['commission_upper_bound_bps'],5/28240*10000)
            d['net_cash']=28000;p.write_text(json.dumps(d))
            with self.assertRaises(ValueError):import_fee_observation(root,p)
    def test_missing_index_unknown(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'data').mkdir();self.assertEqual(market_environment(root,'2026-10-19')['state'],'unknown')
    def test_unknown_snapshot_timestamp_blocks(self):
        r=build_plan([],None,DEFAULTS,dict(cash=525.0,available_cash=525.0,positions={},source='screenshot',asof=None),AT)
        self.assertIn('ACCOUNT_STALE_OR_UNVERIFIED',r['blockers'])
class CalibrationTests(unittest.TestCase):
    def fixture(self):
        days=pd.bdate_range('2023-01-01',periods=520);rng=np.random.default_rng(51)
        return pd.DataFrame([dict(date=d,label_end=days[i+20],symbol=str(s),predicted=float(x),actual=float(x+rng.normal(0,.03))) for i,d in enumerate(days[:-20]) for s,x in enumerate(rng.normal(0,.05,5))])
    def test_purged_split_and_insufficient_independent_blocks(self):
        r=calibrate(self.fixture(),'2025-12-31');self.assertLess(r['train_last_label_end'],r['test_first_date']);self.assertLess(r['holdout_blocks'],30);self.assertFalse(r['independent_validated'])
    def test_holdout_cannot_change_fitted_model(self):
        f=self.fixture();a=calibrate(f,'2025-12-31');f.loc[f.date>=pd.Timestamp(a['split_date']),'actual']*=-1
        self.assertEqual(a['model'],calibrate(f,'2025-12-31')['model'])
    def test_future_labels_excluded(self):
        f=self.fixture();a=calibrate(f,'2024-01-01');f.loc[f.label_end>'2024-01-01','actual']=99;self.assertEqual(a,calibrate(f,'2024-01-01'))
    def test_duplicate_rejected(self):
        f=self.fixture()
        with self.assertRaises(ValueError):calibrate(pd.concat([f,f.iloc[:1]]),'2025-12-31')
class ReplayTests(unittest.TestCase):
    def setup_data(self):
        days=pd.bdate_range('2026-01-05',periods=4);bars={'600031':pd.DataFrame(dict(open=[10.,11.,12.,12.],close=[10.,11.,12.,12.]),index=days)}
        statuses={('600031',str(d.date())):dict(source='SYNTHETIC',status='trading',corporate_action_status='none',limit_down=8.,limit_up=14.) for d in days}
        signals={str(days[0].date()):{'600031':.1},str(days[1].date()):{'600031':0.}}
        return days,bars,statuses,signals
    def test_next_open_t_plus_one_and_cash(self):
        days,bars,status,signals=self.setup_data();r=simulate(days,bars,signals,status,DEFAULTS)
        self.assertEqual(len(r['fills']),2);self.assertEqual(r['fills'][0]['date'],str(days[1].date()));self.assertGreater(r['fills'][0]['price'],11.)
        self.assertEqual(r['fills'][1]['date'],str(days[2].date()));self.assertEqual(r['fills'][0]['shares']%100,0);self.assertTrue(all(x['cash']>=0 for x in r['ledger']))
        self.assertEqual(r['ledger'][1]['positions']['600031']['sellable'],0)
    def test_unknown_status_never_filled(self):
        days,bars,status,signals=self.setup_data();r=simulate(days,bars,signals,{},DEFAULTS);self.assertEqual(r['fills'],[]);self.assertEqual(r['coverage'],0)
    def test_limit_open_and_suspension_no_fill(self):
        days,bars,status,signals=self.setup_data();status[('600031',str(days[1].date()))]['limit_up']=11.
        self.assertEqual(simulate(days,bars,signals,status,DEFAULTS)['fills'],[])
        status[('600031',str(days[1].date()))]['status']='suspended';self.assertEqual(simulate(days,bars,signals,status,DEFAULTS)['fills'],[])
    def test_corporate_cash_and_split(self):
        days,bars,status,signals=self.setup_data();signals={str(days[0].date()):{'600031':.1}}
        status[('600031',str(days[2].date()))].update(corporate_action_status='event',action=dict(share_multiplier=2.,cash_per_share=.1,source='SYNTHETIC'))
        r=simulate(days,bars,signals,status,DEFAULTS);self.assertEqual(r['ledger'][2]['positions']['600031']['shares'],2*r['ledger'][1]['positions']['600031']['shares'])
    def test_future_prices_cannot_change_prior_fill(self):
        days,bars,status,signals=self.setup_data();r=simulate(days,bars,signals,status,DEFAULTS);bars['600031'].loc[days[3],'open']=50.
        self.assertEqual(r['fills'],simulate(days,bars,signals,status,DEFAULTS)['fills'])

class ArchiveTests(unittest.TestCase):
    def test_archive_pointer_validates_matching_forecast(self):
        from asharequant.probability_calibration import source_records
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);archive=root/'reports/forecasts/arbitrary-stamp';archive.mkdir(parents=True)
            payload=dict(generated_at='2026-10-19T15:00:00.000001+08:00',model='test')
            (archive/'forecast.json').write_text(json.dumps(payload))
            (archive/'validation_predictions.csv').write_text('symbol,date,label_end,predicted,actual,horizon\n600031,2026-01-01,2026-02-01,0.01,0.02,20\n')
            (root/'reports/forecast_latest.json').write_text(json.dumps(dict(folder=str(archive))))
            frame,path=source_records(root,payload);self.assertEqual(len(frame),1)
            with self.assertRaises(ValueError):source_records(root,{**payload,'model':'other'})
    def test_sina_price_crosscheck_preserves_limit_source(self):
        t=tq();e={k:v for k,v in t.items() if k not in ('limit_up','limit_down')};e['source']='Sina'
        q=merge_quote(t,e,dict(complete=True,date='2026-10-19',items=[]),'2026-10-19',AT,True)
        self.assertEqual(q['status'],'trading');self.assertEqual(q['limit_source'],'SYNTHETIC')
