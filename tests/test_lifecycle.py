import io
import json
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch
import requests

from config import Config
from execution import Client, UnknownWrite, RateLimited, HttpError, ApiError, Order, clean_book
from signals import Market, Snapshot
from policy import phase, sweep
from engine import Bot, Recorder


def row(oid=1, ticker='CNR', action='BUY', quantity=1000, filled=0, status='OPEN', price=10, **kwargs):
    return dict(order_id=oid, ticker=ticker, action=action, quantity=quantity,
                quantity_filled=filled, status=status, price=price, **kwargs)


def snapshot(**kw):
    data = dict(bid=10., ask=10.02, mid=10.01, fair=10.01, sigma=0., imbalance=0.,
                flow=0., churn=0., stable_for=2., samples=20)
    return Snapshot(**dict(data, **kw))


class ExecutionTests(unittest.TestCase):
    def bot(self, now=1):
        return Bot(Mock(), clock=lambda: now)

    def test_partial_fill_counted_once_and_vwap_delta_logged(self):
        log = io.StringIO()
        b = self.bot()
        b.record = Recorder(log)
        b.ledger.orders[1] = Order(1, 'CNR', 'BUY', 1000, 10, 0)
        b.update(row(filled=300, vwap=9.99))
        b.update(row(filled=300, vwap=9.99))
        b.update(row(filled=500, status='CANCELLED', vwap=9.994))
        self.assertEqual(b.ledger.volume, 500)
        self.assertEqual(b.ledger.positions['CNR'], 500)
        events = [json.loads(x) for x in log.getvalue().splitlines()]
        self.assertEqual(len(events), 2)
        self.assertAlmostEqual(events[1]['execution_price'], 10)

    def test_cancel_racing_fill_and_no_assumed_cancel(self):
        b = self.bot()
        o = Order(1, 'CNR', 'BUY', 1000, 10, 0)
        b.ledger.orders[1] = o
        b.api = Mock(side_effect=[{}, row(filled=700, status='CANCELLED')])
        b.cancel(o)
        self.assertEqual(b.ledger.positions['CNR'], 700)
        self.assertFalse(b.ledger.active())

    def test_unconfirmed_cancel_blocks_replacement(self):
        b = self.bot()
        o = Order(1, 'CNR', 'BUY', 1000, 10, 0)
        b.ledger.orders[1] = o
        b.api = Mock(return_value=row())
        with patch('engine.time.sleep'), self.assertRaises(ApiError):
            b.cancel(o)
        self.assertEqual(o.remaining, 1000)
        self.assertEqual(b.api.call_count, 12)

    def test_cancel_waits_through_open_and_missing_snapshots(self):
        b = self.bot()
        o = Order(1, 'CNR', 'BUY', 1000, 10, 0)
        b.ledger.orders[1] = o
        b.api = Mock(side_effect=[{'success': True}, row(filled=200),
                                 HttpError(404, 'orders/1'), [], [], [],
                                 row(filled=700, status='CANCELLED')])
        with patch('engine.time.sleep'):
            b.cancel(o)
        self.assertEqual(b.ledger.volume, 700)
        self.assertEqual(b.ledger.positions['CNR'], 700)
        self.assertFalse(b.ledger.active())
        self.assertEqual(sum(c.args[0] == 'DELETE' for c in b.api.call_args_list), 1)
        self.assertFalse(any(c.args[0] == 'POST' for c in b.api.call_args_list))

    def test_cancel_can_race_with_complete_fill(self):
        b = self.bot()
        o = Order(1, 'CNR', 'BUY', 1000, 10, 0)
        b.ledger.orders[1] = o
        b.api = Mock(side_effect=[HttpError(404, 'orders/1'),
                                 row(filled=1000, status='TRANSACTED')])
        b.cancel(o)
        self.assertEqual(b.ledger.volume, 1000)
        self.assertFalse(b.ledger.active())

    def test_missing_order_lookup_recovers_and_is_bounded(self):
        from engine import MissingOrder
        b = self.bot()
        b.lookup_once = Mock(side_effect=[MissingOrder('missing'), row(status='CANCELLED')])
        with patch('engine.time.sleep'):
            self.assertEqual(b.lookup(1)['status'], 'CANCELLED')
        b.lookup_once = Mock(side_effect=MissingOrder('missing'))
        with patch('engine.time.sleep'), self.assertRaises(MissingOrder):
            b.lookup(1)
        self.assertEqual(b.lookup_once.call_count, 11)

    def test_404_terminal_recovery(self):
        b = self.bot()
        b.api = Mock(side_effect=[HttpError(404, 'orders/1'), [], [], [row(status='CANCELLED')]])
        self.assertEqual(b.lookup(1)['status'], 'CANCELLED')

    def test_unknown_write_is_never_retried(self):
        c = Client()
        c.session.request = Mock(side_effect=requests.Timeout())
        with self.assertRaises(UnknownWrite):
            c.call('POST', 'orders')
        self.assertEqual(c.session.request.call_count, 1)
        c.session.close()

    def test_rate_limit_wait(self):
        c = Client()
        c.session.request = Mock(return_value=Mock(status_code=429, headers={'Retry-After': '3'}))
        with self.assertRaises(RateLimited) as e:
            c.call('POST', 'orders')
        self.assertEqual(e.exception.wait, 3)
        c.session.close()

    def test_stale_book_and_self_cross_block_submissions(self):
        b = self.bot(2)
        b.api = Mock()
        b.submit('CNR', 'BUY', 1000, 10, 'MAKE', 0)
        b.ledger.orders[1] = Order(1, 'CNR', 'SELL', 1000, 10, 0)
        b.submit('CNR', 'BUY', 1000, 10, 'TAKE', 2)
        b.api.assert_not_called()

    def test_slow_cycle_cannot_add_exposure_after_entry_cutoff(self):
        b = self.bot(3)
        b.tick, b.case_seen = 274, 1
        b.api = Mock()
        b.submit('CNR', 'BUY', 1000, 10, 'MAKE', 3)
        b.api.assert_not_called()

    def test_reset_never_cancels_old_ids(self):
        b = self.bot()
        b.period, b.tick = 1, 100
        b.api = Mock(return_value={'period': 2, 'tick': 0, 'status': 'ACTIVE'})
        with self.assertRaises(ApiError):
            b.step()
        b.cleanup()
        self.assertEqual(b.api.call_count, 1)

    def test_hard_stop_precedes_qualification(self):
        b = self.bot()
        b.period, b.start_pnl = 1, 0
        b.api = Mock(return_value={'period': 1, 'tick': 100, 'status': 'ACTIVE'})
        b.reconcile = Mock()
        b.securities = Mock(return_value={t: dict(position=0, realized=-600, unrealized=0) for t in ('CNR','RY','AC')})
        self.assertFalse(b.step())
        self.assertTrue(b.stopping)
        self.assertEqual(b.mode, 'EXIT')

    def test_repeated_snapshot_mismatch_blocks_orders(self):
        b = self.bot()
        b.period = 1
        b.api = Mock(return_value={'period': 1, 'tick': 10, 'status': 'ACTIVE'})
        b.reconcile = Mock()
        b.securities = Mock(return_value={t: dict(position=100) for t in ('CNR','RY','AC')})
        b.manage = Mock()
        with self.assertRaises(ApiError):
            b.step()
        self.assertEqual(b.reconcile.call_count, 3)
        b.manage.assert_not_called()

    def test_analyzer_handles_interrupted_last_line(self):
        import tempfile
        from analyze_run import summarize
        with tempfile.NamedTemporaryFile(mode='w+', suffix='.jsonl', delete=False) as f:
            f.write(json.dumps(dict(event='fill', ticker='CNR', quantity=500, intent='MAKE'))+'\n{')
            path = f.name
        try:
            result = summarize(path)
            self.assertEqual(result['confirmed_fill_volume']['CNR'], 500)
            self.assertFalse(result['completed'])
            self.assertEqual(len(result['errors']), 1)
        finally:
            from pathlib import Path
            Path(path).unlink()

    def test_markouts_skip_delayed_samples_and_learn_adverse_drift(self):
        b = self.bot(1)
        b.pending = [(0, 1, 'CNR', 'BUY', 100, 10.02, None, 'MAKE'),
                     (-10, 1, 'CNR', 'BUY', 100, 11, None, 'MAKE')]
        b.markouts('CNR', snapshot(mid=10))
        self.assertEqual(b.markout_count[('CNR', 'BUY')], 1)
        self.assertGreater(b.penalty('CNR', 'BUY'), 0)
        self.assertFalse(b.pending)
