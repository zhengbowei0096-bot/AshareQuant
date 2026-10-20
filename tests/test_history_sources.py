"""SYNTHETIC HTTP doubles only; no real independent validation evidence."""
from pathlib import Path
import tempfile,unittest
from unittest.mock import patch,Mock
import pandas as pd
from asharequant import history_sources as hs
from asharequant.core import write_json
from asharequant.connections import digest
class HistoryTests(unittest.TestCase):
 def data(self):
  return {'daily':[{'trade_date':'20200102','open':10,'close':10,'high':11,'low':9},{'trade_date':'20200103','open':10,'close':10,'high':11,'low':9}], 'stk_limit':[{'trade_date':'20200102','up_limit':11,'down_limit':9,'pre_close':10},{'trade_date':'20200103','up_limit':11,'down_limit':9,'pre_close':10}], 'adj_factor':[{'trade_date':'20200102','adj_factor':1},{'trade_date':'20200103','adj_factor':1}], 'suspend_d':[],'dividend':[],'namechange':[{'name':'测试股票','start_date':'20190101','end_date':None,'ann_date':'20181201'}]}
 def test_no_token_no_network(self):
  with patch.dict('os.environ',{},clear=True),patch('requests.post',side_effect=AssertionError('no credentials')):
   with self.assertRaises(RuntimeError):hs.Tushare()
 def test_http_timeout_and_credentials(self):
  response=Mock();response.json.return_value={'code':0,'data':{'fields':['symbol'],'items':[['600000']]}}
  with patch.dict('os.environ',{'TUSHARE_TOKEN':'SYNTHETIC_SECRET'}),patch('requests.post',return_value=response) as request:
   self.assertEqual(hs.Tushare().query('daily',ts_code='600000.SH'),[{'symbol':'600000'}]);self.assertEqual(request.call_args.kwargs['timeout'],(5,20))
 def test_permission_redaction(self):
  response=Mock();response.json.return_value={'code':-1,'msg':'secret could appear here'}
  with patch.dict('os.environ',{'TUSHARE_TOKEN':'SYNTHETIC_SECRET'}),patch('requests.post',return_value=response):
   with self.assertRaisesRegex(RuntimeError,'TUSHARE_API_REJECTED') as error:hs.Tushare().query('daily')
   self.assertNotIn('secret',str(error.exception))
 def test_adjustment_not_faked_as_no_event(self):
  data=self.data();rows=hs.normalize_tushare('600000',data,{})
  self.assertEqual(rows[0]['corporate_action_status'],'unknown');self.assertEqual(rows[1]['corporate_action_status'],'none')
  data['adj_factor'][1]['adj_factor']=1.1;self.assertEqual(hs.normalize_tushare('600000',data,{})[1]['corporate_action_status'],'unknown')
 def test_binding_and_tampering(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);data=self.data();bindings={api:hs._save(root,api,{'ts_code':'600000.SH'},rows,'Tushare') for api,rows in data.items()};records=hs.normalize_tushare('600000',data,bindings)
   manifest={'execution_records':records,'references':list(bindings.values())};write_json(root/'data/history/latest.json',manifest);write_json(root/'data/history/manifests'/(digest(manifest)+'.json'),manifest)
   states,proof,errors=hs.execution_states(root,['600000'],pd.DatetimeIndex(['2020-01-03']));self.assertEqual(errors,[]);self.assertEqual(len(states),1)
   (root/bindings['daily']['path']).write_text('{}')
   with self.assertRaises(ValueError):hs.verified(root,bindings['daily'])
   self.assertTrue(hs.execution_states(root,['600000'],pd.DatetimeIndex(['2020-01-03']))[2])
 def test_normalized_flags_cannot_override_provider(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);data=self.data();bindings={api:hs._save(root,api,{'ts_code':'600000.SH'},rows,'Tushare') for api,rows in data.items()};records=hs.normalize_tushare('600000',data,bindings);records[1]['limit_up']=999
   manifest={'execution_records':records};write_json(root/'data/history/latest.json',manifest);write_json(root/'data/history/manifests'/(digest(manifest)+'.json'),manifest)
   self.assertTrue(hs.execution_states(root,['600000'],pd.DatetimeIndex(['2020-01-03']))[2])
 def test_missing_state_no_trade(self):
  with tempfile.TemporaryDirectory() as tmp:self.assertEqual(hs.execution_states(Path(tmp),['600000'],pd.DatetimeIndex(['2020-01-03']))[0],{})

 def test_st_or_unknown_name_is_not_ordinary_mainboard(self):
  data=self.data();data['namechange'][0]['name']='ST测试'
  self.assertFalse(hs.normalize_tushare('600000',data,{})[1]['ordinary_session'])
  data['namechange']=[];self.assertFalse(hs.normalize_tushare('600000',data,{})[1]['ordinary_session'])
 def test_nonordinary_limit_regime_rejected(self):
  data=self.data();data['stk_limit'][1]['up_limit']=10.5;data['stk_limit'][1]['down_limit']=9.5
  self.assertFalse(hs.normalize_tushare('600000',data,{})[1]['ordinary_session'])

 def test_private_credential_file_works_without_shell_configuration(self):
  with tempfile.TemporaryDirectory() as tmp,patch.dict('os.environ',{},clear=True):
   root=Path(tmp);path=root/'data/credentials/tushare.token';path.parent.mkdir(parents=True);path.write_text('SYNTHETIC_SECRET');path.chmod(0o600)
   self.assertEqual(hs.Tushare(root).token,'SYNTHETIC_SECRET')
   path.chmod(0o644)
   with self.assertRaisesRegex(RuntimeError,'PRIVATE'):hs.Tushare(root)
 def test_stock_basic_pages_are_not_silently_truncated(self):
  first=Mock();first.json.return_value={'code':0,'data':{'fields':['ts_code'],'items':[[str(i)] for i in range(1000)]}}
  second=Mock();second.json.return_value={'code':0,'data':{'fields':['ts_code'],'items':[['last']]}}
  with patch.dict('os.environ',{'TUSHARE_TOKEN':'SYNTHETIC_SECRET'}),patch('requests.post',side_effect=[first,second]) as request,patch('time.sleep'):
   rows=hs.Tushare().query('stock_basic',list_status='L');self.assertEqual(len(rows),1001);self.assertEqual(request.call_args.kwargs['json']['params']['offset'],1000)
 def test_nonadvancing_pagination_rejected(self):
  response=Mock();response.json.return_value={'code':0,'data':{'fields':['ts_code'],'items':[[str(i)] for i in range(1000)]}}
  with patch.dict('os.environ',{'TUSHARE_TOKEN':'SYNTHETIC_SECRET'}),patch('requests.post',return_value=response),patch('time.sleep'):
   with self.assertRaisesRegex(ValueError,'NOT_ADVANCING'):hs.Tushare().query('stock_basic')
 def test_company_action_dates_explicitly_requested(self):
  self.assertIn('div_listdate',hs.API_FIELDS['dividend']);self.assertIn('pay_date',hs.API_FIELDS['dividend']);self.assertIn('ann_date',hs.API_FIELDS['namechange'])
