"""Offline execution-quality metrics. No trading-client imports or network I/O."""
from collections import defaultdict
import statistics


def quality_summary(events):
    slots = {e['order_id']: (e['side'], e['level']) for e in events if e['event'] == 'ladder_order'}
    levels = defaultdict(lambda: dict(submitted_shares=0, filled_shares=0, submitted_orders=0,
                                     cancelled_orders=0, terminal_after_cancel_requests=0,
                                     filled_before_cancel_confirmation=0, cancel_status_unknown=0,
                                     distances=[], ages=[]))
    volumes = defaultdict(int)
    for e in events:
        if e['event'] == 'fill':
            volumes[(e['ticker'], e.get('action'), e['intent'])] += e['quantity']
            slot = slots.get(e.get('order_id'))
            if slot:
                levels[f'{slot[0]} level {slot[1]}']['filled_shares'] += e['quantity']
    book = None
    submissions = {}
    causes = defaultdict(int)
    for e in events:
        kind = e['event']
        if kind == 'book':
            book = e
        elif kind == 'submit':
            submissions[e['order_id']] = e
            slot = slots.get(e['order_id'])
            if slot:
                row = levels[f'{slot[0]} level {slot[1]}']
                row['submitted_orders'] += 1
                row['submitted_shares'] += e['quantity']
                distance = e.get('distance_from_mid')
                if distance is None and book and 0 <= e.get('wall_time', 0)-book['wall_time'] <= .6:
                    distance = (1 if e['action'] == 'BUY' else -1)*(book['signals']['mid']-e['price'])
                if distance is not None:
                    row['distances'].append(distance)
        elif kind == 'cancel':
            slot = slots.get(e['order_id'])
            if slot:
                row = levels[f'{slot[0]} level {slot[1]}']
                row['terminal_after_cancel_requests'] += 1
                if e.get('terminal_status') == 'CANCELLED':
                    row['cancelled_orders'] += 1
                elif e.get('terminal_status') == 'TRANSACTED' or e.get('cancelled_remaining') == 0:
                    row['filled_before_cancel_confirmation'] += 1
                else:
                    row['cancel_status_unknown'] += 1
                age = e.get('resting_seconds')
                if age is None and e['order_id'] in submissions:
                    age = e['wall_time']-submissions[e['order_id']]['wall_time']
                if age is not None:
                    row['ages'].append(age)
        elif kind == 'cancel_request':
            for cause in e.get('causes', [e.get('reason', 'unknown')]):
                causes[cause] += 1
    for row in levels.values():
        distances, ages = row.pop('distances'), row.pop('ages')
        row['median_submit_distance_from_mid'] = statistics.median(distances) if distances else None
        row['distance_observations'] = len(distances)
        row['median_cancel_confirmation_age_seconds'] = statistics.median(ages) if ages else None
        row['confirmed_fill_fraction_of_submitted_shares'] = (
            row['filled_shares']/row['submitted_shares'] if row['submitted_shares'] else None)
    marks = defaultdict(lambda: [0., 0, 0., 0])
    denominators = {}
    missing_ids = 0
    for e in events:
        if e['event'] != 'markout':
            continue
        side_key = f"{e['ticker']} {e['action']} {e['intent']} {e['horizon']}s"
        keys = [side_key]
        denominators[side_key] = volumes[(e['ticker'], e['action'], e['intent'])]
        slot = slots.get(e.get('order_id'))
        if slot:
            level_key = f'{slot[0]} level {slot[1]} {e["horizon"]}s'
            keys.append(level_key)
            denominators[level_key] = levels[f'{slot[0]} level {slot[1]}']['filled_shares']
        else:
            missing_ids += 1
        for key in keys:
            row = marks[key]
            if e.get('gross_markout') is not None:
                row[0] += e['gross_markout']*e['quantity']; row[1] += e['quantity']
            if e.get('maker_adjusted_markout') is not None:
                row[2] += e['maker_adjusted_markout']*e['quantity']; row[3] += e['quantity']
    markouts = {k: dict(gross_per_share=a/q if q else None,
                       maker_assumption_adjusted_per_share=b/n if n else None,
                       gross_observed_shares=q, adjusted_observed_shares=n,
                       fill_volume_coverage=q/denominators[k] if denominators[k] else None)
                for k, (a, q, b, n) in marks.items()}
    samples = [e for e in events if e['event'] == 'runtime_cycle' and 'position' in e]
    start = next((e for e in events if e['event'] == 'start'), {})
    budget = start.get('config', {}).get('stock_budget', 25000)
    durations = dict.fromkeys(sorted({int(budget*r) for r in (.6, .8, .96, 1)}), 0.)
    elapsed = largest_gap = 0.
    for a, b in zip(samples, samples[1:]):
        dt = max(0, b['wall_time']-a['wall_time'])
        largest_gap = max(largest_gap, dt); elapsed += dt
        for threshold in durations:
            if abs(a['position']) >= threshold:
                durations[threshold] += dt
    errors = list(dict.fromkeys((e.get('wall_time'), e['message']) for e in events if e['event'] == 'error'))
    return dict(config=start.get('config'), level_execution=dict(levels), execution_price_markouts=markouts,
                markouts_without_order_id=missing_ids, cancellation_causes=dict(causes),
                sampled_inventory_time=dict(observed_seconds=elapsed, largest_sample_gap_seconds=largest_gap,
                    thresholds={str(t): dict(seconds=v, fraction=v/elapsed if elapsed else None)
                                for t, v in durations.items()}),
                unique_errors=[dict(wall_time=t, message=m) for t, m in errors],
                quality_note='Markouts are measured after fill detection, not execution timestamps; '
                    'they are not realized P&L. Fee-adjusted values assume passive execution. '
                    'Inventory durations hold each sample until the next; gaps add uncertainty. '
                    'Old markouts without order IDs cannot be assigned to ladder levels.')


