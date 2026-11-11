import contextlib
import io
import unittest

from config import Config
from engine import Bot as SerialBot
from fast_engine import Bot
from execution import risk_bounds
from tests.test_fast_execution import BatchExchange


class ReplenishmentTests(unittest.TestCase):
    def test_repeated_one_sided_fills_replenish_without_opposite_fill(self):
        for backend in (Bot, SerialBot):
            for side in ('BUY', 'SELL'):
                with self.subTest(backend=backend.__module__, side=side):
                    x = BatchExchange()
                    # Isolate replenishment from intentional price-recentering.
                    b = backend(x, Config(sizes=(5000,4000,2500,1000),skew=0,child_order_size=5000), clock=lambda: x.now)
                    with contextlib.redirect_stdout(io.StringIO()):
                        b.start()
                        b.step()
                        opposite_ids = {o.id for o in b.ledger.active() if o.action != side}
                        for count in (1, 2):
                            inner = next(o for o in b.ledger.active() if b.slots[o.id] == (side, 0))
                            x.fill(x.orders[inner.id], inner.remaining, inner.price, True)
                            x.now += .1
                            b.step()
                            replacement = next(o for o in b.ledger.active() if b.slots[o.id] == (side, 0))
                            self.assertNotEqual(inner.id, replacement.id)
                            self.assertEqual(replacement.remaining, 5000)
                            self.assertEqual({o.id for o in b.ledger.active() if o.action != side}, opposite_ids)
                            self.assertEqual(b.ledger.volume, count*5000)
                            low, high, gross, net = risk_bounds(b.ledger.positions, b.ledger.active())
                            self.assertLessEqual(gross, 25000)
                            self.assertLessEqual(net, 25000)

    def test_repeated_fill_replenishment_respects_capacity_until_limit(self):
        x = BatchExchange()
        b = Bot(x, Config(sizes=(5000,4000,2500,1000),skew=0,child_order_size=5000), clock=lambda: x.now)
        with contextlib.redirect_stdout(io.StringIO()):
            b.start()
            b.step()
            for _ in range(30):
                buys = [o for o in b.ledger.active() if o.action == 'BUY']
                if buys:
                    inner = min(buys, key=lambda o: b.slots[o.id][1])
                    x.fill(x.orders[inner.id], inner.remaining, inner.price, True)
                x.now += .1
                b.step()
                _, _, gross, net = risk_bounds(b.ledger.positions, b.ledger.active())
                self.assertLessEqual(gross, 25000)
                self.assertLessEqual(net, 25000)
        self.assertEqual(b.ledger.positions['RY'], 25000)
        self.assertFalse([o for o in b.ledger.active() if o.action == 'BUY'])
        self.assertTrue([o for o in b.ledger.active() if o.action == 'SELL'])

    def test_inventory_skew_preserves_nearest_opposite_quote_with_fee_margin(self):
        from policy import ladder
        from tests.test_lifecycle import snapshot
        from execution import MAKER_REBATE
        for pos in (15000, -15000):
            side = 'SELL' if pos > 0 else 'BUY'
            snap = snapshot(bid=99.99, ask=100.01, fair=100, mid=100)
            plan = ladder(snap, pos, Config())
            price, qty = plan[(side, 0)]
            self.assertEqual(qty, 6500)
            sign = 1 if side == 'BUY' else -1
            self.assertGreaterEqual(sign*(snap.fair-price)+MAKER_REBATE['RY'], Config().min_edge)
            self.assertEqual(price, 100.02 if side == 'SELL' else 99.98)
