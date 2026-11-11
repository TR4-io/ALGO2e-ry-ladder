# ALGO2e RY ladder — continuous-v4

RY-only market making for the RIT Client REST API. This version rebuilds the execution loop around bounded cycles, independent replenishment and confirmed order accounting. This update increases near-level allocation and child size, strengthens inventory price skew, and measures fill quality against execution prices. The 25,000-share stock/gross/net ceilings remain. It is a simulation strategy, not a proven profit-maximizing calibration.

## Run the complete updated folder

Python 3.10+. Enable API orders in RIT, load ALGO2e, start flat with no open orders, and use one bot on the account.

```sh
python -m pip install -r requirements.txt
python strategy.py --check
python strategy.py
```

`--check` reads only. The last command sends orders to the connected simulation. Defaults are `http://localhost:9999/v1` and API key `Rotman`; override locally with `RIT_BASE_URL` and `RIT_API_KEY`.

**Spyder:** replace the whole repository folder, restart the Python kernel once, then run this folder's `strategy.py`. Script startup refreshes the complete local dependency group before creating the bot, including when invoked with `%runfile`. It also prints the code folder. The expected banner is **`RY ladder continuous-v4: fresh runtime`**. Copying only `strategy.py` can leave incompatible files behind. The heartbeat tolerates missing diagnostics and reports a clear message instead of dying.

## How placement now works

The default runner uses `runtime.py` and `concurrent_client.py`. `engine.py` supplies shared accounting and market helpers; its old cycle is not called. `fast_engine.py` is retained only for historical regression tests. `--serial` runs the **same new runtime** with one worker.

1. Fetch case, open orders and positions concurrently. Confirm cumulative fills. When known orders disappear, fetch the two terminal-history lists once for the entire group; at most four rotating detail requests cover truncated history. A position mismatch refreshes both orders and positions, so a fill racing the first snapshot can resolve in the same cycle. There are no repeated per-order sleep/poll loops inside the cycle.
2. Fetch the RY book **after reconciliation**. Freshness starts at actual HTTP dispatch, excluding time spent waiting for request capacity. A genuinely slow response still cannot authorize a stale quote.
3. Recompute the ladder and request up to eight necessary cancellations. Acknowledgements do not release quantity or slots. Confirm cancellations/fills in subsequent normal cycles.
4. Replenish up to eight eligible child slots, inner levels first and alternating sides. A pending cancellation keeps its own slot and risk reservation; it does not impose a global stop on unrelated slots. All independent combinations of outstanding fills are reserved before dispatch.
5. Pace the next cycle to leave capacity for writes. At the default 60 total requests/second and eight-order submission batch, the minimum target cycle interval is 0.20 seconds, including work already done. Extra I/O, rate limits or server latency can make it longer.

Four independent keep-alive sessions provide bounded concurrent I/O. Workers never mutate the ledger. New order submissions have freshness/end-of-case deadlines checked immediately before dispatch. Successful siblings are accounted for even when another request fails. An uncertain **order submission** stops new orders and is never replayed. An uncertain **cancellation** stays pending for confirmation; it does not permanently disable submissions by itself.

