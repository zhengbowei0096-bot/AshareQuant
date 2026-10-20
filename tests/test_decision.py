import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
import pandas as pd
from asharequant.decision import (DEFAULTS, METHODS, costs, sell_quantity, rule_for, covariance,
    optimize, build_plan, candidate_plan, market_blockers, generate)
from asharequant.portfolio_validation import compare_oos

AT='2026-10-19T10:00:00+08:00'

def cfg():
    return {**copy.deepcopy(DEFAULTS),'fees_verified':True,'fee_source':'SYNTHETIC_TEST_FEE',
            'market_environment':'risk_on','minimum_trade_value':0.}

def row(s='600000',sector='银行',price=10.):
    return dict(symbol=s,name='测试股票',sector=sector,rule=rule_for(s),data_eligible=True,synthetic=True,
        forecast=dict(predicted_return=.08,up_probability_empirical=.7,probability_method='SYNTHETIC ONLY',
            interval80_empirical=[-.04,.12],volatility_horizon=.1,downside_es95_normal_proxy=-.1,
            validation=dict(preliminary_screen_passed=True)),
        quote=dict(price=price,source='SYNTHETIC_TEST',asof=AT,kind='live',status='trading',status_source='SYNTHETIC_TEST',
            status_asof=AT,risk_warning=False,ordinary_session=True,limit_regime='ordinary_10pct',limit_down=price*.9,limit_up=price*1.1,session_verified=True))

def account(positions=None,cash=150000.):
    return dict(cash=cash,available_cash=cash,positions=positions or {},source='SYNTHETIC_TEST',asof=AT)

