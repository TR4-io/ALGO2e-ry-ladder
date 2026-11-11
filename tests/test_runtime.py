"""Tests exercise the new default runtime, including slow/non-atomic exchange I/O."""
import contextlib
import io
import json
import unittest
from dataclasses import replace
from unittest.mock import Mock

from concurrent_client import Batch, ExpiredQuote
from config import Config
from engine import Recorder
from execution import ApiError, HttpError, UnknownWrite, risk_bounds
from runtime import Bot
from tests.test_fast_execution import BatchExchange


class RuntimeExchange(BatchExchange):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.requests = []
        self.dispatch_delay = 0
        self.response_delay = 0
        self.hide_history = False
        self.delay_cancel = False
        self.pending_deletes = set()

    def call(self, method, path, _deadline=None, **params):
        self.requests.append((self.now, method, path, params))
        if path == 'orders' and method == 'GET' and params.get('status') != 'OPEN' and self.hide_history:
            return []
        if path.startswith('orders/'):
            oid = int(path.split('/')[1])
            if method == 'DELETE' and self.delay_cancel:
                self.pending_deletes.add(oid)
                return {'success': True}
            if method == 'GET' and self.hide_history and self.orders[oid]['status'] != 'OPEN':
                raise HttpError(404, path)
        return super().call(method, path, **params)

    def batch(self, calls, deadline=None, guard=None, capture_timing=False):
        results, times = [], []
        for method, path, params in calls:
            self.now += self.dispatch_delay
            times.append(self.now)
            if (deadline is not None and self.now >= deadline) or (guard and not guard()):
                results.append(ExpiredQuote('expired before dispatch'))
                continue
            try:
                results.append(self.call(method, path, **params))
            except Exception as exc:
                results.append(exc)
            self.now += self.response_delay
        return Batch(results, started=times if capture_timing else None)


