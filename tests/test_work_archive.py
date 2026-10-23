import unittest,tempfile,json,zipfile
from pathlib import Path
from unittest.mock import patch
from datetime import datetime
from zoneinfo import ZoneInfo
from asharequant.work_archive import record
class WorkArchiveTests(unittest.TestCase):
 def test_environment_snapshots_and_tools_are_archived_without_account_data(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);(root/'config/environments').mkdir(parents=True);(root/'tools').mkdir();(root/'data').mkdir()
   (root/'config/environments/requirements-models.lock.txt').write_text('numpy==1.0\n');(root/'tools/verify_models.py').write_text('pass\n');(root/'data/account.sqlite3').write_bytes(b'private ledger')
   delivery=dict(session='2026-10-19',directory=str(root/'report'),overview=str(root/'report/index.html'))
   result=record(root,delivery,{'status':'SUCCESS'})
   with zipfile.ZipFile(result['source_snapshot']) as z:
    self.assertEqual(z.read('config/environments/requirements-models.lock.txt'),b'numpy==1.0\n');self.assertEqual(z.read('tools/verify_models.py'),b'pass\n');self.assertNotIn('data/account.sqlite3',z.namelist())
   self.assertEqual((root/'data/account.sqlite3').read_bytes(),b'private ledger')
 def test_work_date_separate_and_snapshot_reused(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);(root/'asharequant').mkdir();(root/'asharequant/demo.py').write_text('VALUE=1')
   delivery=dict(session='2026-10-19',directory=str(root/'report'),overview=str(root/'report/index.html'))
   with patch('asharequant.work_archive.now',return_value=datetime(2026,10,20,8,0,1,tzinfo=ZoneInfo('Asia/Shanghai'))):a=record(root,delivery,{'status':'SUCCESS'})
   with patch('asharequant.work_archive.now',return_value=datetime(2026,10,20,8,0,2,tzinfo=ZoneInfo('Asia/Shanghai'))):b=record(root,delivery,{'status':'FAILED'})
   self.assertEqual(a['work_date'],'2026-10-20');self.assertEqual(a['source_snapshot'],b['source_snapshot']);self.assertNotEqual(a['record'],b['record'])
   with zipfile.ZipFile(a['source_snapshot']) as z:self.assertEqual(z.read('asharequant/demo.py'),b'VALUE=1');self.assertIsNone(z.testzip())
   self.assertEqual(json.loads(Path(a['record']).read_text())['market_session'],'2026-10-19')
