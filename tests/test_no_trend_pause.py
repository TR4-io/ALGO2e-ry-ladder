import contextlib
import io
import unittest
from unittest.mock import patch

from config import Config
from engine import Bot as LegacyBot
from runtime import Bot
from policy import ladder
from tests.test_runtime import RuntimeExchange


class NoTrendPauseTests(unittest.TestCase):
    def test_reversals_always_leave_both_sides_eligible_while_flat(self):
        # Old JSON configurations must not silently re-enable the removed gate.
        for pause in (0, 3, 100):
            with self.subTest(old_config_pause=pause):
                x=RuntimeExchange()
                b=Bot(x,Config(trend_pause=pause),clock=lambda:x.now)
                with contextlib.redirect_stdout(io.StringIO()):b.start()
                for tick,mid in ((0,10),(.2,10.08),(.4,9.92),(.6,10.12),(.8,9.88)):
                    x.now=tick;x.mid['RY']=mid;b.tick=tick
                    book,observed=b.fresh_book()
                    _,targets=b.targets(book,observed)
                    self.assertEqual({slot[0] for slot in targets},{'BUY','SELL'})
                    self.assertFalse(b.blocked_until)

    def test_legacy_management_also_never_supplies_blocked_sides(self):
        x=RuntimeExchange();b=LegacyBot(x,Config(trend_pause=100),clock=lambda:x.now)
        with patch('engine.ladder',wraps=ladder) as plan:
            for tick,mid in ((0,10),(.2,10.08),(.4,9.92)):
                x.now=tick;x.mid['RY']=mid
                b.manage('RY')
                self.assertFalse(plan.call_args.args[6])

    def test_hard_inventory_limit_still_blocks_extra_exposure(self):
        x=RuntimeExchange();b=Bot(x,clock=lambda:x.now)
        b.ledger.positions['RY']=25000
        book,observed=b.fresh_book()
        _,targets=b.targets(book,observed)
        self.assertEqual({slot[0] for slot in targets},{'SELL'})
