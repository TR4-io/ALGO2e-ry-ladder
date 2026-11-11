import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from analyze_run import summarize
from concurrent_client import Batch, ExpiredQuote
from engine import Recorder
from execution import UnknownWrite, HttpError
from run_metrics import activity_summary, completion_summary, quality_summary
from runtime import Bot
from tests.test_runtime import RuntimeExchange


class BoundaryTests(unittest.TestCase):
    def ready(self):
        x=RuntimeExchange(); log=io.StringIO()
        b=Bot(x,recorder=Recorder(log),clock=lambda:x.now)
        with contextlib.redirect_stdout(io.StringIO()): b.start()
        return x,b,log

    def test_uncertain_batch_retains_attempt_and_successful_siblings_without_replay(self):
        x,b,log=self.ready(); real=x.batch
        def batch(calls,**kwargs):
            if calls and calls[0][0]=='POST' and calls[0][1]=='orders':
                out=real(calls[:-1],**kwargs)
                out.results[0]=UnknownWrite('POST orders: HTTP 500; outcome unknown')
                out.results.append(ExpiredQuote('not dispatched'))
                if out.started is not None: out.started.append(None)
                return out
            return real(calls,**kwargs)
        x.batch=batch
        with self.assertRaises(UnknownWrite): b.step()
        self.assertEqual(len(b.uncertain_submissions),1)
        a=b.uncertain_submissions[0]
        self.assertEqual(a['quantity'],1000)
        self.assertIn(a['action'],('BUY','SELL'))
        self.assertEqual(a['level'],0)
        self.assertEqual(len(b.ledger.orders),6)
        before=len(x.orders)
        x.requests.clear()
        with contextlib.redirect_stdout(io.StringIO()): b.audit_submission_failure(UnknownWrite('500'))
        self.assertEqual(b.submission_failure_context['state'],'case_not_ended')
        self.assertTrue(all(method=='GET' and path=='case' for _,method,path,_ in x.requests))
        self.assertEqual(len(x.orders),before)
        with self.assertRaises(UnknownWrite): b.place({},x.now)
        self.assertEqual(len(x.orders),before)

    def test_boundary_classification_never_resolves_unknown_order(self):
        cases=[({'period':1,'tick':300,'status':'STOPPED'},'case_ended',False),
               ({'period':1,'tick':0,'status':'ACTIVE'},'case_reset',True),
               ({'period':2,'tick':299,'status':'ACTIVE'},'case_reset',True),
               ({'period':1,'tick':299,'status':'ACTIVE'},'case_not_ended',False)]
        for case,state,changed in cases:
            with self.subTest(state=state):
                x,b,_=self.ready();b.tick=299
                b.reads=Mock(return_value=([case],[0]))
                with contextlib.redirect_stdout(io.StringIO()): b.audit_submission_failure(UnknownWrite('500'))
                self.assertEqual(b.submission_failure_context['state'],state)
                self.assertEqual(b.round_changed,changed)
                self.assertTrue(b.write_uncertain)
                self.assertEqual(b.submission_failure_context['outcome'],'unknown')

    def test_unavailable_or_invalid_case_is_unverified(self):
        for result in (HttpError(401,'case'),{'period':1,'tick':float('nan'),'status':'ACTIVE'}):
            x,b,_=self.ready()
            b.reads=Mock(side_effect=result) if isinstance(result,Exception) else Mock(return_value=([result],[0]))
            with contextlib.redirect_stdout(io.StringIO()): b.audit_submission_failure(UnknownWrite('500'))
            self.assertEqual(b.submission_failure_context['state'],'unverified')
            self.assertTrue(b.write_uncertain)
            self.assertFalse(b.round_changed)

    def test_reset_audit_prevents_cleanup_writes_to_new_case(self):
        x,b,log=self.ready();b.tick=299;x.now=0
        with contextlib.redirect_stdout(io.StringIO()):
            b.audit_submission_failure(UnknownWrite('500'))
            x.requests.clear();b.cleanup()
        self.assertFalse(x.requests)
        end=json.loads(log.getvalue().splitlines()[-1])
        self.assertTrue(end['round_changed'])
        self.assertTrue(end['write_uncertain'])
        self.assertFalse(end['cleanup_confirmed'])

    def test_cli_audits_unknown_submission_once_and_returns_failure(self):
        import strategy
        x=RuntimeExchange();x.close=Mock()
        b=Bot(x,clock=lambda:x.now)
        b.step=Mock(side_effect=UnknownWrite('POST orders: HTTP 500; outcome unknown'))
        old_cwd=os.getcwd()
        with tempfile.TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                with patch.object(sys,'argv',['strategy.py']), patch.object(strategy,'Client',return_value=x), \
                     patch.object(strategy,'Bot',return_value=b),patch.object(strategy,'Heartbeat'), \
                     contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
                    result=strategy.main()
                events=[json.loads(line) for line in next(Path('logs').glob('*.jsonl')).read_text().splitlines()]
            finally: os.chdir(old_cwd)
        self.assertEqual(result,1)
        self.assertEqual(b.step.call_count,1)
        self.assertEqual(sum(e['event']=='submission_failure_context' for e in events),1)
        self.assertEqual(sum(e['event']=='error' for e in events),1)
        self.assertFalse(events[-1]['cleanup_confirmed'])
        x.close.assert_called_once()


