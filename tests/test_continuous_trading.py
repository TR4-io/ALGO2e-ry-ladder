import contextlib
import io
import unittest
from dataclasses import replace
from config import Config
from policy import ladder, phase
from fast_engine import Bot
from execution import Order, risk_bounds
from tests.test_fast_execution import BatchExchange
from tests.test_lifecycle import snapshot

class ContinuousTradingTests(unittest.TestCase):
    def test_default_quotes_until_case_end_without_phase_wind_down(self):
        cfg=Config()
        self.assertEqual((cfg.stock_budget,cfg.gross_budget,cfg.net_budget),(25000,25000,25000))
        self.assertEqual(cfg.entry_cutoff,300)
        for tick in (50,260,275,280,299.9):
            self.assertEqual(phase(cfg,tick,200000,False),'MAKE')
        self.assertEqual(phase(cfg,299,200000,True),'EXIT')

    def test_age_and_old_inventory_brake_do_not_reduce_size(self):
        cfg=Config();snap=snapshot(bid=99.99,ask=100.01,mid=100,fair=100)
        for pos in (15000,-15000):
            plan=ladder(snap,pos,cfg,age=200)
            adding='BUY' if pos>0 else 'SELL'; reducing='SELL' if pos>0 else 'BUY'
            self.assertEqual(sum(q for (s,i),(p,q) in plan.items() if s==adding),10000)
            self.assertEqual(sum(q for (s,i),(p,q) in plan.items() if s==reducing),12500)
            self.assertTrue(all(q <= cfg.sizes[i] for (side,i),(p,q) in plan.items()))
            self.assertFalse(any(i==-1 for s,i in plan))
            orders=[Order(i,'RY',s,q,p,0) for i,((s,_),(p,q)) in enumerate(plan.items())]
            low,high,gross,net=risk_bounds({'CNR':0,'RY':pos,'AC':0},orders)
            self.assertLessEqual(gross,25000);self.assertLessEqual(net,25000)

    def test_hard_limit_blocks_only_inventory_increasing_side(self):
        for pos in (25000,-25000):
            plan=ladder(snapshot(),pos,Config(),age=100)
            reducing='SELL' if pos>0 else 'BUY'
            self.assertTrue(plan)
            self.assertTrue(all(s==reducing for s,i in plan))

    def test_small_inventory_does_not_reduce_opposite_side_clips(self):
        plan=ladder(snapshot(),500,Config(),age=100)
        self.assertEqual(sum(q for (s,i),(p,q) in plan.items() if s=='SELL'),12500)
        self.assertTrue(all(q == Config().sizes[i] for (side,i),(p,q) in plan.items()))

    def test_active_299_places_orders_300_cancels_without_new_entries(self):
        x=BatchExchange();x.now=299;b=Bot(x,clock=lambda:x.now)
        with contextlib.redirect_stdout(io.StringIO()):
            b.start();self.assertTrue(b.step())
            self.assertEqual(len(x.orders),8)
            x.now=300;self.assertFalse(b.step())
        self.assertEqual(len(x.orders),8)
        self.assertFalse(b.ledger.active())

    def test_case_close_can_leave_inventory_and_does_not_submit_liquidation(self):
        x=BatchExchange();x.now=299;b=Bot(x,clock=lambda:x.now)
        with contextlib.redirect_stdout(io.StringIO()):
            b.start();b.step()
            buy=next(o for o in x.orders.values() if o['action']=='BUY')
            x.fill(buy,5000,buy['price'],True)
            x.now=300;b.step();b.cleanup()
        self.assertEqual(x.positions['RY'],5000)
        self.assertEqual(b.ledger.positions['RY'],5000)
        self.assertEqual(len(x.orders),8)

    def test_optional_old_controls_and_validation(self):
        cfg=Config(scheduled_closeout=True,inventory_throttle=True)
        self.assertEqual(cfg.entry_cutoff,275)
        self.assertEqual(phase(cfg,280,0,False),'EXIT')
        for name in ('scheduled_closeout','inventory_throttle'):
            with self.assertRaises(ValueError):replace(cfg,**{name:'false'}).validate()
        with self.assertRaises(ValueError):replace(cfg,stock_budget=25001).validate()

if __name__=='__main__':unittest.main()
