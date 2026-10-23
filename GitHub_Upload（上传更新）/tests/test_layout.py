import importlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from scripts.init_config import initialize

class LayoutTests(unittest.TestCase):
    def test_old_and_new_module_identity(self):
        pairs = {'asharequant/database.py': 'asharequant/data_sources/database.py', 'asharequant/broker_records.py': 'asharequant/data_sources/broker_records.py', 'asharequant/report.py': 'asharequant/reporting/report.py', 'asharequant/report_navigation.py': 'asharequant/reporting/report_navigation.py', 'asharequant/daily_bundle.py': 'asharequant/reporting/daily_bundle.py', 'asharequant/holding_summary.py': 'asharequant/reporting/holding_summary.py', 'asharequant/probability_display.py': 'asharequant/reporting/probability_display.py', 'asharequant/next_session_plan.py': 'asharequant/strategies/next_session_plan.py', 'asharequant/risk_control.py': 'asharequant/risk/risk_control.py', 'asharequant/research.py': 'asharequant/backtesting/research.py', 'asharequant/validation_readiness.py': 'asharequant/evaluation/validation_readiness.py', 'asharequant/prediction_quality.py': 'asharequant/evaluation/prediction_quality.py', 'asharequant/connection_health.py': 'asharequant/evaluation/connection_health.py', 'asharequant/factor_research.py': 'asharequant/evaluation/factor_research.py'}
        for old,new in pairs.items():
            a=old[:-3].replace('/','.')
            b=new[:-3].replace('/','.')
            self.assertIs(importlib.import_module(a),importlib.import_module(b))
    def test_initialization_preserves_existing_config(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'config').mkdir()
            (root/'config/config.example.json').write_text('{}')
            (root/'config.json').write_text('{"private":true}')
            self.assertEqual(initialize(root),[])
            self.assertEqual((root/'config.json').read_text(),'{"private":true}')
    def test_nested_sources_are_archived(self):
        from asharequant.work_archive import PATTERNS
        root=Path(__file__).resolve().parents[1]
        paths={str(p.relative_to(root)) for pattern in PATTERNS for p in root.glob(pattern)}
        self.assertIn('asharequant/reporting/report.py',paths)
        self.assertIn('asharequant/data_sources/database.py',paths)

    def test_public_templates_initialize_into_classified_runtime_paths(self):
        from scripts.organize_workspace import load_plan
        source=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            shutil.copytree(source/'config',root/'config')
            (root/'asharequant').mkdir()
            (root/'.venv').mkdir()
            created=initialize(root)
            self.assertIn('dynamic_config.json',created)
            self.assertNotIn('dynamic.json',created)
            self.assertFalse(json.loads((root/'dynamic_config.json').read_text())['enabled'])
            self.assertEqual(len(load_plan(root)['groups']),10)
            self.assertEqual(initialize(root),[])
