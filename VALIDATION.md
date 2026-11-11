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

## Execution stall recovery and diagnostics

All 81 tests passed. New regressions reproduce reconciliation aging a prefetched book past its expiry and verify that a fresh book permits quoting; a fresh request that is itself too slow still blocks submissions. Tests also cover sibling 429 deduplication, bounded automatic rate recovery, actual-zero positions with 40,000 shares of resting orders, explicit drawdown stop reasons, and wall-clock heartbeat reporting without API calls or tick progress.

The changes address reproducible code-level stall paths. No practice-run console transcript or JSONL recording was available to confirm which path caused the reported live symptom. No live RIT orders were placed during this work.

## Inner-weighted ladder and independent replenishment

Defaults now quote per-side offsets 2/4/8/15 cents with sizes 5,000/4,000/2,500/1,000, and a low-fill fallback of 2/3/5/8 cents with the same sizes. When inventory skew would invalidate the near quote's fee-adjusted edge, policy moves its price to the minimum edge floor instead of dropping the level. Passive pricing and risk limits still apply.

All 84 tests passed. New regressions verify repeated same-side full-fill replenishment in both serial and concurrent engines while opposite orders retain their original IDs, continued replenishment until the 25,000-share capacity ceiling, and preservation of the nearest opposite quote with the required fee margin. Existing all-5,000-share tests explicitly retain that supported configuration.

All 25 synthetic stress rounds completed with maximum sampled absolute inventory of 25,000 shares; 4 triggered loss stops and 21 ended with residual inventory. These mechanical checks do not establish live fill rates or profit improvement. No live RIT trades were run.

## Fifty resting child orders and no-order diagnostics

The default 500-share child size divides the existing 12,500-share-per-side ladder into up to 25 orders per side. Submission is capped at eight orders per cycle, prioritizes inner levels, and alternates sides. Total price-level allocations, request cap and position limits are unchanged. A 5,000-share child size retains the legacy single-order-per-level mode.

All 92 tests passed. New coverage establishes all 50 resting orders in both engines, verifies the eight-order batch bound and balanced first batch, replaces individual filled children without cancelling siblings, preserves partially filled children, checks exact split remainders, confirms fills racing with cancellation of all 50 orders, and exercises independent-fill risk through the 25,000-share ceiling. Historical single-order tests now explicitly request the supported 5,000-share child mode; full-round integration exercises the new defaults. Heartbeat snapshots report no-order decisions including queued expiry, stale decision expiry and inactive cases.

All 25 synthetic stress rounds completed with sampled absolute inventory at most 25,000; 4 triggered loss stops and 21 finished with residual inventory. No live RIT latency, fills or profitability were validated. The reported heartbeat-with-no-orders incident remains unconfirmed without the user's actual console line or log; increased order count is not claimed as its root-cause fix.

## Continuous-v2 runtime rebuild

The default CLI now runs `runtime.py`, replacing the former default's step/reconcile/cancel/place lifecycle. It retains shared fill accounting and quote policy helpers; the old runtime paths remain solely for regression coverage. `--serial` selects one worker for the same new runtime. See `REVIEW.md` for findings and regression evidence.

The complete **115-test suite passed**, including the added regressions for known rejection with accepted siblings, interruption after accepted submissions, non-monotonic order snapshots, live-script module refresh, timed request dispatch and uncertain cancellation recovery. `python3 -m compileall -q .` and `git diff --check` also passed.

All 25 continuous-v2 synthetic stress rounds completed, with sampled absolute inventory at most 25,000, 5 configured loss-stop rounds, and 19 rounds ending with residual inventory. Default scheduled liquidation remains disabled. Final confirmed fills/positions agree with the synthetic exchange. Full results are in `validation/continuous-v2-stress.json`; these are uncalibrated mechanics tests, not performance forecasts.

The offline real-worker transport benchmark (8 seconds per setting) produced:

