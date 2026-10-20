import unittest,copy
from asharequant.next_session_plan import annotate,render
class NextSessionTests(unittest.TestCase):
 def result(self):
  return dict(generated_at='2026-10-19T18:00:00+08:00',formal_method='equal_weight',decisions=[dict(symbol='600000',name='纯测试',price=10.,price_source='SYNTHETIC_TEST_ONLY',price_asof='2026-10-19T15:00:00+08:00',predicted_return=.1,up_probability=.7,probability_calibrated=True,current_shares=0,target_amount=10000.,target_weight=.1,target_kind='RESEARCH_TARGET',action='NO_TRADE',suggested_shares=0,executable=False,blockers=['OUTSIDE_SUPPORTED_CONTINUOUS_SESSION','PRICE_NOT_CURRENT_LIVE','TRADING_STATUS_UNVERIFIED'])])
 def test_closed_market_does_not_block_next_session_intention(self):
  r=self.result();before=copy.deepcopy(r['decisions'][0]);annotate(r,'2026-10-20',True);d=r['decisions'][0]
  self.assertEqual(d['next_session_action'],'BUY');self.assertEqual(d['planning_blockers'],[])
  self.assertEqual(d['blockers'],before['blockers']);self.assertFalse(d['executable']);self.assertEqual(d['suggested_shares'],0);self.assertIsNone(d['next_session_suggested_shares'])
 def test_model_account_and_probability_gates_still_block(self):
  for flag in ['FORMAL_METHOD_NOT_VALIDATED','ACCOUNT_STALE_OR_UNVERIFIED','FORECAST_VALIDATION_FAILED','MARKET_SOURCE_CONFLICT']:
   r=self.result();r['decisions'][0]['blockers'].append(flag);annotate(r,'2026-10-20',True)
   self.assertEqual(r['decisions'][0]['next_session_action'],'NO_TRADE');self.assertIn(flag,r['decisions'][0]['planning_blockers'])
  r=self.result();r['decisions'][0]['probability_calibrated']=False;annotate(r,'2026-10-20',True);self.assertEqual(r['decisions'][0]['next_session_action'],'NO_TRADE')
 def test_unverified_or_expired_plan_blocked(self):
  for date,verified in [('2026-10-19',True),('2026-10-20',False),(None,False)]:
   r=self.result();annotate(r,date,verified);self.assertEqual(r['decisions'][0]['next_session_action'],'NO_TRADE')
 def test_premarket_and_evening_same_intention(self):
  a=self.result();b=self.result();b['generated_at']='2026-10-20T07:00:00+08:00'
  annotate(a,'2026-10-20',True);annotate(b,'2026-10-20',True);self.assertEqual(a['decisions'][0]['next_session_action'],b['decisions'][0]['next_session_action'])
 def test_preserved_position_not_labelled_as_target(self):
  r=self.result();r['formal_method']=None;r['decisions'][0]['target_kind']='PRESERVE_CURRENT_PENDING_VALIDATION';annotate(r,'2026-10-20',True)
  page=render(r);self.assertIn('尚未确定',page);self.assertIn('下一交易日：2026-10-20',page);self.assertNotIn('10000.0',page)
