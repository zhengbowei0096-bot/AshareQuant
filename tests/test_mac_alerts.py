import unittest
from datetime import datetime,timedelta
from unittest.mock import patch
from asharequant.mac_alerts import messages,popup,TZ
class TestAlerts(unittest.TestCase):
 def setUp(self):
  self.t=datetime(2026,10,21,10,tzinfo=TZ);self.h=dict(checked_at=self.t.isoformat(),status='RUNNING',reason=None)
  self.a=dict(id='a',order_id='o',at=self.t.isoformat(),status='ACCEPTED',side='BUY',symbol='600031',shares=1000,filled=0,limit_price=18.,reason='signal')
 def test_dedup(self):
  l,s=messages([self.a],self.h,{},self.t,notify_trades=True);self.assertIn('待撮合模拟委托',l[0]);self.assertNotIn('新增模拟成交',l[0]);self.assertEqual(messages([self.a],self.h,s,self.t)[0],[])
 def test_partial(self):
  f=dict(self.a,id='b',status='PAPER_FILL',shares=100,price=17.99);e=dict(self.a,id='c',status='PARTIALLY_FILLED',filled=100)
  l,_=messages([self.a,f,e],self.h,{},self.t,notify_trades=True);self.assertEqual(len(l),1);self.assertIn('新增模拟成交 100股',l[0]);self.assertIn('累计成交100股',l[0])
 def test_history_sell(self):
  a=dict(self.a,side='SELL',at=(self.t-timedelta(minutes=6)).isoformat());l,_=messages([a],self.h,{},self.t,notify_trades=True);self.assertIn('历史事件',l[0]);self.assertIn('卖出',l[0])
 def test_error_recovery(self):
  h=dict(self.h,status='FAILED',reason='network');l,s=messages([],h,{},self.t);self.assertEqual(len(l),1);self.assertEqual(messages([],h,s,self.t)[0],[]);self.assertIn('恢复',messages([],self.h,s,self.t)[0][0])
 def test_persistent(self):
  h=dict(self.h,reason='missing');l,s=messages([],h,{},self.t);self.assertEqual(l,[]);self.assertIn('持续不可用',messages([],h,s,self.t)[0][0])
 def test_stale(self):
  h=dict(self.h,checked_at=(self.t-timedelta(seconds=121)).isoformat());self.assertIn('120秒',messages([],h,{},self.t)[0][0]);self.assertEqual(messages([],h,{},self.t.replace(hour=7))[0],[])
 def test_silent(self):
  with patch('asharequant.mac_alerts.subprocess.run') as r:
   popup('test');args=r.call_args.args[0];self.assertNotIn('sound',args[2]);self.assertEqual(args[-1],'test')

 def test_trades_suppressed_by_default(self):
  l,s=messages([self.a],self.h,{},self.t);self.assertEqual(l,[]);self.assertIn("a",s["seen"])
