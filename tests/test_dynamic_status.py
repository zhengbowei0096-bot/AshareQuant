"""Synthetic report evidence; no trading, prediction or risk policy changes."""
import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from asharequant.paper_account import put
from asharequant.reporting.dynamic_status import snapshot, render


class DynamicStatusTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE records(kind TEXT,key TEXT,body TEXT,sha256 TEXT,PRIMARY KEY(kind,key))')
        self.universe = {'600031':'测试甲','600875':'测试乙'}
        self.now = '2026-10-23T18:00:00+08:00'

    def add(self, minute, reason=None, change=None):
        at = f'2026-10-23T10:{minute:02d}:00+08:00'
        rows = [dict(symbol=k, name=v, target_weight=.1, quote_time=at, net_expected_return=.01,
                     original_rank=i+1, reasons=[]) for i,(k,v) in enumerate(self.universe.items())]
        result = dict(status='RUNNING_PAPER_ONLY',decision_rows=rows,model=dict(at=at,stocks={k:{} for k in self.universe}))
        if reason:
            result = dict(status='RUNNING_PAPER_ONLY',reason=reason,inference_status='UNAVAILABLE_RISK_REDUCTIONS_ONLY')
        if change:
            change(result)
        record = dict(at=at,result=result,checks={'600031':['BOOK_INVALID','INVALID_QUOTE','INVALID_QUOTE']} if reason else {})
        put(self.db,'cycles',str(minute),record)
        return record

    def read(self):
        return snapshot(self.db,self.universe,self.now,'MARKET_CLOSED')

    def test_no_cycles_explains_wait_without_empty_table(self):
        d = self.read();page = render(d,self.universe,{})
        self.assertEqual(d['state'],'WAITING')
        self.assertIn('尚无盘中判断记录',page)
        self.assertNotIn('<table',page)

    def test_closed_session_preserves_error_and_does_not_claim_recovery(self):
        self.add(1,'HISTORY_MODEL_VERSION_MISMATCH')
        d = self.read();page = render(d,self.universe,{})
        self.assertEqual(d['state'],'BLOCKED')
        self.assertIsNone(d['last_success_at']);self.assertIsNone(d['last_decision_at'])
        self.assertIsNone(d['recovered_at']);self.assertEqual(d['decision_stocks'],0)
        self.assertIn('历史数据版本与模型绑定版本不一致',page)
        self.assertIn('买卖盘价格或数量无效',page)
        self.assertEqual(page.count('报价或买卖盘字段缺失、非正数或无效'),1)
        self.assertIn('2026-10-23 10:01:00（北京时间）',page)
        self.assertNotIn('<table',page)

    def test_success_requires_complete_rows_and_model_audit(self):
        self.add(1)
        d = self.read()
        self.assertEqual(d['state'],'AVAILABLE');self.assertEqual(len(d['rows']),2)
        self.assertIn('<table',render(d,self.universe,{}))
        self.assertIsNone(d['recovered_at'])

    def test_recovery_keeps_first_success_after_most_recent_failure(self):
        self.add(1,'MODEL_QUOTE_NOT_VALID:600031');self.add(2);self.add(3)
        d = self.read()
        self.assertEqual(d['state'],'RECOVERED')
        self.assertIn('10:02:00',d['recovered_at']);self.assertIn('10:03:00',d['last_success_at'])
        self.assertIn('测试甲',render(d,self.universe,{}))

    def test_new_failure_does_not_display_old_success_rows(self):
        self.add(1);self.add(2,'MODEL_QUOTE_NOT_VALID:600875')
        d = self.read();self.assertEqual(d['state'],'BLOCKED')
        self.assertEqual(d['rows'],[]);self.assertIsNone(d['recovered_at'])
        self.assertIn('10:01:00',d['last_success_at'])
        self.assertNotIn('<table',render(d,self.universe,{}))
        self.add(3);self.assertIn('10:03:00',self.read()['recovered_at'])

    def test_partial_and_duplicate_rows_never_prove_recovery(self):
        self.add(1,'NO_RECORDED_DECISIONS')
        self.add(2,change=lambda r:r['decision_rows'].pop())
        self.assertEqual(self.read()['reason'],'DECISION_COVERAGE_INCOMPLETE')
        self.add(3,change=lambda r:r['decision_rows'].__setitem__(1,r['decision_rows'][0]))
        self.assertEqual(self.read()['state'],'BLOCKED')

    def test_missing_model_and_future_observation_fail_closed(self):
        self.add(1,change=lambda r:r.pop('model'))
        self.assertEqual(self.read()['reason'],'MODEL_EVIDENCE_INCOMPLETE')
        self.add(2,change=lambda r:r['model'].update(at='2026-10-23T10:03:00+08:00'))
        self.assertEqual(self.read()['reason'],'DECISION_TIME_INVALID')
        self.add(3,change=lambda r:r['decision_rows'][0].update(quote_time='2026-10-24T10:01:00+08:00'))
        self.assertEqual(self.read()['reason'],'DECISION_TIME_INVALID')

    def test_future_attempt_cannot_prove_recovery(self):
        self.add(1);self.now='2026-10-23T09:59:00+08:00'
        self.assertEqual(self.read()['reason'],'DECISION_TIME_INVALID')

    def test_tampered_success_or_failure_cannot_prove_recovery(self):
        self.add(1,'HISTORY_MODEL_VERSION_MISMATCH');self.add(2)
        self.db.execute("UPDATE records SET sha256='invalid' WHERE key='1'")
        d = self.read();self.assertEqual(d['state'],'EVIDENCE_UNVERIFIED')
        self.assertIsNone(d['recovered_at']);self.assertEqual(d['rows'],[])

    def test_invalid_json_is_explicit_not_blank(self):
        self.db.execute("INSERT INTO records VALUES('cycles','1','not json','invalid')")
        d = self.read();self.assertEqual(d['state'],'EVIDENCE_UNVERIFIED')
        self.assertIn('无法核验',render(d,self.universe,{}))

    def test_invalid_nested_structure_is_explicit(self):
        self.add(1,change=lambda r:r.update(model=['invalid']))
        self.assertEqual(self.read()['state'],'EVIDENCE_UNVERIFIED')

    def test_reader_never_writes_and_html_escapes_unknown_errors(self):
        self.add(1,'<script>alert(1)</script>')
        before = self.db.total_changes
        self.db.execute('PRAGMA query_only=ON')
        page = render(self.read(),self.universe,{})
        self.assertEqual(before,self.db.total_changes)
        self.assertNotIn('<script>',page);self.assertIn('尚未归类的异常',page)

    def test_publish_integration_does_not_change_account_or_records(self):
        from asharequant.dynamic_service import db_open, publish, code_fingerprints
        from asharequant.order_engine import seed
        import pandas as pd
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            state = seed(dict(cash=150000.,positions={},marks={},equity=150000.,previous_close_equity=150000.,
                              peak=150000.,day='2026-10-22',closed_day='2026-10-22',turnover=0.,fees=0.,
                              used_quotes={},conditions={},notification_queue=[],fills_count=0,
                              valuation_at='2026-10-22',valuation_status='VERIFIED_CLOSE'),pd.Timestamp(self.now))
            meta = dict(code=code_fingerprints(),universe=self.universe,initial_equity=150000.,basis_note='测试成本')
            record = self.add(1,'HISTORY_MODEL_VERSION_MISMATCH')
            with db_open(root) as db:
                put(db,'cycles','1',record)
                before = db.execute('SELECT * FROM records').fetchall()
            original = copy.deepcopy(state)
            with patch('asharequant.dynamic_service.now',return_value=pd.Timestamp(self.now)):
                publish(root,meta,state,dict(status='MARKET_CLOSED'))
            with db_open(root) as db:self.assertEqual(before,db.execute('SELECT * FROM records').fetchall())
            self.assertEqual(state,original)
            report = json.loads((root/'reports/dynamic/latest/report.json').read_text())
            self.assertEqual(report['decision_diagnostic']['state'],'BLOCKED')
            self.assertEqual(report['decision_rows'],[]);self.assertIsNone(report['last_decision_at'])
            health = json.loads((root/'logs/dynamic_health.json').read_text())
            self.assertEqual(health['status'],'MARKET_CLOSED');self.assertIsNone(health['reason'])
            self.assertIn('尚未验证恢复',(root/'reports/dynamic/latest/index.html').read_text())


if __name__ == '__main__':
    unittest.main()
