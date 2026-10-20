import copy,json,unittest
from pathlib import Path
import numpy as np
from asharequant.strategy_experiment import configurations,action,measurements,allocation,validation
from asharequant.research_policy import POLICY,targets
from asharequant.decision import DEFAULTS

class StrategyExperimentTests(unittest.TestCase):
 def setUp(self):
  self.settings=dict(enabled=True,mode='SHADOW_PAPER_ONLY',risk_multipliers=[1.,1.25,1.5],threshold_multipliers=[1.,.9,.8],maximum_drawdown=.15,minimum_equity=120000,minimum_sessions=252,rolling_window=63,minimum_trades=20,auto_promote=False)
  self.base={**DEFAULTS,'fees_verified':True};self.variants=configurations(self.base,POLICY,self.settings)
 def test_grid_and_unchanged_baseline(self):
  self.assertEqual(len(self.variants),9);self.assertEqual(self.variants['baseline']['config'],self.base);self.assertEqual(self.variants['baseline']['policy'],POLICY)
  for v in self.variants.values():
   for k in ('commission_bps','slippage_bps','max_cost_fraction','max_downside','max_uncertainty','minimum_probability'):self.assertEqual(v['config'][k],self.base[k])
   self.assertLessEqual(v['config']['max_invested'],1)
 def test_six_actions(self):
  for a,b,c in [(0,100,'BUY'),(100,200,'ADD'),(100,100,'HOLD'),(200,100,'REDUCE'),(100,0,'SELL'),(0,0,'WAIT')]:self.assertEqual(action(a,b),c)
 def test_no_auto_promotion_or_weaker_limits(self):
  for key,value in [('auto_promote',True),('maximum_drawdown',.2),('minimum_equity',100000)]:
   with self.assertRaises(ValueError):configurations(self.base,POLICY,{**self.settings,key:value})
 def test_missing_returns_not_zero(self):
  m=measurements([],[],150000);self.assertIsNone(m['net_return']);self.assertIsNone(m['trades'])
 def test_known_returns_and_costs(self):
  rows=[dict(date='2026-01-01',equity=165000,cash=15000,turnover=.2,fees=10),dict(date='2026-01-02',equity=148500,cash=15000,turnover=.1,fees=20)]
  m=measurements(rows,[{}],150000);self.assertAlmostEqual(m['net_return'],-.01);self.assertAlmostEqual(m['maximum_drawdown'],-.1);self.assertEqual(m['fees'],30);self.assertIsNone(m['annualized_return'])
 def test_no_selection_from_backtest_return_alone(self):
  r=validation(dict(ledger=[],fills=[],blocked={}),150000,self.settings);self.assertEqual(r['status'],'NOT_VALIDATED');self.assertFalse(r['auto_promote'])
 def test_daily_hook_and_report_preserve_original_sections(self):
  root=Path(__file__).parents[1];cli=(root/'asharequant/cli.py').read_text();self.assertIn('compare_strategies(root)',cli)
  from asharequant.daily_bundle import REPORTS
  names=[x[1] for x in REPORTS]
  for name in ['research_paper/latest','paper_account/latest','forecasts/latest','strategy_experiment/latest']:self.assertIn(name,names)
 def fixture(self):
  f=dict(predicted_return=.08,volatility_horizon=.08,downside_es95_normal_proxy=-.08,model_uncertainty={'std':.005},interval80_empirical=[-.05,.15],up_probability_empirical=.7)
  p=dict(asof='2026-10-20',seven_metrics=[dict(symbol='600031',horizon=20,composite_rank=1)],forecasts=[dict(symbol='600031',name='测试',sector='机械',close=10.,price_source='SYNTHETIC_TEST',composite_score={'score':80},data_eligible=True,forecasts={'20':f})])
  return p,np.array([[.0064]])
 def test_preserves_prediction_and_rank_and_exact_baseline(self):
  p,cov=self.fixture();before=copy.deepcopy(p);variant=self.variants['baseline'];expected,_=targets(p,{},150000,cov,self.base,'risk_off',POLICY)
  result=allocation(p,{},150000,cov,variant,'risk_off',.2,self.settings,150000)
  self.assertEqual(result['weights'],expected);self.assertEqual(p,before);self.assertEqual(result['rows'][0]['original_rank'],1);self.assertEqual(result['rows'][0]['original_prediction'],.08)
 def test_relative_volatility_envelope(self):
  p,cov=self.fixture();v=self.variants['risk1.5_threshold0.8'];r=allocation(p,{},150000,cov,v,'risk_on',.01,self.settings,150000)
  self.assertLessEqual(r['predicted_annual_volatility'],.015+1e-12);self.assertTrue(r['risk_flags'])
 def test_loss_and_drawdown_trigger_candidates_only(self):
  p,cov=self.fixture();v=self.variants['risk1.5_threshold0.8']
  for equity,peak in [(119999,150000),(150000,180000)]:
   r=allocation(p,{},equity,cov,v,'risk_on',.2,self.settings,peak);self.assertEqual(sum(r['weights'].values()),0);self.assertFalse(r['rows'][0]['executable'])
 def test_no_positive_edge_keeps_cash_even_aggressive(self):
  p,cov=self.fixture();p['forecasts'][0]['forecasts']['20']['predicted_return']=.0001
  r=allocation(p,{},150000,cov,self.variants['risk1.5_threshold0.8'],'risk_on',.2,self.settings,150000);self.assertEqual(r['cash_target'],1.);self.assertEqual(r['rows'][0]['decision'],'WAIT')
 def test_rolling_is_nonoverlapping_and_metrics_anchor_previous_equity(self):
  ledger=[dict(date=f'd{i}',equity=150000,cash=150000,turnover=0,fees=0) for i in range(126)]
  r=validation(dict(ledger=ledger,fills=[],blocked={}),150000,self.settings);self.assertEqual(len(r['rolling_windows']),2);self.assertEqual(r['rolling_windows'][1]['start'],'d63')
 def test_two_day_shadow_pipeline_settles_only_frozen_plans(self):
  import tempfile,sqlite3,pandas as pd
  from unittest.mock import patch
  from asharequant.strategy_experiment import run
  from asharequant.paper_account import get
  payload,cov=self.fixture();payload['model']='SYNTHETIC_TEST'
  base={**self.base,'max_volume_participation':.01,'require_fill_in_range':True}
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory);(root/'data').mkdir();folder=root/'reports/research_paper/latest';folder.mkdir(parents=True)
   (root/'strategy_experiment_config.json').write_text(json.dumps(self.settings))
   (root/'data/calendar.csv').write_text('date\n2026-10-20\n2026-10-21\n2026-10-22\n')
   dates=pd.bdate_range(end='2026-10-20',periods=260);hist=pd.DataFrame({'date':dates,'close':10+np.sin(np.arange(260))*.05})
   source=dict(status='ACTIVE_RESEARCH_PAPER',asof='2026-10-20',valuation_date='2026-10-20',positions={'600031':{'shares':1000,'sellable':1000}},equity=150000.,cash=140000.,meta={'config':base},spec={'version':'SYNTHETIC_TEST','policy':POLICY},predictions=payload,plan={'target_session':'2026-10-21','origin_session':'2026-10-20','environment':'risk_on'})
   payload['generated_at']='2026-10-20T16:00:00+08:00';(folder/'report.json').write_text(json.dumps(source))
   observation=dict(session='2026-10-21',bars={'600031':dict(open=10.,high=11.,low=9.,close=10.1,volume=1000000)},states={'600031':dict(status='trading',source='SYNTHETIC_TEST',corporate_action_status='none',limit_down=9.,limit_up=11.)})
   with patch('asharequant.strategy_experiment.read_snapshot',return_value={'hfq':hist}),patch('asharequant.strategy_experiment.now',return_value=pd.Timestamp('2026-10-20T18:00:00+08:00')):
    first=run(root);self.assertTrue(all(x['metrics']['sessions']==0 for x in first['results'].values()))
   source['asof']=source['valuation_date']=payload['asof']='2026-10-21';payload['generated_at']='2026-10-21T16:00:00+08:00';source['plan'].update(origin_session='2026-10-21',target_session='2026-10-22');(folder/'report.json').write_text(json.dumps(source))
   with patch('asharequant.strategy_experiment.read_snapshot',return_value={'hfq':hist}),patch('asharequant.strategy_experiment.now',return_value=pd.Timestamp('2026-10-21T18:00:00+08:00')),patch('asharequant.strategy_experiment.evidence',return_value=observation):
    second=run(root)
    with sqlite3.connect(root/'data/strategy_experiment.sqlite3') as db:before=db.execute('select * from records order by kind,key').fetchall()
    third=run(root)
    with sqlite3.connect(root/'data/strategy_experiment.sqlite3') as db:after=db.execute('select * from records order by kind,key').fetchall()
   self.assertEqual(before,after);self.assertEqual(second['snapshot_count'],2)
   for x in second['results'].values():self.assertEqual(x['metrics']['sessions'],1);self.assertGreater(x['metrics']['trades'],0);self.assertEqual(x['plan']['origin_session'],'2026-10-21')
   with patch('asharequant.strategy_experiment.read_snapshot',return_value={'hfq':hist}),patch('asharequant.strategy_experiment.now',return_value=pd.Timestamp('2026-10-21T18:00:00+08:00')):
    source['predictions']['forecasts'][0]['forecasts']['20']['predicted_return']=.09;(folder/'report.json').write_text(json.dumps(source))
    with self.assertRaisesRegex(ValueError,'FROZEN_FORECAST_CHANGED'):run(root)
