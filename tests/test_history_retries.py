import unittest
from unittest.mock import Mock,patch
import requests
from asharequant.history_sources import query_with_retry
class HistoryRetryTests(unittest.TestCase):
 def test_network_retry_recovers(self):
  c=Mock();c.query.side_effect=[requests.exceptions.Timeout(),[{'real':'provider result'}]]
  with patch('asharequant.history_sources.time.sleep'):r=query_with_retry(c,'daily',{'ts_code':'600031.SH'})
  self.assertEqual(r,[{'real':'provider result'}]);self.assertEqual(c.query.call_count,2)
 def test_retry_bounded(self):
  c=Mock();c.query.side_effect=requests.exceptions.ConnectionError()
  with patch('asharequant.history_sources.time.sleep'),self.assertRaises(requests.exceptions.ConnectionError):query_with_retry(c,'daily',{})
  self.assertEqual(c.query.call_count,3)
 def test_permission_and_schema_failures_not_retried(self):
  for error in (RuntimeError('REJECTED'),ValueError('SCHEMA')):
   c=Mock();c.query.side_effect=error
   with self.assertRaises(type(error)):query_with_retry(c,'daily',{})
   self.assertEqual(c.query.call_count,1)
