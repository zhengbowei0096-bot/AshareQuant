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

class RetentionTests(unittest.TestCase):
 def test_latest_timestamp_not_folder_name_and_other_days_untouched(self):
  from asharequant.daily_bundle import retain_latest
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);day=root/'desktop/2026-10-20'
   for name,stamp in [('运行-231920','2026-10-20T23:19:20+08:00'),('运行-044214','2026-10-21T04:42:14+08:00')]:
    p=day/name;p.mkdir(parents=True);(p/'归档清单.json').write_text(json.dumps({'created_at':stamp}));(p/'00_每日总览.html').write_text(name)
   other=root/'desktop/2026-10-19/运行-old';other.mkdir(parents=True)
   result=retain_latest(root,day)
   self.assertEqual(result['removed'],1);self.assertTrue(other.exists())
   self.assertEqual([p.name for p in day.glob('运行-*')],['运行-044214'])
   self.assertEqual((Path(result['backup'])/'运行-231920/00_每日总览.html').read_text(),'运行-231920')
   self.assertIn('运行-044214',(day/'打开当天报告.html').read_text())
   self.assertEqual(retain_latest(root,day)['removed'],0)
 def test_incomplete_export_does_not_remove_existing_reports(self):
  from asharequant.daily_bundle import retain_latest
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);day=root/'desktop/2026-10-20';p=day/'运行-incomplete';p.mkdir(parents=True)
   with self.assertRaises(FileNotFoundError):retain_latest(root,day)
   self.assertTrue(p.exists())

class MainAccountTests(unittest.TestCase):
 def make_ledger(self,root):
  import sqlite3
  from asharequant.paper_account import put
  path=root/'data/dynamic/account.sqlite3';path.parent.mkdir(parents=True)
  rows=[dict(date=d,equity=e,daily_pnl=p,daily_return=r,cumulative_return=c,mode='PAPER_ONLY') for d,e,p,r,c in [('2026-10-21',101000,1000,.01,.01),('2026-10-22',100000,-1000,-1000/101000,0),('2026-10-23',102000,2000,.02,.02)]]
  with sqlite3.connect(path) as db:
   db.execute('CREATE TABLE records(kind TEXT,key TEXT,body TEXT,sha256 TEXT,PRIMARY KEY(kind,key))')
   for r in rows:put(db,'nav',r['date'],r)
  return path,rows
 def test_frozen_main_nav_only_no_future_or_recalculation(self):
  from asharequant.daily_bundle import account_returns
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);path,rows=self.make_ledger(root);raw=path.read_bytes()
   self.assertEqual(account_returns(root,'2026-10-22'),rows[:2]);self.assertEqual(path.read_bytes(),raw)
 def test_tampered_record_rejected(self):
  import sqlite3
  from asharequant.daily_bundle import account_returns
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);path,_=self.make_ledger(root)
   with sqlite3.connect(path) as db:db.execute("UPDATE records SET body='{}' WHERE key='2026-10-22'")
   with self.assertRaisesRegex(ValueError,'TAMPERED'):account_returns(root,'2026-10-22')
 def test_missing_main_account_does_not_create_ledger(self):
  from asharequant.daily_bundle import account_returns,account_section
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);self.assertEqual(account_returns(root,'2026-10-22'),[]);self.assertFalse((root/'data').exists())
   out=account_section([], '2026-10-22');self.assertIn('当日尚未完成结算',out);self.assertNotIn('0.00%',out)
 def test_export_uses_one_account_and_valid_final_manifest(self):
  import hashlib,csv
  from asharequant.reporting.plain_chinese import localize
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);_,rows=self.make_ledger(root)
   # An unrelated imported broker observation must not alter the main account.
   import sqlite3
   with sqlite3.connect(root/'data/market.sqlite3') as db:
    db.execute('CREATE TABLE broker_observations(id TEXT,body TEXT)');db.execute('INSERT INTO broker_observations VALUES(?,?)',('nav|x',json.dumps(dict(date='2026-10-22',nav=999999,external_flow=0))))
   with patch('asharequant.daily_bundle.read_watchlist',return_value=pd.DataFrame(columns=['symbol','name'])):
    result=export(root,'2026-10-22',{'status':'SUCCESS'},root/'desktop')
   folder=Path(result['directory']);page=(folder/'00_每日总览.html').read_text()
   with (folder/'账户每日与累计收益.csv').open(encoding='utf-8-sig') as f:data=list(csv.DictReader(f))
   self.assertEqual([r['date'] for r in data],['2026-10-21','2026-10-22']);self.assertEqual(float(data[-1]['equity']),100000)
   self.assertIn('主账户每日与累计收益',page);self.assertNotIn('真实账户收益单',page);self.assertNotIn('999999',page)
   self.assertEqual(localize(folder)['broken'],[])
   manifest=json.loads((folder/'归档清单.json').read_text())
   for name,sha in manifest['files'].items():self.assertEqual(hashlib.sha256((folder/name).read_bytes()).hexdigest(),sha,name)
 def test_current_day_not_replaced_by_previous_day(self):
  from asharequant.daily_bundle import account_section
  out=account_section([dict(date='2026-10-21',daily_return=.01,cumulative_return=.02)],'2026-10-22')
  self.assertIn('当日收益率：待补数据',out);self.assertIn('2026-10-21',out);self.assertIn('2.00%',out)