| Artificial response latency | Request cap | Build 50 resting orders | Tested replenishments | Slowest tested refill |
|---|---:|---:|---:|---:|
| 20 ms | 60/s | 1.393 s | 6 | 0.103 s |
| 50 ms | 20/s | 4.391 s | 5 | 0.666 s |

Both runs stayed within their rolling-second request budgets and cleaned up all orders. Fills were injected; results do not imply actual RIT fills every second. No live RIT orders or latency measurements were performed.

## Remove trend-triggered pausing

Removed the midpoint-move side blocks and their re-entry timers from both continuous-v2 and legacy management. Rises, falls and reversals no longer suppress either side. Legacy trend settings remain accepted but are ignored, including positive pause values supplied by old JSON files. Independent replenishment, live risk ceilings, data/fee checks and explicit loss stops are unchanged.

New regression tests exercise repeated fast reversals with both sides eligible while flat, verify that neither management path supplies trend-blocked sides, and retain hard-limit enforcement. The old regression requiring buys to pause was updated to require both sides instead.

All 118 tests passed after this change; compilation and diff checks passed. No live RIT orders were submitted during validation.


## Continuous-v3 automatic recovery and removal of P&L stops

The default runtime now refreshes both order and position snapshots after a mismatch. Missing history triggers automatic cancellation and verified position recovery after one second by default, rather than a permanent stop. Recovery requires two stable position observations with ordered empty-order reads and a same-round check; adopted positions must lie within outstanding-fill reservations. Historical fills remain unknown until confirmed, and late history never changes the adopted position twice. Per-order fallback lookups rotate across missing IDs. Manual exit uses the same recovery path.

Hard-loss stops, profit-drawdown stops and P&L-based size reductions were removed from the current and legacy engines. Old JSON risk threshold fields remain accepted but are ignored. Hard share limits and operator/case termination remain active.

- `python3 -m unittest discover -s tests -q`: **131 tests passed**. Thirteen new recovery regressions cover missing history with flat/nonzero inventory, late fills arriving after new fills, cancellation races, snapshot races, rotating detail requests, delayed/lost cancellation responses, manual exit, case end/reset, foreign orders, the 25,000-share ceiling, and P&L-stop removal. Existing loss-stop tests now assert continued quoting or explicitly requested liquidation.
- Targeted recovery/telemetry regressions passed again after the final diagnostic refinements. Compilation and diff checks passed.
- All **25 synthetic stress rounds** completed with sampled absolute inventory at most **25,000**, **0 automatic loss stops**, and **24 rounds with residual inventory** at case end. Scheduled liquidation remains disabled by default. Confirmed accounting matched the synthetic exchange in these rounds. Results: `validation/continuous-v3-stress.json`.
- Offline paced-worker benchmark: at 60 requests/second and 20 ms artificial response latency, 50 orders were established in 1.389 seconds; six injected fills replenished with a maximum tested delay of 0.103 seconds. At 20 requests/second and 50 ms latency, 50 orders took 4.398 seconds and maximum tested refill delay was 0.702 seconds. Both respected the rolling-second request cap. Results: `validation/continuous-v3-benchmark.json`.

No live RIT connection or trading was used. These results verify execution mechanics, not live profitability, fill frequency, or guaranteed recovery from a persistently contradictory/unavailable server. The supplied status line confirmed `EXIT` plus `reconciliation_pending`; without its run log the exact missing server record remains unknown.

## Continuous-v4 candidate profitability settings

The default candidate retains the continuous-v3 recovery engine and 25,000-share limits. Level allocations are 6,500/4,000/1,500/500 shares per side, with 1,000-share children (28 orders at distinct prices), 5-cent maximum inventory skew, and 1.2-second ordinary repricing. Base offsets remain 2/4/8/15 cents. Immediate cancellation conditions and independent replenishment remain active. No P&L stops or trend pauses were added.

