"""RY excursion ladder. --check reads only; default starts live simulation trading."""
import argparse
from datetime import datetime
from pathlib import Path
import signal
import sys
import time
import threading

# Spyder can retain old module objects across %runfile invocations. Refresh the
# complete dependency group only when launching the script, never on library import.
LOCAL_MODULES = ('config', 'execution', 'signals', 'policy', 'engine',
                 'concurrent_client', 'fast_engine', 'runtime')


def refresh_local_modules():
    import importlib
    directory = Path(__file__).resolve().parent
    missing = [name+'.py' for name in LOCAL_MODULES if not (directory/(name+'.py')).is_file()]
    if missing:
        raise RuntimeError('Incomplete bot folder; download the complete repository. Missing: '+', '.join(missing))
    sys.path.insert(0, str(directory))
    for name in LOCAL_MODULES:
        sys.modules.pop(name, None)
    importlib.invalidate_caches()
    for name in LOCAL_MODULES:
        module = importlib.import_module(name)
        if Path(module.__file__).resolve().parent != directory:
            raise RuntimeError(f'Mixed bot modules: {name} loaded from {module.__file__}')


if __name__ == '__main__':
    refresh_local_modules()

from config import Config
from engine import Recorder
from runtime import Bot, BUILD
from concurrent_client import ConcurrentClient as Client
from execution import ApiError, HttpError, UnknownWrite, RateLimited


class Heartbeat:
    """Report stalled I/O independently of simulation ticks; never place orders."""
    def __init__(self, bot):
        self.bot = bot
        self.done = threading.Event()
        self.state = ('startup', time.monotonic())
        self.thread = threading.Thread(target=self.run, name='rit-heartbeat', daemon=True)

    def stage(self, name):
        self.state = (name, time.monotonic())

    def report(self):
        stage, started = self.state
        activity = getattr(self.bot, 'activity', None)
        if not isinstance(activity, dict):
            activity = dict(reason='missing_activity_restart_kernel_and_update_all_files',
                            observed=time.monotonic())
        details = {k:v for k,v in activity.items() if k not in ('observed', 'reason')}
        budget = getattr(getattr(self.bot, 'transport', None), 'budget', None)
        cap = getattr(budget, 'rate', 'serial/unknown')
        print(f'HEARTBEAT: tick={self.bot.tick:g} mode={self.bot.mode} '
              f'operation={getattr(self.bot, "operation", stage)} {stage}; elapsed={time.monotonic()-started:.1f}s; '
              f'last_decision={activity["reason"]} '
              f'decision_age={max(0,self.bot.clock()-activity["observed"]):.1f}s '
              f'request_cap={cap} {details}', flush=True)

    def run(self):
        while not self.done.wait(5):
            try:
                self.report()
            except Exception as exc:
                # Diagnostics must not silently die while the trading loop runs.
                print(f'HEARTBEAT ERROR: {type(exc).__name__}: {exc}; '
                      'trading status unknown; restart the kernel and update the full folder.', flush=True)

    def close(self):
        self.done.set()
        self.thread.join(timeout=1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--serial', action='store_true', help='Use the rebuilt runtime with one I/O worker')
    parser.add_argument('--config', help='JSON overrides of Config defaults')
    parser.add_argument('--minimum-volume', type=int, help='Override per-run requirement only after instructor confirmation')
    args = parser.parse_args()
    cfg = Config.load(args.config)
    from dataclasses import replace
    if args.minimum_volume is not None:
        cfg = replace(cfg, minimum_volume=args.minimum_volume)
    cfg.validate()
    client = Client(cfg.request_cap, 1 if args.serial else cfg.io_workers)
    stream = None
    bot = Bot(client, cfg)
    started = False
    previous = None
    heartbeat = Heartbeat(bot)
    heartbeat.thread.start()
    print(f"RY ladder {BUILD}: fresh runtime; LIVE execution counters every second.", flush=True)
    print(f"Trading code folder: {Path(__file__).resolve().parent}", flush=True)
    try:
        if not args.check:
            Path('logs').mkdir(exist_ok=True)
            stream = open(Path('logs')/f'run-{datetime.now():%Y%m%d-%H%M%S-%f}.jsonl', 'w', encoding='utf-8')
            bot.record = Recorder(stream)
            print(f"Run log: {Path(stream.name).resolve()}", flush=True)
        bot.start()
        started = True
        if args.check:
            print('Read-only check passed; no orders submitted or cancelled.')
            return 0
        def stop(signum, frame):
            if bot.stopping:
                raise KeyboardInterrupt
            bot.stopping = True
            bot.stop_reason = 'user_interrupt'
            print('Stopping new exposure and closing inventory; Ctrl+C again exits immediately.')
        previous = signal.signal(signal.SIGINT, stop)
        failures = 0
        while True:
            heartbeat.stage("reading/reconciling/quoting")
            cycle_started = time.monotonic()
            try:
                if not bot.step():
                    break
                failures = 0
            except RateLimited as exc:
                # No replay; a fresh case/book decision follows the server wait.
                print(exc, flush=True)
                heartbeat.stage("rate-limit cooldown")
                time.sleep(exc.wait)
                continue
            except UnknownWrite:
                raise
            except ApiError as exc:
                failures += 1
                bot.record.emit('error', message=str(exc))
                print(exc)
                if bot.round_changed or isinstance(exc, HttpError) and exc.status in (401, 403) or failures >= 5:
                    raise
                # Next bounded cycle revalidates the case and reconciles before
                # any new write. Never enter a nested full-cancel polling loop.
                bot.note_activity('api_error_retry', message=str(exc), failures=failures)
                time.sleep(min(1, .2*failures))
            time.sleep(max(0, bot.next_delay-(time.monotonic()-cycle_started)))
        return 0
    except (ApiError, KeyError, ValueError, TypeError) as exc:
        bot.record.emit('error', message=str(exc))
        print(f'STOPPED: {exc}', file=sys.stderr, flush=True)
        return 1
    except KeyboardInterrupt:
        print('Immediate exit; inspect remaining positions in RIT.')
        return 130
    finally:
        if started and not args.check:
            heartbeat.stage("cleanup")
            bot.cleanup()
        if previous is not None:
            signal.signal(signal.SIGINT, previous)
        if stream:
            stream.close()
        heartbeat.close()
        client.close()


if __name__ == '__main__':
    sys.exit(main())
