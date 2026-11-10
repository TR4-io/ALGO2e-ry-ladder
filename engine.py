"""Exchange state machine. Single writer; confirmed fills reserve all open risk."""
from collections import defaultdict, deque
from dataclasses import asdict
import json
import math
import time

from config import Config
from execution import (ApiError, HttpError, UnknownWrite, Order, Ledger, TICKERS,
                       MAKER_REBATE, integer, capacity, clean_book)
from signals import Market
from policy import phase, sweep, ladder


class MissingOrder(ApiError):
    """Order absent from detail and status snapshots."""


class Recorder:
    def __init__(self, stream=None):
        self.stream = stream
        self.last_flush = 0

    def emit(self, kind, **fields):
        if self.stream:
            self.stream.write(json.dumps(dict(event=kind, wall_time=time.time(), **fields), allow_nan=False)+'\n')
            if kind not in ('book','execution_timing') or time.monotonic()-self.last_flush >= .25:
                self.stream.flush()
                self.last_flush = time.monotonic()


class Bot:
    def __init__(self, client, cfg=None, recorder=None, clock=time.monotonic):
        self.api = client.call
        self.cfg = (cfg or Config()).validate()
        self.record = recorder or Recorder()
        self.clock = clock
        self.ledger = Ledger()
        self.markets = {t: Market() for t in TICKERS}
        self.snapshots = {}
        self.intents = {}
        self.notionals = {}
        self.pending = []
        self.toxicity = defaultdict(float)
        self.markout_count = defaultdict(int)
        self.last_take = defaultdict(lambda: -math.inf)
        self.period = None
        self.tick = -1
        self.case_seen = None
        self.active_seen = False
        self.round_changed = False
        self.stopping = False
        self.cautious = False
        self.mode = 'MAKE'
        self.start_pnl = None
        self.peak = 0
        self.last_report = -10
        self.gross_limit = self.cfg.gross_budget
        self.net_limit = self.cfg.net_budget
        self.slots = {}
        self.inventory_since = None
        self.mid_history = []
        self.blocked_until = {}
        self.recent_ry_fills = deque()
        self.fill_monitor_started = None
        self.tight_ladder = False
        self.requote_orders = set()

    def securities(self):
        rows = {r['ticker']: r for r in self.api('GET', 'securities')}
        if not set(TICKERS).issubset(rows):
            raise ValueError('Expected ALGO2e CNR, RY, AC')
        if any(integer(r['position']) for t, r in rows.items() if t not in TICKERS):
            raise ValueError('Unexpected inventory in another security')
        return rows

    @staticmethod
    def pnl(rows):
        try:
            value = sum(float(rows[t]['realized'])+float(rows[t]['unrealized']) for t in TICKERS)
            return value if math.isfinite(value) else None
        except (KeyError, ValueError, TypeError):
            return None

    def start(self):
        case = self.api('GET', 'case')
        self.period = case.get('period')
        rows = self.securities()
        if any(integer(rows[t]['position']) for t in TICKERS) or self.api('GET', 'orders', status='OPEN'):
            raise ValueError('Start flat with no open orders; use one bot and no manual trades')
        limits = self.api('GET', 'limits')
        if not isinstance(limits, list) or not limits:
            raise ValueError('Cannot verify live limits')
        for row in limits:
            g, n = float(row['gross_limit']), float(row['net_limit'])
            if not math.isfinite(g+n) or min(g, n) <= 0:
                raise ValueError('Invalid live limits')
            self.gross_limit = min(self.gross_limit, int(g))
            self.net_limit = min(self.net_limit, int(n))
        self.start_pnl = self.pnl(rows)
        self.record.emit('start', config=asdict(self.cfg), gross_limit=self.gross_limit, net_limit=self.net_limit, start_pnl=self.start_pnl)
        print(f'Connected. Minimum {self.cfg.minimum_volume:,} shares EACH run; target {self.cfg.target_volume:,}.')
        print(f'Gross/net budgets: {self.gross_limit}/{self.net_limit}.')
        if self.start_pnl is None:
            print('P&L unavailable: dollar stops disabled; share risk limits remain active.')

    def update(self, row):
        oid = integer(row['order_id'])
        o = self.ledger.orders[oid]
        before = o.filled
        previous_position = self.ledger.positions[o.ticker]
        self.ledger.update(row)
        position = self.ledger.positions[o.ticker]
        if o.ticker == 'RY':
            if not position:
                self.inventory_since = None
            elif not previous_position or previous_position*position < 0:
                self.inventory_since = self.clock()
        delta = o.filled-before
        if not delta:
            return
        if o.ticker == 'RY':
            self.recent_ry_fills.append((self.tick, delta))
        # Optional server cumulative VWAP. Never label order limit as execution price.
        fill_price = None
        try:
            vwap = float(row['vwap'])
            if math.isfinite(vwap) and vwap > 0:
                notional = vwap*o.filled
                if before == 0 or oid in self.notionals:
                    fill_price = (notional-self.notionals.get(oid, 0))/delta
                    if not math.isfinite(fill_price) or fill_price <= 0:
                        fill_price = None
                self.notionals[oid] = notional
            else:
                self.notionals.pop(oid, None)
        except (KeyError, ValueError, TypeError):
            self.notionals.pop(oid, None)
        now = self.clock()
        intent = self.intents.get(oid, 'unknown')
        self.record.emit('fill', tick=self.tick, order_id=oid, ticker=o.ticker, action=o.action,
                         quantity=delta, limit_price=o.price, execution_price=fill_price, intent=intent)
        snapshot = self.snapshots.get(o.ticker)
        if snapshot and now-snapshot[1] <= self.cfg.max_book_age:
            for horizon in (.5, 1., 3.):
                self.pending.append((now, horizon, o.ticker, o.action, delta, snapshot[0].mid, fill_price, intent))

    def lookup_once(self, oid):
        try:
            return self.api('GET', f'orders/{oid}')
        except HttpError as exc:
            if exc.status != 404:
                raise
            for status in ('OPEN', 'TRANSACTED', 'CANCELLED'):
                row = next((r for r in self.api('GET', 'orders', status=status)
                            if integer(r['order_id']) == oid), None)
                if row is not None:
                    return row
            raise MissingOrder(f'Order {oid} missing; refusing to guess its fills')

    def lookup(self, oid):
        # Detail and status lists can briefly disagree during a transition.
        for attempt in range(11):
            try:
                return self.lookup_once(oid)
            except MissingOrder:
                if attempt == 10:
                    raise
                time.sleep(0.1)

    def reconcile(self):
        rows = {integer(r['order_id']): r for r in self.api('GET', 'orders', status='OPEN')}
        if any(oid not in self.ledger.orders for oid in rows):
            raise ApiError('Untracked order; account must have one writer')
        for o in list(self.ledger.active()):
            self.update(rows[o.id] if o.id in rows else self.lookup(o.id))

    def cancel(self, o, reason='replace'):
        try:
            self.api('DELETE', f'orders/{o.id}')
        except HttpError as exc:
            if exc.status != 404:
                raise
        # DELETE acknowledgement is not a final fill report. Poll reads only;
        # retain the remaining reservation and never submit while unresolved.
        for attempt in range(11):
            try:
                self.update(self.lookup_once(o.id))
            except MissingOrder:
                pass
            else:
                if o.status != 'OPEN' or o.filled == o.quantity:
                    self.record.emit('cancel', tick=self.tick, order_id=o.id, reason=reason)
                    return
            if attempt < 10:
                time.sleep(0.1)
        raise ApiError(f'Cancellation of order {o.id} not confirmed after 11 checks; '
                       'no replacement submitted')

    def cancel_many(self, orders, reason):
        for o in orders:
            self.cancel(o, reason)

    def cancel_all(self):
        self.cancel_many(list(self.ledger.active()), 'cancel_all')

    def submit(self, ticker, action, qty, price, intent, observed):
        # Last-moment gate, shared by maker, taker and liquidation.
        if self.clock()-observed > self.cfg.max_book_age or qty <= 0 or price <= 0:
            return None
        if self.case_seen is not None:
            estimated_tick = self.tick + max(0, self.clock()-self.case_seen)
            if estimated_tick >= self.cfg.end_tick or (
                    intent in ('MAKE', 'TAKE') and estimated_tick >= self.cfg.entry_cutoff):
                return None
        qty = capacity(self.ledger.positions, self.ledger.active(), ticker, action, int(qty),
                       self.gross_limit, self.net_limit, self.cfg.stock_budget)
        if not qty:
            return None
        for other in self.ledger.active(ticker):
            if other.action != action and ((action == 'BUY' and price >= other.price)
                                          or (action == 'SELL' and price <= other.price)):
                return None
        row = self.api('POST', 'orders', ticker=ticker, action=action, type='LIMIT', quantity=qty, price=price)
        try:
            oid = integer(row['order_id'])
            if oid in self.ledger.orders:
                raise ValueError('Reused order ID')
            self.ledger.orders[oid] = Order(oid, ticker, action, qty, price, self.clock())
            self.intents[oid] = intent
            self.record.emit('submit', tick=self.tick, order_id=oid, ticker=ticker, action=action,
                             quantity=qty, price=price, intent=intent)
            self.update(row)
            return self.ledger.orders[oid]
        except (KeyError, ValueError, TypeError) as exc:
            raise UnknownWrite('Accepted write could not be reconciled; never replay it') from exc

    def book(self, ticker):
        started = self.clock()
        raw = self.api('GET', 'securities/book', ticker=ticker, limit=1000)
        book = clean_book(raw, {str(o.id) for o in self.ledger.orders.values()})
        return book, started

    def observe(self, ticker, book, observed):
        if self.clock()-observed > self.cfg.max_book_age:
            return None
        snap = self.markets[ticker].observe(book, self.clock())
        if snap:
            self.snapshots[ticker] = (snap, observed)
            self.markouts(ticker, snap)
            self.record.emit('book', tick=self.tick, ticker=ticker, bids=book['bids'][:30], asks=book['asks'][:30],
                             signals=asdict(snap), request_seconds=self.clock()-observed)
        return snap

    def markouts(self, ticker, snap):
        now = self.clock()
        keep = []
        for born, horizon, t, side, qty, ref, price, intent in self.pending:
            if t != ticker or now-born < horizon:
                if now-born < 10:
                    keep.append((born, horizon, t, side, qty, ref, price, intent))
                continue
            if now-born > horizon+.75:
                continue  # do not mislabel delayed observations as short-horizon markouts
            sign = 1 if side == 'BUY' else -1
            drift = sign*(snap.mid-ref)
            self.record.emit('markout', tick=self.tick, ticker=t, action=side, quantity=qty,
                             horizon=horizon, elapsed=now-born, post_detection_drift=drift,
                             gross_markout=sign*(snap.mid-price) if price is not None else None, intent=intent)
            if horizon == 1 and intent == 'MAKE':
                key = (t, side)
                self.markout_count[key] += 1
                self.toxicity[key] += .1*(max(0, -drift)-self.toxicity[key])
        self.pending = keep

    def penalty(self, ticker, action):
        key = (ticker, action)
        return min(.03, self.toxicity[key])*min(1, self.markout_count[key]/10)

    def manage_exit(self, ticker, book, observed):
        # No midpoint/ask required to sell, and no bid required to buy back shorts.
        active = self.ledger.active(ticker)
        if active:
            self.cancel_many(list(active), 'exit_refresh')
            book, observed = self.book(ticker)
        pos = self.ledger.positions[ticker]
        if not pos:
            return
        action = 'SELL' if pos > 0 else 'BUY'
        slip = self.cfg.urgent_slippage if self.tick >= self.cfg.end_tick-8 else self.cfg.exit_slippage
        result = sweep(book, action, min(5000, abs(pos)), slip)
        if result:
            qty, price = result
            order = self.submit(ticker, action, qty, price, 'EXIT', observed)
            if order and order.remaining:
                self.cancel(order, 'exit_unfilled_remainder')

    def check_fill_activity(self):
        """One-way fallback, based on confirmed shares and simulation ticks."""
        if self.fill_monitor_started is None:
            self.fill_monitor_started = self.tick
        while self.recent_ry_fills and self.recent_ry_fills[0][0] <= self.tick-self.cfg.fill_window:
            self.recent_ry_fills.popleft()
        if (self.tight_ladder or self.mode != 'MAKE' or self.tick < self.cfg.tighten_after
                or self.tick-self.fill_monitor_started < self.cfg.fill_window):
            return
        volume = sum(qty for _, qty in self.recent_ry_fills)
        if volume >= self.cfg.min_window_fills:
            return
        self.tight_ladder = True
        # Persist cancellation obligations across errors; no mixed old/new ladder.
        self.requote_orders = {o.id for o in self.ledger.active('RY')}
        self.record.emit('ladder_tightened', tick=self.tick, window=self.cfg.fill_window,
                         confirmed_shares=volume, offsets=self.cfg.tight_offsets)
        print(f't={self.tick:.1f}: RY filled {volume:,} shares in the last '
              f'{self.cfg.fill_window:g} ticks; switching to tighter ladder '
              f'{self.cfg.tight_offsets}.')

    def manage(self, ticker):
        if ticker != 'RY':
            return
        book, observed = self.book(ticker)
        if self.mode == 'EXIT':
            self.manage_exit(ticker, book, observed)
            return
        snap = self.observe(ticker, book, observed)
        if snap is None or snap.spread > self.cfg.max_spread:
            self.cancel_many(list(self.ledger.active(ticker)), 'invalid_book')
            return
        self.check_fill_activity()
        now = self.clock()
        pos = self.ledger.positions[ticker]
        if not pos:
            self.inventory_since = None
        elif self.inventory_since is None:
            self.inventory_since = now
        age = 0 if self.inventory_since is None else now-self.inventory_since
        # Net movement, not candle extremes. Reset after a data gap.
        if self.mid_history and now-self.mid_history[-1][0] > 2:
            self.mid_history.clear()
        self.mid_history.append((now, snap.mid))
        while len(self.mid_history) > 1 and now-self.mid_history[0][0] > self.cfg.trend_window:
            self.mid_history.pop(0)
        drift = snap.mid-self.mid_history[0][1]
        if abs(drift) >= self.cfg.trend_move:
            blocked = 'BUY' if drift < 0 else 'SELL'
            self.blocked_until[blocked] = now+self.cfg.trend_pause
        blocked = {a for a, until in self.blocked_until.items() if now < until}
        targets = ladder(snap, pos, self.cfg, age, self.mode, self.cautious, blocked,
                         {a: self.penalty(ticker, a) for a in ('BUY', 'SELL')},
                         offsets=self.cfg.tight_offsets if self.tight_ladder else None)
        cancellations = []
        for o in list(self.ledger.active(ticker)):
            slot = self.slots.get(o.id)
            target = targets.get(slot)
            # Cancel for risk immediately; preserve queue for small fair-price changes.
            cancel = o.id in self.requote_orders or target is None or o.remaining > target[1]
            if target is not None:
                sign = 1 if o.action == 'BUY' else -1
                if sign*pos >= 0:
                    cancel |= sign*(snap.fair-o.price)+MAKER_REBATE[ticker] < self.cfg.min_edge
                cancel |= (o.action == 'BUY' and o.price >= snap.ask or
                           o.action == 'SELL' and o.price <= snap.bid)
                cancel |= (abs(o.price-target[0]) >= self.cfg.recenter_move-1e-9 and
                           now-o.born >= self.cfg.reprice_delay)
            if cancel:
                cancellations.append(o)
        if cancellations:
            self.cancel_many(cancellations, 'ladder_refresh')
        self.requote_orders.intersection_update(o.id for o in self.ledger.active('RY'))
        if cancellations:
            # Cancellation can race with fills: make all decisions again next cycle.
            return
        self.place_targets(ticker, targets, pos, observed, age)

    def place_targets(self, ticker, targets, pos, observed, age):
        sides = ('SELL', 'BUY') if pos > 0 else ('BUY', 'SELL')
        for side in sides:
            for slot, (price, qty) in targets.items():
                if slot[0] != side or any(self.slots.get(o.id) == slot for o in self.ledger.active(ticker)):
                    continue
                if self.ledger.positions[ticker] != pos:
                    return  # A synchronous fill invalidates the inventory-based plan.
                order = self.submit(ticker, side, qty, price, self.mode, observed)
                if order:
                    self.slots[order.id] = slot
                    self.record.emit('ladder_order', tick=self.tick, order_id=order.id,
                                     level=slot[1], side=side, inventory_age=age)

    def step(self):
        self.case_seen = self.clock()
        case = self.api('GET', 'case')
        tick = float(case['tick'])
        if not math.isfinite(tick):
            raise ValueError('Invalid case tick')
        if case.get('period') != self.period or tick < self.tick:
            self.round_changed = True
            raise ApiError('Case reset: restart this process for the next run')
        self.tick = tick
        if case['status'] != 'ACTIVE' or tick >= self.cfg.end_tick:
            self.cancel_all()
            return not (self.active_seen or self.stopping or tick >= self.cfg.end_tick)
        self.active_seen = True
        # Bounded reconciliation retries tolerate fills between independent snapshots.
        for _ in range(3):
            self.reconcile()
            rows = self.securities()
            if {t: integer(rows[t]['position']) for t in TICKERS} == self.ledger.positions:
                break
        else:
            raise ApiError('Position snapshots do not reconcile; no new exposure')
        value = self.pnl(rows)
        pnl = value-self.start_pnl if value is not None and self.start_pnl is not None else None
        if pnl is not None:
            self.peak = max(self.peak, pnl)
            self.cautious = pnl <= -self.cfg.soft_loss
            if pnl <= -self.cfg.hard_loss or self.peak >= self.cfg.profit_arm and self.peak-pnl >= self.cfg.drawdown:
                self.stopping = True
        self.mode = phase(self.cfg, tick, self.ledger.volume, self.stopping)
        if tick-self.last_report >= 5:
            self.last_report = tick
            needed = max(0, self.cfg.minimum_volume-self.ledger.volume)
            rate = needed/max(1, self.cfg.entry_cutoff-tick)
            print(f't={tick:.1f} volume={self.ledger.volume:,} need={needed:,} rate={rate:.0f}/s P&L={pnl} mode={self.mode}')
            self.record.emit('status', tick=tick, volume=self.ledger.volume, needed=needed, required_rate=rate,
                             pnl=pnl, positions=dict(self.ledger.positions), mode=self.mode, cautious=self.cautious)
        if self.stopping and not any(self.ledger.positions.values()) and not self.ledger.active():
            return False
        self.manage('RY')
        return True

    def cleanup(self):
        if self.round_changed:
            print('Case reset: no old order IDs will be cancelled in the new run.')
            return
        try:
            case = self.api('GET', 'case')
            if case.get('period') != self.period or float(case['tick']) < self.tick:
                print('Reset during cleanup: inspect RIT.')
                return
            # Exclusive-account startup contract permits cancellation of unknown accepted writes.
            for ticker in TICKERS:
                self.api('POST', 'commands/cancel', ticker=ticker)
            self.reconcile()
            rows = self.securities()
            positions = {t: integer(rows[t]['position']) for t in TICKERS}
            opened = self.api('GET', 'orders', status='OPEN')
            qualified = self.ledger.volume >= self.cfg.minimum_volume
            self.record.emit('end', tick=self.tick, volume=self.ledger.volume, qualified=qualified,
                             positions=positions, open_orders=len(opened), server_pnl=self.pnl(rows),
                             run_pnl=(self.pnl(rows)-self.start_pnl
                                      if self.pnl(rows) is not None and self.start_pnl is not None else None))
            print(f'Final confirmed volume={self.ledger.volume:,}; qualified={qualified}; positions={positions}; open orders={len(opened)}')
            if opened or any(positions.values()):
                print('ATTENTION: remaining exposure requires inspection in RIT.')
        except (ApiError, KeyError, ValueError, TypeError) as exc:
            print(f'Cleanup not confirmed: {exc}. Inspect RIT immediately.')
