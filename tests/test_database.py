import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
from asharequant.database import sync_database


class DatabaseTests(unittest.TestCase):
    def test_idempotent_revision_nulls_and_missing_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'data').mkdir()
            dates = pd.to_datetime(['2024-01-02','2024-01-04'])
            raw = pd.DataFrame(dict(date=dates, open=[10.,10.], high=[11.,11.], low=[9.,9.], close=[10.,10.], volume=[100.,100.], amount=[1000.,1000.], turnover=[.01,.01]))
            snap = dict(raw=raw, hfq=raw.copy(), meta=dict(source='fixture', version='v1', fetched_at='2024-01-05'))
            watch = pd.DataFrame(dict(symbol=['000001']))
            cfg = dict(start_date='2020-01-01')
            cal = pd.to_datetime(['2024-01-02','2024-01-03','2024-01-04'])
            with patch('asharequant.database.read_snapshot', return_value=snap):
                for _ in range(2):
                    result = sync_database(root,cfg,watch,pd.Timestamp('2024-01-04'),cal)
                self.assertEqual(result['symbols'][0]['unexplained_missing'],1)
                with sqlite3.connect(root/'data/market.sqlite3') as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM daily_bars').fetchone()[0],2)
                    self.assertEqual(db.execute('SELECT count(*) FROM imports').fetchone()[0],1)
                    self.assertEqual(db.execute('SELECT limit_up, limit_status FROM daily_bars LIMIT 1').fetchone(),(None,'unknown'))
                snap['raw'].loc[0,'amount'] = 1234.
                snap['meta']['version']='v2'
                sync_database(root,cfg,watch,pd.Timestamp('2024-01-04'),cal)
                with sqlite3.connect(root/'data/market.sqlite3') as db:
                    self.assertEqual(db.execute("SELECT amount FROM daily_bars WHERE date='2024-01-02'").fetchone()[0],1234.)

    def test_failed_batch_rolls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'data').mkdir()
            raw=pd.DataFrame(dict(date=pd.to_datetime(['2024-01-02']),open=[10.],high=[11.],low=[9.],close=[10.],volume=[100.],amount=[1000.],turnover=[.01]))
            snap=dict(raw=raw,hfq=raw,meta=dict(source='fixture',version='v1',fetched_at='2024-01-05'))
            with patch('asharequant.database.read_snapshot',side_effect=[snap,None]):
                with self.assertRaises(ValueError):
                    sync_database(root,dict(start_date='2020-01-01'),pd.DataFrame(dict(symbol=['000001','600000'])),pd.Timestamp('2024-01-04'),pd.to_datetime(['2024-01-02']))
            with sqlite3.connect(root/'data/market.sqlite3') as db:
                self.assertEqual(db.execute('SELECT count(*) FROM daily_bars').fetchone()[0],0)
