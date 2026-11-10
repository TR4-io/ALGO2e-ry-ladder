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

Defaults: `http://localhost:9999/v1`, API key `Rotman`. Set `RIT_BASE_URL` and `RIT_API_KEY` in your local environment if needed. Never commit credentials. The default concurrent client uses a 60 requests/second global budget with four I/O workers, shared HTTP 429 backoff, and no replay of uncertain submissions. These are configurable client settings, not a verified server allowance.

## Concurrent execution

The default runner uses `fast_engine.py` with `concurrent_client.py`. It is a low-latency REST execution engine for this simulator, not a colocated exchange HFT system. The ladder and risk limits stay the same.

- Fetch case, open orders, positions/P&L and the RY book concurrently. Snapshots are not atomic; position discrepancies still trigger reconciliation, and old books cannot authorize orders.
- Dispatch up to four HTTP requests concurrently through separate keep-alive sessions. A complete ladder can be dispatched as one client-side batch, with every order's worst-case exposure reserved before the first submission. There is no claimed server batch-order endpoint.
- Send selected cancellations together, then confirm their terminal states before replacing. A slow cancellation does not postpone dispatch of the other selected cancellations. Unknown or unconfirmed orders remain unsafe to replace.
- Process every batch response on the main thread, including successful siblings of a failed order. No worker mutates the ledger. An uncertain write stops new exposure; it is never automatically replayed.
- Enforce book-age and case-cutoff deadlines immediately before actual dispatch, including requests delayed by the rate budget. Unsent expired requests are discarded. New order batches stop dispatching when a graceful stop is requested.
- Target a 20 ms minimum cycle period, subtracting time already spent working. This is not a promised 50 Hz loop: request limits, server latency, reconciliation and logging determine actual speed. There is no extra 100 ms sleep after every cycle.
- Default budget: 60 total requests per rolling second, with a small four-request burst and smoothing between bursts. Every read, submit and cancel consumes budget. A 429 shares its cooldown across workers and reduces the rate by 20% once per cooldown, rather than once per rejected sibling request. After ten seconds without another rate-limit response, successful requests allow recovery by one request/second every ten seconds, capped at the configured rate. Adjust `request_cap` and `io_workers` in JSON only for the actual environment.
- Every five seconds, log cycle time, rolling request p50/p95 and rate-queue p95; inspect them with `analyze_run.py`. Book/timing logs buffer briefly; execution and risk events still flush immediately.

