import unittest,tempfile,json
from pathlib import Path
import pandas as pd
from asharequant.factors import normalise,period,persist,connect,asof_records

class FactorsTest(unittest.TestCase):
 def test_month_quarter(self):
  self.assertEqual(period('2026年09月份'),'2026-09-30')
  self.assertEqual(period('2026年第2季度'),'2026-06-30')
  self.assertEqual(period('2026年第1-3季度'),'2026-09-30')
 def test_missing_dates_negative_pe_and_null(self):
  f=pd.DataFrame({'数据日期':['2026-01-02'],'总市值':[100],'流通市值':[50],'总股本':[10],'流通股本':[5],'PE(TTM)':[-3],'市净率':[None]})
  rows=normalise(f,'valuations','2021-01-01','2026-10-19')
  self.assertEqual(rows[0]['PE(TTM)'],-3);self.assertIsNone(rows[0]['市净率'])
  self.assertNotIn('NOTICE_DATE',rows[0])
  with self.assertRaises(ValueError):normalise(f.assign(总市值=-1),'valuations','2021-01-01','2026-10-19')
 def test_conflicting_dates_and_bad_ohlc(self):
  f=pd.DataFrame({'日期':['2026-01-02'],'开盘':[10],'最高':[11],'最低':[9],'收盘':[10]})
  with self.assertRaises(ValueError):normalise(f.assign(收盘=12),'industry_bars','2021-01-01','2026-10-19')
  with self.assertRaises(ValueError):normalise(pd.concat([f,f.assign(收盘=11)]),'industry_bars','2021-01-01','2026-10-19')
 def test_vintages_no_lookahead_idempotence_and_rollback(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);(root/'data').mkdir()
   row={'_observation_date':'2026-06-30','REPORT_DATE':'2026-06-30','NOTICE_DATE':'2026-08-15','UPDATE_DATE':'2026-08-15','EPSJB':1}
   meta={'start':'2021-01-01','end':'2026-10-19','fetched_at':'2026-10-19T12:00:00+00:00'}
   h=persist(root,'financial_main','000001',pd.DataFrame([row]),[row],meta)
   persist(root,'financial_main','000001',pd.DataFrame([row]),[row],{**meta,'fetched_at':'2026-10-19T13:00:00+00:00'})
   with connect(root) as db:self.assertEqual(db.execute('select count(*) from factor_snapshots').fetchone()[0],1)
   self.assertEqual(asof_records(root,'financial_main','000001','2026-09-01T00:00:00+00:00'),[])
   revised={**row,'EPSJB':2,'UPDATE_DATE':'2026-10-20'}
   persist(root,'financial_main','000001',pd.DataFrame([revised]),[revised],{**meta,'fetched_at':'2026-10-20T12:00:00+00:00'})
   self.assertEqual(asof_records(root,'financial_main','000001','2026-10-19T14:00:00+00:00')[0]['EPSJB'],1)
   self.assertEqual(asof_records(root,'financial_main','000001','2026-10-20T14:00:00+00:00')[0]['EPSJB'],2)
   with self.assertRaises(ValueError):persist(root,'financial_main','000001',pd.DataFrame(),[],{**meta,'fetched_at':'2026-10-21T12:00:00+00:00'})
   with connect(root) as db:self.assertEqual(db.execute('select count(*) from factor_fetches').fetchone()[0],3)
 def test_no_macro_or_membership_backfill(self):
  with tempfile.TemporaryDirectory() as tmp:
   r=Path(tmp);(r/'data').mkdir()
   for dataset in ['macro_cpi','industry_members']:
    rows=[{'value':1}];meta={'start':'2021-01-01','end':'2026-10-19','fetched_at':'2026-10-20T12:00:00+00:00'}
    persist(r,dataset,'a',pd.DataFrame(rows),rows,meta)
    self.assertEqual(asof_records(r,dataset,'a','2026-10-19T12:00:00+00:00'),[])

 def test_future_notice_and_timezone(self):
  with tempfile.TemporaryDirectory() as tmp:
   r=Path(tmp);(r/'data').mkdir()
   rows=[{'_observation_date':'2026-06-30','NOTICE_DATE':'2026-10-22'}]
   meta={'start':'2021-01-01','end':'2026-10-19','fetched_at':'2026-10-20T12:00:00+00:00'}
   persist(r,'financial_main','a',pd.DataFrame(rows),rows,meta)
   self.assertEqual(asof_records(r,'financial_main','a','2026-10-21T12:00:00+00:00'),[])
   with self.assertRaises(ValueError):asof_records(r,'financial_main','a','2026-10-21')

 def test_next_session_does_not_reuse_overnight_cache(self):
  from unittest.mock import patch
  from asharequant.factors import fetch,stamp
  with tempfile.TemporaryDirectory() as tmp:
   r=Path(tmp);(r/'data').mkdir()
   rows=[{'_observation_date':'2026-09-30','value':1}]
   meta={'start':'2021-10-19','end':'2026-10-19','fetched_at':stamp()}
   persist(r,'macro_cpi','china',pd.DataFrame(rows),rows,meta)
   with patch('asharequant.factors.subprocess.run',side_effect=RuntimeError('fresh download required')) as net,patch('asharequant.factors.time.sleep'):
    self.assertEqual(fetch(r,'macro_cpi','china',{},meta['start'],meta['end']),rows)
    net.assert_not_called()
    with self.assertRaisesRegex(RuntimeError,'fresh download required'):fetch(r,'macro_cpi','china',{},meta['start'],'2026-10-20')
    self.assertEqual(net.call_count,2)
