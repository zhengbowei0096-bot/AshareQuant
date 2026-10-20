import sqlite3
import unittest
import numpy as np
import pandas as pd
from asharequant.indices import validate_index, upsert, market_features
from asharequant.forecast import FEATURES, DEFAULTS, features_and_labels, validate_forecast


def frame(n=1100):
    days=pd.bdate_range('2021-01-01',periods=n)
    rng=np.random.default_rng(123)
    close=1000*np.exp(np.cumsum(rng.normal(0,.01,n)))
    f=pd.DataFrame(dict(date=days,open=close,high=close*1.01,low=close*.99,close=close,amount=np.full(n,10000.)))
    return f,days


class IndexTests(unittest.TestCase):
    def test_rejects_duplicates_missing_sessions_and_bad_prices(self):
        f,d=frame(100)
        self.assertEqual(len(validate_index(f,d.min(),d.max(),d)),100)
        for bad in [pd.concat([f,f.iloc[:1]]),f.iloc[1:],f.assign(close=-1),f.assign(high=np.nan)]:
            with self.assertRaises(ValueError):validate_index(bad,d.min(),d.max(),d)

    def test_upsert_keeps_units_null_amount_and_idempotent_keys(self):
        f,d=frame(10)
        meta=dict(fetched_at='2026-10-19',version='v1')
        with sqlite3.connect(':memory:') as db:
            upsert(db,'sh000300',f,meta);upsert(db,'sh000300',f,meta)
            self.assertEqual(db.execute('SELECT count(*) FROM market_index_bars').fetchone()[0],10)
            self.assertEqual(db.execute('SELECT source_volume_lots,amount_cny FROM market_index_bars LIMIT 1').fetchone(),(10000.,None))
            f.loc[0,'close']=999
            upsert(db,'sh000300',f,{**meta,'version':'v2'})
            self.assertEqual(db.execute('SELECT close FROM market_index_bars ORDER BY date LIMIT 1').fetchone()[0],999)

    def test_market_features_do_not_use_future_or_fill_gaps(self):
        f,d=frame(300);b=f.set_index('date');stock=b.close*2
        a=market_features(b,b,stock)
        later=b.copy();later.loc[d[200]:,'close']*=2
        changed=market_features(later,later,stock)
        pd.testing.assert_frame_equal(a.iloc[:200],changed.iloc[:200])
        missing=b.drop(d[150])
        with_gap=market_features(missing,missing,stock)
        self.assertTrue(pd.isna(with_gap.loc[d[150],'market_r1']))
        self.assertTrue(pd.isna(with_gap.loc[d[151],'market_r1']))

    def test_explicit_base_features_preserve_existing_model_and_purging(self):
        f,d=frame();f['volume']=f.amount
        stock=features_and_labels(f,d,20)
        a,am=validate_forecast(stock,20,DEFAULTS)
        b,bm=validate_forecast(stock,20,DEFAULTS,feature_columns=FEATURES)
        pd.testing.assert_frame_equal(a,b)
        self.assertEqual(am,bm)
        market=market_features(f.set_index('date'),f.set_index('date'),f.set_index('date').close)
        joined=stock.join(market)
        result,metrics=validate_forecast(joined,20,DEFAULTS,feature_columns=FEATURES+list(market.columns))
        self.assertTrue(result.date.equals(a.date))
        for fold in metrics['folds']:
            self.assertLess(fold['training_last_label_end'],fold['test_start'])
