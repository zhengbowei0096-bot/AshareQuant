import unittest,tempfile,json,zipfile
from pathlib import Path
from unittest.mock import patch
from datetime import datetime
from zoneinfo import ZoneInfo
from asharequant.work_archive import record
class WorkArchiveTests(unittest.TestCase):
 def test_work_date_separate_and_snapshot_reused(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);(root/'asharequant').mkdir();(root/'asharequant/demo.py').write_text('VALUE=1')
   delivery=dict(session='2026-10-19',directory=str(root/'report'),overview=str(root/'report/index.html'))
   with patch('asharequant.work_archive.now',return_value=datetime(2026,10,20,8,0,1,tzinfo=ZoneInfo('Asia/Shanghai'))):a=record(root,delivery,{'status':'SUCCESS'})
   with patch('asharequant.work_archive.now',return_value=datetime(2026,10,20,8,0,2,tzinfo=ZoneInfo('Asia/Shanghai'))):b=record(root,delivery,{'status':'FAILED'})
   self.assertEqual(a['work_date'],'2026-10-20');self.assertEqual(a['source_snapshot'],b['source_snapshot']);self.assertNotEqual(a['record'],b['record'])
   with zipfile.ZipFile(a['source_snapshot']) as z:self.assertEqual(z.read('asharequant/demo.py'),b'VALUE=1');self.assertIsNone(z.testzip())
   self.assertEqual(json.loads(Path(a['record']).read_text())['market_session'],'2026-10-19')
