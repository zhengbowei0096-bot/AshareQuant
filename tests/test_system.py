import copy
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from asharequant.core import (validate, cutoff, TZ, read_watchlist, load_config,
    write_json, read_snapshot, commit_snapshot, quality, run_lock, stock_config, adapt_tencent)
from asharequant.data import merge_increment, update_all, network_job, calendar
from asharequant.research import simulate
from asharequant.scheduler import due

ROOT = Path(__file__).resolve().parents[1]
CFG = load_config(ROOT)


def bars(n=400):
    dates = pd.bdate_range('2020-01-01', periods=n)
    prices = 100 + np.arange(n) * .1 + np.sin(np.arange(n) / 7)
    return pd.DataFrame({'date': dates, 'open': prices, 'close': prices,
                         'high': prices + 1, 'low': prices - 1,
                         'volume': 1000000., 'turnover': .01, 'amount': 2e8})


class SystemTests(unittest.TestCase):
    def test_cutoff_china_and_buffer(self):
        self.assertEqual(str(cutoff(datetime(2026,10,20,1,tzinfo=TZ)).date()), '2026-10-19')
        self.assertEqual(str(cutoff(datetime(2026,10,20,18,tzinfo=TZ)).date()), '2026-10-20')

    def test_bad_prices_duplicates_nan(self):
        df = bars(4)
        for field, value in [('close', -1), ('amount', np.inf), ('date', pd.NaT), ('high', 1)]:
            bad = df.copy(); bad.loc[1, field] = value
            with self.assertRaises(ValueError):
                validate(bad, df.date.min(), df.date.max())
        with self.assertRaises(ValueError):
            validate(pd.concat([df, df.iloc[:1]]), df.date.min(), df.date.max())

    def test_mainboard_watchlist_leading_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'watchlist.csv').write_text('symbol,name,enabled\n000681,视觉中国,1\n')
            self.assertEqual(read_watchlist(root).symbol.iloc[0], '000681')
            for body in ['60003,错误,1', '300750,创业板,1', '600000,ST测试,1',
                         '600000,测试,yes', '600000,测试,1\n600000,测试,1']:
                (root/'watchlist.csv').write_text('symbol,name,enabled\n'+body+'\n')
                with self.assertRaises(ValueError): read_watchlist(root)

    def test_sz000_unit_adapter(self):
        df = bars(1)
        self.assertEqual(adapt_tencent(df, '000681').volume.iloc[0], 100000000)
        self.assertEqual(adapt_tencent(df, '001309').volume.iloc[0], 1000000)

    def test_increment_cannot_erase_dates(self):
        df = bars(10)
        merged = merge_increment(df.iloc[:8], df.iloc[5:], df.date.iloc[5], df.date.iloc[-1])
        self.assertEqual(len(merged), 10)
        with self.assertRaises(ValueError):
            merge_increment(df.iloc[:8], df.iloc[6:], df.date.iloc[5], df.date.iloc[-1])

    def test_atomic_snapshot_checksum_and_failed_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); df = bars(10)
            meta = {'start': str(df.date.min().date()), 'end': str(df.date.max().date())}
            commit_snapshot(root, '600519', df, df, meta)
            before = (root/'data/normalized/600519/latest.json').read_bytes()
            with patch('asharequant.core.write_json', side_effect=OSError('disk full')):
                with self.assertRaises(OSError): commit_snapshot(root, '600519', df, df, meta)
            self.assertEqual(before, (root/'data/normalized/600519/latest.json').read_bytes())
            snapshot = read_snapshot(root, '600519')
            path = root/'data/normalized/600519/versions'/snapshot['meta']['version']/'raw.csv'
            path.write_text('damaged')
            with self.assertRaises(ValueError): read_snapshot(root, '600519')

    def test_calendar_gaps_stale_and_no_fill(self):
        df = bars(10); cfg = {**CFG, 'start_date': str(df.date.min().date())}
        q = quality(df.drop(index=4), df.date, cfg, df.date.max())
        self.assertEqual(q['internal_missing_count'], 1)
        self.assertFalse(q['calendar_complete'])
        self.assertFalse(quality(df.iloc[:-1], df.date, cfg, df.date.max())['fresh'])
        with self.assertRaises(ValueError): quality(df, df.date.iloc[:5], cfg, df.date.max())

    def test_listing_date_bounds_calendar(self):
        cfg = stock_config(CFG, '001309')
        self.assertEqual(cfg['start_date'], '2022-07-01')

    def test_update_continues_after_one_failure(self):
        watch = pd.DataFrame({'symbol':['600031','600875'], 'name':['A','B']})
        cfg = {**CFG, 'attempts':1, 'request_spacing_seconds':0}
        with patch('asharequant.data.update_symbol', side_effect=[ValueError('network'),
                {'version':'ok', 'quality':{}}]):
            result = update_all(ROOT, cfg, watch, pd.Timestamp('2026-10-19'), [], False)
        self.assertFalse(result[0]['ok']); self.assertTrue(result[1]['ok'])

    def test_network_worker_has_hard_timeout(self):
        import subprocess
        with patch('asharequant.data.subprocess.run', side_effect=subprocess.TimeoutExpired('job',90)):
            with self.assertRaises(subprocess.TimeoutExpired):
                network_job(ROOT, CFG, kind='calendar')

    def test_no_lookahead(self):
        raw = bars(); hfq = raw.copy()
        _, a, trades = simulate(raw, hfq, CFG)
        changed = hfq.copy()
        changed.loc[250:, ['open','close','high','low']] *= 2
        _, b, _ = simulate(raw, changed, CFG)
        np.testing.assert_allclose(a.equity.iloc[:250], b.equity.iloc[:250])
        self.assertGreaterEqual(trades.date.min(), raw.date.iloc[CFG['slow_window']])

    def test_costs_reduce_returns(self):
        df = bars()
        zero = {**CFG, 'commission_bps':0, 'slippage_bps':0, 'sell_tax_bps':0}
        paid, _, _ = simulate(df, df, CFG)
        free, _, _ = simulate(df, df, zero)
        self.assertLess(paid['total_return'], free['total_return'])
        self.assertLessEqual(paid['max_drawdown'], 0)

    def test_scheduled_china_time_retry_and_weekend(self):
        t = datetime(2026,10,19,18,30,tzinfo=TZ)
        self.assertTrue(due(t, {}, CFG))
        self.assertFalse(due(t.replace(hour=17), {}, CFG))
        self.assertFalse(due(t, {'success_date':'2026-10-19'}, CFG))
        self.assertFalse(due(t + timedelta(days=5), {}, CFG))
        self.assertFalse(due(t, {'attempt_date':'2026-10-19', 'attempts':3}, CFG))

    def test_lock_prevents_concurrent_updates(self):
        with tempfile.TemporaryDirectory() as tmp:
            with run_lock(Path(tmp)):
                with self.assertRaises(RuntimeError):
                    with run_lock(Path(tmp)): pass


    def test_network_quote_codes_keep_leading_zeros(self):
        from asharequant.data import network_job
        def fake_run(*args, **kwargs):
            job = json.loads(Path(args[0][-1]).read_text())
            Path(job['output']).write_text('symbol,name,quote_time\n000681,视觉中国,20261019161430\n')
            return __import__('subprocess').CompletedProcess(args[0], 0, '', '')
        with patch('asharequant.data.subprocess.run', side_effect=fake_run):
            df = network_job(ROOT, CFG, kind='quotes', symbols=['000681'])
        self.assertEqual(df.symbol.iloc[0], '000681')

    def test_report_offline_end_to_end(self):
        from asharequant.cli import main
        from asharequant.core import now, write_csv
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'config.json').write_text(json.dumps({**CFG, 'pool_target':30, 'pool_min':30}))
            (root/'watchlist.csv').write_text('symbol,name,enabled\n600519,贵州茅台,1\n')
            df = bars()
            meta = {'start': str(df.date.min().date()), 'end': str(df.date.max().date())}
            commit_snapshot(root, '600519', df, df, meta)
            write_csv(root/'data/calendar.csv', pd.DataFrame({'trade_date':df.date}))
            write_json(root/'data/calendar.json', {'fetched_at':now().isoformat(), 'source':'test fixture'})
            write_csv(root/'data/quotes.csv', pd.DataFrame([{'symbol':'600519', 'name':'贵州茅台',
                'price':100, 'quote_time':df.date.max().strftime('%Y%m%d')+'161500',
                'pe':20, 'market_cap_100m':1000}]))
            with patch('asharequant.cli.cutoff', return_value=df.date.max()):
                code = main(['report', '--root', str(root)])
            self.assertEqual(code, 0)
            report = json.loads((root/'reports/latest/summary.json').read_text())
            self.assertEqual(report['pool_count'], 1)
            self.assertEqual(report['pool_shortfall_min'], 29)
            self.assertEqual(report['pool_target'], 30)
            self.assertIn('600519', report['research'])
            self.assertIn('AshareQuant', (root/'reports/latest/index.html').read_text())


    def test_confirmed_suspension_is_not_download_gap(self):
        df = bars(12)
        cal = df.date
        suspension = {'start':str(cal.iloc[4].date()), 'end':str(cal.iloc[5].date())}
        cfg = {**CFG, 'start_date': str(cal.min().date()), 'stock_suspensions':[suspension]}
        q = quality(df.drop(index=[4,5]), cal, cfg, cal.max())
        self.assertTrue(q['calendar_complete'])
        self.assertEqual(q['known_suspension_count'],2)
        self.assertEqual(q['missing_count'],0)
        conflict = quality(df, cal, cfg, cal.max())
        self.assertEqual(conflict['suspension_conflict_count'],2)
        self.assertFalse(conflict['calendar_complete'])

    def test_suspension_ledger_carries_valuation_without_orders(self):
        df = bars()
        cal = df.date
        halted = cal.iloc[200:205]
        actual = df.drop(index=range(200,205)).reset_index(drop=True)
        _, curve, trades = simulate(actual, actual, CFG, calendar_dates=cal)
        self.assertEqual(len(curve),len(df))
        self.assertFalse(trades.date.isin(halted).any())
        ledger = curve.set_index('date')
        self.assertEqual(ledger.loc[halted,'equity'].nunique(),1)


    def test_busy_update_does_not_overwrite_last_run_state(self):
        from asharequant.cli import main
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'config.json').write_text(json.dumps({**CFG, 'pool_target':30, 'pool_min':30}))
            (root/'watchlist.csv').write_text('symbol,name,enabled\n600519,贵州茅台,1\n')
            write_json(root/'logs/last_attempt.json', {'state':'success'})
            with run_lock(root):
                code = main(['report','--root',str(root)])
            self.assertEqual(code,75)
            self.assertEqual(json.loads((root/'logs/last_attempt.json').read_text())['state'],'success')


    def test_cross_source_units_and_amount_tolerance(self):
        from asharequant.compare import compare_frames
        tx = bars(3)
        em = tx.rename(columns={'date':'日期','open':'开盘','close':'收盘','high':'最高','low':'最低',
                                 'volume':'成交量','amount':'成交额','turnover':'换手率'}).copy()
        em['成交量'] /= 100
        em['换手率'] *= 100
        em['成交额'] += 10
        result = compare_frames(em, tx)
        self.assertTrue(all(v['outside_tolerance']==0 for v in result['fields'].values()))
        em.loc[0,'收盘'] += 1
        self.assertEqual(compare_frames(em,tx)['fields']['close']['outside_tolerance'],1)


if __name__ == '__main__':
    unittest.main()
