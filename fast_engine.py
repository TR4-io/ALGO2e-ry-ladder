"""Concurrent execution around a single, deterministic owner of positions/orders."""
import time
from engine import Bot as SerialBot, MissingOrder
from execution import (ApiError, HttpError, UnknownWrite, Order, integer, capacity,
                       clean_book)
from concurrent_client import ExpiredQuote


class Bot(SerialBot):
    def __init__(self, client, *args, **kwargs):
        super().__init__(client, *args, **kwargs)
        self.transport = client
        self.raw_api = self.api
        self.frame = {}
        self.frame_started = 0
        self.api = self.cycle_api
        self.inflight = []
        self.write_uncertain = False
        self.last_timing = -1e9
        self.single_deadline = None

    @staticmethod
    def key(method, path, params):
        return method, path, tuple(sorted(params.items()))

    def cycle_api(self, method, path, **params):
        key = self.key(method, path, params)
        if key in self.frame:
            if path == 'case':
                self.case_seen = self.frame_started
            return self.frame.pop(key)
        if method == 'POST' and path == 'orders' and self.single_deadline is not None:
            return self.raw_api(method, path, _deadline=self.single_deadline, **params)
        return self.raw_api(method, path, **params)

    def step(self):
        started = self.clock()
        calls = [('GET','case',{}), ('GET','orders',{'status':'OPEN'}),
                 ('GET','securities',{}), ('GET','securities/book',{'ticker':'RY','limit':1000})]
        batch = self.transport.batch(calls)
        if batch.interrupted:
            raise KeyboardInterrupt
        for result in batch.results:
            if isinstance(result, Exception):
                raise result
        self.frame_started = started
        self.frame = {self.key(*call): result for call, result in zip(calls, batch.results)}
        try:
            # These reads are concurrent, not atomic. Base reconciliation re-reads
            # mismatching inventory; expired market data cannot authorize a write.
            return super().step()
        finally:
            self.frame.clear()
            if self.clock()-self.last_timing >= 5:
                self.last_timing = self.clock()
                self.record.emit('execution_timing', tick=self.tick,
                                 cycle_seconds=self.clock()-started, **self.transport.metrics())

    def book(self, ticker):
        key = self.key('GET','securities/book',{'ticker':ticker,'limit':1000})
        if key not in self.frame:
            return super().book(ticker)
        raw = self.frame.pop(key)
        return clean_book(raw, {str(o.id) for o in self.ledger.orders.values()}), self.frame_started

    def cancel_many(self, orders, reason):
        orders = [o for o in orders if o.status == 'OPEN']
        if not orders:
            return
        # Submit all selected cancels before waiting for terminal order snapshots.
        batch = self.transport.batch([('DELETE',f'orders/{o.id}',{}) for o in orders])
        errors = [r for r in batch.results if isinstance(r, Exception)
                  and not (isinstance(r,HttpError) and r.status == 404)]
        interrupted = batch.interrupted
        waiting = list(orders)
        for attempt in range(51):
            reads = self.transport.batch([('GET',f'orders/{o.id}',{}) for o in waiting])
            interrupted |= reads.interrupted
            remaining = []
            for order, result in zip(waiting, reads.results):
                try:
                    if isinstance(result, HttpError) and result.status == 404:
                        result = self.lookup_once(order.id)
                    if isinstance(result, Exception):
                        raise result
                    self.update(result)
                except MissingOrder:
                    remaining.append(order)
                    continue
                except Exception as exc:
                    errors.append(exc)
                    remaining.append(order)
                    continue
                if order.status == 'OPEN' and order.remaining:
                    remaining.append(order)
                else:
                    self.record.emit('cancel',tick=self.tick,order_id=order.id,reason=reason)
                    self.requote_orders.discard(order.id)
            waiting = remaining
            if not waiting or interrupted or errors:
                break
            if attempt < 50:
                time.sleep(.02)
        if interrupted:
            raise KeyboardInterrupt
        if errors:
            # All successful confirmations have already been accounted for.
            raise next((e for e in errors if isinstance(e,UnknownWrite)), errors[0])
        if waiting:
            raise ApiError('Cancellation not confirmed; no batch replacement submitted')

    def submit(self, ticker, action, qty, price, intent, observed):
        if self.write_uncertain:
            raise UnknownWrite('New orders blocked after uncertain batch')
        self.single_deadline = observed+self.cfg.max_book_age
        if self.case_seen is not None:
            cutoff = self.cfg.entry_cutoff if intent in ('MAKE','TAKE') else self.cfg.end_tick
            self.single_deadline = min(self.single_deadline, self.case_seen+max(0,cutoff-self.tick))
        try:
            return super().submit(ticker, action, qty, price, intent, observed)
        except ExpiredQuote:
            return None
        finally:
            self.single_deadline = None

    def place_targets(self, ticker, targets, pos, observed, age):
        if self.write_uncertain:
            raise UnknownWrite('New orders blocked after uncertain batch')
        deadline = observed+self.cfg.max_book_age
        if self.case_seen is not None:
            cutoff = self.cfg.entry_cutoff if self.mode == 'MAKE' else self.cfg.end_tick
            deadline = min(deadline, self.case_seen+max(0,cutoff-self.tick))
        if self.clock() >= deadline or self.stopping:
            return
        planned = []
        reservations = list(self.ledger.active())
        sides = ('SELL','BUY') if pos > 0 else ('BUY','SELL')
        for side in sides:
            for slot,(price,wanted) in targets.items():
                if slot[0] != side or any(self.slots.get(o.id)==slot for o in self.ledger.active(ticker)):
                    continue
                if any(o.ticker==ticker and o.action!=side and
                       (price >= o.price if side=='BUY' else price <= o.price) for o in reservations):
                    continue
                qty = capacity(self.ledger.positions,reservations,ticker,side,wanted,
                               self.gross_limit,self.net_limit,self.cfg.stock_budget)
                if not qty:
                    continue
                candidate = Order(-1-len(planned),ticker,side,qty,price,self.clock())
                reservations.append(candidate)
                planned.append((slot,candidate))
        if not planned:
            return
        # Reserve the entire batch before dispatch. Every combination of fills fits.
        # No other thread can update the ledger or issue strategy orders while awaiting.
        self.inflight = [order for _,order in planned]
        batch = self.transport.batch([('POST','orders',dict(ticker=ticker,action=o.action,
                                      type='LIMIT',quantity=o.quantity,price=o.price)) for _,o in planned],
                                     deadline=deadline, guard=lambda: not self.stopping)
        errors, unresolved = [], []
        for (slot,reservation), result in zip(planned,batch.results):
            if isinstance(result, Exception):
                if isinstance(result,UnknownWrite):
                    unresolved.append(reservation)
                    self.write_uncertain = True
                if not isinstance(result,ExpiredQuote):
                    errors.append(result)
                continue
            try:
                oid = integer(result['order_id'])
                if oid in self.ledger.orders:
                    raise ValueError('Reused order ID')
                o = Order(oid,ticker,reservation.action,reservation.quantity,reservation.price,self.clock())
                self.ledger.orders[oid] = o
                self.intents[oid] = self.mode
                self.slots[oid] = slot
                self.record.emit('submit',tick=self.tick,order_id=oid,ticker=ticker,action=o.action,
                                 quantity=o.quantity,price=o.price,intent=self.mode)
                self.record.emit('ladder_order',tick=self.tick,order_id=oid,level=slot[1],
                                 side=o.action,inventory_age=age)
                self.update(result)
            except (KeyError, ValueError, TypeError) as exc:
                unresolved.append(reservation)
                self.write_uncertain = True
                errors.append(UnknownWrite('Accepted batch order could not be reconciled; never replay'))
        self.inflight = unresolved
        # Process every sibling result before failing: cleanup must see accepted IDs.
        if errors:
            raise next((e for e in errors if isinstance(e,UnknownWrite)),errors[0])
        if batch.interrupted:
            raise KeyboardInterrupt
