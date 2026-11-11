import unittest
from dataclasses import replace
from unittest.mock import Mock
from config import Config
from engine import Bot
from execution import Order, capacity, risk_bounds, TICKERS
from policy import ladder, phase, split_targets
from tests.test_lifecycle import snapshot

class LadderTests(unittest.TestCase):
    def test_four_levels_per_side_and_fee_margin(self):
        plan = ladder(snapshot(bid=99.99, ask=100.01, mid=100, fair=100), 0, Config())
        self.assertEqual([plan[('BUY', i)] for i in range(4)],
                         [(99.98,6500),(99.96,4000),(99.92,1500),(99.85,500)])
        self.assertEqual([plan[('SELL', i)] for i in range(4)],
                         [(100.02,6500),(100.04,4000),(100.08,1500),(100.15,500)])

    def test_never_improves_wider_market_or_crosses(self):
        snap=snapshot(bid=99.8,ask=100.2,mid=100,fair=100)
        plan=ladder(snap,0,Config())
        self.assertTrue(all(p<=snap.bid if side=='BUY' else p>=snap.ask
                            for (side,_),(p,q) in plan.items()))
        self.assertEqual(len(plan),2)  # coalesce identical prices, no stacked duplicates

    def test_brake_cancels_adding_side_and_exit_cannot_reverse(self):
        for pos in (15000,-15000):
            plan=ladder(snapshot(),pos,Config(inventory_throttle=True))
            side='SELL' if pos>0 else 'BUY'
            self.assertEqual(set(plan),{(side,-1)})
            self.assertLessEqual(plan[(side,-1)][1],abs(pos))

    def test_partial_inventory_reducing_ladder_cannot_reverse(self):
        plan=ladder(snapshot(),700,Config(inventory_throttle=True))
        self.assertLessEqual(sum(q for (s,_),(p,q) in plan.items() if s=='SELL'),700)
        self.assertLess(sum(q for (s,_),(p,q) in plan.items() if s=='BUY'),12500)

    def test_age_and_caution_use_near_passive_exit(self):
        for kwargs in ({'age':9},{'cautious':True},{'mode':'REDUCE'}):
            plan=ladder(snapshot(),1200,Config(inventory_throttle=True),**kwargs)
            self.assertEqual(plan[('SELL',-1)],(10.02,1200))

    def test_adverse_trend_blocks_adds_but_allows_exit(self):
        self.assertFalse(any(s=='BUY' for s,i in ladder(snapshot(),0,Config(),blocked={'BUY'})))
        self.assertIn(('BUY',-1),ladder(snapshot(),-500,Config(),blocked={'BUY'}))

    def test_volatility_widens_quotes(self):
        a=ladder(snapshot(),0,Config()); b=ladder(snapshot(sigma=.1),0,Config())
        self.assertLess(b[('BUY',0)][0],a[('BUY',0)][0])
        self.assertGreater(b[('SELL',0)][0],a[('SELL',0)][0])

    def test_volume_target_does_not_halt_profit_opportunities(self):
        c=Config(scheduled_closeout=True)
        self.assertEqual(phase(c,260,200000,False),'MAKE')
        self.assertEqual(phase(c,275,0,False),'REDUCE')
        self.assertEqual(phase(c,280,0,False),'EXIT')
        self.assertEqual(phase(c,20,0,True),'EXIT')

    def test_bad_config(self):
        for kw in ({'offsets':[.03,.02]}, {'sizes':[7000]*4}, {'sizes':[True]*4},
                   {'offsets':[float('nan')]*4}, {'inventory_brake':25000},
                   {'recenter_move':0}, {'stock_budget':12000}):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                replace(Config(),**kw).validate()

    def test_full_side_sweep_fits_capacity_without_netting(self):
        positions=dict.fromkeys(TICKERS,0); orders=[]; cfg=Config()
        for slot,(price,q) in split_targets(ladder(snapshot(),0,cfg),cfg.child_order_size).items():
            side=slot[0]
            safe=capacity(positions,orders,'RY',side,q,cfg.gross_budget,cfg.net_budget,cfg.stock_budget)
            self.assertEqual(safe,q)
            orders.append(Order(len(orders),'RY',side,q,price,0))
        low,high,gross,net=risk_bounds(positions,orders)
        self.assertEqual((low['RY'],high['RY'],gross,net),(-12500,12500,12500,12500))

    def test_manage_only_ry_and_preserves_unchanged_queue(self):
        from tests.exchange import Exchange
        x=Exchange(); b=Bot(x,Config(sizes=(5000,4000,2500,1000),child_order_size=5000),clock=lambda:x.now)
        b.manage('CNR'); self.assertEqual(x.calls,0)
        b.manage('RY'); self.assertEqual(len(x.orders),8)
        b.manage('RY'); self.assertEqual(len(x.orders),8); self.assertEqual(x.cancels,0)

    def test_brake_cancels_existing_bids_before_new_orders(self):
        from tests.exchange import Exchange
        x=Exchange(); b=Bot(x,Config(inventory_throttle=True,child_order_size=5000),clock=lambda:x.now); b.manage('RY')
        buys=[o for o in x.orders.values() if o['action']=='BUY']
        for o in buys[:3]:
            x.fill(o,o['quantity'],o['price'],True)
        b.reconcile(); before=len(x.orders); b.manage('RY')
        self.assertFalse(any(o.action=='BUY' for o in b.ledger.active()))
        self.assertEqual(len(x.orders),before)

    def test_cancel_race_replans_before_submitting(self):
        b=Bot(Mock(),Config(sizes=(5000,4000,2500,1000),child_order_size=5000),clock=lambda:2)
        b.book=Mock(return_value=({'bids':[(10,1000)],'asks':[(10.02,1000)]},2))
        b.ledger.orders[1]=Order(1,'RY','BUY',1500,10.10,0)
        b.slots[1]=('BUY',0)
        def cancel(o,reason):
            b.update(dict(order_id=1,ticker='RY',action='BUY',quantity=1500,
                          quantity_filled=1500,status='TRANSACTED'))
        b.cancel=Mock(side_effect=cancel); b.submit=Mock()
        b.manage('RY'); b.submit.assert_not_called()
        self.assertEqual(b.ledger.positions['RY'],1500)

    def test_keeps_both_sides_after_downward_mid_move(self):
        b=Bot(Mock(),Config(sizes=(5000,4000,2500,1000),child_order_size=5000),clock=lambda:2)
        b.mid_history=[(.5,10.15)]
        b.book=Mock(return_value=({'bids':[(10,1000)],'asks':[(10.02,1000)]},2))
        b.submit=Mock(return_value=None)
        b.manage('RY')
        self.assertTrue(b.submit.call_args_list)
        self.assertEqual({c.args[1] for c in b.submit.call_args_list}, {'BUY', 'SELL'})

