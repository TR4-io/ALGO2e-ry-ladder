import contextlib
import io
import threading
import time
import unittest
from unittest.mock import Mock, patch
from dataclasses import replace
from concurrent_client import Budget, ConcurrentClient, Batch, ExpiredQuote
from execution import ApiError, UnknownWrite, RateLimited, HttpError, Order, risk_bounds
from fast_engine import Bot
from config import Config
from policy import ladder
from tests.test_lifecycle import row, snapshot
from tests.test_round import LadderExchange

class BatchExchange(LadderExchange):
    def call(self, method, path, _deadline=None, **params):
        return super().call(method,path,**params)
    def batch(self,calls,deadline=None,guard=None):
        results=[]
        for method,path,params in calls:
            try:results.append(self.call(method,path,**params))
            except Exception as exc:results.append(exc)
        return Batch(results)
    def metrics(self):return {}

class FastTests(unittest.TestCase):
    def test_parallel_snapshot_and_single_ledger_owner(self):
        x=BatchExchange();b=Bot(x,clock=lambda:x.now)
        with contextlib.redirect_stdout(io.StringIO()):b.start();self.assertTrue(b.step())
        self.assertEqual(len(x.orders),8)
        self.assertEqual(b.ledger.positions,x.positions)
        self.assertFalse(b.inflight)
        self.assertFalse(b.frame)

    def test_all_sibling_successes_recorded_after_unknown_write(self):
        x=BatchExchange();b=Bot(x,clock=lambda:0)
        plan=ladder(snapshot(),0,Config())
        real=x.batch
        def batch(calls,**kwargs):
            out=real(calls,**kwargs)
            out.results[0]=UnknownWrite('response lost')
            return out
        x.batch=batch
        with self.assertRaises(UnknownWrite):b.place_targets('RY',plan,0,0,0)
        self.assertEqual(len(x.orders),8)
        self.assertEqual(len(b.ledger.orders),7)
        self.assertEqual(len(b.inflight),1)
        self.assertTrue(b.write_uncertain)
        with self.assertRaises(UnknownWrite):b.place_targets('RY',plan,0,0,0)
        self.assertEqual(len(x.orders),8)

    def test_known_rejection_does_not_lose_successful_siblings(self):
        x=BatchExchange();b=Bot(x,clock=lambda:0)
        plan=ladder(snapshot(),0,Config())
        x.batch=Mock(return_value=Batch([HttpError(400,'orders')]+[
            row(oid=i,ticker='RY',action=slot[0],quantity=qty,price=price)
            for i,(slot,(price,qty)) in enumerate(list(plan.items())[1:],2)]))
        with self.assertRaises(HttpError):b.place_targets('RY',plan,0,0,0)
        self.assertEqual(len(b.ledger.orders),7)
        self.assertFalse(b.write_uncertain)

    def test_entire_batch_reserved_and_safe_for_one_sided_fills(self):
        x=BatchExchange();b=Bot(x,clock=lambda:0);plan=ladder(snapshot(),0,Config())
        real=x.batch
        def batch(calls,**kwargs):
            self.assertEqual(len(b.inflight),8)
            low,high,gross,net=risk_bounds(b.ledger.positions,b.inflight)
            self.assertEqual((low['RY'],high['RY']),(-12500,12500))
            out=real(calls,**kwargs)
            for o in x.orders.values():
                if o['action']=='BUY':x.fill(o,o['quantity'],o['price'],True)
            return Batch([dict(x.orders[r['order_id']]) for r in out.results])
        x.batch=batch
        b.place_targets('RY',plan,0,0,0)
        self.assertEqual(b.ledger.positions['RY'],12500)
        _,_,gross,net=risk_bounds(b.ledger.positions,b.ledger.active())
        self.assertLessEqual(gross,b.gross_limit)
        self.assertEqual(b.ledger.volume,12500)

    def test_stale_batch_is_never_dispatched(self):
        x=BatchExchange();x.batch=Mock();b=Bot(x,clock=lambda:2)
        b.place_targets('RY',ladder(snapshot(),0,Config()),0,0,0)
        x.batch.assert_not_called()

    def test_expired_queued_orders_are_not_replayed(self):
        x=BatchExchange();b=Bot(x,clock=lambda:0)
        x.batch=Mock(return_value=Batch([ExpiredQuote('stale')]*8))
        b.place_targets('RY',ladder(snapshot(),0,Config()),0,0,0)
        self.assertFalse(b.ledger.orders);self.assertFalse(b.inflight)
        self.assertEqual(x.batch.call_count,1)

    def test_cancel_batch_confirms_racing_fills_before_returning(self):
        x=BatchExchange();b=Bot(x,clock=lambda:0);b.manage('RY')
        real=x.batch
        def batch(calls,**kw):
            if calls[0][0]=='DELETE':
                for o in x.orders.values():x.fill(o,100,o['price'],True)
            return real(calls,**kw)
        x.batch=batch
        b.cancel_many(b.ledger.active(),'test')
        self.assertEqual(b.ledger.volume,800)
        self.assertFalse(b.ledger.active())

    def test_unconfirmed_batch_keeps_reservations(self):
        x=BatchExchange();b=Bot(x,clock=lambda:0);b.manage('RY')
        real=x.batch
        def batch(calls,**kw):
            if calls[0][0]=='DELETE':return Batch([{'success':True}]*len(calls))
            return real(calls,**kw)
        x.batch=batch
        with patch('fast_engine.time.sleep'),self.assertRaises(ApiError):
            b.cancel_many(b.ledger.active(),'test')
        self.assertEqual(len(b.ledger.active()),8)

    def test_interrupt_still_accounts_for_accepted_orders(self):
        x=BatchExchange();b=Bot(x,clock=lambda:0);real=x.batch
        def batch(calls,**kw):
            out=real(calls,**kw);out.interrupted=True;return out
        x.batch=batch
        with self.assertRaises(KeyboardInterrupt):b.place_targets('RY',ladder(snapshot(),0,Config()),0,0,0)
        self.assertEqual(len(b.ledger.orders),8)

    def test_reset_snapshot_causes_no_write(self):
        x=BatchExchange();b=Bot(x,clock=lambda:0);b.period=2
        with self.assertRaises(ApiError):b.step()
        self.assertTrue(b.round_changed);self.assertFalse(x.orders)

    def test_fast_full_rounds(self):
        for regime in ('oscillation','crowded','trend','jump','one_sided'):
            with self.subTest(regime=regime):
                x=BatchExchange(11,regime);b=Bot(x,clock=lambda:x.now)
                with contextlib.redirect_stdout(io.StringIO()):
                    b.start()
                    while b.step():
                        _,_,gross,net=risk_bounds(b.ledger.positions,b.ledger.active())
                        self.assertLessEqual(gross,b.gross_limit)
                        self.assertEqual(b.ledger.volume,x.volume)
                        self.assertEqual(b.ledger.positions,x.positions)
                        x.advance()
                        self.assertLessEqual(x.now,300.25)
                    b.cleanup()
                self.assertFalse(b.ledger.active());self.assertEqual(b.ledger.positions,x.positions)