class AnalysisTests(unittest.TestCase):
    def test_closed_reset_log_is_not_completed(self):
        end=dict(event='end',round_changed=True,accounting_confirmed=False,cleanup_confirmed=False,
                 positions=None,open_orders=None)
        with tempfile.NamedTemporaryFile(mode='w',suffix='.jsonl') as f:
            f.write(json.dumps(end)+'\n');f.flush();r=summarize(f.name)
        self.assertTrue(r['log_closed'])
        self.assertFalse(r['completed'])
        self.assertEqual(r['completion']['status'],'case_reset_unverified')

    def test_verified_accounting_required_and_unknown_submission_retained(self):
        end=dict(event='end',accounting_confirmed=True,cleanup_confirmed=True,open_orders=0)
        self.assertTrue(completion_summary([end])['verified'])
        self.assertFalse(completion_summary([])['verified'])
        self.assertFalse(completion_summary([dict(end,write_uncertain=True)])['verified'])
        self.assertFalse(completion_summary([dict(end,cleanup_confirmed=False)])['verified'])
        self.assertFalse(completion_summary([dict(end,open_orders=1)])['verified'])
        old=dict(end);del old['cleanup_confirmed']
        self.assertFalse(completion_summary([old])['verified'])
        unknown=dict(event='uncertain_submission',quantity=1000,price=10)
        self.assertEqual(completion_summary([unknown,end])['uncertain_submissions'],[unknown])
        self.assertFalse(completion_summary([unknown,end])['verified'])

    def test_late_start_activity_uses_observed_ticks_and_separate_wall_time(self):
        events=[dict(event='runtime_cycle',tick=17,wall_time=100,pnl=0),
                dict(event='submit',tick=17,quantity=1000),
                dict(event='fill',tick=18,wall_time=101,quantity=500),
                dict(event='fill',tick=20,wall_time=104,quantity=500),
                dict(event='runtime_cycle',tick=27,wall_time=112,pnl=50)]
        r=activity_summary(events)
        self.assertEqual(r['observed_tick_span'],10)
        self.assertEqual(r['observed_wall_seconds'],12)
        self.assertEqual(r['first_fill_tick'],18)
        self.assertEqual(r['confirmed_shares_per_observed_tick'],100)
        self.assertEqual(r['confirmed_shares_per_submission'],1000)
        self.assertEqual(r['recorded_fill_gaps_over_two_seconds'],1)
        self.assertIsNone(activity_summary([])['confirmed_shares_per_observed_tick'])
        events.append(dict(event='fill',tick=28,wall_time=113,quantity=500))
        self.assertEqual(activity_summary(events)['confirmed_shares_per_observed_tick'],100)
        self.assertEqual(activity_summary(events)['confirmed_filled_shares'],1500)

    def test_cancellation_race_not_counted_as_confirmed_cancel(self):
        events=[dict(event='ladder_order',order_id=i,side='BUY',level=0) for i in range(4)]
        events += [dict(event='cancel',order_id=0,terminal_status='TRANSACTED',cancelled_remaining=0),
                   dict(event='cancel',order_id=1,terminal_status='CANCELLED',cancelled_remaining=500),
                   dict(event='cancel',order_id=2,cancelled_remaining=0),
                   dict(event='cancel',order_id=3)]
        r=quality_summary(events)['level_execution']['BUY level 0']
        self.assertEqual(r['terminal_after_cancel_requests'],4)
        self.assertEqual(r['cancelled_orders'],1)
        self.assertEqual(r['filled_before_cancel_confirmation'],2)
        self.assertEqual(r['cancel_status_unknown'],1)
