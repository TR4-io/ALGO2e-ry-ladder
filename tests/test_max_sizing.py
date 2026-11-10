import contextlib
import io
import itertools
import unittest
from config import Config
from execution import risk_bounds
from fast_engine import Bot
from tests.test_fast_execution import BatchExchange

class MaxSizingTests(unittest.TestCase):
    def test_all_independent_full_fill_combinations_with_max_clips(self):
        x=BatchExchange();b=Bot(x,clock=lambda:x.now)
        b.manage('RY')
        orders=list(b.ledger.active())
        self.assertEqual(len(orders),8)
        self.assertTrue(all(o.quantity==5000 for o in orders))
        for mask in itertools.product((0,1),repeat=8):
            pos=sum(o.quantity*fill*(1 if o.action=='BUY' else -1) for o,fill in zip(orders,mask))
            self.assertLessEqual(abs(pos),20000)

    def test_live_limits_clip_batch_even_with_max_defaults(self):
        class LowerLimits(BatchExchange):
            def call(self,method,path,**params):
                if path=='limits':return [dict(gross_limit=10000,net_limit=8000)]
                return super().call(method,path,**params)
        x=LowerLimits();b=Bot(x,clock=lambda:x.now)
        with contextlib.redirect_stdout(io.StringIO()):b.start();b.step()
        self.assertEqual((b.gross_limit,b.net_limit),(10000,8000))
        low,high,gross,net=risk_bounds(b.ledger.positions,b.ledger.active())
        self.assertLessEqual(gross,10000);self.assertLessEqual(net,8000)
        self.assertEqual((low['RY'],high['RY']),(-8000,8000))
        self.assertTrue(all(o.quantity<=5000 for o in b.ledger.active()))

    def test_full_sweep_then_adverse_move_liquidates_entire_position(self):
        x=BatchExchange();b=Bot(x,clock=lambda:x.now)
        with contextlib.redirect_stdout(io.StringIO()):
            b.start();b.step()
            for o in x.orders.values():
                if o['action']=='BUY':x.fill(o,o['quantity'],o['price'],True)
            self.assertEqual(x.positions['RY'],20000)
            x.mid['RY']=9.5;x.now=1
            for _ in range(30):
                running=b.step()
                self.assertTrue(b.stopping)
                self.assertLessEqual(abs(x.positions['RY']),25000)
                if not running:break
                x.now+=.25
            self.assertFalse(running)
            b.cleanup()
        self.assertFalse(any(x.positions.values()))
        self.assertFalse(b.ledger.active())
        self.assertTrue(all(o['quantity']<=5000 for o in x.orders.values()))

if __name__=='__main__':unittest.main()
