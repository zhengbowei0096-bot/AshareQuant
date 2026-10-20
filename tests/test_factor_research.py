import unittest
import numpy as np
import pandas as pd
from asharequant.factor_research import (decision_time,released,latest_period,industry_features,valuation_features,aligned,promotion_allowed)

class FakeStore:
 def __init__(self,rows,seen):self.rows=rows;self.seen=pd.Timestamp(seen)
 def visible(self,ds,entity,point):return (self.seen,'hash',self.rows) if self.seen<=point else None

class FactorResearchTests(unittest.TestCase):
 def test_notice_date_and_revision_do_not_enter_early(self):
  row={'_observation_date':'2026-06-30','NOTICE_DATE':'2026-08-15','UPDATE_DATE':'2026-09-10'}
  self.assertFalse(released(row,decision_time('2026-09-09')))
  self.assertTrue(released(row,decision_time('2026-09-11')))
  self.assertIsNone(latest_period([row],decision_time('2026-08-20'),550))
 def test_first_seen_financial_not_backfilled(self):
  days=pd.bdate_range('2026-10-15','2026-10-22');close=pd.Series(1.,index=days)
  row={'_observation_date':'2026-06-30','NOTICE_DATE':'2026-08-15','UPDATE_DATE':'2026-08-15','ROEJQ':10,'ZCFZL':50,'TOTALOPERATEREVETZ':5,'PARENTNETPROFITTZ':6}
  from unittest.mock import patch
  with patch('asharequant.factor_research.now',return_value=pd.Timestamp('2026-10-30T00:00:00Z')):
   f=aligned(FakeStore([row],'2026-10-21T00:01:00Z'),'600001',days,close,'financial')
  self.assertTrue(f.loc[:'2026-10-20'].isna().all().all())
  self.assertAlmostEqual(f.loc['2026-10-21','roe'],.1)
 def test_industry_missing_window_is_not_filled(self):
  days=pd.bdate_range('2026-01-01',periods=100);rows=[{'_observation_date':str(d.date()),'收盘':100+i} for i,d in enumerate(days) if i!=70]
  f=industry_features(rows,days,pd.Series(np.arange(100)+20.,index=days))
  self.assertTrue(f.loc[days[70]:days[90],'industry_r20'].isna().all())
  self.assertFalse(pd.isna(f.loc[days[91],'industry_r20']))
  changed=[{**x,'收盘':x['收盘']*2} if x['_observation_date']>=str(days[95].date()) else x for x in rows]
  pd.testing.assert_frame_equal(f.iloc[:95],industry_features(changed,days,pd.Series(np.arange(100)+20.,index=days)).iloc[:95])
 def test_valuation_negative_yield_zero_and_missing(self):
  days=pd.bdate_range('2026-01-01',periods=3)
  rows=[{'_observation_date':str(days[0].date()),'PE(TTM)':-10,'市净率':2,'总市值':100},{'_observation_date':str(days[1].date()),'PE(TTM)':0,'市净率':None,'总市值':0}]
  f=valuation_features(rows,days)
  self.assertEqual(f.earnings_yield.iloc[0],-.1);self.assertTrue(f.iloc[1:].isna().all().all())
 def test_low_frequency_staleness_and_conflict(self):
  row={'_observation_date':'2026-01-31'}
  self.assertIsNone(latest_period([row],decision_time('2026-10-19'),100))
  with self.assertRaises(ValueError):latest_period([row,row],decision_time('2026-02-02'),100)
 def test_diagnostic_cannot_promote(self):
  self.assertFalse(promotion_allowed({'preliminary_screen_passed':True}))
  self.assertFalse(promotion_allowed(None,True))