Adaptive widening now measures execution-price markouts under an explicit passive-fee assumption, with a 5,000-share per-side warmup and share-weighted updates. Negative midpoint drift alone no longer widens an otherwise profitable fill. New telemetry identifies markout order/level, actual submission distance, and cancellation causes. The offline analyzer reports markout coverage and sampled inventory duration; older logs retain aggregate attribution only. Cleanup records available exchange positions even if terminal history fails. A boundary 401/403 requires a fresh same-case end confirmation; active/unverified failures remain fatal to submission.

- **143 tests passed**, including 12 new candidate-setting, replenishment, recovery, markout, queue-preservation, profile, boundary-error and analyzer regressions. Historical child-order lifecycle fixtures retain explicit prior sizing; full-round runtime tests use current defaults.
- The supplied continuous-v3 JSONL log still yields 1,339,000 confirmed RY shares and 598,300 BUY level-0 shares. Its one-second execution-price markouts are positive on both sides, despite negative midpoint drift. The new analyzer does not fabricate per-level markouts for this older log, which lacks markout order IDs.
- The offline real-worker benchmark built 28 resting orders in 0.799 seconds at 60 requests/second with 20 ms artificial latency; six injected fills replenished with maximum delay 0.139 seconds. At 20 requests/second and 50 ms latency, the ladder took 2.486 seconds and five injected fills replenished within 0.455 seconds. Both respected rolling request caps and cancelled all remaining orders. Results: `validation/continuous-v4-benchmark.json`.
- Compilation and whitespace/diff checks passed. No live RIT connection, live orders, or claims of higher live P&L are part of this validation.

Full snapshot profiles isolate each change on the current execution engine. They support comparison across competitive runs; these settings are candidates, not an empirically established profit optimum. Final official P&L should be evaluated alongside inventory, drawdown, fees and execution quality. An `end` event means the log terminated; `accounting_confirmed` and `cleanup_confirmed` separately report whether the account was fully verified.

All 25 additional continuous-v4 synthetic stress rounds completed with confirmed accounting matching the exchange, maximum sampled absolute inventory 25,000, 0 automatic loss stops, and 25 rounds with residual inventory at case end. Scheduled liquidation remains disabled. Results: `validation/continuous-v4-stress.json`. These artificial order-flow scenarios validate mechanics, not future profitability.

## Continuous-v4.1 run-boundary diagnostics and analyzer corrections

Run 3 exposed an unknown POST outcome at tick 299 followed by a reset, while the analyzer marked the presence of an `end` record as completion. The analyzer now separates `log_closed` from verified `completed` and reports explicit completion states. Older logs without cleanup confirmation remain unverified. Current logs retain each uncertain submission's attempted side, quantity, price and slot. After draining successful batch siblings, the CLI performs one read-only case observation to distinguish a reset/end from an active or unavailable case. Unknown submissions are never replayed or declared resolved by that observation; cleanup confirmation remains false while the outcome is unknown. A detected reset prevents cleanup writes into the new round.

Cancellation telemetry now includes terminal status; analysis separates confirmed cancellations, fills racing cancellation, and historical unknown status. Activity metrics report actual first ticks, observed tick/wall-clock durations, confirmed shares per submission, fill gaps, and reconciliation waits. Per-tick throughput excludes fills outside the sampled interval. This patch changes no sizing, quoting, repricing, request caps, P&L stops or continuous-trading settings; it cannot prevent an HTTP 500 generated by RIT itself.

- 152 tests passed, including nine new tests for uncertain batch siblings, case end/reset/active/unavailable classifications, no replay, no new-case cleanup writes, CLI failure handling, verified completion, late-start interval accounting, and cancellation/fill races. Targeted boundary tests also passed after the interval-rate refinement.
- Supplied run 3 re-analysis preserves 2,068,100 confirmed shares and tick-17 first submission; it now reports `log_closed=true`, `completed=false`, and `case_reset_unverified`. Supplied run 2 retains 1,339,000 confirmed shares and an unclosed log. No official settlement results are inferred from either runtime log.
- Compilation and diff checks passed. All validation was offline; no live RIT orders or connections were used. No additional profit improvement is claimed.