def setup(**kwargs):
    x = RuntimeExchange()
    log = io.StringIO()
    b = Bot(x, Config(**kwargs), Recorder(log), clock=lambda: x.now)
    b.start()
    return x, b, log


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()

    def tearDown(self):
        self.output.__exit__(None, None, None)

    def stage(self, x, b, count=7):
        for _ in range(count):
            self.assertTrue(b.step())
            _, _, gross, net = risk_bounds(b.ledger.positions, b.working())
            self.assertLessEqual(max(gross, net), 25000)
            x.now += .2

    def test_fifty_orders_and_stable_queue_without_opposite_fill_requirement(self):
        x, b, _ = setup(skew=0)
        self.stage(x, b)
        self.assertEqual(len(b.working()), 50)
        self.assertEqual([sum(o.remaining for o in b.working() if o.action==s)
                          for s in ('BUY', 'SELL')], [12500, 12500])
        before = set(x.orders)
        b.step()
        self.assertEqual(set(x.orders), before)
        for _ in range(3):
            child = next(o for o in b.working() if b.slots[o.id] == ('BUY',0,0))
            x.fill(x.orders[child.id], child.remaining, child.price, True)
            b.step()
            replacement = next(o for o in b.working() if b.slots[o.id] == ('BUY',0,0))
            self.assertNotEqual(child.id, replacement.id)
        self.assertEqual(b.ledger.positions['RY'], 1500)
        self.assertEqual(x.cancels, 0)

    def test_cancellation_ack_does_not_release_slots_and_other_slots_continue(self):
        x, b, _ = setup(skew=0)
        b.step()  # eight near children; outer slots are still empty
        old = {o.id for o in b.working()}
        x.delay_cancel = True
        x.now = 2
        x.mid['RY'] = 10.04  # eligible repricing, below trend threshold
        b.step()
        self.assertEqual(set(b.pending_cancel), old)
        self.assertTrue(old.issubset({o.id for o in b.working()}))
        self.assertGreater(len(b.working()), 8)  # unrelated missing slots placed now
        deletes = sum(method=='DELETE' for _, method, _, _ in x.requests)
        self.assertEqual(deletes, 8)
        self.assertFalse(any(path.startswith('orders/') and method=='GET'
                             for _, method, path, _ in x.requests))
        # A fill races the pending cancellation. History confirms its exact size.
        for oid in old:
            x.fill(x.orders[oid], 100, x.orders[oid]['price'], True)
            x.orders[oid]['status'] = 'CANCELLED'
        b.step()
        self.assertEqual(b.ledger.volume, 800)
        self.assertFalse(old.intersection(b.pending_cancel))
        self.assertEqual(b.ledger.positions, x.positions)

    def test_fifty_terminal_orders_cost_two_history_reads_not_fifty_detail_polls(self):
        x, b, _ = setup(skew=0)
        self.stage(x, b)
        for o in x.orders.values():
            o['status'] = 'CANCELLED'
        x.requests.clear()
        b.step()
        history = [p['status'] for _, method, path, p in x.requests if path=='orders' and method=='GET']
        self.assertEqual(history, ['OPEN', 'TRANSACTED', 'CANCELLED'])
        self.assertFalse(any(path.startswith('orders/') for _, _, path, _ in x.requests))
        self.assertEqual(len(b.working()), 8)

    def test_missing_history_yields_cycle_then_recovers_without_sleep_or_restart(self):
        x, b, _ = setup(skew=0)
        b.step()
        child = b.working()[0]
        x.fill(x.orders[child.id], child.quantity, child.price, True)
        x.hide_history = True
        before = len(x.orders)
        b.step()
        self.assertEqual(b.activity['reason'], 'reconciliation_pending')
        self.assertEqual(len(x.orders), before)
        self.assertEqual(x.now, 0)  # no internal retry sleep
        x.hide_history = False
        b.step()
        self.assertEqual(b.ledger.volume, 500)
        self.assertGreater(len(x.orders), before)

    def test_permanently_missing_order_enters_automatic_recovery(self):
        x, b, _ = setup()
        b.step()
        o=b.working()[0];x.orders[o.id]['status']='CANCELLED';x.hide_history=True
        b.step();before=len(x.orders)
        x.now=6
        b.step()
        self.assertTrue(b.recovering)
        self.assertEqual(len(x.orders),before)

    def test_queue_wait_is_not_book_age_but_slow_http_response_is(self):
        x, b, _ = setup()
        x.dispatch_delay=.15  # three account reads + book already take .60s
        b.step()
        self.assertGreater(len(x.orders), 0)
        self.assertEqual(b.last_book_age, 0)
        self.assertGreater(b.expired, 0)  # later queued children still expire correctly
        x, b, _ = setup()
        x.response_delay=.7
        b.step()
        self.assertFalse(x.orders)
        self.assertEqual(b.activity['reason'], 'stale_or_invalid_book')

    def test_capacity_under_repeated_one_sided_sweeps(self):
        x, b, _ = setup(skew=0)
        for _ in range(45):
            b.step()
            for order in list(x.orders.values()):
                if order['status']=='OPEN' and order['action']=='BUY':
                    x.fill(order,order['quantity']-order['quantity_filled'],order['price'],True)
            _,_,gross,net=risk_bounds(b.ledger.positions,b.working())
            self.assertLessEqual(max(gross,net),25000)
            self.assertLessEqual(abs(x.positions['RY']),25000)
            x.now+=.2
        b.step()
        self.assertEqual(b.ledger.positions['RY'],25000)
        self.assertFalse([o for o in b.working() if o.action=='BUY'])
        self.assertTrue([o for o in b.working() if o.action=='SELL'])

    def test_unknown_submit_keeps_successful_siblings_then_stops(self):
        x,b,_=setup();real=x.batch
        def batch(calls,**kw):
            result=real(calls,**kw)
            if calls and calls[0][0]=='POST' and calls[0][1]=='orders':
                result.results[0]=UnknownWrite('accepted response lost')
            return result
        x.batch=batch
        with self.assertRaises(UnknownWrite):b.step()
        self.assertEqual(len(x.orders),8)
        self.assertEqual(len(b.ledger.orders),7)
        self.assertTrue(b.write_uncertain)

    def test_expired_submission_batch_is_reported_without_replay(self):
        x,b,_=setup();real=x.batch
        def batch(calls,**kw):
            if calls and calls[0][0]=='POST':
                return Batch([ExpiredQuote('late') for _ in calls])
            return real(calls,**kw)
        x.batch=batch;b.step()
        self.assertEqual(b.expired,8)
        self.assertFalse(x.orders)

    def test_reset_and_end_never_submit_new_orders(self):
        x,b,_=setup();b.period=2
        with self.assertRaises(ApiError):b.step()
        b.cleanup();self.assertFalse(x.orders)
        x,b,_=setup();self.stage(x,b);count=len(x.orders);x.now=300
        self.assertFalse(b.step());b.cleanup()
        self.assertEqual(len(x.orders),count)
        self.assertFalse(b.working())

    def test_manual_stop_liquidates_after_bounded_cancel_batches(self):
        x,b,_=setup();self.stage(x,b)
        for o in list(x.orders.values()):
            if o['action']=='BUY':x.fill(o,o['quantity'],o['price'],True)
        x.mid['RY']=9.5
        b.stopping = True
        for _ in range(35):
            running=b.step();x.now+=.2
            if not running:break
        self.assertTrue(b.stopping)
        self.assertFalse(running)
        self.assertFalse(b.working())
        self.assertEqual(b.ledger.positions['RY'],0)

    def test_telemetry_reports_actual_submissions_and_idle_reason(self):
        x,b,log=setup(skew=0);self.stage(x,b);x.now=3;b.step()
        events=[json.loads(line) for line in log.getvalue().splitlines()]
        cycles=[e for e in events if e['event']=='runtime_cycle']
        self.assertTrue(cycles)
        self.assertEqual(cycles[-1]['reason'],'quotes_resting')
        self.assertEqual(cycles[-1]['open_orders'],50)
        self.assertEqual(cycles[-1]['submissions'],50)
        self.assertEqual(cycles[-1]['volume'],0)

    def test_shorter_live_limits_and_config_validation(self):
        x,b,_=setup();b.gross_limit=5000;b.net_limit=3000;self.stage(x,b)
        _,_,gross,net=risk_bounds(b.ledger.positions,b.working())
        self.assertLessEqual(max(gross,net),3000)
        for kw in ({'max_cancel_batch':0},{'max_cancel_batch':17}, {'reconcile_timeout':0},
                   {'cancel_retry_seconds':0},{'telemetry_seconds':0}):
            with self.assertRaises(ValueError):replace(Config(),**kw).validate()

    def test_complete_rounds_use_new_runtime_and_reconcile_every_fill(self):
        from tests.test_round import exercise
        for regime in ('oscillation','crowded','trend','jump','one_sided'):
            with self.subTest(regime=regime):
                result=exercise(11,regime,runtime=True)
                self.assertEqual(result['execution_backend'],'continuous-v3')
                self.assertGreater(result['volume'],0)
                self.assertLessEqual(result['max_inventory'],25000)

    def test_uncertain_cancel_can_be_confirmed_without_poisoning_submissions(self):
        x,b,_=setup(skew=0);b.step();real=x.batch
        def batch(calls,**kw):
            result=real(calls,**kw)
            if calls and calls[0][0]=='DELETE':
                result.results=[UnknownWrite('cancel ack lost') for _ in calls]
            return result
        x.batch=batch;x.mid['RY']=10.04;x.now=2
        b.step()
        self.assertTrue(b.pending_cancel)
        b.step()
        self.assertFalse(b.write_uncertain)
        self.assertFalse(b.pending_cancel)
        self.assertGreater(b.submissions,8)

    def test_known_rejection_accounts_for_siblings_then_replans_fresh(self):
        x,b,_=setup();real=x.call;rejected=False
        def call(method,path,**params):
            nonlocal rejected
            if method=='POST' and path=='orders' and not rejected:
                rejected=True
                raise HttpError(400,path)
            return real(method,path,**params)
        x.call=call
        with self.assertRaises(HttpError):b.step()
        self.assertEqual(len(b.ledger.orders),7)
        self.assertEqual(len(x.orders),7)
        self.assertFalse(b.write_uncertain)
        b.step()
        self.assertEqual(len(b.working()),15)

    def test_interrupted_submit_accounts_for_accepted_siblings_before_exit(self):
        x,b,_=setup();real=x.batch
        def batch(calls,**kw):
            result=real(calls,**kw)
            if calls and calls[0][0]=='POST' and calls[0][1]=='orders':result.interrupted=True
            return result
        x.batch=batch
        with self.assertRaises(KeyboardInterrupt):b.step()
        self.assertEqual(len(b.ledger.orders),8)
        self.assertEqual(len(x.orders),8)
        b.cleanup()
        self.assertFalse(b.working())

    def test_old_snapshot_does_not_reverse_fill_or_resurrect_terminal_order(self):
        x,b,_=setup();b.step();o=b.working()[0]
        original=dict(x.orders[o.id])
        x.fill(x.orders[o.id],200,o.price,True)
        b.apply_order(dict(x.orders[o.id]))
        b.apply_order(original)
        self.assertEqual(o.filled,200)
        terminal=dict(x.orders[o.id],status='CANCELLED')
        b.apply_order(terminal)
        b.apply_order(dict(terminal,status='OPEN'))
        self.assertEqual(o.status,'CANCELLED')
