from pathlib import Path
import tempfile,unittest
from asharequant.report_navigation import label_control,entry_page,primary_section,MARK
class NavigationTests(unittest.TestCase):
 def test_control_label_is_explicit_idempotent_and_preserves_data(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'index.html';p.write_text('<title>持续模拟账户</title><h1>持续模拟账户</h1><p>权益150123.45；无合格计划</p>')
   label_control(p,'../../research_paper/latest/index.html');a=p.read_text();label_control(p,'../../research_paper/latest/index.html')
   self.assertEqual(a,p.read_text());self.assertEqual(a.count(MARK),1);self.assertIn('权益150123.45；无合格计划',a);self.assertIn('非当前操作计划',a);self.assertIn('research_paper',a)
 def test_default_entry_opens_new_plan(self):
  p=entry_page('研究/index.html#current-holdings','00_每日总览.html','最新计划')
  self.assertIn('http-equiv="refresh"',p);self.assertIn('0;url=研究/index.html#current-holdings',p);self.assertIn('00_每日总览.html',p)
 def test_missing_new_plan_is_not_silently_replaced_by_control(self):
  with tempfile.TemporaryDirectory() as d:self.assertIn('当前新模型计划尚未生成',primary_section(d))
