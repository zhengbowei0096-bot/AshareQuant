import tempfile,unittest
from pathlib import Path
from asharequant.holding_summary import render,update,START
class HoldingSummaryTests(unittest.TestCase):
 def report(self):
  return {'equity':150000,'meta':{'config':{'max_turnover':.3,'minimum_trade_value':2000,'horizon':20}},'plan':{'target_session':'2026-10-21','rows':[{'symbol':s,'name':s,'current_shares':3000,'direction':'SELL','reasons':['研究概率不足'],'reference_price':20,'price_date':'2026-10-20','price_source':'TEST','probability':{'research_up_probability':.48}} for s in ['000681','600031','600875']]},'reference_scenario':{'fills':[{'symbol':'000681','side':'SELL','shares':2200,'reference_price':20}]}}
 def test_quantity_reason_and_horizon(self):
  text=render(self.report());self.assertIn('2200',text);self.assertIn('800',text);self.assertIn('剩余换手预算不足',text);self.assertIn('20交易日上涨概率',text);self.assertEqual(text.count('<tr>'),4)
 def test_update_at_top_idempotent_and_preserves_report(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'reports/research_paper/latest';p.mkdir(parents=True);(p/'index.html').write_text('<h1>报告</h1><p>旧内容</p>')
   r=self.report();update(d,r);first=(p/'index.html').read_text();update(d,r)
   self.assertEqual(first,(p/'index.html').read_text());self.assertEqual(first.count(START),1);self.assertLess(first.index(START),first.index('旧内容'))
