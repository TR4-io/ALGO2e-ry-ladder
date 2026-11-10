"""Summarize a recorded run without importing the trading client or connecting to RIT."""
import argparse
from collections import defaultdict
import json


def summarize(path):
    totals = defaultdict(int)
    drift = defaultdict(lambda: [0., 0])
    last_status = end = latest_execution_timing = None
    errors = []
    fills = []
    levels = {}
    max_inventory = 0
    peak = 0.0
    max_drawdown = 0.0
    with open(path, encoding='utf-8') as f:
        for line_number, line in enumerate(f, 1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                errors.append(f'Malformed log line {line_number}; possibly interrupted write')
                continue
            kind = event['event']
            if kind == 'ladder_order':
                levels[event['order_id']] = (event['side'], event['level'])
            if kind == 'fill':
                fills.append(event)
                totals[event['ticker']] += event['quantity']
                totals['intent_'+event['intent']] += event['quantity']
            elif kind == 'markout':
                key = f"{event['ticker']} {event['action']} {event['intent']} {event['horizon']}s"
                drift[key][0] += event['post_detection_drift']*event['quantity']
                drift[key][1] += event['quantity']
            elif kind == 'execution_timing':
                latest_execution_timing = event
            elif kind == 'status':
                last_status = event
                max_inventory = max(max_inventory, abs(event.get('positions', {}).get('RY', 0)))
                pnl = event.get('pnl')
                if pnl is not None:
                    peak = max(peak, pnl)
                    max_drawdown = max(max_drawdown, peak-pnl)
            elif kind == 'end':
                end = event
            elif kind == 'error':
                errors.append(event['message'])
    by_level = defaultdict(int)
    for event in fills:
        slot = levels.get(event.get('order_id'))
        key = f'{slot[0]} level {slot[1]}' if slot else event['intent']
        by_level[key] += event['quantity']
    return dict(latest_execution_timing=latest_execution_timing, fill_volume_by_level=dict(by_level),
                sampled_max_ry_inventory=max_inventory, sampled_max_drawdown=max_drawdown,
                file=str(path), completed=end is not None, final=end, latest_status=last_status,
                confirmed_fill_volume=dict(totals),
                post_detection_drift_per_share={k: v[0]/v[1] for k, v in drift.items() if v[1]},
                errors=errors,
                note='Drift is measured from fill detection, excludes fees, and is not realized P&L. Use official RIT results for grading.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('logs', nargs='+')
    args = parser.parse_args()
    print(json.dumps([summarize(path) for path in args.logs], indent=2))
