# ALGO2e RY ladder

A standalone RY excursion market maker derived from [TR4-io/ALGO2e-rebuilt](https://github.com/TR4-io/ALGO2e-rebuilt), source commit `faafa32`. It seeks net trading profit from passive executions at several distances around an estimated fair price. It trades **RY only**; CNR and AC remain included in account reconciliation and shared risk checks.

This is a new strategy, not a demonstrated profit-maximizing calibration. The competitive screenshots motivated testing an excursion ladder; historical candle extremes are not executable quotes and cannot establish fill probability or expected profit. The supplied one-pager's “Building it” section was excluded. This implementation retains the rebuilt repository's **RIT Client REST connection**, not DMA.

## Start on the RIT computer

Python 3.10+; enable API orders in RIT, load ALGO2e, start flat with no open orders, and use this bot as the account's only writer. Do not run the rebuilt bot alongside it. Restart this process for every round.

```sh
python -m pip install -r requirements.txt
python strategy.py --check
python strategy.py
```

`--check` performs read-only checks; it never sends or cancels orders. The last command trades in the connected simulation. In Spyder, open this repository's `strategy.py`, set its working directory to this folder, and run it. Put `--check` in the run configuration for a read-only check.

Defaults: `http://localhost:9999/v1`, API key `Rotman`. Set `RIT_BASE_URL` and `RIT_API_KEY` in your local environment if needed. Never commit credentials. The client paces total requests to at most 40/second, honors HTTP 429 waits, and does not replay uncertain submissions.

## Ladder

| Level | Distance below/above adjusted fair price | Shares on each side when flat |
|---|---:|---:|
| 1 | 3 cents | 1,500 |
| 2 | 7 cents | 1,000 |
| 3 | 12 cents | 1,000 |
| 4 | 20 cents | 500 |

Distances are **per side**: the outer pair is 40 cents apart when centered symmetrically. At a $100 center and a narrow book, bids start at 99.97 / 99.93 / 99.88 / 99.80 and asks at 100.03 / 100.07 / 100.12 / 100.20. At most 4,000 shares per side are initially reserved. Prices never intentionally cross the observed external book or improve an already wider best price. Duplicate prices are coalesced without aggregating size.

The center uses a depth/microprice and momentum estimate, not the last trade. Inventory shifts the center by up to 3 cents toward flattening. Volatility and observed adverse post-fill movement can widen inner levels up to the outer offset. Book constraints, inventory skew and tick rounding can place quotes farther than 20 cents from the raw midpoint.

RY maker fee: $0.002/share; taker rebate: $0.0014/share, following the case settings inherited from the rebuild. A completed passive buy/sell cycle earns its actual price difference minus $0.004/share. Repricing and unmatched inventory mean the initial displayed width is not guaranteed profit. The bot has no speculative taker leg; marketable limit orders are reserved for liquidation.

## Profit and inventory management

- Continue making after the 100,000-share qualification minimum and 110,000-share reporting target. Do not shrink size just because the target is reached, or narrow quotes solely to manufacture volume. Qualification is not guaranteed.
- Scale down the inventory-adding side as exposure grows. The reducing-side planned quantities cannot collectively exceed current inventory.
- At 3,000 shares of absolute inventory, cancel the adding side and use a nearer passive exit. This is a management trigger, not a guaranteed ceiling: resting orders can fill before cancellation. The hard reservation budget is 6,000 shares per stock, with 18,000 gross/net account budgets (also clamped to 90% of live limits).
- After eight seconds of continuous inventory, use a nearer passive exit for that position rather than waiting only at distant ladder prices. New same-sign fills do not restart the timer.
- A six-cent midpoint move within the rolling two-second window pauses the adverse inventory-adding side for three seconds. Reducing inventory remains allowed. These are starting hypotheses, not fitted parameters.
- Preserve unchanged quotes and partial-order queue position. Recenter a level after a three-cent target change and at least 0.8 seconds of order age; cancel unsafe/crossing orders immediately. After any cancellation, recompute on the next cycle before submitting replacements.
- At a $500 run loss, halve new exposure and prioritize reducing inventory. At a $1,500 run loss, or a $900 drawdown after reaching $500 profit, switch to liquidation. These are decision thresholds, not guaranteed loss caps.
- Stop adding exposure at tick 275; passive reduction until tick 280, then bounded marketable-limit liquidation. Per-order slippage cap is 10 cents from the current best, rising to 30 cents in the last eight seconds. Fresh quotes are required. Missing depth or connectivity can leave residual inventory.
- Ctrl+C once requests liquidation; twice exits with best-effort cancellation. Inspect remaining RIT positions after any abnormal stop. A reset prevents old order IDs from being cancelled in a new round.

Risk reserves every outstanding share independently. Opposite-side orders are not netted against each other. Cancellation and temporarily missing order histories receive bounded confirmation retries. An acknowledgement or a missing order is never treated as proof of a fill/cancellation. Unknown accepted writes stop the bot and trigger best-effort account cleanup, rather than being resubmitted.

## Configuration and evidence

```sh
python strategy.py --config config.example.json
python analyze_run.py logs/run-YOUR-RUN.jsonl
python -m unittest discover -s tests -v
python stress_check.py
```

The example includes all defaults; JSON can override only selected fields. `offsets` and `sizes` must have matching lengths. The ladder's total per-side size cannot exceed the stock budget. Keep size fixed when comparing quote offsets so profitability changes can be interpreted.

Logs record external depth (up to 30 levels each side), signals, submissions, confirmed fills, ladder level, cancellation reason, sampled P&L/inventory, and final account P&L change. The analyzer reports fill volume by side/level, sampled drawdown/inventory and post-detection price drift. A level `-1` is a near-market inventory exit. Volume is buys plus sells, not completed matched cycles. Fill intent is not proof of maker/taker classification; execution prices are recorded only if valid cumulative VWAP is available. Level-specific realized P&L is not fabricated from order limit prices. The official RIT result remains authoritative.

Tests and `stress_check.py` run entirely offline. Synthetic flow explicitly consumes order quantity; it does not assume both sides fill because a candle crossed their prices. Its competing depth and order arrivals are stylized and do not estimate competition P&L or qualification probability. See `VALIDATION.md` for tested behaviors and remaining limits.
