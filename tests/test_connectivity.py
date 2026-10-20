"""Synthetic temporary data only; no brokerage or investment acceptance evidence."""
import unittest,tempfile,json
from pathlib import Path
from unittest.mock import patch
from datetime import datetime
from zoneinfo import ZoneInfo
from asharequant import broker_records as b,public_history as h
from asharequant.scheduler import maintenance

class ConnectivityTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
 def fill(self):return {'成交日期':'20260105','成交时间':'10:00:00','证券代码':'000681','成交编号':'SYNTHETIC','买卖标志':'买入','成交数量':'100','成交价格':'10','费用合计':'5'}
 def test_fill_zeros_and_readonly(self):
  _,r=b.normalize('fills',self.fill());self.assertEqual(r['symbol'],'000681');self.assertFalse(r['verified']);self.assertEqual(r['fees'],5)
 def test_missing_fees_not_zero(self):
  r=self.fill();r.pop('费用合计');self.assertIsNone(b.normalize('fills',r)[1]['fees'])
 def test_future_date(self):
  r=self.fill();r['成交日期']='20990101'
  with self.assertRaises(ValueError):b.normalize('fills',r)
 def test_missing_fill_id(self):
  r=self.fill();r.pop('成交编号')
  with self.assertRaises(ValueError):b.normalize('fills',r)
 def test_nav_without_asof_no_facts(self):
  with self.assertRaises(ValueError):b.normalize('nav',{'总资产':'1'})
 def test_date_only_nav_not_live(self):
  _,r=b.normalize('nav',{'日期':'20260105','总资产':'100','资金余额':'20'});self.assertIsNone(r['timestamp']);self.assertFalse(r['verified']);self.assertIsNone(r['external_flow'])
 def test_nav_consistency(self):
  with self.assertRaises(ValueError):b.normalize('nav',{'日期':'20260105','总资产':'100','资金余额':'20','可用资金':'30'})
 def test_bad_side(self):
  r=self.fill();r['买卖标志']='送股'
  with self.assertRaises(ValueError):b.normalize('fills',r)
 def test_ambiguous_columns(self):
  r=self.fill();r['shares']='200'
  with self.assertRaises(ValueError):b.normalize('fills',r)
 def test_csv_gbk(self):
  raw='成交日期,证券代码,成交编号,买卖标志,成交数量,成交价格\n20260105,000681,synthetic,买入,100,10\n'.encode('gb18030');self.assertEqual(len(b.parse('fills',raw)),1)
 def test_ingest_empty_no_account_overwrite(self):
  p=self.root/'data/decision_account.json';p.parent.mkdir();p.write_text('{"existing":true}')
  r=b.ingest(self.root);self.assertEqual(r['counts'],{'nav':0,'fills':0});self.assertEqual(p.read_text(),'{"existing":true}')
 def test_ingest_idempotent_and_conflict(self):
  d=self.root/'data/broker_inbox/nav';d.mkdir(parents=True);p=d/'example.csv';p.write_text('date,nav\n2026-01-05,100\n')
  self.assertEqual(b.ingest(self.root)['added'],1);self.assertEqual(b.ingest(self.root)['added'],0)
  p.write_text('date,nav\n2026-01-05,200\n');r=b.ingest(self.root);self.assertTrue(r['errors']);self.assertEqual(r['counts']['nav'],1)
 def test_symlink_rejected(self):
  external=self.root/'external.csv';external.write_text('date,nav\n2026-01-05,100\n');d=self.root/'data/broker_inbox/nav';d.mkdir(parents=True);(d/'link.csv').symlink_to(external);self.assertTrue(b.ingest(self.root)['errors'])
 def data(self):return {'daily':[dict(code='sh.600031',date='2026-01-05',open='10',high='11',low='9',close='10',adjustflag='3',tradestatus='1',isST='0')]}
 def test_history_real_schema(self):self.assertEqual(len(h.normalize('600031',self.data())),1)
 def test_history_wrong_code(self):
  with self.assertRaises(ValueError):h.normalize('600030',self.data())
 def test_history_duplicate(self):
  d=self.data();d['daily']*=2
  with self.assertRaises(ValueError):h.normalize('600031',d)
 def test_history_unknown_state(self):
  d=self.data();d['daily'][0]['tradestatus']=''
  with self.assertRaises(ValueError):h.normalize('600031',d)
 def test_history_invalid_ohlc(self):
  d=self.data();d['daily'][0]['low']='100'
  with self.assertRaises(ValueError):h.normalize('600031',d)
 def test_crosscheck_price_disagreement(self):
  import pandas as pd
  snap=dict(raw=pd.DataFrame([dict(date=pd.Timestamp('2026-01-05'),open=12,high=12,low=12,close=12)]),meta={'version':'fixture'})
  with patch.object(h,'read_snapshot',return_value=snap):self.assertEqual(h.crosscheck(self.root,'600031',self.data()['daily'])['status'],'MISMATCH')
 def test_maintenance_without_daily(self):
  state={'asof':'2026-01-05','predictions':0,'confirmed':0,'pending':{}}
  with patch('asharequant.prediction_quality.run',return_value=state):
   r=maintenance(self.root);self.assertEqual(r['status'],'SUCCESS');self.assertIn('prediction_quality',r['steps']);self.assertEqual(maintenance(self.root)['status'],'ALREADY_MAINTAINED_RECENTLY')
 def test_quality_failure_visible(self):
  with patch('asharequant.prediction_quality.run',side_effect=ValueError('synthetic failure')):
   self.assertEqual(maintenance(self.root)['status'],'FAILED')
 def test_broker_failure_does_not_skip_quality(self):
  with patch('asharequant.broker_records.ingest',side_effect=ValueError()),patch('asharequant.prediction_quality.run',return_value=dict(asof='2026-01-05',predictions=0,confirmed=0,pending={})) as q:
   self.assertEqual(maintenance(self.root)['status'],'FAILED');q.assert_called_once()
 def test_authorized_external_source(self):
  directory=self.root/'official-export';directory.mkdir();(directory/'asset.csv').write_text('date,nav\n2026-01-05,100\n')
  (self.root/'broker_sources.json').write_text(json.dumps({'nav_directories':[str(directory)]}))
  self.assertEqual(b.ingest(self.root)['counts']['nav'],1)
 def test_relative_source_not_automatically_authorized(self):
  (self.root/'broker_sources.json').write_text(json.dumps({'nav_directories':['../other']}));self.assertTrue(b.ingest(self.root)['errors'])
 def test_invalid_source_config_does_not_skip_quality(self):
  (self.root/'broker_sources.json').write_text('{invalid')
  with patch('asharequant.prediction_quality.run',return_value=dict(asof='2026-01-05',predictions=0,confirmed=0,pending={})) as q:
   self.assertEqual(maintenance(self.root)['status'],'FAILED');q.assert_called_once()
