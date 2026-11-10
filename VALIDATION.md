# Validation

Local checks performed for the initial implementation:

- `python3 -m unittest discover -s tests -q`: 38 tests passed, including 10 complete synthetic rounds.
- `python3 stress_check.py`: 25 additional synthetic rounds across oscillation, crowded depth, sustained downtrend, sudden downward jump and one-sided closeout scenarios. All ended with zero residual inventory and no open orders; sampled absolute RY inventory reached at most 4,000 shares. Two jump scenarios triggered configured loss/drawdown liquidation. These results describe the fixture only, not likely live outcomes.
- `python3 -m compileall -q .`: passed.
- Read-only startup is tested to make GET requests only. No live RIT orders were placed during development.

Coverage includes:

- Exact default levels, tick rounding, fee margin, volatility widening, passive prices and wider-market handling.
- Aggregate independent-fill reservations and random fill combinations; signed inventory, gross and net limits.
- Inventory brake, age-based passive exits, reducing-side quantities that cannot reverse inventory, adverse-trend pause, unchanged queue preservation and synchronous-fill plan invalidation.
- Continued making after the volume target, entry cutoff, passive reduction and marketable-limit liquidation.
- Partial fills, cumulative VWAP deltas, racing fills during cancellation, delayed OPEN/missing/CANCELLED transitions and bounded unresolved-order failures.
- No retry of unknown writes, rate-limit signaling, stale books, self-cross protection, position snapshot mismatch and round resets.
- Offline log analysis and interrupted log lines.

Important practical limits:

1. RIT REST snapshots are not atomic. Market changes can outrun cancellation and risk-reduction decisions. Loss thresholds cannot guarantee a maximum loss.
2. The 3,000-share brake is not a fill ceiling. Up to 4,000 shares per side are initially resting; worst-case outstanding reservations, not cancellation timing, enforce the hard configured budget.
3. Candle highs/lows do not demonstrate achievable executions. The fixture's price paths, flow, competing depth, queue and fees are artificial. It cannot select optimal ladder widths.
4. Profitable completed cycles must outweigh adverse fills, closeout costs and missed fills. Qualification and competitive P&L are unverified.
5. Compare fixed-size variants on multiple competitive practice rounds, keeping adverse runs. Use final account P&L after flattening, drawdown, matched inventory cycles, qualification and residual exposure. Do not choose parameters solely from these synthetic results.
6. Connection/permission/rate failures, permanently unavailable order history or insufficient exit depth can force a stop with remaining positions. Inspect RIT after an abnormal exit.

## Tick-50 low-fill fallback

After adding the fallback, `python3 -m unittest discover -s tests -q` passed all 46 tests, including the existing 10 full-round integration scenarios. Eight new tests cover activation at tick 50, a complete observation window for late starts, exact-threshold behavior, cumulative-fill deduplication, excluding non-RY fills, one-time switching, confirmation of old cancellations before tighter replacements (including a failed cancellation), preserved passive/inventory controls, and invalid configuration. The earlier 25-round stress results above describe the initial implementation.

## Concurrent execution upgrade

- All 65 tests pass, including five full rounds through the concurrent engine and the ten existing sequential round scenarios. The new tests cover worker overlap, independent sessions, global pacing/burst limits, shared 429 cooldown, queued expiry, full-batch risk reservation, partial failures, lost write responses, sibling accounting, interrupts during scheduling/response waits, cancellation races, resets and dispatch deadlines for liquidation.
- All 25 stress rounds using the concurrent engine completed with no residual inventory or open orders. Sampled absolute RY inventory reached at most 4,000 shares; two jump scenarios triggered configured liquidation. The exchange adapter executes deterministically; separate transport tests use actual worker threads.
- `python3 -m compileall -q .` and `git diff --check` passed.
- No live RIT trading or live latency benchmarking was performed.

`python3 benchmark_latency.py` runs an offline transport benchmark with artificial response delays and no network calls. Median of three isolated batches, four workers, 60 requests/second concurrent budget, versus the old sequential 40 requests/second transport:

| Artificial response delay | Requests | Sequential | Concurrent |
|---|---:|---:|---:|
| 10 ms | 4 | 96.9 ms | 12.5 ms |
| 10 ms | 8 | 203.2 ms | 81.7 ms |
| 50 ms | 4 | 214.2 ms | 55.6 ms |
| 50 ms | 8 | 425.4 ms | 122.2 ms |
| 100 ms | 4 | 416.9 ms | 105.4 ms |
| 100 ms | 8 | 828.3 ms | 205.8 ms |

These measurements use a fresh limiter per batch, exclude strategy work and loop sleeps, and demonstrate concurrency only. They are not throughput guarantees: sustained operations share one request budget, the live server may serialize requests, and 429 responses lower the effective client cap. The 20 ms minimum cycle period is not an achieved 50 Hz rate. Use recorded `execution_timing` events to assess actual latency, queue time and cycle duration in practice.

## Maximum four-level sizing

Defaults now use 5,000 shares at each of four levels per side (20,000 per side when flat), 22,500-share stock/gross/net budgets, a 15,000-share inventory brake and 5,000-share passive exit clips. Loss and drawdown thresholds are unchanged. These replace the smaller settings used for earlier results in this document.

All 68 tests passed. Additional sizing tests enumerate all 256 combinations of full fills across the eight initial orders, verify that lower live gross/net limits clip the concurrent batch, and force a 20,000-share one-sided sweep followed by an adverse move to test complete liquidation. Existing independent-fill, cancellation, concurrency and fallback regressions remain in the suite.

All 25 concurrent-engine synthetic stress rounds ended with zero residual inventory. Maximum sampled absolute inventory was 19,065 shares; 3 rounds triggered configured loss/drawdown stops. The separate one-sided-sweep test explicitly reaches 20,000 shares. Synthetic behavior is not a live liquidity or profitability guarantee.

## Continuous quoting with 25,000-share budgets

Stock, gross and net defaults are now 25,000 shares, clamped to any lower live exchange limit without the former 10% haircut. Scheduled closeout and inventory throttling default to false. The four 5,000-share levels remain active through the active case; hard capacity can still clip or block orders. Inventory price skew, loss stops, trend and data-quality controls remain enabled. These defaults supersede the earlier sizing and closeout settings above.

All 75 tests passed. New tests cover continued full-size quoting at ticks 275, 280 and 299, aged inventory, both signs of 15,000-share inventory, exact 25,000-share capacity, optional restoration of legacy controls, cancellation at case end and accurately reported residual inventory. Existing tests still cover concurrent risk reservations and loss-stop liquidation.

All 25 concurrent synthetic stress rounds completed. Maximum sampled absolute inventory was 25,000 shares. Three loss-stop rounds finished flat; the other 22 rounds finished with residual inventory, as expected without scheduled liquidation. These synthetic results validate mechanics, not competitive profitability. No live RIT trades were run.
