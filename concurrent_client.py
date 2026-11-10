"""Bounded concurrent REST I/O. Workers never mutate the trading ledger."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor, CancelledError
from dataclasses import dataclass
import threading
import time
import requests
from execution import Client, API_KEY, ApiError, UnknownWrite, RateLimited

class ExpiredQuote(ApiError):
    """Request was never dispatched; discard the old decision."""

@dataclass
class Batch:
    results: list
    interrupted: bool = False

class Budget:
    """Global rolling-second budget, shared cooldown and adaptive 429 backoff."""
    def __init__(self, rate, clock=time.monotonic, sleep=time.sleep, burst=4):
        self.rate = rate
        self.max_rate = rate
        self.recover_after = 0
        self.clock, self.sleep = clock, sleep
        self.burst = min(rate, burst)
        self.tokens = float(self.burst)
        self.updated = clock()
        self.recent = deque()
        self.blocked_until = 0
        self.lock = threading.Lock()

    def acquire(self, deadline=None):
        while True:
            with self.lock:
                now = self.clock()
                if deadline is not None and now >= deadline:
                    raise ExpiredQuote('Decision expired before HTTP dispatch')
                while self.recent and now-self.recent[0] >= 1:
                    self.recent.popleft()
                self.tokens = min(self.burst, self.tokens+(now-self.updated)*self.rate)
                self.updated = now
                wait = max(0, self.blocked_until-now, max(0, 1-self.tokens-1e-9)/self.rate)
                if len(self.recent) >= self.rate:
                    wait = max(wait, self.recent[0]+1-now)
                if wait <= 0:
                    self.tokens = max(0, self.tokens-1)
                    self.recent.append(now)
                    return
                wait = max(wait, 1e-6)
                if deadline is not None:
                    wait = min(wait, max(0, deadline-now))
            self.sleep(wait)

    def throttle(self, wait):
        with self.lock:
            now = self.clock()
            # Sibling 429s during the same cooldown are one congestion event.
            if now >= self.blocked_until:
                self.rate = max(1, int(self.rate*.8))
            self.blocked_until = max(self.blocked_until, now+wait)
            self.recover_after = self.blocked_until+10
            self.tokens = 0

    def success(self):
        with self.lock:
            now = self.clock()
            if self.rate < self.max_rate and now >= self.recover_after:
                self.rate = min(self.max_rate, self.rate+1)
                self.recover_after = now+10

class ConcurrentClient(Client):
    def __init__(self, rate=60, workers=4):
        self.budget = Budget(rate, burst=workers)
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='rit-io')
        self.local = threading.local()
        self.lock = threading.Lock()
        self.sessions = []
        self.samples = deque(maxlen=2000)
        self.uncertain_write = threading.Event()

    def _session(self):
        if not hasattr(self.local, 'session'):
            session = requests.Session()
            session.headers.update({'X-API-Key': API_KEY})
            # Per-thread session: no concurrent Session mutation, keep-alive enabled.
            self.local.session = session
            with self.lock:
                self.sessions.append(session)
        return self.local.session

    def call(self, method, path, _deadline=None, _guard=None, **params):
        queued = time.monotonic()
        if _guard is not None and not _guard():
            raise ExpiredQuote('Decision withdrawn before dispatch')
        self.budget.acquire(_deadline)
        if _guard is not None and not _guard():
            raise ExpiredQuote('Decision withdrawn while queued')
        if method == 'POST' and path == 'orders' and self.uncertain_write.is_set():
            raise ExpiredQuote('New submission blocked after uncertain write')
        started = time.monotonic()
        try:
            session = self._session()
            if _deadline is not None and time.monotonic() >= _deadline:
                raise ExpiredQuote('Decision expired before HTTP dispatch')
            result = self._request(session, method, path, params)
            self.budget.success()
            return result
        except RateLimited as exc:
            self.budget.throttle(exc.wait)
            raise
        except UnknownWrite:
            self.uncertain_write.set()
            raise
        finally:
            with self.lock:
                self.samples.append((started-queued, time.monotonic()-started))

    def batch(self, calls, deadline=None, guard=None):
        calls = list(calls)
        futures, results, interrupted = [], [], False
        try:
            for method, path, params in calls:
                futures.append(self.pool.submit(self.call, method, path, _deadline=deadline,
                                                _guard=guard, **params))
        except KeyboardInterrupt:
            interrupted = True
            for pending in futures:
                pending.cancel()
        for future in futures:
            while True:
                try:
                    results.append(future.result())
                    break
                except KeyboardInterrupt:
                    interrupted = True
                    # Drain in-flight requests before the caller reconciles/cleans up.
                    for pending in futures:
                        pending.cancel()
                except CancelledError:
                    results.append(ExpiredQuote('Batch interrupted before dispatch'))
                    break
                except Exception as exc:
                    results.append(exc)
                    break
        results.extend(ExpiredQuote('Batch interrupted before scheduling')
                       for _ in range(len(calls)-len(futures)))
        return Batch(results, interrupted)

    def metrics(self):
        with self.lock:
            rows = list(self.samples)
        def percentile(values, fraction):
            return sorted(values)[int((len(values)-1)*fraction)] if values else None
        return dict(request_samples=len(rows), effective_request_cap=self.budget.rate,
                    request_p50=percentile([r[1] for r in rows],.5),
                    request_p95=percentile([r[1] for r in rows],.95),
                    queue_p95=percentile([r[0] for r in rows],.95))

    def close(self):
        self.pool.shutdown(wait=True, cancel_futures=True)
        for session in self.sessions:
            session.close()
