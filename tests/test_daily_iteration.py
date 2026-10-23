import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
import pandas as pd
from asharequant import daily_iteration as daily, lifecycle as lc
from asharequant.reporting.iteration_status import render


class DailyResearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.at=pd.Timestamp('2026-10-23T19:00:00+08:00')
        self.sessions=pd.DatetimeIndex(['2026-10-22','2026-10-23','2026-10-26'])
        self.study=dict(candidate=dict(alpha=100.,features=['r1']),mean_gain=.009,
                        minimum_positive_folds=0,eligible_for_future_test=False,errors=[])
        self.ids=['one'];self.errors=[]
        self.stack=self.enterContext(ExitStack())
        self.stack.enter_context(patch.object(daily,'now',side_effect=lambda:self.at))
        self.stack.enter_context(patch.object(daily,'load_config',return_value={}))
        self.stack.enter_context(patch('asharequant.data.calendar',return_value=(self.sessions,{})))
        self.stack.enter_context(patch.object(daily,'inputs',side_effect=lambda *a:({('600000',1):object()},[],True,self.errors.copy())))
        self.stack.enter_context(patch.object(daily,'outcome_evidence',side_effect=lambda *a:(dict(confirmed=len(self.ids),primary_confirmed=len(self.ids),summary={'mae':.1},settlement={'errors':[]}),self.ids.copy())))
        self.search=self.stack.enter_context(patch.object(daily.research,'select_candidate',return_value=([self.study],None)))
        self.stack.enter_context(patch.object(lc,'monitor',return_value={}))
        self.stack.enter_context(patch.object(lc,'admission',return_value={'status':'BLOCKED'}))

    def test_daily_study_without_drift_and_same_session_deduplicated(self):
        a=daily.legacy_run(self.root);b=daily.legacy_run(self.root)
        self.assertEqual(self.search.call_count,1)
        self.assertEqual(a['research_progress']['status'],'DAILY_MATURED_OUTCOMES')
        self.assertEqual(b['research_progress']['status'],'ALREADY_RESEARCHED_SESSION')
        self.assertEqual(b['research_progress']['new_primary_outcomes'],0)
        self.assertEqual(b['research_progress']['last_study']['studies'],[self.study])
        self.assertEqual(b['research_progress']['next_research_at'],'2026-10-26T18:00:00+08:00')
        self.assertEqual(b['studies'],[self.study])
        self.assertTrue((self.root/'reports/iteration/latest/index.html').is_file())

    def test_new_day_requires_new_actual_outcomes(self):
        daily.legacy_run(self.root);self.at=pd.Timestamp('2026-10-26T19:00:00+08:00')
        r=daily.legacy_run(self.root)
        self.assertEqual(r['research_progress']['status'],'NO_NEW_MATURED_OUTCOMES')
        self.ids.append('two');r=daily.legacy_run(self.root)
        self.assertEqual(self.search.call_count,2)
        self.assertEqual(r['research_progress']['status'],'DAILY_MATURED_OUTCOMES')
        self.assertIsNone(r['research_progress']['next_review_at'])

    def test_data_failure_does_not_consume_outcomes(self):
        self.errors=[dict(symbol='600000',error='STALE_OR_MISSING_SNAPSHOT')]
        r=daily.legacy_run(self.root);self.assertEqual(r['status'],'INPUT_ERRORS');self.search.assert_not_called()
        self.assertFalse((self.root/'data/iteration/daily_studies/2026-10-23.json').exists())
        self.errors=[];daily.legacy_run(self.root);self.search.assert_called_once()

    def test_candidate_error_does_not_complete_checkpoint(self):
        self.study['errors']=[{'error':'bad'}]
        r=daily.legacy_run(self.root);self.assertEqual(r['status'],'INPUT_ERRORS')
        self.assertFalse((self.root/'data/iteration/daily_studies/2026-10-23.json').exists())

    def test_pending_experiment_preserved_and_evaluated(self):
        state=self.root/'data/iteration/state.json';state.parent.mkdir(parents=True)
        state.write_text(json.dumps({'candidate_experiment':'frozen'}))
        with lc.database(self.root) as db:lc.put(db,'specs','frozen',dict(experiment='frozen',runtime_hash=lc.runtime_hash(),frozen_at=self.at.isoformat(),training_asof='2026-10-22'))
        with patch.object(lc,'settle') as settle,patch.object(lc,'select_portfolio'),patch.object(lc,'collect'),patch.object(lc,'evaluate',return_value={'status':'WAITING_FOR_UNTOUCHED_TEST'}),patch.object(lc,'promote',return_value=None):
            r=daily.legacy_run(self.root)
        settle.assert_called_once();self.search.assert_not_called()
        self.assertEqual(json.loads(state.read_text())['candidate_experiment'],'frozen')
        self.assertEqual(r['research_progress']['status'],'FROZEN_VALIDATION_PENDING')
        self.assertIsNone(r['research_progress']['next_research_at'])

    def test_checkpoint_recovers_prepare_failure_without_second_search(self):
        self.study['eligible_for_future_test']=True
        self.search.return_value=([self.study],self.study)
        with patch.object(lc,'prepare',side_effect=ValueError('disk full')):
            self.assertEqual(daily.legacy_run(self.root)['status'],'INPUT_ERRORS')
        with patch.object(lc,'prepare',return_value={'experiment':'new'}) as prepare:
            r=daily.legacy_run(self.root)
        self.assertEqual(self.search.call_count,1);prepare.assert_called_once()
        self.assertEqual(r['candidate']['evaluation'],'WAITING_FOR_FIRST_FUTURE_CLOSE')
        self.assertIsNone(r['research_progress']['next_research_at'])

    def test_clock_uses_exchange_calendar_and_after_close(self):
        self.assertEqual(daily.next_review(self.sessions,pd.Timestamp('2026-10-23T09:00:00Z')),'2026-10-23T18:00:00+08:00')
        self.assertEqual(daily.decision('2026-10-23',pd.Timestamp('2026-10-23T17:59:00+08:00'),None,None,[],['x']),'WAIT_AFTER_CLOSE')
        self.assertEqual(daily.decision('2026-10-23',self.at,None,None,[],[]),'NO_NEW_MATURED_OUTCOMES')

    def test_unknown_or_changed_frozen_version_is_not_discarded(self):
        p=self.root/'data/iteration/state.json';p.parent.mkdir(parents=True);p.write_text('{"candidate_experiment":"missing"}')
        r=daily.legacy_run(self.root);self.search.assert_not_called()
        self.assertEqual(r['status'],'INPUT_ERRORS')
        self.assertEqual(json.loads(p.read_text())['candidate_experiment'],'missing')

    def test_completed_checkpoint_tampering_blocks_reuse(self):
        daily.legacy_run(self.root)
        p=self.root/'data/iteration/daily_studies/2026-10-23.json'
        value=json.loads(p.read_text());value['winner']=self.study;p.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError,'CHECKSUM'):daily.legacy_run(self.root)
        self.assertEqual(self.search.call_count,1)


