"""v4 candidate sizing, execution-quality adaptation and end-of-case accounting."""
import contextlib
import io
import json
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import Mock

from config import Config
from engine import Bot as AccountingBot, Recorder
from execution import ApiError, HttpError, Order, risk_bounds
from policy import ladder, split_targets
from run_metrics import quality_summary
from runtime import Bot, BUILD
from tests.test_lifecycle import snapshot, row
from tests.test_runtime import RuntimeExchange


class ProfitabilityTests(unittest.TestCase):
    def ready(self, **kwargs):
        x = RuntimeExchange()
        log = io.StringIO()
        b = Bot(x, Config(**kwargs), Recorder(log), clock=lambda: x.now)
        with contextlib.redirect_stdout(io.StringIO()):
            b.start()
            for _ in range(5):
                b.step()
                x.now += .2
        return x, b, log

    def test_default_allocation_and_continuous_independent_replenishment(self):
        x, b, _ = self.ready(skew=0)
        self.assertEqual(BUILD, 'continuous-v4')
        self.assertEqual(len(b.working()), 28)
        for side in ('BUY', 'SELL'):
            self.assertEqual([sum(o.remaining for o in b.working()
                                  if b.slots[o.id][:2] == (side, i)) for i in range(4)],
                             [6500, 4000, 1500, 500])
        opposite = {o.id for o in b.working() if o.action == 'SELL'}
        for _ in range(3):
            child = next(o for o in b.working() if b.slots[o.id] == ('BUY', 0, 0))
            x.fill(x.orders[child.id], child.remaining, child.price, True)
            with contextlib.redirect_stdout(io.StringIO()): b.step()
            self.assertNotIn(child.id, [o.id for o in b.working()])
            self.assertTrue(any(b.slots[o.id] == ('BUY', 0, 0) for o in b.working()))
            self.assertEqual(opposite, {o.id for o in b.working() if o.action == 'SELL'})
            self.assertTrue(all(o.quantity <= 1000 for o in b.working()))
            _, _, gross, net = risk_bounds(b.ledger.positions, b.working())
            self.assertLessEqual(max(gross, net), 25000)
        self.assertEqual(b.ledger.volume, 3000)
        self.assertFalse(b.stopping)

    def test_current_defaults_survive_missing_history_without_restart(self):
        x, b, _ = self.ready()
        child = b.working()[0]
        x.fill(x.orders[child.id], child.quantity, child.price, True)
        x.hide_history = True
        with contextlib.redirect_stdout(io.StringIO()):
            b.step()
            x.now += 1.1
            b.step()
            self.assertTrue(b.recovering)
            for _ in range(12):
                x.now += .25
                b.step()
        self.assertFalse(b.recovering)
        self.assertEqual(b.ledger.positions, x.positions)
        self.assertTrue(b.working())
        self.assertFalse(b.stopping)

    def test_profiles_isolate_one_change_and_match_default(self):
        root = Path(__file__).resolve().parents[1]/'profiles'
        names = ['baseline-v3', 'a-children', 'b-skew', 'c-allocation', 'd-markouts', 'e-queue']
        configs = [Config.load(root/(name+'.json')) for name in names]
        expected = ['child_order_size', 'skew', 'sizes', 'markout_penalty_mode', 'reprice_delay']
        for old, new, field in zip(configs, configs[1:], expected):
            self.assertEqual([k for k in asdict(old) if getattr(old,k) != getattr(new,k)], [field])
        self.assertEqual(json.loads(json.dumps(asdict(configs[-1]))),
                         json.loads(json.dumps(asdict(Config()))))
        for kw in ({'child_order_size':5001}, {'sizes':(26000,4000,1500,500)},
                   {'markout_penalty_mode':'invalid'}, {'markout_min_shares':0}):
            with self.assertRaises(ValueError): replace(Config(), **kw).validate()
        plan = split_targets(ladder(snapshot(), 0, Config()), 1000)
        self.assertEqual(sum(q for (s,*_), (p,q) in plan.items() if s=='BUY'),12500)

    def mark(self, price=9.9, ref=10.1, quantity=5000, **kwargs):
        log = io.StringIO()
        b = AccountingBot(Mock(), Config(**kwargs), Recorder(log), clock=lambda:1)
        b.pending = [(0,1.,'RY','BUY',quantity,ref,price,'MAKE',42,0)]
        b.markouts('RY', snapshot(mid=10))
        return b, json.loads(log.getvalue())

    def test_negative_midpoint_drift_does_not_penalize_profitable_fill(self):
        b, event = self.mark()
        self.assertAlmostEqual(event['post_detection_drift'], -.1)
        self.assertAlmostEqual(event['gross_markout'], .1)
        self.assertAlmostEqual(event['maker_adjusted_markout'], .098)
        self.assertEqual((event['order_id'],event['level'],event['fee_assumption']),(42,0,'passive'))
        self.assertEqual(b.penalty('RY','BUY'),0)

    def test_adverse_execution_penalty_requires_sample_volume_and_remains_capped(self):
        b, _ = self.mark(price=10.1, quantity=4000)
        self.assertEqual(b.penalty('RY','BUY'),0)
        b.pending = [(0,1.,'RY','BUY',1000,10,10.1,'MAKE',43,0)]
        b.markouts('RY',snapshot(mid=10))
        self.assertGreater(b.penalty('RY','BUY'),0)
        self.assertLessEqual(b.penalty('RY','BUY'),.03)

    def test_equal_share_volume_has_equal_learning_weight(self):
        b, _ = self.mark(price=10.1, quantity=1000, markout_min_shares=1)
        c, _ = self.mark(price=10.1, quantity=500, markout_min_shares=1)
        c.pending = [(0,1.,'RY','BUY',500,10.1,10.1,'MAKE',43,0)]
        c.markouts('RY',snapshot(mid=10))
        self.assertAlmostEqual(b.penalty('RY','BUY'),c.penalty('RY','BUY'))

    def test_unknown_execution_price_is_not_invented(self):
        b, event = self.mark(price=None)
        self.assertIsNone(event['gross_markout'])
        self.assertEqual(b.markout_shares[('RY','BUY')],0)
        self.assertEqual(b.penalty('RY','BUY'),0)
        old, _ = self.mark(price=None,markout_penalty_mode='drift')
        self.assertGreater(old.penalty('RY','BUY'),0)

    def test_known_execution_schedules_markouts_without_fresh_pre_fill_book(self):
        b = AccountingBot(Mock(), clock=lambda:10)
        b.ledger.orders[1] = Order(1,'RY','BUY',1000,10,0)
        b.slots[1] = ('BUY',0,0)
        b.intents[1] = 'MAKE'
        b.update(row(ticker='RY', filled=1000, status='TRANSACTED',vwap=9.99))
        self.assertEqual(len(b.pending),3)
        self.assertTrue(all(e[5] is None and e[6]==9.99 and e[8:]==(1,0) for e in b.pending))

    def test_queue_delay_applies_only_to_ordinary_recentering(self):
        x = RuntimeExchange(); b = Bot(x)
        o = Order(1,'RY','BUY',1000,9.9,0)
        b.ledger.orders[1]=o; b.slots[1]=('BUY',0,0)
        b.clock=lambda:x.now
        targets={('BUY',0,0):(9.95,1000)}
        x.now=.9
        self.assertFalse(b.cancels_needed(snapshot(),targets))
        x.now=1.3
        self.assertEqual(b.cancels_needed(snapshot(),targets),[o])
        self.assertIn('recenter',b.cancel_causes[1])
        x.now=.1
        self.assertEqual(b.cancels_needed(snapshot(bid=9.7,ask=9.8,fair=9.75),targets),[o])
        self.assertIn('crosses_external_book',b.cancel_causes[1])

    def test_boundary_401_requires_verified_same_case_end(self):
        x,b,_=self.ready();err=HttpError(401,'orders/1')
        self.assertFalse(b.finish_if_case_ended(err))
        x.now=300
        self.assertTrue(b.finish_if_case_ended(err))
        b.period=2
        self.assertFalse(b.finish_if_case_ended(err))
        b.reads=Mock(side_effect=err)
        self.assertFalse(b.finish_if_case_ended(err))
        self.assertFalse(b.finish_if_case_ended(ApiError('other')))

    def test_cleanup_retains_exchange_positions_when_history_is_unavailable(self):
        x,b,log=self.ready()
        child=b.working()[0]
        x.fill(x.orders[child.id],child.quantity,child.price,True)
        b.reconcile_frame=Mock(side_effect=HttpError(401,'orders/1'))
        with contextlib.redirect_stdout(io.StringIO()): b.cleanup()
        end=json.loads(log.getvalue().splitlines()[-1])
        self.assertEqual(end['event'],'end')
        self.assertEqual(end['positions'],x.positions)
        self.assertEqual(end['open_orders'],0)
        self.assertFalse(end['accounting_confirmed'])
        self.assertFalse(end['cleanup_confirmed'])
        self.assertEqual(len(end['cleanup_errors']),1)

    def test_analyzer_reports_weighted_execution_quality_and_coverage(self):
        events=[dict(event='start',config={'stock_budget':25000}),
                dict(event='ladder_order',order_id=1,side='BUY',level=0),
                dict(event='submit',order_id=1,quantity=2000,action='BUY',price=9.9,wall_time=0,distance_from_mid=.1),
                dict(event='fill',order_id=1,quantity=2000,ticker='RY',action='BUY',intent='MAKE'),
                dict(event='markout',order_id=1,quantity=1000,ticker='RY',action='BUY',intent='MAKE',horizon=1.,
                     gross_markout=.1,maker_adjusted_markout=.098),
                dict(event='runtime_cycle',wall_time=0,position=20000),
                dict(event='runtime_cycle',wall_time=2,position=0)]
        result=quality_summary(events)
        m=result['execution_price_markouts']['BUY level 0 1.0s']
        self.assertEqual(m['fill_volume_coverage'],.5)
        self.assertAlmostEqual(m['maker_assumption_adjusted_per_share'],.098)
        self.assertEqual(result['sampled_inventory_time']['thresholds']['20000']['seconds'],2)
        del events[4]['order_id']
        legacy=quality_summary(events)
        self.assertNotIn('BUY level 0 1.0s',legacy['execution_price_markouts'])
        self.assertEqual(legacy['markouts_without_order_id'],1)