HTTP 429 cooldown is shared, with one 20% cap reduction per cooldown and gradual recovery after successful traffic without further rate limits. No server capacity is assumed: instructors can control API submission speed ([RIT overview](https://rotman.utoronto.ca/faculty-and-research/education-labs/bmo-financial-group-finance-research-and-trading-lab/rit-market-simulator/overview--features/)).

## Ladder and quantity

| Base distance on each side of adjusted fair | Total shares per side at this level | Children per side |
|---|---:|---:|
| 2 cents | 6,500 | 6 × 1,000 + 500 |
| 4 cents | 4,000 | 4 × 1,000 |
| 8 cents | 1,500 | 1,000 + 500 |
| 15 cents | 500 | 1 × 500 |

Up to **28 resting orders**, totaling **12,500 shares per side** from flat, are staged over several cycles. These are per-side distances: a symmetric 2-cent bid/ask pair is four cents wide. Splitting orders does not increase aggregate quantity, improve queue priority or create counterparties.

Fully filled children replenish independently; partial children retain their remaining quantity and queue position. Child IDs within each level remain stable even as available capacity changes. Prices preserve a fee-adjusted minimum edge and stay passive against the external book. Inventory skew, volatility, adverse-fill penalties and duplicate-price filtering can change prices or reduce the number of eligible slots. The nearest quote is moved to the edge floor rather than dropped solely because inventory skew moved it too close to fair.

Ordinary repricing waits at least **1.2 seconds** after order creation and requires a **3-cent** target move, preserving queue time through small changes. Crossed quotes, insufficient edge and obsolete/oversized slots can still be cancelled immediately. Filled slots replenish without this repricing delay. Inventory price skew reaches **5 cents** at the 25,000-share ceiling; it moves the quote center without introducing a trading pause.

Adaptive widening now uses the signed difference between execution price and the midpoint one second after fill detection, less the assumed maker fee. Negative midpoint drift alone no longer penalizes a still-profitable fill. Each side needs 5,000 observed shares before applying the penalty, which remains capped at 3 cents; learning is weighted by share volume. Missing execution prices are excluded, and per-fill liquidity role is not known, so the fee assumption is recorded explicitly. This is a markout measure, not realized round-trip profit.

RY maker fee is $0.002/share and taker rebate is $0.0014/share under the supplied case settings. A completed passive buy/sell cycle subtracts $0.004/share in maker fees from its actual price difference. The screenshots motivated the strategy; candle highs/lows do not establish executable fills.

From tick 50 onward, fewer than 1,000 confirmed RY shares over the trailing 20 ticks triggers a one-time switch to **2/3/5/8-cent offsets** with unchanged allocations. The 20-tick interval is a measurement window, not a delay between submissions. Old children transition through confirmed cancellations at their own slots; independent empty slots remain eligible.

## Limits and exits

- Stock, gross and net budgets are 25,000 shares, clamped to any lower live limits. Each individual order is at most 5,000 shares. Pending cancellations remain fully reserved. At the hard position ceiling, additional inventory-increasing orders must wait for capacity to be freed.
- No default wind-down at ticks 275/280 and no default 15,000-share inventory brake, age-based exit or inventory-based quantity reduction. `scheduled_closeout` and `inventory_throttle` remain optional. Price skew still reflects inventory.
- **No trend-triggered pauses:** rises, drops and reversals never disable a quote side or start a re-entry timer. The old `trend_window`, `trend_move` and `trend_pause` settings are accepted for compatibility but ignored. Inventory skew and volatility may still change prices; fee, hard-capacity and stale-data checks remain active.
- **No P&L risk stop or loss-based size reduction.** Losses and profit drawdowns never latch `EXIT`, stop quoting or shrink the ladder. The legacy `soft_loss`, `hard_loss`, `drawdown` and `profit_arm` fields are accepted but ignored, including in older JSON configurations. P&L and peak P&L are telemetry only.
- Ctrl+C once requests liquidation; twice exits immediately with best-effort cleanup. Case end stops new orders and cancels outstanding orders. Without scheduled liquidation, residual inventory can remain at close.
- A reset prevents cancellation of old IDs in a new round. Persistent reconciliation discrepancies trigger automatic account recovery, described below. Fills are never inferred from a missing order or cancellation acknowledgement. Unknown order-submission outcomes and untracked orders still stop submission to avoid duplicate or unreserved exposure.

## Automatic reconciliation recovery

The account must have one writer. Ordinary snapshot races retry both order and position data. If a discrepancy remains for `reconcile_timeout` (default one second), the bot switches to recovery instead of throwing repeated errors and exiting. Manual exit can enter recovery immediately.

Recovery submits a bulk cancellation for this bot's known outstanding order IDs, using the documented `ids` parameter and `cancelled_order_ids` response ([RIT API reference](https://rit.306w.ca/RIT-DMA-API/1.0.4/)). It then reads **open orders, positions, open orders**, in that order, and rechecks the case. Two matching position observations on separate cycles at least 0.2 seconds apart, each with both open-order reads empty, establish a quiescent account. Positions must also lie inside the previously reserved fill bounds. Delayed cancellations and changing positions keep recovery active; no replacement is sent on an acknowledgement alone. A lost cancellation response is retried, never treated as an uncertain order submission.

After verification, the bot adopts the actual exchange positions, retires unresolved old slots, and automatically rebuilds the ladder. It does not require a flat position or a manual rerun. Manual liquidation can proceed from the same verified position. Missing historical records remain explicit `history_gaps`; confirmed volume may temporarily undercount actual trading. Late terminal records update confirmed volume once without adding the same inventory a second time. The run log records previous/new positions and unresolved IDs. Recovery does **not** reset P&L, volume, or an operator's stop request.

Recovery briefly withholds new orders while the account is uncertain. It cannot guarantee progress if the API keeps reporting contradictory positions or orders that never cancel; these conditions remain visible in diagnostics. Case limits, data validity, operator stop and case end remain enforced.

## Read the live output

`LIVE` lines appear every wall-clock second after completed cycles, even if simulation ticks do not advance. They show actual accepted orders per second (`sent/s`), confirmed filled shares per second, open-order count, position, cycle duration and the latest reason. The live line also reports missing-order count, recovery state and historical gaps. JSONL cycle records include exact missing IDs and server/ledger position differences. A separate five-second heartbeat reports the current operation during slow I/O.

| Reason | Meaning |
|---|---|
| `quotes_resting` | Desired quotes are present; no replacement is needed. Zero new submissions is normal here. |
| `submitted` | At least one submission batch completed; it does not imply a fill. |
| `queued_quotes_expired` / `decision_expired` | The decision became stale before dispatch. Inspect cycle time and effective request cap. |
| `reconciliation_pending` | Order/position snapshots disagree; both are retried, followed by automatic recovery if persistent. |
| `recovery_verifying` / `recovery_cancel_pending` | Checking a stable position and confirmed absence of outstanding orders before resuming. |
| `recovery_cancel_retry` | A cancellation response was lost; retrying without replaying any order submission. |
| `recovery_position_outside_reservations` | Position cannot be explained by known outstanding orders; no position is guessed. |
| `awaiting_cancel_confirmation` | Pending cancellations still own their slots. |
| `capacity_or_self_cross` | Remaining share capacity or an existing opposite quote blocks the proposed orders. |
| `stale_or_invalid_book` / `wide_spread` | Market data cannot authorize these quotes. |
| `case_inactive` | The server reports an inactive case, or the configured round end was reached. |
| `exit_cancel_pending` / `exit_waiting_for_depth` | The bot is in an explicit liquidation phase. |

`gross=0` or `RY=0` means no held inventory, not a zero configured limit. Resting orders can exist while the account is flat. A market maker manages quotes continuously; it cannot guarantee a transaction every second.

## Configuration and verification

```sh
python strategy.py --config config.example.json
python strategy.py --serial
python analyze_run.py logs/run-YOUR-RUN.jsonl
python -m unittest discover -s tests -q
python stress_check.py
python benchmark_execution.py
```

`child_order_size` defaults to 1,000. `sizes` are aggregate level allocations, so the 6,500-share nearest level is split across individual orders even with a 5,000-share child setting. `max_submit_batch` and `max_cancel_batch` default to eight. Child plans are limited to 100 orders across both sides. `request_cap` defaults to 60 and `io_workers` to four. `reconcile_timeout` defaults to one elapsed second before automatic recovery, `cancel_retry_seconds` to one, and `telemetry_seconds` to one.

### Compare candidates reproducibly

The default is the complete v4 candidate. Full configuration snapshots in `profiles/` isolate incremental changes:

| Profile | Change from the previous row |
|---|---|
| `baseline-v3.json` | Prior sizing, 500-share children, 3-cent skew, drift-based widening, 0.8-second repricing |
| `a-children.json` | 1,000-share children |
| `b-skew.json` | 5-cent inventory skew |
| `c-allocation.json` | 6,500 / 4,000 / 1,500 / 500 shares per side |
| `d-markouts.json` | Execution-price markouts for adaptive widening |
| `e-queue.json` | 1.2-second ordinary repricing; matches v4 defaults |

For example: `python strategy.py --config profiles/a-children.json`. An existing custom JSON overrides defaults; use the updated example or these profiles when comparing settings. The baseline profile reproduces prior policy settings on the current execution engine, not an identical replay of the old binary.

`analyze_run.py` now reports `quality`: fill/submission shares by level, actual midpoint distances, cancellation causes and ages, execution-price markouts with observed-volume coverage, and sampled time near the inventory ceiling. Existing fields remain compatible. New logs identify the order and level for each markout; older logs cannot support that attribution. Final records distinguish confirmed accounting/cleanup from an ended log. A 401/403 is treated as a case-boundary event only after a fresh same-round case read verifies termination; an unverified or active-case authentication failure still ends submission.

Select settings using several comparable competitive runs and official final P&L, alongside inventory, drawdown and execution-quality results. Fewer requests and more near-level shares are candidate improvements; this update does not establish a profit-optimal size or width.

The benchmark uses the real paced worker transport with an in-memory exchange and artificial latency; it never contacts RIT. Fill events are injected to test replenishment. Stress tests validate mechanics and accounting, not profitability. See [REVIEW.md](REVIEW.md) and [VALIDATION.md](VALIDATION.md) for review findings and measured results.

The strategy originated from [ALGO2e-rebuilt](https://github.com/TR4-io/ALGO2e-rebuilt), source commit `faafa32`. The supplied one-pager's “Building it” section was excluded.