if __name__=='__main__': unittest.main()

class LifecycleIntegrationTests(unittest.TestCase):
    def test_reduce_phase_submits_passive_inventory_exit(self):
        from tests.exchange import Exchange
        x=Exchange();x.now=276;x.positions['RY']=500
        b=Bot(x,Config(sizes=(5000,4000,2500,1000),child_order_size=5000),clock=lambda:x.now);b.mode='REDUCE';b.tick=276;b.case_seen=276
        b.ledger.positions['RY']=500
        b.manage('RY')
        self.assertEqual(len(x.orders),1)
        order=next(iter(x.orders.values()))
        self.assertEqual((order['action'],order['quantity']),('SELL',500))

    def test_read_only_cli_does_not_write(self):
        import sys
        from unittest.mock import patch
        from tests.exchange import Exchange
        import strategy
        x=Exchange(); client=Mock();client.call=Mock(side_effect=x.call)
        with patch.object(sys,'argv',['strategy.py','--check']), patch('strategy.Client',return_value=client):
            self.assertEqual(strategy.main(),0)
        self.assertTrue(all(c.args[0]=='GET' for c in client.call.call_args_list))
        self.assertFalse(x.orders)

    def test_reducing_order_cannot_cross_inventory_on_synchronous_fill(self):
        b=Bot(Mock(),Config(sizes=(5000,4000,2500,1000),child_order_size=5000),clock=lambda:1);b.ledger.positions['RY']=500
        b.book=Mock(return_value=({'bids':[(10,1000)],'asks':[(10.02,1000)]},1))
        def submit(*args):
            b.ledger.positions['RY']=0
            return None
        b.submit=Mock(side_effect=submit)
        b.manage('RY')
        self.assertEqual(b.submit.call_count,1)

    def test_widening_keeps_outer_offset(self):
        cfg=Config();plan=ladder(snapshot(bid=99.99,ask=100.01,mid=100,fair=100,sigma=.2),0,cfg,
                                penalties={'BUY':.03,'SELL':.03})
        self.assertEqual(min(p for (side,i),(p,q) in plan.items() if side=='BUY'),99.85)
        self.assertEqual(max(p for (side,i),(p,q) in plan.items() if side=='SELL'),100.15)
