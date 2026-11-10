"""RY excursion ladder. --check reads only; default starts live simulation trading."""
import argparse
from datetime import datetime
from pathlib import Path
import signal
import sys
import time
import threading

from config import Config
from engine import Bot as SerialBot, Recorder
from fast_engine import Bot
from concurrent_client import ConcurrentClient as Client
from execution import Client as SerialClient, ApiError, HttpError, UnknownWrite, RateLimited


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
        activity = self.bot.activity
        details = {k:v for k,v in activity.items() if k not in ('observed', 'reason')}
        print(f'HEARTBEAT: tick={self.bot.tick:g} mode={self.bot.mode} '
              f'{stage}; elapsed={time.monotonic()-started:.1f}s; '
              f'last_decision={activity["reason"]} '
              f'decision_age={max(0,self.bot.clock()-activity["observed"]):.1f}s '
              f'{details}', flush=True)

    def run(self):
        while not self.done.wait(5):
            self.report()

    def close(self):
        self.done.set()
        self.thread.join(timeout=1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--serial', action='store_true', help='Use the original sequential execution path')
    parser.add_argument('--config', help='JSON overrides of Config defaults')
    parser.add_argument('--minimum-volume', type=int, help='Override per-run requirement only after instructor confirmation')
    args = parser.parse_args()
    cfg = Config.load(args.config)
    from dataclasses import replace
    if args.minimum_volume is not None:
        cfg = replace(cfg, minimum_volume=args.minimum_volume)
    cfg.validate()
    client = SerialClient() if args.serial else Client(cfg.request_cap, cfg.io_workers)
    stream = None
    bot = (SerialBot if args.serial else Bot)(client, cfg)
    started = False
    previous = None
    heartbeat = Heartbeat(bot)
    heartbeat.thread.start()
    print("RY ladder: execution diagnostics enabled; heartbeat every 5 seconds.", flush=True)
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
                # Remove stale exposure before a retry when the connection allows it.
                heartbeat.stage("cancelling after API error")
                bot.cancel_all()
                time.sleep(min(1, .2*failures))
            time.sleep(max(0, cfg.poll_seconds-(time.monotonic()-cycle_started)))
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
