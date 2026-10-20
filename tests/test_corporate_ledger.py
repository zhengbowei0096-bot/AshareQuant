"""SYNTHETIC dividend/bonus lifecycle fixtures, not actual brokerage settlements."""
import copy,unittest
import pandas as pd
from asharequant.execution_replay import simulate
from asharequant.decision import DEFAULTS
from asharequant.history_sources import normalize_tushare

class CorporateLedgerTests(unittest.TestCase):
 def run_event(self,cash=5.,ratio=0.,listing=4):
  days=pd.bdate_range('2020-01-01',periods=6);price=5. if cash else 8.33
  bars=pd.DataFrame({'open':[10,10]+[price]*4,'close':[10,10]+[price]*4},index=days)
  event=dict(event_id='SYNTHETIC',record_date=str(days[1].date()),ex_date=str(days[2].date()),pay_date=str(days[4].date()),listing_date=str(days[listing].date()),cash_per_share_gross=cash,bonus_ratio=ratio,tax_reserve_rate=.2)
  states={('600000',str(day.date())):dict(status='trading',source='SYNTHETIC',limit_down=float(bars.loc[day,'open'])*.9,limit_up=float(bars.loc[day,'open'])*1.1,corporate_action_status='event',action={'events':[event]}) for day in days}
  config={**DEFAULTS,'max_stock':1.,'max_sector':1.,'max_invested':1.,'max_turnover':1.,'minimum_trade_value':0.,'commission_bps':0.,'minimum_commission':0.,'transfer_bps':0.,'slippage_bps':0.,'sell_tax_bps':0.}
  signals={str(day.date()):{'600000':1. if cash or i<1 else 0.} for i,day in enumerate(days)}
  return days,simulate(days,{'600000':bars},signals,states,config,initial_cash=10000)
 def test_cash_receivable_not_spendable_before_payment_close(self):
  days,result=self.run_event();ledger=result['ledger']
  self.assertEqual(ledger[2]['cash'],0);self.assertEqual(ledger[2]['dividend_receivable'],4000)
  self.assertEqual(ledger[3]['cash'],0);self.assertEqual(ledger[4]['cash'],4000);self.assertEqual(ledger[4]['dividend_receivable'],0)
  self.assertEqual(result['summary']['dividend_tax_reserved_conservative'],1000)
  buys=[r for r in result['fills'] if r['side']=='BUY'];self.assertEqual([r['date'] for r in buys],[str(days[1].date()),str(days[5].date())])
 def test_bonus_shares_not_sellable_until_listing(self):
  days,result=self.run_event(cash=0,ratio=.2)
  self.assertEqual(result['ledger'][2]['locked_bonus_shares'],{'600000':200})
  sells=[r for r in result['fills'] if r['side']=='SELL'];self.assertEqual(sells[-1]['date'],str(days[4].date()));self.assertEqual(sells[-1]['shares'],200)
  self.assertFalse(result['ledger'][4]['locked_bonus_shares'])
 def test_provider_dividend_requires_dates_and_factor_reconciliation(self):
  base={'daily':[{'trade_date':'20200102','close':10},{'trade_date':'20200103','close':9}], 'stk_limit':[],'suspend_d':[], 'adj_factor':[{'trade_date':'20200102','adj_factor':1},{'trade_date':'20200103','adj_factor':10/9}], 'namechange':[], 'dividend':[dict(div_proc='实施',ann_date='20191201',record_date='20200102',ex_date='20200103',pay_date='20200106',cash_div_tax=1.,stk_div=0.)]}
  rows=normalize_tushare('600000',base,{})
  self.assertEqual(rows[1]['corporate_action_status'],'event');self.assertEqual(rows[1]['action']['events'][0]['tax_reserve_rate'],.2)
  broken=copy.deepcopy(base);broken['dividend'][0]['pay_date']=None
  self.assertEqual(normalize_tushare('600000',broken,{})[1]['corporate_action_status'],'unknown')
 def test_duplicate_provider_dates_are_not_silently_cleaned(self):
  base={'daily':[{'trade_date':'20200102'},{'trade_date':'20200102'}],'stk_limit':[],'suspend_d':[],'adj_factor':[],'dividend':[],'namechange':[]}
  with self.assertRaisesRegex(ValueError,'DUPLICATE'):normalize_tushare('600000',base,{})
