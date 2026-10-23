import json,tempfile,unittest
from pathlib import Path
from asharequant.reporting.plain_chinese import render,localize
class ReadableTests(unittest.TestCase):
 def test_values_unchanged_and_state_translated(self):
  s=render('<p>SELL 123.45 RESEARCH_ONLY</p><script>var x="SELL";</script>')
  self.assertIn('卖出 123.45 仅供研究',s);self.assertIn('var x="SELL"',s)
 def test_json_link_becomes_readable_and_original_unchanged(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d);raw='{"status":"FAILED","cash":12.3}';(p/'x.json').write_text(raw);(p/'index.html').write_text('<a href="x.json">JSON</a>')
   result=localize(p);self.assertEqual(result['broken'],[]);self.assertEqual((p/'x.json').read_text(),raw)
   self.assertIn('x.json.view.html',(p/'index.html').read_text());self.assertIn('现金',(p/'x.json.view.html').read_text())
 def test_pre_is_table_and_escaped(self):
  s=render('<pre>{"cash": 10, "reason":"&lt;script&gt;"}</pre>')
  self.assertNotIn('<pre',s);self.assertIn('现金',s);self.assertNotIn('<script>',s)
 def test_broken_anchor_detected(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d);(p/'index.html').write_text('<a href="#absent">a</a><a href="missing.html">b</a>')
   self.assertEqual(len(localize(p)['broken']),2)

 def test_legacy_empty_view_reads_same_archived_main_account(self):
  from asharequant.reporting.plain_chinese import attachment
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);p=root/'账户每日与累计收益.csv';p.write_text('date,nav,daily_pnl,daily_return,cumulative_return,status\n')
   nav=root/'详细报告/dynamic/latest/NAV.csv';nav.parent.mkdir(parents=True);nav.write_text('date,equity,daily_pnl,mode\n2026-10-22,123000,-100,PAPER_ONLY\n')
   raw=p.read_bytes();out=attachment(p)
   self.assertIn('主账户',out);self.assertNotIn('尚未导入',out);self.assertIn('123,000.00',out)
   self.assertIn('展示更正',out);self.assertIn('1 条数据',out);self.assertEqual(p.read_bytes(),raw)
 def test_empty_paper_no_fabricated_return(self):
  from asharequant.reporting.plain_chinese import attachment
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'NAV.csv';p.write_text('\ufeffdate,equity\n')
   out=attachment(p);self.assertIn('0 条数据',out);self.assertIn('没有记录不代表收益为零',out)
 def test_populated_csv_is_not_marked_missing(self):
  from asharequant.reporting.plain_chinese import attachment
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'NAV.csv';p.write_text('date,equity\n2026-10-22,123456.789\n')
   out=attachment(p);self.assertIn('1 条数据',out);self.assertIn('123,456.79',out);self.assertNotIn('暂无可显示',out)

 def test_legacy_without_archive_never_uses_other_account(self):
  from asharequant.reporting.plain_chinese import attachment
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'账户每日与累计收益.csv';p.write_text('date,nav\n2026-10-22,999999\n')
   out=attachment(p);self.assertNotIn('999999',out);self.assertIn('0 条数据',out);self.assertNotIn('券商净值',out)

 def test_render_is_idempotent(self):
  page='<!doctype html><html><h1>主账户</h1><p>SELL PAPER_ONLY</p></html>'
  once=render(page);self.assertEqual(render(once),once)

 def test_account_percent_format_does_not_change_evidence(self):
  from asharequant.reporting.plain_chinese import attachment
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'NAV.csv';raw='date,equity,daily_return,cumulative_return,mode\n2026-10-22,123456.789,-0.01234567,0.02345678,PAPER_ONLY\n';p.write_text(raw)
   out=attachment(p);self.assertIn('-1.23%',out);self.assertIn('2.35%',out);self.assertNotIn('（小数）',out);self.assertEqual(p.read_text(),raw)
