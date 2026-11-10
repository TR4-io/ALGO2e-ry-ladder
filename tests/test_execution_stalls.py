import contextlib
import io
import json
import unittest
from unittest.mock import patch

from concurrent_client import Budget
from engine import Recorder
from fast_engine import Bot
from strategy import Heartbeat
from tests.test_fast_execution import BatchExchange, FakeClock


class StallTests(unittest.TestCase):
    def test_sibling_429s_reduce_rate_once_and_recover_without_restart(self):
        clock = FakeClock()
        budget = Budget(60, clock=clock, sleep=clock.sleep)
        for _ in range(8):
            budget.throttle(.5)
        self.assertEqual(budget.rate, 48)
        clock.now = 10
        budget.success()
        self.assertEqual(budget.rate, 48)
        clock.now = 10.5
        budget.success()
        self.assertEqual(budget.rate, 49)
        for _ in range(100):
            budget.success()
        self.assertEqual(budget.rate, 49)
        for _ in range(20):
            clock.now += 10
            budget.success()
        self.assertEqual(budget.rate, 60)
        budget.throttle(.5)
        self.assertEqual(budget.rate, 48)

    def test_slow_reconciliation_refreshes_book_and_still_quotes(self):
        x = BatchExchange()
        log = io.StringIO()
        bot = Bot(x, recorder=Recorder(log), clock=lambda: x.now)
        real = bot.reconcile
        def slow_reconcile():
            real()
            x.now += .7
        bot.reconcile = slow_reconcile
        with contextlib.redirect_stdout(io.StringIO()):
            bot.start()
            self.assertTrue(bot.step())
        self.assertEqual(len(x.orders), 8)
        self.assertIn('prefetch_expired', log.getvalue())

    def test_slow_fresh_book_still_blocks_stale_orders(self):
        x = BatchExchange()
        bot = Bot(x, clock=lambda: x.now)
        real_reconcile, real_api = bot.reconcile, bot.raw_api
        def slow_reconcile():
            real_reconcile()
            x.now += .7
        def slow_api(method, path, **params):
            result = real_api(method, path, **params)
            if path == 'securities/book':
                x.now += .7
            return result
        bot.reconcile, bot.raw_api = slow_reconcile, slow_api
        with contextlib.redirect_stdout(io.StringIO()):
            bot.start()
            bot.step()
        self.assertFalse(x.orders)

    def test_zero_inventory_status_distinguishes_resting_orders(self):
        x = BatchExchange()
        bot = Bot(x, clock=lambda: x.now)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            bot.start()
            bot.step()
            x.now = 5
            bot.step()
        self.assertIn('gross=0/25,000 net=0/25,000 open=8 buy=20,000 sell=20,000', out.getvalue())

    def test_drawdown_stop_has_explicit_reason(self):
        x = BatchExchange()
        log, out = io.StringIO(), io.StringIO()
        bot = Bot(x, recorder=Recorder(log), clock=lambda: x.now)
        with contextlib.redirect_stdout(out):
            bot.start()
            bot.peak = 1000
            self.assertFalse(bot.step())
        self.assertIn('RISK STOP: profit_drawdown', out.getvalue())
        events = [json.loads(line) for line in log.getvalue().splitlines()]
        self.assertEqual(next(e for e in events if e['event'] == 'risk_stop')['reason'], 'profit_drawdown')

    def test_heartbeat_reports_without_tick_progress_or_api_calls(self):
        x = BatchExchange()
        bot = Bot(x, clock=lambda: x.now)
        beat = Heartbeat(bot)
        with patch('strategy.time.monotonic', return_value=100):
            beat.stage('reading/reconciling/quoting')
        out = io.StringIO()
        with patch('strategy.time.monotonic', return_value=120), contextlib.redirect_stdout(out):
            beat.report()
        self.assertIn('elapsed=20.0s', out.getvalue())
        self.assertEqual(x.calls, 0)
