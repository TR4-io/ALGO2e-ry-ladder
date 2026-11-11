"""Continuous RY execution runtime: bounded I/O, independent slots, no polling waits.

The legacy engine supplies accounting/market helpers only. This module owns the
entire live cycle, reconciliation, cancellation, placement and final cleanup.
"""
from dataclasses import asdict
import math
import time

from engine import Bot as AccountingBot
from execution import (ApiError, HttpError, UnknownWrite, Order, TICKERS,
                       MAKER_REBATE, integer, capacity, clean_book, risk_bounds)
from concurrent_client import ExpiredQuote
from policy import ladder, split_targets, submission_targets, phase, sweep

BUILD = 'continuous-v4'


class Bot(AccountingBot):
    def __init__(self, client, cfg=None, recorder=None, clock=time.monotonic):
        super().__init__(client, cfg, recorder, clock)
        self.transport = client
        self.pending_cancel = {}
        self.cancel_causes = {}
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
        self.detail_cursor = 0
        self.recovering = False
        self.recovery_cancelled = False
        self.recovery_sample = None
        self.recovery_sample_at = None
        self.last_recovery_cancel = -math.inf
        self.archived_ids = set()
        self.history_gaps = set()
        self.last_history_check = -math.inf
        self.reconciliation = dict(missing_order_ids=[], server_positions=None,
                                   ledger_positions=dict(self.ledger.positions), wait_seconds=0)

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
        if oid in self.archived_ids:
            # Position was already adopted from the exchange after cancellation.
            # Late history improves volume accounting, never moves inventory twice.
            positions = dict(self.ledger.positions)
            before = o.filled
            self.ledger.update(row)
            self.ledger.positions = positions
            self.history_gaps.discard(oid)
            if o.filled > before:
                self.record.emit('fill', tick=self.tick, order_id=oid, ticker=o.ticker,
                                 action=o.action, quantity=o.filled-before, limit_price=o.price,
                                 execution_price=None, intent=self.intents.get(oid, 'unknown'),
                                 historical=True)
            return
        self.update(row)
        if o.status != 'OPEN' or o.remaining == 0:
            pending = self.pending_cancel.pop(oid, None)
            if pending:
                self.record.emit('cancel', tick=self.tick, order_id=oid, reason=pending[1],
                                 resting_seconds=self.clock()-o.born,
                                 cancelled_remaining=o.quantity-o.filled)
            self.requote_orders.discard(oid)

    def reconcile_orders(self, opened):
        rows = {integer(r['order_id']): r for r in opened}
        if any(oid not in self.ledger.orders for oid in rows):
            raise ApiError('Untracked order: run one bot only on this account')
        if any(oid in self.archived_ids for oid in rows):
            raise ApiError('Previously closed order reappeared; cannot trust open-order snapshot')
        for row in rows.values():
            self.apply_order(row)
        missing = [o.id for o in self.working() if o.id not in rows]
        audit = set(self.history_gaps) if self.clock()-self.last_history_check >= 1 else set()
        lookup = list(dict.fromkeys([*missing, *sorted(audit)]))
        if lookup:
            # Two history requests cover any number of children. No per-order
            # retry loop, repeated history downloads or sleeps inside a cycle.
            histories, _ = self.reads([('GET', 'orders', {'status': s})
                                       for s in ('TRANSACTED', 'CANCELLED')])
            terminal = {integer(r['order_id']): r for history in histories for r in history}
            for oid in lookup:
                if oid in terminal:
                    self.apply_order(terminal[oid])
            lookup = [oid for oid in lookup if self.ledger.orders[oid].remaining > 0
                      or oid in self.history_gaps]
            # History can be truncated. Resolve at most four details this cycle.
            # Rotate so permanently missing early IDs cannot starve later orders.
            if lookup:
                start = self.detail_cursor % len(lookup)
                selected = (lookup[start:]+lookup[:start])[:4]
                self.detail_cursor = start+len(selected)
                batch, _ = self.batch([('GET', f'orders/{oid}', {}) for oid in selected])
                for oid, result in zip(selected, batch.results):
                    if isinstance(result, HttpError) and result.status == 404:
                        continue
                    if isinstance(result, Exception):
                        raise result
                    self.apply_order(result)
                if batch.interrupted:
                    raise KeyboardInterrupt
            if audit:
                self.last_history_check = self.clock()
            missing = [oid for oid in missing if self.ledger.orders[oid].remaining > 0]
        return missing

    @staticmethod
    def position_rows(securities):
        rows = {r['ticker']: r for r in securities}
        if not set(TICKERS).issubset(rows):
            raise ApiError('Missing security position data')
        if any(integer(r['position']) for t, r in rows.items() if t not in TICKERS):
            raise ApiError('Unexpected inventory in another security')
        for t in TICKERS:
            integer(rows[t]['position'])
        return rows

    def reconcile_frame(self, opened, securities):
        self.operation = 'reconciling'
        missing = self.reconcile_orders(opened)
        rows = self.position_rows(securities)
        def matches():
            return {t: integer(rows[t]['position']) for t in TICKERS} == self.ledger.positions
        if not missing and not matches():
            # Re-read BOTH sides of a non-atomic snapshot. Re-reading positions
            # alone cannot incorporate a fill missing from the order snapshot.
            refreshed, _ = self.reads([('GET', 'orders', {'status': 'OPEN'})])
            missing = self.reconcile_orders(refreshed[0])
            refreshed, _ = self.reads([('GET', 'securities', {})])
            rows = self.position_rows(refreshed[0])
        self.reconciliation = dict(missing_order_ids=missing,
                                   server_positions={t: integer(rows[t]['position']) for t in TICKERS},
                                   ledger_positions=dict(self.ledger.positions), wait_seconds=0)
        if missing or not matches():
            if self.unresolved_since is None:
                self.unresolved_since = self.clock()
            self.reconciliation['wait_seconds'] = round(self.clock()-self.unresolved_since, 3)
            self.note_activity('reconciliation_pending', missing_orders=len(missing), **self.reconciliation)
            self.record.emit('reconciliation_pending', tick=self.tick, **self.reconciliation)
            if (self.stopping or self.recovering or
                    self.clock()-self.unresolved_since >= self.cfg.reconcile_timeout):
                self.recovering = True
            return None
        self.unresolved_since = None
        return rows

    def recovery_step(self):
        """Cancel, establish a quiescent account, then resume without a restart.

        No fill quantity is inferred from disappearance. Exchange positions are
        adopted only after acknowledged cancellation and two stable, ordered
        empty-orders/positions/empty-orders observations on separate cycles.
        """
        self.operation = 'recovering_account'
        now = self.clock()
        if not self.recovery_cancelled:
            if now-self.last_recovery_cancel < self.cfg.cancel_retry_seconds:
                return None
            self.last_recovery_cancel = now
            # Cancel only this bot's known orders, including IDs absent from OPEN.
            # Explicit IDs avoid accidentally cancelling an unrelated writer.
            ids = [o.id for o in self.working()]
            if ids:
                batch, _ = self.batch([('POST', 'commands/cancel', {'ids': ','.join(map(str, ids))})])
                if batch.interrupted:
                    raise KeyboardInterrupt
                result = batch.results[0]
                if isinstance(result, UnknownWrite):
                    self.note_activity('recovery_cancel_retry', message=str(result))
                    return None
                if isinstance(result, Exception):
                    raise result
                if not isinstance(result, dict) or not isinstance(result.get('cancelled_order_ids'), list):
                    raise ApiError('Invalid bulk-cancellation acknowledgement')
            self.recovery_cancelled = True
            self.recovery_sample = None
            self.record.emit('recovery_started', tick=self.tick, order_ids=ids,
                             **self.reconciliation)
        opened, _ = self.reads([('GET', 'orders', {'status': 'OPEN'})])
        self.reconcile_orders(opened[0])
        positions, _ = self.reads([('GET', 'securities', {})])
        rows = self.position_rows(positions[0])
        after, _ = self.reads([('GET', 'orders', {'status': 'OPEN'})])
        self.reconcile_orders(after[0])
        # Check the round again: recovery must never authorize a new-period order.
        cases, times = self.reads([('GET', 'case', {})])
        case = cases[0]
        tick = float(case['tick'])
        if not math.isfinite(tick):
            raise ApiError('Invalid recovery case tick')
        if case.get('period') != self.period or tick < self.tick:
            self.round_changed = True
            raise ApiError('Case reset during recovery')
        self.tick, self.case_seen = tick, times[0]
        if case['status'] != 'ACTIVE' or tick >= self.cfg.end_tick:
            self.note_activity('case_inactive')
            return None
        if opened[0] or after[0]:
            self.recovery_cancelled = False
            self.recovery_sample = None
            self.note_activity('recovery_cancel_pending', observed_open_orders=len(after[0]))
            return None
        position = {t: integer(rows[t]['position']) for t in TICKERS}
        self.reconciliation = dict(missing_order_ids=[o.id for o in self.working()],
                                   server_positions=dict(position),
                                   ledger_positions=dict(self.ledger.positions),
                                   wait_seconds=round(self.clock()-self.unresolved_since, 3)
                                   if self.unresolved_since is not None else 0)
        low, high, _, _ = risk_bounds(self.ledger.positions, self.working())
        if any(not low[t] <= position[t] <= high[t] for t in TICKERS):
            self.recovery_sample = None
            self.note_activity('recovery_position_outside_reservations', server_positions=position,
                               low=low, high=high)
            return None
        if position != self.recovery_sample:
            self.recovery_sample, self.recovery_sample_at = position, self.clock()
        elif self.clock()-self.recovery_sample_at >= max(.2, self.cfg.poll_seconds):
            old_position = dict(self.ledger.positions)
            gaps = [o.id for o in self.working()]
            self.history_gaps.update(gaps)
            self.archived_ids.update(self.ledger.orders)
            for oid in gaps:
                self.ledger.orders[oid].status = 'RECOVERED_UNKNOWN'
            self.ledger.positions = position
            self.pending_cancel.clear()
            self.requote_orders.clear()
            self.inventory_since = self.clock() if position['RY'] else None
            self.unresolved_since = None
            self.recovering = self.recovery_cancelled = False
            self.recovery_sample = None
            self.reconciliation = dict(missing_order_ids=[], server_positions=dict(position),
                                       ledger_positions=dict(position), wait_seconds=0)
            self.record.emit('recovery_complete', tick=self.tick, previous_positions=old_position,
                             positions=position, unknown_order_ids=sorted(self.history_gaps),
                             confirmed_volume=self.ledger.volume)
            self.note_activity('recovery_complete', unknown_history=len(self.history_gaps))
            return rows
        self.note_activity('recovery_verifying', server_positions=position,
                           unknown_history=len(self.history_gaps))
        return None

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
        pos = self.ledger.positions['RY']
        age = 0 if not pos or self.inventory_since is None else now-self.inventory_since
        plan = ladder(snap, pos, self.cfg, age, self.mode, self.cautious, (),
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
            causes = []
            if o.id in self.requote_orders:
                causes.append('ladder_transition')
            if o.id in self.pending_cancel:
                causes.append('cancel_retry')
            if target is None:
                causes.append('slot_removed')
            elif o.remaining > target[1]:
                causes.append('quantity_reduced')
            if target is not None:
                sign = 1 if o.action == 'BUY' else -1
                if (sign*self.ledger.positions['RY'] >= 0 and
                        sign*(snap.fair-o.price)+MAKER_REBATE['RY'] < self.cfg.min_edge):
                    causes.append('insufficient_edge')
                if (o.price >= snap.ask if o.action == 'BUY' else o.price <= snap.bid):
                    causes.append('crosses_external_book')
                if (abs(o.price-target[0]) >= self.cfg.recenter_move-1e-9 and
                        self.clock()-o.born >= self.cfg.reprice_delay):
                    causes.append('recenter')
            if causes:
                self.cancel_causes[o.id] = causes
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
            self.record.emit('cancel_request', tick=self.tick, order_id=o.id, reason=reason,
                             causes=self.cancel_causes.pop(o.id, [reason]),
                             resting_seconds=now-o.born, remaining=o.remaining)
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
                reference = self.snapshots.get('RY')
                mid = reference[0].mid if reference else None
                self.record.emit('submit', tick=self.tick, order_id=oid, ticker='RY', action=o.action,
                                 quantity=o.quantity, price=o.price, intent=intent,
                                 reference_mid=mid, level=slot[1],
                                 distance_from_mid=((1 if o.action == 'BUY' else -1)*(mid-o.price)
                                                    if mid is not None else None))
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
        self.cautious = False  # P&L is telemetry, never an execution gate.
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
            self.mode = phase(self.cfg, self.tick, self.ledger.volume, self.stopping)
            rows = None if self.recovering else self.reconcile_frame(values[1], values[2])
            if self.recovering:
                rows = self.recovery_step()
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
                       reconciliation=self.reconciliation, recovering=self.recovering,
                       unknown_history=sorted(self.history_gaps), stop_reason=self.stop_reason,
                       book_age=self.last_book_age, requests=self.cycle_requests,
                       expired_quotes=self.expired, **self.transport.metrics())
        self.record.emit('runtime_cycle', **metrics)
        print(f'LIVE t={self.tick:.1f} mode={self.mode} open={metrics["open_orders"]} '
              f'RY={metrics["position"]:,} volume={self.ledger.volume:,} '
              f'sent/s={metrics["submissions_per_second"]:.1f} '
              f'filled_shares/s={metrics["fills_per_second"]:.0f} '
              f'cycle={self.last_cycle_seconds:.3f}s reason={metrics["reason"]} '
              f'missing={len(self.reconciliation["missing_order_ids"])} '
              f'recovering={self.recovering} history_gaps={len(self.history_gaps)}', flush=True)
        self.report_started = now
        self.report_counts = self.submissions, self.cancel_requests, self.ledger.volume

    def finish_if_case_ended(self, error):
        """Distinguish a boundary authentication response from a mid-case failure."""
        if not isinstance(error, HttpError) or error.status not in (401, 403):
            return False
        try:
            values, times = self.reads([('GET', 'case', {})])
            case = values[0]
            tick = float(case['tick'])
            if not math.isfinite(tick) or case.get('period') != self.period or tick < self.tick:
                return False
            if case['status'] != 'STOPPED' and tick < self.cfg.end_tick:
                return False
        except (ApiError, KeyError, ValueError, TypeError):
            return False  # An unverified 401 remains fatal, never silently ignored.
        self.tick, self.case_seen = tick, times[0]
        self.note_activity('case_ended_during_request', message=str(error))
        self.record.emit('case_end_api_error', tick=tick, message=str(error))
        return True

    def cleanup(self):
        self.operation = 'cleanup'
        errors = []
        final_rows = opened = None
        accounting = False
        try:
            if self.round_changed:
                raise ApiError('Case reset: old order IDs will not be cancelled')
            case = self.api('GET', 'case')
            if case.get('period') != self.period or float(case['tick']) < self.tick:
                self.round_changed = True
                raise ApiError('Reset during cleanup; old order IDs will not be cancelled')
            # Record useful final positions even if terminal history has vanished.
            batch, _ = self.batch([('POST', 'commands/cancel', {'ticker': t}) for t in TICKERS])
            errors.extend(str(r) for r in batch.results if isinstance(r, Exception))
            if batch.interrupted:
                raise KeyboardInterrupt
            batch, _ = self.batch([('GET', 'orders', {'status': 'OPEN'}), ('GET', 'securities', {})])
            if batch.interrupted:
                raise KeyboardInterrupt
            for i, value in enumerate(batch.results):
                if isinstance(value, Exception):
                    errors.append(str(value))
                elif i == 0:
                    opened = value
                else:
                    final_rows = self.position_rows(value)
            if opened is not None and final_rows is not None:
                rows = self.reconcile_frame(opened, list(final_rows.values()))
                if rows is not None:
                    final_rows = rows
                    accounting = not self.history_gaps
        except (ApiError, KeyError, ValueError, TypeError) as exc:
            errors.append(str(exc))
        finally:
            positions = ({t: integer(final_rows[t]['position']) for t in TICKERS}
                         if final_rows is not None else None)
            value = self.pnl(final_rows) if final_rows is not None else None
            confirmed = accounting and opened == [] and not errors
            self.record.emit('end', tick=self.tick, volume=self.ledger.volume,
                             qualified=self.ledger.volume >= self.cfg.minimum_volume,
                             positions=positions, open_orders=len(opened) if opened is not None else None,
                             accounting_confirmed=accounting, cleanup_confirmed=confirmed,
                             unknown_order_ids=sorted(self.history_gaps), cleanup_errors=errors,
                             round_changed=self.round_changed, server_pnl=value,
                             run_pnl=value-self.start_pnl if value is not None and self.start_pnl is not None else None)
            print(f'Final confirmed volume={self.ledger.volume:,}; positions={positions}; '
                  f'cleanup confirmed={confirmed}', flush=True)
            if errors:
                print('Cleanup incomplete: '+'; '.join(errors)+'. Inspect RIT.', flush=True)