def completion_summary(events):
    """Log termination alone is not proof of confirmed account cleanup."""
    end = next((e for e in reversed(events) if e['event'] == 'end'), None)
    context = next((e for e in reversed(events) if e['event'] == 'submission_failure_context'), None)
    attempts = [e for e in events if e['event'] == 'uncertain_submission']
    if end is None:
        state = 'log_not_closed'
    elif end.get('round_changed'):
        state = 'case_reset_unverified'
    elif end.get('write_uncertain') or attempts:
        state = 'submission_outcome_unknown'
    elif not end.get('accounting_confirmed'):
        state = 'accounting_unverified'
    elif end.get('open_orders') != 0:
        state = 'open_orders_unverified_or_remaining'
    elif end.get('cleanup_confirmed') is not True or end.get('cleanup_errors'):
        state = 'cleanup_unverified'
    else:
        state = 'verified'
    return dict(status=state, verified=state == 'verified',
                submission_failure_context=context or (end or {}).get('submission_failure_context'),
                uncertain_submissions=attempts or (end or {}).get('uncertain_submissions', []),
                note='Verified means account cleanup/accounting were confirmed; it does not imply a full-round run, flat inventory '
                     'or an official final result. Older logs without explicit cleanup confirmation remain unverified.')


def activity_summary(events):
    """Observed intervals, not extrapolated full-round performance."""
    cycles = [e for e in events if e['event'] == 'runtime_cycle']
    submissions = [e for e in events if e['event'] == 'submit']
    fills = [e for e in events if e['event'] == 'fill']
    shares = sum(e['quantity'] for e in fills)
    submitted_shares = sum(e['quantity'] for e in submissions)
    first, last = (cycles[0], cycles[-1]) if cycles else ({}, {})
    ticks = (last['tick']-first['tick']) if cycles else None
    seconds = (last['wall_time']-first['wall_time']) if cycles else None
    timed_fills = [e for e in fills if 'wall_time' in e]
    interval_shares = sum(e['quantity'] for e in timed_fills
                          if cycles and first['wall_time'] <= e['wall_time'] <= last['wall_time'])
    gaps = [b['wall_time']-a['wall_time'] for a,b in zip(timed_fills, timed_fills[1:])]
    waits = [e.get('wait_seconds',0) for e in events if e['event'] == 'reconciliation_pending']
    return dict(first_observed_tick=first.get('tick'), last_observed_tick=last.get('tick'),
                first_submission_tick=submissions[0].get('tick') if submissions else None,
                first_fill_tick=fills[0].get('tick') if fills else None,
                observed_tick_span=ticks, observed_wall_seconds=seconds,
                confirmed_filled_shares=shares, submitted_orders=len(submissions),
                submitted_shares=submitted_shares,
                confirmed_shares_per_submission=shares/len(submissions) if submissions else None,
                confirmed_fill_fraction_of_submitted_shares=shares/submitted_shares if submitted_shares else None,
                confirmed_shares_within_observed_interval=interval_shares,
                confirmed_shares_per_observed_tick=interval_shares/ticks if ticks and ticks>0 else None,
                latest_runtime_pnl=last.get('pnl'),
                longest_recorded_fill_gap_seconds=max(gaps,default=None),
                recorded_fill_gaps_over_two_seconds=sum(g>2 for g in gaps),
                max_recorded_reconciliation_wait_seconds=max(waits,default=None),
                note='Tick and wall-clock spans are distinct. Rates use the observed log interval, not a '
                     '300-tick projection. Runtime P&L is not official final P&L; fills may be detected late.')
