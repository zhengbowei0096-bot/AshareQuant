import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
from asharequant import iteration as it,lifecycle as lc
class NullCandidateRegression(unittest.TestCase):
 def test_baseline_with_explicit_null_candidate_keeps_audit_history(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);(root/'forecast_config.json').write_text('{}')
   with lc.database(root) as db:
    lc.put(db,'specs','baseline',dict(experiment='baseline',candidate=None,frozen_at='2026-10-19T18:00:00+08:00',training_asof='2026-10-19'))
   with patch.object(it,'load_config',return_value={}),patch('asharequant.data.calendar',return_value=(pd.DatetimeIndex(['2026-10-19']),{})),patch.object(it,'read_watchlist',return_value=pd.DataFrame()),patch.object(lc,'monitor',return_value={}),patch.object(lc,'admission',return_value={'status':'BLOCKED'}),patch.object(it,'select_candidate',return_value=([],None)):
    r=it.run(root)
   self.assertEqual(r['status'],'COMPLETE');self.assertEqual(r['update_history'][0]['reason'],'BASELINE_FREEZE')
   self.assertIsNone(r['update_history'][0]['expected_improvement']);self.assertIsNone(r['update_history'][0]['actual_improvement'])
   self.assertEqual(r['production_admission']['status'],'BLOCKED');self.assertEqual(r['submitted_orders'],0)