The official [RIT Client API specification](https://rit.306w.ca/RIT-REST-API/1.0.3/swagger.yaml) documents global/security-specific rate limits and `429`/`Retry-After`, but does not give a universally valid requests-per-second figure. The supplied DMA rate observation is not assumed to be a Client API guarantee.

```sh
python strategy.py --serial       # original sequential execution path for comparison
python benchmark_latency.py       # offline artificial-latency comparison, no RIT calls
```

Simultaneous submissions can fill before the next inventory-management decision. Aggregate reservations enforce the hard exposure budget; inventory-triggered throttling is disabled by default. Keep this process as the account's only writer.

## Ladder

| Level | Distance below/above adjusted fair price | Shares on each side when flat |
|---|---:|---:|
| 1 | 2 cents | 5,000 |
| 2 | 4 cents | 4,000 |
| 3 | 8 cents | 2,500 |
| 4 | 15 cents | 1,000 |

Distances are **per side**. At a $100 adjusted center and a narrow book, bids start at 99.98 / 99.96 / 99.92 / 99.85 and asks at 100.02 / 100.04 / 100.08 / 100.15. Initial reservations total 12,500 shares per side: 25,000 outstanding across both sides, with independent fills producing inventory between -12,500 and +12,500 from flat. The 25,000-share inventory ceiling remains unchanged. Smaller outer orders leave room for repeated inner fills and replenishment. Live capacity can reduce orders. Prices stay passive against the external book; duplicate prices are coalesced without aggregating size.

The center uses a depth/microprice and momentum estimate, not the last trade. Inventory shifts the center by up to 3 cents toward flattening, without reducing order quantities merely because inventory increased. Volatility and observed adverse post-fill movement can widen inner levels up to the outer offset. Book constraints, inventory skew and tick rounding can place quotes farther than 15 cents from the raw midpoint.

RY maker fee: $0.002/share; taker rebate: $0.0014/share, following the case settings inherited from the rebuild. A completed passive buy/sell cycle earns its actual price difference minus $0.004/share. Repricing and unmatched inventory mean the initial displayed width is not guaranteed profit. The bot has no speculative taker leg; marketable limit orders are reserved for liquidation.

## Low-fill fallback after tick 50

Starting at simulation tick 50, check confirmed RY fill volume over the trailing 20 ticks. If fewer than 1,000 shares filled (buys plus sells), switch once to **±2 / ±3 / ±5 / ±8 cents** for the rest of that run. Sizes remain 5,000 / 4,000 / 2,500 / 1,000 shares per side. Monitor throughout the remaining entry period, so a later drought also triggers the fallback. A late-started process first observes a complete 20-tick window. Exactly 1,000 shares meets the threshold and does not trigger.

Cancel and confirm all existing RY quotes before posting the new ladder; cancellation failures keep the old shares reserved. Hard position limits, trend pauses, fee margin, passive-price checks and volatility widening still apply, so the displayed distances can differ from base offsets. The switch is printed and logged as `ladder_tightened`. Configure `tight_offsets`, `tighten_after`, `fill_window`, and `min_window_fills` in JSON. Custom level counts require matching original offsets, tight offsets and sizes.

## Profit and inventory management

- Continue making after the 100,000-share qualification minimum and 110,000-share reporting target. Do not shrink size just because the target is reached, or bypass fee/risk checks to manufacture volume. The explicit low-fill fallback above can tighten base offsets. Qualification is not guaranteed.
- Keep full configured clip sizes as inventory grows, subject to remaining hard-limit capacity. Opposite-side orders can reverse inventory, with every independent combination of outstanding fills reserved against the limits.
- Stock, gross and net budgets are **25,000 shares**, clamped to the actual live limits with no automatic 10% haircut. Initial four-level ladders post at most 12,500 shares per side when flat. At +15,000 shares, for example, another 10,000 buys can be reserved while up to 12,500 sells remain possible. At +25,000, additional buys are blocked by the hard limit; sells remain possible.
- `inventory_throttle=false` disables the old 15,000-share brake, inventory-proportional size reductions, eight-second inventory-age exits and the rule that the opposite ladder could only flatten rather than reverse. Inventory-aware price skew and all hard risk reservations remain. Set this flag to `true` only to restore those optional controls.
- A six-cent midpoint move within the rolling two-second window pauses the adverse inventory-adding side for three seconds. Reducing inventory remains allowed. These are starting hypotheses, not fitted parameters.
- Preserve unchanged quotes and partial-order queue position. Recenter a level after a three-cent target change and at least 0.8 seconds of order age; cancel unsafe/crossing orders immediately. After any cancellation, recompute on the next cycle before submitting replacements.
- At a $500 run loss, halve new exposure and prioritize reducing inventory. At a $1,500 run loss, or a $900 drawdown after reaching $500 profit, switch to liquidation. These are decision thresholds, not guaranteed loss caps.
- `scheduled_closeout=false` keeps normal quoting throughout the active case, including after ticks 275 and 280. The server ending the case or tick 300 stops new orders and cancels outstanding orders; there is **no scheduled pre-close liquidation**, so residual inventory at case close is expected to be possible. The bot reports it for inspection. Set this flag to `true` to restore passive reduction at 275 and liquidation at 280. Loss-stop or manual-stop liquidation still uses marketable limits capped at 10 cents from the current best, rising to 30 cents in the final eight seconds; fresh quotes and sufficient depth are required.
- Ctrl+C once requests liquidation; twice exits with best-effort cancellation. Inspect remaining RIT positions after any abnormal stop. A reset prevents old order IDs from being cancelled in a new round.

Risk reserves every outstanding share independently. Opposite-side orders are not netted against each other. Cancellation and temporarily missing order histories receive bounded confirmation retries. An acknowledgement or a missing order is never treated as proof of a fill/cancellation. Unknown accepted writes stop the bot and trigger best-effort account cleanup, rather than being resubmitted.

Sizing is concentrated at the inner levels. Full fills free their individual ladder slots for replacement on the next eligible cycle, without requiring an opposite-side fill. Partial orders keep their remaining quantity and queue position; they are not cancelled merely to top up. Cancellation races require confirmation and a fresh decision before replacements. At the hard inventory ceiling, only orders that fit the remaining capacity can be submitted.

When inventory skew would put the near quote inside the fee-adjusted minimum edge, that quote is moved back to the edge floor instead of being dropped, preserving the closest eligible level. Fees, passive pricing, trend/data checks and loss controls remain enforced. These settings aim for more accessible fills; continuous fills and higher profits are not guaranteed.

## Configuration and evidence

```sh
python strategy.py --config config.example.json
python analyze_run.py logs/run-YOUR-RUN.jsonl
python -m unittest discover -s tests -v
python stress_check.py
```

The example includes all defaults; JSON can override only selected fields. `offsets` and `sizes` must have matching lengths. The ladder's total per-side size cannot exceed the stock budget. Keep size fixed when comparing quote offsets so profitability changes can be interpreted.

Logs record external depth (up to 30 levels each side), signals, submissions, confirmed fills, ladder level, cancellation reason, sampled P&L/inventory, and final account P&L change (which can include unrealized P&L on residual inventory). The analyzer reports fill volume by side/level, sampled drawdown/inventory and post-detection price drift. A level `-1` is a near-market inventory exit. Volume is buys plus sells, not completed matched cycles. Fill intent is not proof of maker/taker classification; execution prices are recorded only if valid cumulative VWAP is available. Level-specific realized P&L is not fabricated from order limit prices. The official RIT result remains authoritative.

Tests and `stress_check.py` run entirely offline. The stress runner exercises the concurrent engine with a deterministic exchange adapter; separate tests verify real worker overlap and request pacing. Synthetic flow explicitly consumes order quantity; it does not assume both sides fill because a candle crossed their prices. Its competing depth and order arrivals are stylized and do not estimate competition P&L or qualification probability. See `VALIDATION.md` for tested behaviors and remaining limits.

## Diagnosing quiet practice runs

The console prints a wall-clock heartbeat every five seconds, independently of simulation-tick progress. A heartbeat stuck at `reading/reconciling/quoting` with increasing elapsed time indicates a slow cycle; a frozen tick can also mean the simulation is paused. The startup line says `execution diagnostics enabled` and prints the run-log path.

Status lines separate actual RY inventory, actual gross/net positions and their ceilings, and remaining open buy/sell quantities. For example, `gross=0/25,000 net=0/25,000 open=8 buy=12,500 sell=12,500` means the bot is flat with a resting two-sided ladder. The 25,000-share setting is a ceiling, not a target holding. No fills despite resting orders can reflect wide prices or queue position; fast submission cannot guarantee executions. The 20-tick fill window is an observation window, not a wait between batches.

Expired prefetched books are refreshed once after reconciliation; a fresh request that itself takes too long still cannot authorize a stale quote. `RISK STOP` identifies a hard-loss or profit-drawdown exit, and the log analyzer reports these along with stale/invalid-book blocks and refreshes. Loss thresholds are unchanged. Do not use repeated restarts to reset an intentional loss stop.

For a quiet run, retain the console output and analyze its JSONL log. A `STOPPED` message or `RISK STOP` identifies an exit; heartbeat and execution timing distinguish a running-but-delayed loop. These diagnostics do not automatically resume uncertain writes or guess fills on missing orders.
