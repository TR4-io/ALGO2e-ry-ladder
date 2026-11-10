"""Offline synthetic scenarios. No RIT connection; no performance forecasts."""
import json
from tests.test_round import exercise

if __name__=='__main__':
    print(json.dumps(dict(warning='Synthetic order flow, not calibrated to competition.',
                          runs=[exercise(s,r) for r in ('oscillation','crowded','trend','jump','one_sided')
                                for s in (11,29,53,71,97)]),indent=2))
