"""Execution recovery against delayed/missing history; never contacts RIT."""
import contextlib
import io
import json
import unittest

from execution import ApiError, UnknownWrite, risk_bounds
from tests.test_runtime import setup


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()

    def tearDown(self):
        self.output.__exit__(None, None, None)

    def recover(self, x, b):
        for _ in range(20):
            b.step()
            low, high, gross, net = risk_bounds(b.ledger.positions, b.working())
            self.assertLessEqual(max(gross, net), 25000)
            self.assertTrue(all(low[t] <= x.positions[t] <= high[t] for t in low))
            x.now += .25
            if not b.recovering and b.unresolved_since is None:
                return
        self.fail('Recovery did not complete')

    def test_missing_history_recovers_nonzero_inventory_and_late_fills_once(self):
        x, b, log = setup(skew=0)
        b.step()
        buy = next(o for o in x.orders.values() if o['action'] == 'BUY')
        x.fill(buy, buy['quantity'], buy['price'], True)
        x.hide_history = True
        before = len(x.orders)
        self.recover(x, b)
        self.assertEqual(b.ledger.positions['RY'], 500)
        self.assertEqual(b.ledger.volume, 0)  # Unknown historical fills are NOT invented.
        self.assertGreater(len(x.orders), before)
        self.assertIn(buy['order_id'], b.history_gaps)
        self.assertFalse(b.stopping)
        x.hide_history = False
        x.now += 1
        b.step()
        self.assertEqual(b.ledger.volume, 500)
        self.assertEqual(b.ledger.positions['RY'], 500)  # No double-counted position.
        self.assertFalse(b.history_gaps)
        b.step()
        self.assertEqual(b.ledger.volume, 500)
        events = [json.loads(line) for line in log.getvalue().splitlines()]
        self.assertTrue(any(e['event'] == 'recovery_complete' for e in events))
        self.assertEqual(sum(e['quantity'] for e in events if e['event'] == 'fill'), 500)

    def test_cancelled_history_missing_while_flat_does_not_require_restart(self):
        x, b, _ = setup(skew=0)
        b.step()
        for row in x.orders.values():
            row['status'] = 'CANCELLED'
        x.hide_history = True
        self.recover(x, b)
        self.assertEqual(b.ledger.positions, x.positions)
        self.assertEqual(b.ledger.volume, 0)
        self.assertGreater(len(x.orders), 8)

    def test_position_race_refreshes_orders_and_resumes_in_same_cycle(self):
        x, b, _ = setup(skew=0)
        b.step()
        call = x.call
        armed = [True]

        def race(method, path, **params):
            result = call(method, path, **params)
            if armed[0] and path == 'orders' and params.get('status') == 'OPEN':
                armed[0] = False
                buy = next(o for o in x.orders.values() if o['action'] == 'BUY')
                x.fill(buy, 100, buy['price'], True)
            return result

        x.call = race
        before = len(x.orders)
        b.step()
        self.assertEqual(b.ledger.positions, x.positions)
        self.assertGreater(len(x.orders), before)
        self.assertFalse(b.recovering)

    def test_history_detail_requests_rotate_past_missing_first_four(self):
        x, b, _ = setup(skew=0, reconcile_timeout=30)
        b.step()
        for row in x.orders.values():
            row['status'] = 'CANCELLED'
        x.hide_history = True
        x.requests.clear()
        b.step()
        b.step()
        ids = [int(path.split('/')[1]) for _, method, path, _ in x.requests
               if method == 'GET' and path.startswith('orders/')]
        self.assertEqual(set(ids), set(range(1, 9)))

    def test_recovery_waits_for_actual_cancel_and_stable_position(self):
        x, b, _ = setup(skew=0)
        b.step()
        first = next(iter(x.orders.values()))
        first['status'] = 'CANCELLED'
        x.hide_history = True
        call = x.call
        delay = [True]

        def slow_cancel(method, path, **params):
            if path == 'commands/cancel' and delay[0]:
                return {'cancelled_order_ids': []}
            return call(method, path, **params)

        x.call = slow_cancel
        before = len(x.orders)
        for _ in range(15):
            b.step()
            x.now += .25
        self.assertTrue(b.recovering)
        self.assertEqual(len(x.orders), before)
        delay[0] = False
        self.recover(x, b)
        self.assertGreater(len(x.orders), before)

    def test_unknown_cancel_is_retried_without_replaying_submissions(self):
        x, b, _ = setup(skew=0)
        b.step()
        next(iter(x.orders.values()))['status'] = 'CANCELLED'
        x.hide_history = True
        call = x.call
        lost = [True]

        def lose_ack(method, path, **params):
            result = call(method, path, **params)
            if path == 'commands/cancel' and lost[0]:
                lost[0] = False
                raise UnknownWrite('Cancellation response lost')
            return result

        x.call = lose_ack
        self.recover(x, b)
        self.assertFalse(b.write_uncertain)
        self.assertGreater(len(x.orders), 8)

    def test_manual_exit_can_recover_and_liquidate_missing_fill_history(self):
        x, b, _ = setup(skew=0)
        b.step()
        buy = next(o for o in x.orders.values() if o['action'] == 'BUY')
        x.fill(buy, 500, buy['price'], True)
        x.hide_history = True
        b.stopping = True
        running = True
        for _ in range(30):
            running = b.step()
            x.now += .25
            if not running:
                break
        self.assertFalse(running)
        self.assertEqual(x.positions['RY'], 0)
        self.assertEqual(b.ledger.positions['RY'], 0)

    def test_risk_stops_and_pnl_size_throttle_are_removed(self):
        x, b, log = setup(skew=0, soft_loss=0, hard_loss=0, drawdown=0, profit_arm=0)
        for pnl in (1000, -10000, 20000, 0, -100000):
            x.cash['RY'] = pnl
            self.assertTrue(b.step())
            x.now += .25
            self.assertFalse(b.stopping)
            self.assertFalse(b.cautious)
            self.assertEqual(b.mode, 'MAKE')
        self.assertTrue(b.working())
        self.assertNotIn('"event": "risk_stop"', log.getvalue())

    def test_fills_racing_recovery_cancel_and_new_fills_preserve_inventory(self):
        x, b, _ = setup(skew=0)
        b.step()
        buy = next(o for o in x.orders.values() if o['action'] == 'BUY')
        x.fill(buy, 100, buy['price'], True)
        b.step()  # Account for the first partial fill.
        first_known = b.ledger.volume
        x.orders[buy['order_id']]['status'] = 'CANCELLED'
        x.hide_history = True
        call = x.call
        raced = [False]

        def cancel_race(method, path, **params):
            if path == 'commands/cancel' and not raced[0]:
                raced[0] = True
                other = next(o for o in x.orders.values() if o['action'] == 'BUY' and o['status'] == 'OPEN')
                x.fill(other, 250, other['price'], True)
            return call(method, path, **params)

        x.call = cancel_race
        self.recover(x, b)
        self.assertEqual(b.ledger.positions['RY'], 350)
        self.assertEqual(b.ledger.volume, first_known)
        # New fills may happen before old history becomes available.
        fresh = next(o for o in b.working() if o.action == 'BUY')
        x.fill(x.orders[fresh.id], 100, fresh.price, True)
        b.step()
        x.hide_history = False
        x.now += 1
        b.step()
        self.assertEqual(b.ledger.positions['RY'], 450)
        self.assertEqual(b.ledger.volume, 450)
        self.assertFalse(b.history_gaps)

    def test_recovery_at_share_ceiling_only_posts_inventory_reducing_orders(self):
        x, b, _ = setup(skew=0)
        # Reach the ceiling with confirmed partial/full replenishments first.
        for _ in range(30):
            b.step()
            for o in list(x.orders.values()):
                if o['action'] == 'BUY' and o['status'] == 'OPEN':
                    x.fill(o, o['quantity']-o['quantity_filled'], o['price'], True)
            x.now += .25
        b.step()
        self.assertEqual(x.positions['RY'], 25000)
        x.hide_history = True
        for o in x.orders.values():
            if o['status'] == 'OPEN':
                o['status'] = 'CANCELLED'
        self.recover(x, b)
        self.assertTrue(b.working())
        self.assertTrue(all(o.action == 'SELL' for o in b.working()))
        self.assertEqual(b.ledger.positions['RY'], 25000)

    def test_case_end_during_recovery_does_not_rebuild_ladder(self):
        x, b, _ = setup()
        b.step()
        for o in x.orders.values():
            o['status'] = 'CANCELLED'
        x.hide_history = True
        b.step()
        x.now = 299
        b.step()
        self.assertTrue(b.recovering)
        before = len(x.orders)
        x.now = 300
        self.assertFalse(b.step())
        self.assertEqual(len(x.orders), before)

    def test_round_reset_during_recovery_cannot_authorize_orders(self):
        x, b, _ = setup()
        b.step()
        x.hide_history = True
        next(iter(x.orders.values()))['status'] = 'CANCELLED'
        b.step()
        x.now = 2
        call = x.call
        reads = [0]

        def reset(method, path, **params):
            result = call(method, path, **params)
            if path == 'case':
                reads[0] += 1
                if reads[0] >= 2:
                    result = dict(result, period=2, tick=0)
            return result

        x.call = reset
        before = len(x.orders)
        with self.assertRaises(ApiError):
            b.step()
        self.assertTrue(b.round_changed)
        self.assertEqual(len(x.orders), before)

    def test_foreign_order_is_not_cancelled_or_adopted(self):
        x, b, _ = setup()
        b.step()
        x.call('POST', 'orders', ticker='RY', action='BUY', quantity=500, type='LIMIT', price=9)
        x.requests.clear()
        with self.assertRaises(ApiError):
            b.step()
        self.assertFalse(any(method in ('DELETE', 'POST') for _, method, _, _ in x.requests))


if __name__ == '__main__':
    unittest.main()