class FakeClock:
    def __init__(self):self.now=0
    def __call__(self):return self.now
    def sleep(self,dt):self.now+=dt

class TransportTests(unittest.TestCase):
    def test_global_rate_and_burst_budget(self):
        c=FakeClock();b=Budget(10,clock=c,sleep=c.sleep,burst=2);starts=[]
        for _ in range(25):b.acquire();starts.append(c())
        for start in starts:
            self.assertLessEqual(sum(start<=x<start+1-1e-8 for x in starts),10)
        self.assertEqual(starts[:2],[0,0]);self.assertGreaterEqual(starts[2],.099)

    def test_cooldown_shared_and_deadline_prevents_dispatch(self):
        c=FakeClock();b=Budget(10,clock=c,sleep=c.sleep)
        b.throttle(.5)
        with self.assertRaises(ExpiredQuote):b.acquire(deadline=.2)
        self.assertAlmostEqual(c(),.2)
        b.acquire();self.assertGreaterEqual(c(),.5);self.assertEqual(b.rate,8)

    def test_real_workers_overlap_without_exceeding_bound(self):
        client=ConcurrentClient(500,4);lock=threading.Lock();active=peak=0
        def request(*args):
            nonlocal active,peak
            with lock:active+=1;peak=max(peak,active)
            time.sleep(.02)
            with lock:active-=1
            return {}
        client._request=request
        try:
            out=client.batch([('GET','case',{})]*8)
            self.assertTrue(all(r=={} for r in out.results))
            self.assertGreater(peak,1);self.assertLessEqual(peak,4)
            self.assertEqual(len(client.sessions),4)
        finally:client.close()

    def test_unknown_write_no_transport_retry(self):
        client=ConcurrentClient();client._request=Mock(side_effect=UnknownWrite('lost'))
        try:
            with self.assertRaises(UnknownWrite):client.call('POST','orders')
            with self.assertRaises(ExpiredQuote):client.call('POST','orders')
            self.assertEqual(client._request.call_count,1)
        finally:client.close()

    def test_429_reduces_shared_cap(self):
        client=ConcurrentClient(60);client._request=Mock(side_effect=RateLimited(.1))
        try:
            with self.assertRaises(RateLimited):client.call('GET','case')
            self.assertEqual(client.budget.rate,48)
            self.assertGreater(client.budget.blocked_until,time.monotonic())
        finally:client.close()

    def test_queued_deadline_and_stop_guard_send_nothing(self):
        client=ConcurrentClient();client._request=Mock()
        try:
            with self.assertRaises(ExpiredQuote):client.call('POST','orders',_deadline=time.monotonic()-1)
            out=client.batch([('POST','orders',{})]*4,guard=lambda:False)
            self.assertTrue(all(isinstance(r,ExpiredQuote) for r in out.results))
            client._request.assert_not_called()
        finally:client.close()

if __name__=='__main__':unittest.main()

class InterruptionTests(unittest.TestCase):
    def test_interruption_while_scheduling_drains_accepted_future(self):
        from concurrent.futures import Future
        c=ConcurrentClient();f=Future();f.set_result({'order_id':123})
        try:
            with patch.object(c.pool,'submit',side_effect=[f,KeyboardInterrupt()]):
                out=c.batch([('POST','orders',{})]*3)
            self.assertTrue(out.interrupted)
            self.assertEqual(out.results[0],{'order_id':123})
            self.assertEqual(len(out.results),3)
            self.assertTrue(all(isinstance(x,ExpiredQuote) for x in out.results[1:]))
        finally:c.close()

    def test_single_liquidation_order_has_dispatch_deadline(self):
        x=BatchExchange();b=Bot(x,clock=lambda:1)
        b.raw_api=Mock(side_effect=ExpiredQuote('queued too long'))
        self.assertIsNone(b.submit('RY','SELL',100,10,'EXIT',1))
        self.assertEqual(b.raw_api.call_args.kwargs['_deadline'],1.6)
        self.assertIsNone(b.single_deadline)
