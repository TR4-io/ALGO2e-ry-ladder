def sweep(book, action, wanted, slippage):
    levels = book['asks' if action == 'BUY' else 'bids']
    if not levels:
        return None
    best = levels[0][0]
    qty, price = 0, best
    for p, available in levels:
        if abs(p-best) > slippage+1e-9:
            break
        qty += min(wanted-qty, available)
        price = p
        if qty >= wanted:
            break
    return (int(qty), price) if qty else None


import math
from execution import MAKER_REBATE


def phase(cfg, tick, volume, stopping):
    # Profit opportunities continue after qualification; volume is not an edge.
    if stopping or tick >= cfg.flatten_at:
        return 'EXIT'
    return 'REDUCE' if tick >= cfg.last_entry else 'MAKE'


def ladder(snap, pos, cfg, age=0, mode='MAKE', cautious=False, blocked=(), penalties=None, offsets=None):
    """Pure target plan. Capacity must still reserve outstanding orders at submission."""
    penalties = penalties or {}
    offsets = cfg.offsets if offsets is None else offsets
    center = snap.fair - cfg.skew*max(-1, min(1, pos/cfg.inventory_brake))
    widening = min(cfg.max_widening, snap.sigma*.5)
    targets = {}
    for side, sign in (('BUY', 1), ('SELL', -1)):
        reducing = sign*pos < 0
        adding_blocked = mode != 'MAKE' or abs(pos) >= cfg.inventory_brake or side in blocked
        if not reducing and adding_blocked:
            continue
        # Old/large inventory gets a nearer passive exit, with no reversal sizing.
        exit_only = reducing and (age >= cfg.inventory_age or abs(pos) >= cfg.inventory_brake
                                  or mode != 'MAKE' or cautious or side in blocked)
        if exit_only:
            price = snap.bid if side == 'BUY' else snap.ask
            targets[(side, -1)] = (round(price, 2), min(abs(pos), cfg.exit_clip))
            continue
        side_remaining = max(0, cfg.stock_budget-sign*pos)
        # If inventory exists, the reducing side may flatten but cannot overshoot.
        if reducing:
            side_remaining = min(side_remaining, abs(pos))
        scale = .5 if cautious else 1
        if not reducing:
            scale *= max(.25, 1-abs(pos)/cfg.inventory_brake)
        seen = set()
        for i, (offset, size) in enumerate(zip(offsets, cfg.sizes)):
            # Widen inner levels within the configured outer distance.
            distance = min(offsets[-1], offset+widening+penalties.get(side, 0))
            raw = center-sign*distance
            price = (math.floor((min(raw, snap.bid)+1e-9)*100)/100 if side == 'BUY'
                     else math.ceil((max(raw, snap.ask)-1e-9)*100)/100)
            price = round(price, 2)
            edge = sign*(snap.fair-price)+MAKER_REBATE['RY']-penalties.get(side, 0)
            qty = min(side_remaining, int(size*scale)//50*50)
            if price <= 0 or edge < cfg.min_edge or qty <= 0 or price in seen:
                continue
            targets[(side, i)] = (price, qty)
            side_remaining -= qty
            seen.add(price)
    return targets
