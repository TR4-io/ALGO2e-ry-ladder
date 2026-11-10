import time
import unittest
from unittest.mock import Mock
from concurrent_client import ConcurrentClient
from execution import UnknownWrite


class RuntimeTransportTests(unittest.TestCase):
    def test_timed_batch_records_dispatch_after_budget_wait(self):
        c=ConcurrentClient(500,1)
        c.budget.acquire=lambda deadline: time.sleep(.025)
        c._request=Mock(return_value={})
        try:
            queued=time.monotonic()
            batch=c.batch([('GET','case',{})],capture_timing=True)
            self.assertGreaterEqual(batch.started[0]-queued,.02)
            self.assertEqual(batch.results,[{}])
        finally:c.close()

    def test_uncertain_delete_does_not_latch_unknown_submission_guard(self):
        c=ConcurrentClient(500,1)
        c._request=Mock(side_effect=[UnknownWrite('cancel response lost'),{'order_id':1}])
        try:
            with self.assertRaises(UnknownWrite):c.call('DELETE','orders/1')
            self.assertFalse(c.uncertain_write.is_set())
            self.assertEqual(c.call('POST','orders',ticker='RY'),{'order_id':1})
        finally:c.close()
