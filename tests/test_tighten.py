import io
import json
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch
from config import Config
from engine import Bot, Recorder
from execution import ApiError, Order
from policy import ladder
from tests.exchange import Exchange
from tests.test_lifecycle import snapshot, row

class TightenTests(unittest.TestCase):
    def bot(self):
        b=Bot(Mock(),Config(sizes=(5000,4000,2500,1000),child_order_size=5000),clock=lambda:1)
        b.tick=0
        b.check_fill_activity()
        return b

    def test_not_before_tick_50_and_switch_once(self):
        b=self.bot();b.record=Recorder(io.StringIO())
        b.tick=49.9;b.check_fill_activity();self.assertFalse(b.tight_ladder)
        b.tick=50;b.check_fill_activity();self.assertTrue(b.tight_ladder)
        b.tick=70;b.check_fill_activity()
        events=[json.loads(s) for s in b.record.stream.getvalue().splitlines()]
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]['offsets'],[.02,.03,.05,.08])

    def test_counts_confirmed_deltas_only_and_threshold_is_exclusive(self):
        b=self.bot();b.tick=40
        b.ledger.orders[1]=Order(1,'RY','BUY',1000,10,0)
        b.update(row(ticker='RY',filled=500));b.update(row(ticker='RY',filled=500))
        b.update(row(ticker='RY',filled=1000,status='TRANSACTED'))
        b.tick=50;b.check_fill_activity()
        self.assertFalse(b.tight_ladder)
        self.assertEqual(sum(q for t,q in b.recent_ry_fills),1000)
        b.tick=60;b.check_fill_activity();self.assertTrue(b.tight_ladder)

    def test_non_ry_volume_does_not_mask_idle_ry(self):
        b=self.bot();b.tick=40;b.ledger.orders[1]=Order(1,'CNR','BUY',1000,10,0)
        b.update(row(filled=1000,status='TRANSACTED'))
        b.tick=50;b.check_fill_activity();self.assertTrue(b.tight_ladder)

    def test_late_start_requires_full_window_and_does_not_tighten_during_exit(self):
        b=Bot(Mock());b.tick=80;b.check_fill_activity();self.assertFalse(b.tight_ladder)
        b.tick=99;b.check_fill_activity();self.assertFalse(b.tight_ladder)
        b.tick=100;b.mode='EXIT';b.check_fill_activity();self.assertFalse(b.tight_ladder)
        b.mode='MAKE';b.check_fill_activity();self.assertTrue(b.tight_ladder)

    def test_transition_cancels_all_old_quotes_before_replacing_even_small_moves(self):
        x=Exchange();b=Bot(x,Config(sizes=(5000,4000,2500,1000),child_order_size=5000),clock=lambda:x.now);b.tick=0;b.manage('RY')
        self.assertEqual(len(x.orders),8)
        x.now=50;b.tick=50;b.manage('RY')
        self.assertTrue(b.tight_ladder)
        self.assertEqual(len(x.orders),8)
        self.assertFalse(b.ledger.active())
        b.manage('RY')
        buys=sorted([o.price for o in b.ledger.active() if o.action=='BUY'],reverse=True)
        self.assertEqual(buys,[9.98,9.97,9.95,9.92])
        self.assertEqual(sum(o.remaining for o in b.ledger.active() if o.action=='BUY'),12500)

    def test_failed_cancellation_retains_transition_obligation(self):
        x=Exchange();b=Bot(x,Config(sizes=(5000,4000,2500,1000),child_order_size=5000),clock=lambda:x.now);b.tick=0;b.manage('RY')
        x.now=50;b.tick=50
        with patch.object(b,'cancel',side_effect=ApiError('pending')):
            with self.assertRaises(ApiError):b.manage('RY')
        self.assertTrue(b.requote_orders)
        self.assertEqual(len(x.orders),8)
        b.manage('RY');self.assertFalse(b.ledger.active());self.assertFalse(b.requote_orders)
        b.manage('RY');self.assertEqual(len(x.orders),16)

    def test_tight_ladder_keeps_inventory_brake_and_passive_pricing(self):
        cfg=Config(inventory_throttle=True);snap=snapshot()
        targets=ladder(snap,15000,cfg,offsets=cfg.tight_offsets)
        self.assertEqual(targets,{('SELL',-1):(10.02,5000)})
        wide=snapshot(bid=9.8,ask=10.2,mid=10,fair=10)
        targets=ladder(wide,0,cfg,offsets=cfg.tight_offsets)
        self.assertTrue(all(p<=wide.bid if s=='BUY' else p>=wide.ask for (s,i),(p,q) in targets.items()))

    def test_bad_settings_rejected(self):
        for kw in ({'tighten_after':300},{'fill_window':0},{'fill_window':60},
                   {'min_window_fills':True},{'min_window_fills':0},
                   {'tight_offsets':[.01]}, {'tight_offsets':[.04,.06,.10,.15]},
                   {'tight_offsets':[.02,.02,.07,.12]}, {'tight_offsets':[float('nan')]*4}):
            with self.subTest(kw=kw),self.assertRaises(ValueError):replace(Config(),**kw).validate()

if __name__=='__main__':unittest.main()
