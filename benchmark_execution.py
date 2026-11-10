"""Offline threaded placement/replenishment benchmark; never contacts RIT.

Injects artificial request latency into the real paced transport, using a local
in-memory exchange. This measures execution mechanics, not fill probability/P&L.
"""
import argparse
import contextlib
import io
import json
import threading
import time
from dataclasses import replace

from concurrent_client import ConcurrentClient
from config import Config
from runtime import Bot
from tests.test_runtime import RuntimeExchange


def run(seconds=8, cap=60, latency=.02):
    client = ConcurrentClient(cap, 4)
    exchange = RuntimeExchange()
    lock = threading.Lock()
    origin = time.monotonic()
    starts = []
    def request(session, method, path, params):
        begins = time.monotonic()
        time.sleep(latency)
        with lock:
            starts.append((begins, method, path))
            exchange.now = time.monotonic()-origin
            return exchange.call(method, path, **params)
    client._request = request  # No HTTP/network access.
    b = Bot(client, replace(Config(), request_cap=cap))
    full_at = None
    latencies = []
    next_fill = origin+2
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            b.start()
            while time.monotonic()-origin < seconds:
                cycle_start = time.monotonic()
                pending = None
                if cycle_start >= next_fill and b.working():
                    side = 'BUY' if len(latencies)%2 == 0 else 'SELL'
                    child = next((o for o in b.working() if b.slots[o.id] == (side,0,0)), None)
                    if child:
                        with lock:
                            exchange.fill(exchange.orders[child.id], child.remaining, child.price, True)
                        pending = (b.slots[child.id], child.id, time.monotonic())
                        next_fill = cycle_start+1
                b.step()
                if full_at is None and len(b.working()) == 50:
                    full_at = time.monotonic()-origin
                if pending:
                    slot, oid, filled_at = pending
                    if any(b.slots[o.id] == slot and o.id != oid for o in b.working()):
                        latencies.append(time.monotonic()-filled_at)
                time.sleep(max(0, b.next_delay-(time.monotonic()-cycle_start)))
            before_cleanup = len(b.working())
            b.cleanup()
    finally:
        client.close()
    start_times = [s for s,_,_ in starts]
    max_requests = max((sum(start <= t < start+1-1e-8 for t in start_times)
                        for start in start_times), default=0)
    assert max_requests <= cap
    assert not b.working()
    return dict(artificial_latency_ms=latency*1000, request_cap=cap,
                benchmark_seconds=seconds, seconds_to_50_orders=full_at,
                submissions=b.submissions, confirmed_fill_shares=b.ledger.volume,
                tested_replenishments=len(latencies),
                refill_seconds_max=max(latencies, default=None),
                refill_seconds_mean=sum(latencies)/len(latencies) if latencies else None,
                open_before_cleanup=before_cleanup, max_requests_in_rolling_second=max_requests,
                note='Artificial local exchange; not measured on a live RIT server.')


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds',type=float,default=8)
    args=parser.parse_args()
    print(json.dumps([run(args.seconds,60,.02),run(args.seconds,20,.05)],indent=2))
