import unittest,tempfile,json
from pathlib import Path
from asharequant.decision import costs,DEFAULTS,load_fee_config
from asharequant.connections import import_fees
class RealFeeTests(unittest.TestCase):
 def config(self):
  return {**DEFAULTS,'commission_bps':1.5,'slippage_bps':0.,'fee_market_overrides':{'SSE_MAIN_A':{'commission_includes_transfer':False},'SZSE_MAIN_A':{'commission_includes_transfer':True}}}
 def test_shanghai_statement(self):
  f=costs(20000,'SELL',self.config(),'600031')
  self.assertEqual(f['commission'],5);self.assertAlmostEqual(round(f['total'],2),15.20)
 def test_shenzhen_no_double_transfer(self):
  f=costs(20000,'SELL',self.config(),'000681')
  self.assertEqual(f['transfer'],0);self.assertAlmostEqual(f['total'],15.0)
 def test_large_order_and_buy_tax(self):
  f=costs(100000,'BUY',self.config(),'600031')
  self.assertAlmostEqual(f['commission'],15.0);self.assertEqual(f['stamp'],0);self.assertEqual(f['regulatory'],0)
 def test_unsupported_market(self):
  with self.assertRaises(ValueError):costs(10000,'BUY',self.config(),'430001')
 def test_persist_and_load(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);(root/'data').mkdir();p=root/'input.json';c=self.config();c.update(broker='Example Broker',evidence='SYNTHETIC',effective_date='2026-10-19');p.write_text(json.dumps(c));import_fees(root,p)
   loaded=load_fee_config(root,dict(DEFAULTS));self.assertTrue(loaded['fees_verified']);self.assertEqual(costs(100000,'SELL',loaded,'000681')['transfer'],0)
 def test_invalid_market_config(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);c=self.config();c.update(broker='Example Broker',evidence='SYNTHETIC',effective_date='2026-10-19');c['fee_market_overrides']['SZSE_MAIN_A']['commission_includes_transfer']='false';p=root/'input.json';p.write_text(json.dumps(c))
   with self.assertRaises(ValueError):import_fees(root,p)