class DecisionTests(unittest.TestCase):
    def test_exchange_security_scope(self):
        self.assertEqual(rule_for('600000'),'SSE_MAIN_A'); self.assertEqual(rule_for('000001'),'SZSE_MAIN_A')
        for s in ['688001','300001','830001','510300','60000','abcdef']: self.assertIsNone(rule_for(s))

    def test_fees_bundling_and_tax(self):
        c=cfg(); b=costs(10000,'BUY',c); s=costs(10000,'SELL',c)
        self.assertEqual(b['commission'],5); self.assertEqual(b['regulatory'],0)
        self.assertAlmostEqual(s['stamp'],5); self.assertAlmostEqual(s['total']-b['total'],5)
        extra=costs(10000,'BUY',{**c,'commission_includes_regulatory':False})
        self.assertAlmostEqual(extra['regulatory'],.541)
        self.assertEqual(costs(0,'SELL',c)['total'],0)

    def test_odd_lots_not_split(self):
        self.assertEqual(sell_quantity(450,1050,1050),450)
        self.assertEqual(sell_quantity(49,1050,1050),0)
        self.assertEqual(sell_quantity(1050,1050,1050),1050)
        self.assertEqual(sell_quantity(230,230,1000),200)

    def test_covariance_shrinkage_and_short_data(self):
        a=np.ones((130,3))*.01; s=covariance(a)
        self.assertGreater(np.linalg.eigvalsh(s).min(),0)
        with self.assertRaises(ValueError): covariance(a[:10])
        with self.assertRaises(ValueError): covariance(np.full((130,3),np.nan))

    def test_four_methods_caps_cash_and_stability(self):
        c=cfg(); mu=np.array([.05,.03,.07,.02]); cov=np.diag([.02,.01,.03,.015])
        for method in METHODS:
            w=optimize(method,mu,cov,['A','A','B','B'],c)
            self.assertTrue((w>=0).all()); self.assertTrue((w<=.15+1e-12).all())
            self.assertLessEqual(w[:2].sum(),.25+1e-12); self.assertLessEqual(w.sum(),.6+1e-12)
            w2=optimize(method,mu+1e-7,cov*1.00001,['A','A','B','B'],c)
            self.assertLess(abs(w2-w).sum(),.005)

    def test_risk_parity_equal_risk_unconstrained(self):
        c={**cfg(),'max_stock':1.,'max_sector':1.,'max_invested':.6}
        s=np.array([[.04,.005],[.005,.01]])
        w=optimize('risk_parity',[.04,.04],s,['A','B'],c)
        rc=w*(s@w); self.assertAlmostEqual(rc[0],rc[1],places=8)

    def test_formal_gate_never_bypassed(self):
        for selected in (None,*METHODS):
            r=build_plan([row()],np.array([[.01]]),{**cfg(),'selected_method':selected},at=AT)
            self.assertEqual(r['cash_after'],150000); self.assertEqual(r['submitted_orders'],0)
            self.assertEqual(r['decisions'][0]['action'],'NO_TRADE')
            self.assertIn('FORMAL_METHOD_NOT_VALIDATED',r['blockers'])
            self.assertTrue(r['account_assumed'])

    def test_missing_forecast_and_covariance(self):
        a=row(); a['forecast']={}; a['quote']={}
        r=build_plan([a],None,cfg(),at=AT)
        self.assertEqual(r['decisions'][0]['suggested_shares'],0)
        self.assertIn('FORECAST_FIELDS_MISSING',r['decisions'][0]['blockers'])
        self.assertEqual(r['comparisons']['equal_weight']['status'],'BLOCKED_COVARIANCE')

    def test_missing_universe_holdings_fails_closed(self):
        with self.assertRaises(ValueError): build_plan([row()],np.array([[.01]]),cfg(),account({'000001':dict(shares=100,sellable=100)}),AT)

    def test_status_and_live_price_fences(self):
        for changes,flag in [({'kind':'historical_close'},'PRICE_NOT_CURRENT_LIVE'),({'status':'unknown'},'TRADING_STATUS_UNVERIFIED'),
            ({'price':11.},'LIMIT_LOCK_OR_PRICE_INVALID'),({'risk_warning':True},'SPECIAL_SESSION_OR_RISK_SECURITY'),
            ({'limit_up':None},'LIMIT_PRICES_MISSING'),({'asof':'2026-10-18T10:00:00+08:00'},'PRICE_NOT_CURRENT_LIVE')]:
            a=row(); a['quote'].update(changes); self.assertIn(flag,market_blockers(a,AT))
            plan=candidate_plan([a],{'600000':.1},account(),cfg(),AT)
            self.assertEqual(plan['rows'][0]['suggested_shares'],0)

    def test_conditional_buy_lots_and_cash(self):
        a=row(price=31.)
        p=candidate_plan([a],{'600000':.15},account(),cfg(),AT)
        d=p['rows'][0]; self.assertEqual(d['action'],'BUY'); self.assertEqual(d['suggested_shares']%100,0)
        self.assertFalse(d['executable']); self.assertGreaterEqual(p['cash_after'],60000)
        self.assertAlmostEqual(p['cash_after']+d['suggested_shares']*31+d['estimated_cost']['total'],150000)

    def test_t_plus_one_and_reduce(self):
        a=row(); acct=account({'600000':dict(shares=1050,sellable=250)})
        d=candidate_plan([a],{'600000':0.},acct,cfg(),AT)['rows'][0]
        self.assertEqual(d['action'],'REDUCE'); self.assertEqual(d['suggested_shares'],250)
        self.assertIn('T_PLUS_1_OR_SELLABLE_CAP',d['blockers'])
        acct['positions']['600000']['sellable']=0
        self.assertEqual(candidate_plan([a],{'600000':0.},acct,cfg(),AT)['rows'][0]['suggested_shares'],0)

    def test_sell_and_hold(self):
        a=row(); acct=account({'600000':dict(shares=1050,sellable=1050)})
        d=candidate_plan([a],{'600000':0.},acct,cfg(),AT)['rows'][0]
        self.assertEqual(d['action'],'SELL'); self.assertEqual(d['suggested_shares'],1050)
        self.assertEqual(candidate_plan([a],{'600000':0.},account(),cfg(),AT)['rows'][0]['action'],'HOLD')

    def test_turnover_cost_cash_caps(self):
        rows=[row('600000','A'),row('000001','B')]
        c={**cfg(),'max_turnover':.05}
        p=candidate_plan(rows,{'600000':.15,'000001':.15},account(),c,AT)
        self.assertLessEqual(p['turnover'],.05); self.assertGreaterEqual(p['available_cash_after'],0)
        p=candidate_plan(rows,{'600000':.15,'000001':.15},account(),{**c,'max_cost_fraction':0},AT)
        self.assertEqual(p['cost'],0); self.assertTrue(all(x['suggested_shares']==0 for x in p['rows']))

    def test_real_uncalibrated_prediction_is_blocked(self):
        a=row(); a['synthetic']=False
        self.assertIn('PROBABILITY_UNCALIBRATED',candidate_plan([a],{'600000':.1},account(),cfg(),AT)['rows'][0]['blockers'])

    def test_bad_config_and_account(self):
        for c in [{**cfg(),'max_stock':2},{**cfg(),'slippage_bps':-1},{**cfg(),'risk_aversion':0}]:
            with self.assertRaises(ValueError): build_plan([row()],np.array([[.01]]),c,at=AT)
        for acct in [account({'600000':dict(shares=100,sellable=101)}),{**account(),'available_cash':float('nan')}]:
            with self.assertRaises(ValueError): build_plan([row()],np.array([[.01]]),cfg(),acct,AT)

    def test_persistence_and_cli_generation(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); (root/'data').mkdir()
            source=dict(asof='2026-10-19',generated_at=AT)
            with patch('asharequant.decision.load_inputs',return_value=([row()],np.array([[.01]]),source,[],{})):
                result,folder=generate(root)
            self.assertTrue((folder/'index.html').exists())
            self.assertTrue((root/'reports/decisions/latest/decisions.csv').exists())
            import sqlite3
            with sqlite3.connect(root/'data/market.sqlite3') as db:
                self.assertEqual(db.execute('select count(*) from decision_runs').fetchone()[0],1)
            self.assertEqual(json.loads((folder/'decision.json').read_text())['submitted_orders'],0)

    def test_oos_does_not_use_future_covariance_or_overlap(self):
        dates=pd.bdate_range('2023-01-01',periods=230); rng=np.random.default_rng(3)
        history=pd.Series(rng.normal(0,.01,len(dates)),index=dates)
        records=pd.DataFrame([dict(symbol='600000',date=dates[i],label_end=dates[i+20],predicted=.03,actual=.01) for i in range(150,210)])
        a=compare_oos(records,{'600000':history},{'600000':'银行'},cfg())
        changed=history.copy(); changed.loc[dates[210]:]=99
        b=compare_oos(records,{'600000':changed},{'600000':'银行'},cfg())
        self.assertEqual(a,b)
        for periods in a['periods'].values():
            for p in periods: self.assertLessEqual(p['covariance_last_date'],p['date'])
            for x,y in zip(periods,periods[1:]): self.assertLess(x['label_end'],y['date'])
        self.assertIsNone(a['formal_method'])

    def test_cli_decision_routes_method_without_market_refresh(self):
        from asharequant.cli import main
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            result=dict(decisions=[],cash_after=150000.,blockers=['FORMAL_METHOD_NOT_VALIDATED'])
            with patch('asharequant.cli.load_config',return_value={}), patch('asharequant.cli.read_watchlist',return_value=[]), patch('asharequant.decision.generate',return_value=(result,root)) as called, patch('asharequant.cli.calendar') as cal:
                self.assertEqual(main(['decision','--root',temp,'--method','risk_parity']),0)
                called.assert_called_once_with(root.resolve(),'risk_parity'); cal.assert_not_called()

    def test_cli_retains_existing_factor_research_command(self):
        from asharequant.cli import main
        with patch('sys.stdout') as output:
            with self.assertRaises(SystemExit) as end: main(['--help'])
        self.assertEqual(end.exception.code,0)
        text=''.join(str(c.args[0]) for c in output.write.call_args_list)
        self.assertIn('factor-research',text)