class ReportTests(unittest.TestCase):
    def test_beijing_timestamps_survive_full_report_localization(self):
        from asharequant.reporting.iteration_status import time_text
        from asharequant.reporting.plain_chinese import render as localize
        self.assertEqual(time_text('2026-10-26T10:00:00Z'),'2026-10-26 18:00:00（北京时间）')
        r=dict(asof='2026-10-23',generated_at='2026-10-23T19:00:00+08:00',schedule=daily.SCHEDULE,
            research_progress=dict(status='NO_NEW_MATURED_OUTCOMES',next_research_at='2026-10-26T18:00:00+08:00',next_review_at='2026-10-26T18:00:00+08:00'))
        with tempfile.TemporaryDirectory() as d:page=localize(render(Path(d),r,{},'2026-10-23'))
        self.assertEqual(page.count('2026-10-26 18:00:00（北京时间）'),2)

    def test_empty_state_has_explanation_instead_of_empty_table(self):
        with tempfile.TemporaryDirectory() as d:
            page=render(Path(d),{}, {}, '2026-10-23')
        self.assertIn('尚无已完成的候选比较',page);self.assertNotIn('<table>',page)

    def test_last_study_date_and_gate_failures_are_explicit(self):
        r=dict(asof='2026-10-23',generated_at='2026-10-23T19:00:00+08:00',schedule=daily.SCHEDULE,
            research_progress=dict(status='NO_NEW_MATURED_OUTCOMES',last_study=dict(asof='2026-10-21',generated_at='2026-10-21T19:04:00+08:00',
                studies=[dict(candidate={'alpha':100,'features':['r1']},mean_gain=.0092,minimum_positive_folds=0)])))
        page=render(Path('.'),r,{},'2026-10-23')
        for value in ('2026-10-21','0.92%','未达到2%','不足3/4','暂无新增','下次可开展新研究'):self.assertIn(value,page)
        self.assertNotIn('NO_NEW_MATURED_OUTCOMES',page)

    def test_missing_legacy_time_and_html_escape(self):
        # Missing evidence must not invent a result or a future calendar date.
        r={'errors':[{'symbol':'<script>','error':'secret-internal-code'}]}
        with tempfile.TemporaryDirectory() as d:page=render(Path(d),r,{},'2026-10-23')
        self.assertNotIn('<script>',page);self.assertNotIn('secret-internal-code',page)
        self.assertIn('&lt;script&gt;',page)


class OutcomeTests(unittest.TestCase):
    def test_future_actual_confirmation_rejected(self):
        # Hash-valid records still cannot make future outcomes available today.
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            with daily.quality.database(root) as db:
                daily.quality.put(db,'q9_predictions','one',dict(target='2026-10-23',primary_daily=True))
                daily.quality.put(db,'q9_outcomes','one',dict(confirmed_at='2026-10-24T18:00:00+08:00'))
            with patch.object(daily.quality,'report',return_value={}):
                with self.assertRaisesRegex(ValueError,'FUTURE'):daily.outcome_evidence(root,pd.Timestamp('2026-10-23T19:00:00+08:00'))

    def test_reruns_do_not_inflate_primary_samples(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            with daily.quality.database(root) as db:
                for key,primary in [('one',True),('rerun',False)]:
                    daily.quality.put(db,'q9_predictions',key,dict(target='2026-10-23',primary_daily=primary))
                    daily.quality.put(db,'q9_outcomes',key,dict(confirmed_at='2026-10-23T18:00:00+08:00'))
            with patch.object(daily.quality,'report',return_value={}):
                _,ids=daily.outcome_evidence(root,pd.Timestamp('2026-10-23T19:00:00+08:00'))
            self.assertEqual(ids,['one'])


if __name__=='__main__':unittest.main()
