import contextlib
import io
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock

from engine import Bot
from strategy import Heartbeat


class StartupTests(unittest.TestCase):
    def test_heartbeat_handles_old_bot_without_activity(self):
        bot = Bot(Mock())
        del bot.activity
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            Heartbeat(bot).report()
        self.assertIn('missing_activity_restart_kernel_and_update_all_files', output.getvalue())
        self.assertIn('request_cap=serial/unknown', output.getvalue())

    def test_script_entry_replaces_stale_modules_before_importing_bot(self):
        script = Path(__file__).resolve().parents[1]/'strategy.py'
        # Mirrors runfile in an existing interpreter: poisoned cached modules
        # would fail imports immediately unless the whole dependency group reloads.
        code = '''
import runpy, sys, types
for name in ('config','execution','signals','policy','engine','concurrent_client','fast_engine','runtime'):
    sys.modules[name] = types.ModuleType(name)
sys.argv = [sys.argv[1], '--help']
try:
    runpy.run_path(sys.argv[0], run_name='__main__')
except SystemExit as exc:
    assert exc.code == 0
from engine import Bot
from fast_engine import Bot as FastBot
from runtime import Bot as RuntimeBot
from unittest.mock import Mock
assert hasattr(Bot(Mock()), 'activity')
assert hasattr(FastBot(Mock()), 'activity')
assert FastBot.__mro__[1] is Bot
assert RuntimeBot.__mro__[1] is Bot
assert hasattr(RuntimeBot(Mock()), 'activity')
'''
        result = subprocess.run([sys.executable, '-c', code, str(script)],
                                capture_output=True, text=True, timeout=15, cwd='/tmp')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--check', result.stdout)

    def test_heartbeat_thread_survives_reporting_exception(self):
        beat = Heartbeat(Bot(Mock()))
        beat.done = Mock()
        beat.done.wait.side_effect = [False, False, True]
        beat.report = Mock(side_effect=[AttributeError('legacy object'), None])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            beat.run()
        self.assertEqual(beat.report.call_count, 2)
        self.assertIn('HEARTBEAT ERROR', output.getvalue())
