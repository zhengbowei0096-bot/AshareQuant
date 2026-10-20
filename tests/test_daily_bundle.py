import unittest,tempfile,json
from pathlib import Path
from unittest.mock import patch
import pandas as pd
from asharequant.daily_bundle import nav_returns,performance,export,safe_date
class BundleTests(unittest.TestCase):
 def test_returns_and_compounding(self):
  days=['2026-10-16','2026-10-19','2026-10-20'];rows=[dict(date=d,nav=n,external_flow=0) for d,n in zip(days,[100,110,99])]
  r=nav_returns(rows,days[-1],days);self.assertAlmostEqual(r[-1]['daily_return'],-.1);self.assertAlmostEqual(r[-1]['cumulative_return'],-.01);self.assertFalse(r[-1]['verified'])
 def test_cashflows_unknown_and_gaps_block(self):
  days=['2026-10-16','2026-10-19','2026-10-20']
  for flow in (None,10):
   rows=[dict(date=days[0],nav=100),dict(date=days[1],nav=120,external_flow=flow)]
   self.assertIsNone(nav_returns(rows,days[-1],days)[-1]['cumulative_return'])
  rows=[dict(date=days[0],nav=100),dict(date=days[2],nav=120,external_flow=0)]
  self.assertIsNone(nav_returns(rows,days[-1],days)[-1]['daily_return'])
 def test_duplicate_nav_rejected(self):
  with self.assertRaises(ValueError):nav_returns([dict(date='2026-10-19',nav=1)]*2,'2026-10-19',[])
 def test_date_path_rejected(self):
  with self.assertRaises((ValueError,TypeError)):safe_date('../x')
 def test_stale_price_and_missing_account(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);(root/'data').mkdir();pd.DataFrame({'trade_date':['2026-10-16','2026-10-19','2026-10-20']}).to_csv(root/'data/calendar.csv',index=False)
   watch=pd.DataFrame([dict(symbol='600000',name='测试')]);snap=dict(hfq=pd.DataFrame(dict(date=pd.to_datetime(['2026-10-16','2026-10-19']),close=[10,11])),meta=dict(source='tencent',version='test',fetched_at='2026-10-19T18:00:00+08:00'))
   with patch('asharequant.daily_bundle.read_watchlist',return_value=watch),patch('asharequant.daily_bundle.read_snapshot',return_value=snap):
    r=performance(root,'2026-10-20');self.assertIsNone(r['stock_reference'][0]['daily_return']);self.assertIsNone(r['account_today']);self.assertFalse((root/'data/performance_tracking.json').exists())
    r=performance(root,'2026-10-19');self.assertAlmostEqual(r['stock_reference'][0]['daily_return'],.1);self.assertTrue((root/'data/performance_tracking.json').exists())
 def test_export_dates_preserved_and_failure_label(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);watch=pd.DataFrame([dict(symbol='600000',name='测试')])
   with patch('asharequant.daily_bundle.read_watchlist',return_value=watch),patch('asharequant.daily_bundle.read_snapshot',return_value=None):
    a=export(root,'2026-10-19',{'status':'FAILED'},root/'desktop');b=export(root,'2026-10-20',{'status':'PREVIEW'},root/'desktop')
   self.assertTrue(Path(a['overview']).exists());self.assertIn('PREVIEW',Path(b['overview']).read_text());self.assertNotEqual(a['directory'],b['directory'])

 def test_delivery_failure_is_not_success(self):
  from asharequant.daily import run
  with tempfile.TemporaryDirectory() as d,patch('asharequant.daily._run',return_value={'status':'SUCCESS','session':'2026-10-19'}),patch('asharequant.daily_bundle.export',side_effect=OSError('disk full')):
   r=run(Path(d));self.assertEqual(r['status'],'FAILED');self.assertIn('disk full',r['delivery_error'])
 def test_existing_success_delivery_not_duplicated(self):
  from asharequant.daily import run
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);page=root/'page.html';page.write_text('ok');p=root/'reports/delivery/2026-10-19/latest.json';p.parent.mkdir(parents=True);p.write_text(json.dumps({'pipeline_status':'SUCCESS','overview':str(page)}))
   with patch('asharequant.daily._run',return_value={'status':'ALREADY_COMPLETED','session':'2026-10-19'}),patch('asharequant.daily_bundle.export',side_effect=AssertionError('duplicate')):
    self.assertEqual(run(root)['delivery']['overview'],str(page))
