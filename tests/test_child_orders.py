import contextlib
import io
import unittest
from dataclasses import replace

from config import Config
from engine import Bot as SerialBot
from fast_engine import Bot
from policy import split_targets
from execution import risk_bounds
from tests.test_fast_execution import BatchExchange


class ChildOrderTests(unittest.TestCase):
    def ready(self, backend=Bot):
        x = BatchExchange()
        b = backend(x, Config(sizes=(5000,4000,2500,1000),child_order_size=500,skew=0), clock=lambda: x.now)
        with contextlib.redirect_stdout(io.StringIO()):
            b.start()
            for _ in range(7):
                before = len(x.orders)
                b.step()
                self.assertLessEqual(len(x.orders)-before, 8)
                x.now += .01
        return x, b

    def test_fifty_resting_orders_preserve_total_quantity_in_both_engines(self):
        for backend in (Bot, SerialBot):
            with self.subTest(backend=backend.__module__):
                x, b = self.ready(backend)
                self.assertEqual(len(b.ledger.active()), 50)
                self.assertTrue(all(o.remaining == 500 for o in b.ledger.active()))
                for side in ('BUY', 'SELL'):
                    levels = [sum(o.remaining for o in b.ledger.active()
                                  if b.slots[o.id][:2] == (side, level)) for level in range(4)]
                    self.assertEqual(levels, [5000, 4000, 2500, 1000])
                _, _, gross, net = risk_bounds(b.ledger.positions, b.ledger.active())
                self.assertEqual((gross, net), (12500, 12500))
                before = set(x.orders)
                b.step()
                self.assertEqual(set(x.orders), before)
                self.assertEqual(x.cancels, 0)

    def test_first_batch_balances_sides_at_nearest_level(self):
        x = BatchExchange()
        b = Bot(x, clock=lambda: x.now)
        with contextlib.redirect_stdout(io.StringIO()):
            b.start(); b.step()
        self.assertEqual([o.action for o in b.ledger.active()], ['BUY', 'SELL']*4)
        self.assertTrue(all(b.slots[o.id][1] == 0 for o in b.ledger.active()))

    def test_filled_child_replenishes_without_cancelling_siblings(self):
        for backend in (Bot, SerialBot):
            x, b = self.ready(backend)
            for side in ('BUY', 'SELL'):
                before = {o.id for o in b.ledger.active()}
                child = next(o for o in b.ledger.active() if b.slots[o.id] == (side, 0, 0))
                x.fill(x.orders[child.id], 500, child.price, True)
                b.step()
                after = {o.id for o in b.ledger.active()}
                self.assertEqual(before-after, {child.id})
                self.assertEqual(len(after-before), 1)
                self.assertEqual(len(after), 50)
                self.assertEqual(x.cancels, 0)

    def test_partial_child_preserves_queue_and_reservation(self):
        x, b = self.ready()
        child = next(iter(b.ledger.active()))
        x.fill(x.orders[child.id], 200, child.price, True)
        before = set(x.orders)
        b.step()
        self.assertEqual(set(x.orders), before)
        self.assertEqual(child.remaining, 300)
        self.assertEqual(x.cancels, 0)

    def test_one_sided_fills_never_exceed_hard_capacity(self):
        x, b = self.ready()
        with contextlib.redirect_stdout(io.StringIO()):
            for _ in range(35):
                for o in list(x.orders.values()):
                    if o['action'] == 'BUY' and o['status'] == 'OPEN':
                        x.fill(o, o['quantity']-o['quantity_filled'], o['price'], True)
                x.now += .01
                b.step()
                _, _, gross, net = risk_bounds(b.ledger.positions, b.ledger.active())
                self.assertLessEqual(gross, 25000)
                self.assertLessEqual(net, 25000)
        self.assertEqual(b.ledger.positions['RY'], 25000)
        self.assertFalse([o for o in b.ledger.active() if o.action == 'BUY'])
        self.assertTrue([o for o in b.ledger.active() if o.action == 'SELL'])

    def test_split_keeps_exact_remainder_and_unsplit_exit(self):
        targets = split_targets({('BUY', 0):(10, 1150), ('SELL', -1):(11, 5000)}, 500)
        self.assertEqual([q for (p,q) in targets.values()], [500, 500, 150, 5000])
        for kw in ({'child_order_size':0}, {'child_order_size':True}, {'child_order_size':5001},
                   {'max_submit_batch':0}, {'max_submit_batch':17},
                   {'child_order_size':250, 'sizes':(5000,5000,5000,5000)}):
            with self.assertRaises(ValueError): replace(Config(), **kw).validate()

    def test_cancel_races_reconcile_all_fifty_children(self):
        x, b = self.ready()
        real = x.batch
        def racing_batch(calls, **kw):
            if calls and calls[0][0] == 'DELETE':
                for o in list(x.orders.values()):
                    if o['status'] == 'OPEN':
                        x.fill(o, 100, o['price'], True)
            return real(calls, **kw)
        x.batch = racing_batch
        b.cancel_all()
        self.assertEqual(b.ledger.volume, 5000)
        self.assertEqual(b.ledger.positions, x.positions)
        self.assertFalse(b.ledger.active())

    def test_no_order_diagnostics_distinguish_expiry_and_inactive_case(self):
        from concurrent_client import Batch, ExpiredQuote
        from policy import ladder
        from tests.test_lifecycle import snapshot
        x = BatchExchange()
        b = Bot(x, clock=lambda: x.now)
        targets = split_targets(ladder(snapshot(), 0, Config()), 500)
        x.batch = lambda calls, **kw: Batch([ExpiredQuote('queued') for _ in calls])
        b.place_targets('RY', targets, 0, x.now, 0)
        self.assertEqual(b.activity['reason'], 'queued_quotes_expired')
        self.assertEqual(b.activity['expired'], 8)
        self.assertEqual(b.activity['open_orders'], 0)
        x.now = 2
        b.place_targets('RY', targets, 0, 0, 0)
        self.assertEqual(b.activity['reason'], 'decision_expired')
        x = BatchExchange(); b = Bot(x, clock=lambda: x.now)
        with contextlib.redirect_stdout(io.StringIO()):
            b.start(); x.now = 300
            self.assertFalse(b.step())
        self.assertEqual(b.activity['reason'], 'case_inactive')
