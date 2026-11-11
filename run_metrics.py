"""Offline execution-quality metrics. No trading-client imports or network I/O."""
from collections import defaultdict
import statistics


def quality_summary(events):
    slots = {e['order_id']: (e['side'], e['level']) for e in events if e['event'] == 'ladder_order'}
    levels = defaultdict(lambda: dict(submitted_shares=0, filled_shares=0, submitted_orders=0,
                                     cancelled_orders=0, distances=[], ages=[]))
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
                row['cancelled_orders'] += 1
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
