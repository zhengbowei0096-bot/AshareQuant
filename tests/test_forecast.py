import copy
import unittest
import numpy as np
import pandas as pd
from asharequant.forecast import (DEFAULTS, FEATURES, features_and_labels,
    validate_forecast, fit_predict, allocate, entry_cost, validate_config)


def fixture(n=1100):
    rng=np.random.default_rng(81)
    dates=pd.bdate_range('2021-01-01',periods=n)
    close=50*np.exp(np.cumsum(rng.normal(.0002,.012,n)))
    return pd.DataFrame(dict(date=dates,open=close,close=close,high=close*1.01,
                            low=close*.99,volume=np.full(n,100000.))),dates


def candidate(code,sector='行业A',price=31.):
    f=dict(predicted_return=.05,up_probability_empirical=.8,volatility_horizon=.1,
           validation=dict(preliminary_screen_passed=True))
    return dict(symbol=code,name=code,sector=sector,close=price,data_eligible=True,
                forecasts={'5':copy.deepcopy(f),'20':copy.deepcopy(f)})


class ForecastTests(unittest.TestCase):
    def test_features_have_no_future_dependency(self):
        raw,cal=fixture()
        a=features_and_labels(raw,cal,20)
        altered=raw.copy()
        altered.loc[800:,'close']*=2
        altered.loc[800:,'high']*=2
        altered.loc[800:,'low']*=2
        b=features_and_labels(altered,cal,20)
        pd.testing.assert_frame_equal(a[FEATURES].iloc[:800],b[FEATURES].iloc[:800])

    def test_labels_are_market_sessions_not_observed_bars(self):
        raw,cal=fixture(120)
        missing=cal[90]
        frame=features_and_labels(raw[raw.date!=missing],cal,5)
        self.assertTrue(pd.isna(frame.loc[cal[85],'target']))
        self.assertEqual(frame.loc[cal[85],'label_end'],missing)
        self.assertTrue(frame.loc[missing,FEATURES].isna().any())

    def test_walk_forward_purges_future_label_overlap(self):
        raw,cal=fixture()
        frame=features_and_labels(raw,cal,20)
        out,metrics=validate_forecast(frame,20,DEFAULTS)
        self.assertEqual(len(out),252)
        self.assertEqual(len(metrics['folds']),4)
        for fold in metrics['folds']:
            self.assertLess(fold['training_last_label_end'],fold['test_start'])
        self.assertLessEqual(metrics['nonoverlapping_observations'],14)
        self.assertTrue(np.isfinite(out.predicted).all())

    def test_test_rows_do_not_change_training_standardization(self):
        raw,cal=fixture()
        f=features_and_labels(raw,cal,5).dropna()
        _,a=fit_predict(f.iloc[:600],f.iloc[600:610],10.)
        changed=f.iloc[600:610].copy()
        changed[FEATURES]*=100
        _,b=fit_predict(f.iloc[:600],changed,10.)
        self.assertEqual(a,b)

    def test_allocation_obeys_lots_cash_sector_and_position_caps(self):
        cfg=copy.deepcopy(DEFAULTS)
        rows=[candidate(str(i),'行业A' if i<4 else '行业B') for i in range(8)]
        plan=allocate(rows,cfg)
        self.assertGreater(len(plan['positions']),0)
        self.assertGreaterEqual(plan['cash'],150000*.4)
        self.assertAlmostEqual(plan['cash']+plan['invested']+plan['estimated_entry_cost'],150000,places=2)
        sectors={}
        for p in plan['positions']:
            self.assertEqual(p['shares']%100,0)
            self.assertLessEqual(p['weight'],.15)
            sectors[p['sector']]=sectors.get(p['sector'],0)+p['reference_notional']
        self.assertTrue(all(v<=150000*.25 for v in sectors.values()))
        self.assertLessEqual(len(plan['positions']),5)

    def test_failed_validation_and_high_lot_price_keep_cash(self):
        cfg=copy.deepcopy(DEFAULTS)
        a=candidate('600000')
        a['forecasts']['5']['validation']['preliminary_screen_passed']=False
        b=candidate('600001',price=2000.)
        plan=allocate([a,b],cfg)
        self.assertEqual(plan['cash'],150000.)
        self.assertEqual(plan['positions'],[])

    def test_risk_threshold_blocks_new_exposure_and_has_report_reason(self):
        cfg={**DEFAULTS,'equity':120000.}
        rows=[candidate('600000')]
        plan=allocate(rows,cfg)
        self.assertEqual(plan['status'],'risk_review_pause')
        self.assertEqual(plan['positions'],[])
        self.assertTrue(rows[0]['allocation_blockers'])

    def test_minimum_commission_and_bad_parameters(self):
        self.assertGreaterEqual(entry_cost(100.,DEFAULTS),5.)
        with self.assertRaises(ValueError):
            validate_config({**DEFAULTS,'max_stock_weight':1.1})
        with self.assertRaises(ValueError):
            validate_config({**DEFAULTS,'ridge_alpha':-1})
