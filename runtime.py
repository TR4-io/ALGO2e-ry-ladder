"""Continuous RY execution runtime: bounded I/O, independent slots, no polling waits.

The legacy engine supplies accounting/market helpers only. This module owns the
entire live cycle, reconciliation, cancellation, placement and final cleanup.
"""
from dataclasses import asdict
import math
import time

from engine import Bot as AccountingBot
from execution import (ApiError, HttpError, UnknownWrite, Order, TICKERS,
                       MAKER_REBATE, integer, capacity, clean_book)
from concurrent_client import ExpiredQuote
from policy import ladder, split_targets, submission_targets, phase, sweep

BUILD = 'continuous-v2'


class Bot(AccountingBot):
    def __init__(self, client, cfg=None, recorder=None, clock=time.monotonic):
        super().__init__(client, cfg, recorder, clock)
        self.transport = client
        self.pending_cancel = {}
        self.unresolved_since = None
        self.write_uncertain = False
        self.own_ids = set()
        self.operation = 'startup'
        self.last_cycle_report = -math.inf
        self.report_started = clock()
        self.cycle_requests = 0
        self.submissions = self.cancel_requests = self.expired = 0
        self.report_counts = (0, 0, 0)
        self.cycle_count = 0
        self.last_cycle_seconds = 0
        self.last_book_age = None
        self.next_delay = self.cfg.poll_seconds

    def batch(self, calls, deadline=None, guard=None):
        calls = list(calls)
        self.cycle_requests += len(calls)
        started = self.clock()
        batch = self.transport.batch(calls, deadline=deadline, guard=guard, capture_timing=True)
        # Real transport records HTTP dispatch, after any request-budget wait.
        times = batch.started if batch.started is not None else [started]*len(calls)
        return batch, times

    def reads(self, calls):
        batch, times = self.batch(calls)
        if batch.interrupted:
            raise KeyboardInterrupt
        for result in batch.results:
            if isinstance(result, Exception):
                raise result
        return batch.results, times

    def working(self, ticker=None):
        return [o for o in self.ledger.active(ticker) if o.remaining > 0]

    def apply_order(self, row):
        oid = integer(row['order_id'])
        o = self.ledger.orders[oid]
        if (row['ticker'] != o.ticker or row['action'] != o.action
                or integer(row['quantity']) != o.quantity):
            raise ApiError('Order identity changed')
        if integer(row['quantity_filled']) < o.filled:
            return  # A delayed snapshot cannot reverse a confirmed fill.
        if o.status != 'OPEN' and row['status'] == 'OPEN':
            return  # A delayed open snapshot cannot resurrect a terminal order.
        self.update(row)
        if o.status != 'OPEN' or o.remaining == 0:
            pending = self.pending_cancel.pop(oid, None)
            if pending:
                self.record.emit('cancel', tick=self.tick, order_id=oid, reason=pending[1])
            self.requote_orders.discard(oid)

    def reconcile_frame(self, opened, securities):
        self.operation = 'reconciling'
        rows = {integer(r['order_id']): r for r in opened}
        if any(oid not in self.ledger.orders for oid in rows):
            raise ApiError('Untracked order: run one bot only on this account')
        for row in rows.values():
            self.apply_order(row)
        missing = [o.id for o in self.working() if o.id not in rows]
        if missing:
            # Two history requests cover any number of children. No per-order
            # retry loop, repeated history downloads or sleeps inside a cycle.
            histories, _ = self.reads([('GET', 'orders', {'status': s})
                                       for s in ('TRANSACTED', 'CANCELLED')])
            terminal = {integer(r['order_id']): r for history in histories for r in history}
            for oid in missing:
                if oid in terminal:
                    self.apply_order(terminal[oid])
            missing = [oid for oid in missing if self.ledger.orders[oid].remaining > 0]
            # History can be truncated. Resolve at most four details this cycle.
            if missing:
                batch, _ = self.batch([('GET', f'orders/{oid}', {}) for oid in missing[:4]])
                for oid, result in zip(missing[:4], batch.results):
                    if isinstance(result, HttpError) and result.status == 404:
                        continue
                    if isinstance(result, Exception):
                        raise result
                    self.apply_order(result)
                if batch.interrupted:
                    raise KeyboardInterrupt
            missing = [oid for oid in missing if self.ledger.orders[oid].remaining > 0]
        rows = {r['ticker']: r for r in securities}
        if not set(TICKERS).issubset(rows):
            raise ApiError('Missing security position data')
        if any(integer(r['position']) for t, r in rows.items() if t not in TICKERS):
            raise ApiError('Unexpected inventory in another security')
        def matches():
            return {t: integer(rows[t]['position']) for t in TICKERS} == self.ledger.positions
        if not missing and not matches():
            refreshed, _ = self.reads([('GET', 'securities', {})])
            rows = {r['ticker']: r for r in refreshed[0]}
        if missing or not matches():
            if self.unresolved_since is None:
                self.unresolved_since = self.clock()
            self.note_activity('reconciliation_pending', missing_orders=len(missing),
                               wait_seconds=round(self.clock()-self.unresolved_since, 3))
            if self.clock()-self.unresolved_since >= self.cfg.reconcile_timeout:
                raise ApiError('Order/position reconciliation remains unresolved; no guessed fills or new exposure')
            return None
        self.unresolved_since = None
        return rows

    def fresh_book(self):
        self.operation = 'reading_fresh_book'
        values, times = self.reads([('GET', 'securities/book', {'ticker': 'RY', 'limit': 1000})])
        observed = times[0]
        if observed is None:
            raise ApiError('Book was not dispatched')
        self.last_book_age = self.clock()-observed
        return clean_book(values[0], self.own_ids), observed

    def targets(self, book, observed):
        snap = self.observe('RY', book, observed)
        if snap is None or snap.spread > self.cfg.max_spread:
            self.note_activity('stale_or_invalid_book' if snap is None else 'wide_spread')
            return None, None
        self.check_fill_activity()
        now = self.clock()
        if self.mid_history and now-self.mid_history[-1][0] > 2:
            self.mid_history.clear()
        self.mid_history.append((now, snap.mid))
        while len(self.mid_history) > 1 and now-self.mid_history[0][0] > self.cfg.trend_window:
            self.mid_history.pop(0)
        drift = snap.mid-self.mid_history[0][1]
        if abs(drift) >= self.cfg.trend_move:
            self.blocked_until['BUY' if drift < 0 else 'SELL'] = now+self.cfg.trend_pause
        blocked = {side for side, until in self.blocked_until.items() if now < until}
        pos = self.ledger.positions['RY']
        age = 0 if not pos or self.inventory_since is None else now-self.inventory_since
        plan = ladder(snap, pos, self.cfg, age, self.mode, self.cautious, blocked,
                      {side: self.penalty('RY', side) for side in ('BUY', 'SELL')},
                      offsets=self.cfg.tight_offsets if self.tight_ladder else None)
        children = split_targets(plan, self.cfg.child_order_size)
        # Child zero keeps its identity even when remaining capacity shrinks a
        # whole level to a single child. This avoids unnecessary queue resets.
        return snap, {slot if len(slot) == 3 else (*slot, 0): value for slot, value in children.items()}

    def cancels_needed(self, snap, targets):
        result = []
        for o in self.working('RY'):
            target = targets.get(self.slots.get(o.id))
            stale = (o.id in self.requote_orders or o.id in self.pending_cancel or
                     target is None or o.remaining > target[1])
            if target is not None:
                sign = 1 if o.action == 'BUY' else -1
                stale |= (sign*self.ledger.positions['RY'] >= 0 and
                          sign*(snap.fair-o.price)+MAKER_REBATE['RY'] < self.cfg.min_edge)
                stale |= o.price >= snap.ask if o.action == 'BUY' else o.price <= snap.bid
                stale |= (abs(o.price-target[0]) >= self.cfg.recenter_move-1e-9 and
                          self.clock()-o.born >= self.cfg.reprice_delay)
            if stale:
                result.append(o)
        return result

    def request_cancels(self, orders, reason):
        now = self.clock()
        orders = [o for o in orders if o.remaining > 0 and
                  (o.id not in self.pending_cancel or
                   now-self.pending_cancel[o.id][0] >= self.cfg.cancel_retry_seconds)]
        orders = orders[:self.cfg.max_cancel_batch]
        if not orders:
            return
        self.operation = 'sending_cancellations'
        # Cancellation requests retain their share reservations and slot ownership.
        # Confirmation happens via the next normal frame, never a blocking poll loop.
        for o in orders:
            self.pending_cancel[o.id] = (now, reason)
        batch, _ = self.batch([('DELETE', f'orders/{o.id}', {}) for o in orders])
        self.cancel_requests += len(orders)
        errors = []
        for o, result in zip(orders, batch.results):
            if isinstance(result, HttpError) and result.status == 404:
                continue  # not proof of cancellation/fill
            if isinstance(result, UnknownWrite):
                # An uncertain DELETE cannot create an order; retain it pending.
                self.record.emit('cancel_pending', tick=self.tick, order_id=o.id, message=str(result))
                continue
            if isinstance(result, Exception):
                errors.append(result)
            # Even success is only an acknowledgement. Reconcile confirmed fills
            # in the next frame before releasing its slot or quantity.
        if batch.interrupted:
            raise KeyboardInterrupt
        if errors:
            raise errors[0]

    def place(self, targets, observed, intent=None):
        if self.write_uncertain:
            raise UnknownWrite('New submissions disabled after an uncertain accepted order')
        intent = intent or self.mode
        cutoff = self.cfg.entry_cutoff if intent == 'MAKE' else self.cfg.end_tick
        deadline = min(observed+self.cfg.max_book_age,
                       self.case_seen+max(0, cutoff-self.tick))
        if self.clock() >= deadline:
            self.note_activity('decision_expired', book_age=round(self.clock()-observed, 3))
            return
        reservations = self.working()
        occupied = {self.slots.get(o.id) for o in reservations}
        planned = []
        blocked = 0
        for slot, (price, wanted) in submission_targets(targets, self.ledger.positions['RY']):
            if slot in occupied:
                continue
            if len(planned) >= self.cfg.max_submit_batch:
                break
            if any(o.ticker == 'RY' and o.action != slot[0] and
                   (price >= o.price if slot[0] == 'BUY' else price <= o.price) for o in reservations):
                blocked += 1
                continue
            quantity = capacity(self.ledger.positions, reservations, 'RY', slot[0], wanted,
                                self.gross_limit, self.net_limit, self.cfg.stock_budget)
            if quantity <= 0:
                blocked += 1
                continue
            order = Order(-1-len(planned), 'RY', slot[0], quantity, price, self.clock())
            reservations.append(order)
            planned.append((slot, order))
        if not planned:
            reason = 'capacity_or_self_cross' if blocked else 'awaiting_cancel_confirmation' if self.pending_cancel else 'quotes_resting'
            self.note_activity(reason, desired_orders=len(targets), pending_cancels=len(self.pending_cancel))
            return
        self.operation = 'submitting'
        self.note_activity('submitting_batch', planned=len(planned))
        batch, _ = self.batch([('POST', 'orders', dict(ticker='RY', action=o.action,
                                 quantity=o.quantity, type='LIMIT', price=o.price)) for _, o in planned],
                             deadline=deadline, guard=lambda: intent == 'EXIT' or not self.stopping)
        errors = []
        accepted = expired = 0
        for (slot, reserved), result in zip(planned, batch.results):
            if isinstance(result, ExpiredQuote):
                expired += 1
                continue
            if isinstance(result, Exception):
                if isinstance(result, UnknownWrite):
                    self.write_uncertain = True
                errors.append(result)
                continue
            try:
                oid = integer(result['order_id'])
                if oid in self.ledger.orders:
                    raise ValueError('Reused order ID')
                o = Order(oid, 'RY', reserved.action, reserved.quantity, reserved.price, self.clock())
                self.ledger.orders[oid] = o
                self.own_ids.add(str(oid))
                self.slots[oid] = slot
                self.intents[oid] = intent
                self.record.emit('submit', tick=self.tick, order_id=oid, ticker='RY', action=o.action,
                                 quantity=o.quantity, price=o.price, intent=intent)
                self.record.emit('ladder_order', tick=self.tick, order_id=oid, level=slot[1],
                                 child=slot[2], side=o.action)
                self.apply_order(result)
                accepted += 1
            except (KeyError, TypeError, ValueError, ApiError) as exc:
                self.write_uncertain = True
                errors.append(UnknownWrite(f'Accepted order could not be reconciled: {exc}'))
        self.submissions += accepted
        self.expired += expired
        self.note_activity('submission_error' if errors else 'queued_quotes_expired' if expired else 'submitted',
                           accepted=accepted, expired=expired, pending_cancels=len(self.pending_cancel))
        # Account for every successful sibling before surfacing any batch error.
        if errors:
            raise next((e for e in errors if isinstance(e, UnknownWrite)), errors[0])
        if batch.interrupted:
            raise KeyboardInterrupt

    def update_risk_mode(self, rows):
        value = self.pnl(rows)
        self.run_pnl = value-self.start_pnl if value is not None and self.start_pnl is not None else None
        if self.run_pnl is not None:
            self.peak = max(self.peak, self.run_pnl)
            self.cautious = self.run_pnl <= -self.cfg.soft_loss
            reason = ('hard_loss' if self.run_pnl <= -self.cfg.hard_loss else
                      'profit_drawdown' if self.peak >= self.cfg.profit_arm and
                      self.peak-self.run_pnl >= self.cfg.drawdown else None)
            if reason and not self.stopping:
                self.stopping, self.stop_reason = True, reason
                self.record.emit('risk_stop', tick=self.tick, reason=reason, pnl=self.run_pnl, peak=self.peak)
                print(f'RISK STOP: {reason}; P&L={self.run_pnl:.2f}; closing inventory.', flush=True)
        self.mode = phase(self.cfg, self.tick, self.ledger.volume, self.stopping)

    def step(self):
        started = self.clock()
        self.cycle_requests = 0
        try:
            self.operation = 'reading_case_orders_positions'
            values, times = self.reads([('GET', 'case', {}), ('GET', 'orders', {'status': 'OPEN'}),
                                       ('GET', 'securities', {})])
            case = values[0]
            tick = float(case['tick'])
            if not math.isfinite(tick):
                raise ValueError('Invalid case tick')
            if case.get('period') != self.period or tick < self.tick:
                self.round_changed = True
                raise ApiError('Case reset; start a new run with a flat account')
            self.tick, self.case_seen = tick, times[0]
            if case['status'] != 'ACTIVE' or tick >= self.cfg.end_tick:
                self.note_activity('case_inactive', case_status=case['status'])
                self.request_cancels(self.working(), 'case_inactive')
                return not (self.active_seen or self.stopping or tick >= self.cfg.end_tick)
            self.active_seen = True
            rows = self.reconcile_frame(values[1], values[2])
            if rows is None:
                return True
            self.update_risk_mode(rows)
            if self.mode == 'EXIT':
                if self.working():
                    self.request_cancels(self.working(), 'exit')
                    self.note_activity('exit_cancel_pending', pending_cancels=len(self.pending_cancel))
                    return True
                pos = self.ledger.positions['RY']
                if not pos:
                    self.note_activity('exit_complete', stop_reason=self.stop_reason)
                    return not self.stopping
                book, observed = self.fresh_book()
                slip = self.cfg.urgent_slippage if self.tick >= self.cfg.end_tick-8 else self.cfg.exit_slippage
                action = 'SELL' if pos > 0 else 'BUY'
                order = sweep(book, action, min(5000, abs(pos)), slip)
                if order:
                    quantity, price = order
                    self.place({(action, -1, 0): (price, quantity)}, observed, 'EXIT')
                else:
                    self.note_activity('exit_waiting_for_depth')
                return True
            book, observed = self.fresh_book()
            snap, targets = self.targets(book, observed)
            if targets is None:
                self.request_cancels(self.working('RY'), 'invalid_book')
                return True
            needed = self.cancels_needed(snap, targets)
            self.request_cancels(needed, 'ladder_refresh')
            # An unrelated pending cancel does not block empty slots. Old slots
            # and full worst-case shares remain reserved until confirmed.
            if self.clock()-observed >= self.cfg.max_book_age:
                book, observed = self.fresh_book()
                snap, targets = self.targets(book, observed)
                if targets is None:
                    return True
            if targets:
                self.place(targets, observed)
            else:
                self.note_activity('no_eligible_quotes')
            return True
        finally:
            self.last_cycle_seconds = self.clock()-started
            self.cycle_count += 1
            self.operation = 'between_cycles'
            rate = getattr(getattr(self.transport, 'budget', None), 'rate', self.cfg.request_cap)
            # Leave capacity for writes instead of flooding the limiter with reads.
            self.next_delay = max(self.cfg.poll_seconds, (4+self.cfg.max_submit_batch)/max(1, rate))
            self.report_cycle()

    def report_cycle(self):
        now = self.clock()
        if now-self.last_cycle_report < self.cfg.telemetry_seconds:
            return
        self.last_cycle_report = now
        elapsed = max(.001, now-self.report_started)
        submitted, cancelled, filled = self.report_counts
        metrics = dict(build=BUILD, tick=self.tick, mode=self.mode, reason=self.activity['reason'],
                       open_orders=len(self.working('RY')), position=self.ledger.positions['RY'],
                       volume=self.ledger.volume, pnl=getattr(self, 'run_pnl', None),
                       submissions=self.submissions, cancels=self.cancel_requests,
                       submissions_per_second=(self.submissions-submitted)/elapsed,
                       fills_per_second=(self.ledger.volume-filled)/elapsed,
                       pending_cancels=len(self.pending_cancel), cycle_seconds=self.last_cycle_seconds,
                       book_age=self.last_book_age, requests=self.cycle_requests,
                       expired_quotes=self.expired, **self.transport.metrics())
        self.record.emit('runtime_cycle', **metrics)
        print(f'LIVE t={self.tick:.1f} mode={self.mode} open={metrics["open_orders"]} '
              f'RY={metrics["position"]:,} volume={self.ledger.volume:,} '
              f'sent/s={metrics["submissions_per_second"]:.1f} '
              f'filled_shares/s={metrics["fills_per_second"]:.0f} '
              f'cycle={self.last_cycle_seconds:.3f}s reason={metrics["reason"]}', flush=True)
        self.report_started = now
        self.report_counts = self.submissions, self.cancel_requests, self.ledger.volume

    def cleanup(self):
        if self.round_changed:
            print('Case reset: old order IDs will not be cancelled.', flush=True)
            return
        self.operation = 'cleanup'
        try:
            case = self.api('GET', 'case')
            if case.get('period') != self.period or float(case['tick']) < self.tick:
                print('Reset during cleanup; inspect RIT.', flush=True)
                return
            batch, _ = self.batch([('POST', 'commands/cancel', {'ticker': t}) for t in TICKERS])
            failures = [r for r in batch.results if isinstance(r, Exception)]
            if batch.interrupted:
                raise KeyboardInterrupt
            if failures:
                raise failures[0]
            values, _ = self.reads([('GET', 'orders', {'status': 'OPEN'}), ('GET', 'securities', {})])
            rows = self.reconcile_frame(*values)
            final_rows = rows if rows is not None else {r['ticker']: r for r in values[1]}
            positions = {t: integer(final_rows[t]['position']) for t in TICKERS}
            self.record.emit('end', tick=self.tick, volume=self.ledger.volume,
                             qualified=self.ledger.volume >= self.cfg.minimum_volume, positions=positions,
                             open_orders=len(values[0]), accounting_confirmed=rows is not None,
                             server_pnl=self.pnl(final_rows),
                             run_pnl=(self.pnl(final_rows)-self.start_pnl
                                      if self.pnl(final_rows) is not None and self.start_pnl is not None else None))
            print(f'Final confirmed volume={self.ledger.volume:,}; positions={positions}; '
                  f'open orders={len(values[0])}; accounting confirmed={rows is not None}', flush=True)
        except (ApiError, KeyError, ValueError, TypeError) as exc:
            print(f'Cleanup not confirmed: {exc}. Inspect RIT.', flush=True)
