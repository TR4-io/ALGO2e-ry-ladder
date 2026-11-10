# ALGO2e RY ladder — continuous-v2

RY-only market making for the RIT Client REST API. This version rebuilds the execution loop around bounded cycles, independent replenishment and confirmed order accounting. The 25,000-share stock/gross/net ceilings and the weighted ladder remain. It is a simulation strategy, not a proven profit-maximizing calibration.

## Run the complete updated folder

Python 3.10+. Enable API orders in RIT, load ALGO2e, start flat with no open orders, and use one bot on the account.

```sh
python -m pip install -r requirements.txt
python strategy.py --check
python strategy.py
```

`--check` reads only. The last command sends orders to the connected simulation. Defaults are `http://localhost:9999/v1` and API key `Rotman`; override locally with `RIT_BASE_URL` and `RIT_API_KEY`.

**Spyder:** replace the whole repository folder, restart the Python kernel once, then run this folder's `strategy.py`. Script startup refreshes the complete local dependency group before creating the bot, including when invoked with `%runfile`. It also prints the code folder. The expected banner is **`RY ladder continuous-v2: fresh runtime`**. Copying only `strategy.py` can leave incompatible files behind. The heartbeat tolerates missing diagnostics and reports a clear message instead of dying.

## How placement now works

The default runner uses `runtime.py` and `concurrent_client.py`. `engine.py` supplies shared accounting and market helpers; its old cycle is not called. `fast_engine.py` is retained only for historical regression tests. `--serial` runs the **same new runtime** with one worker.

1. Fetch case, open orders and positions concurrently. Confirm cumulative fills. When known orders disappear, fetch the two terminal-history lists once for the entire group; at most four detail requests cover truncated history. There are no repeated per-order sleep/poll loops inside the cycle.
2. Fetch the RY book **after reconciliation**. Freshness starts at actual HTTP dispatch, excluding time spent waiting for request capacity. A genuinely slow response still cannot authorize a stale quote.
3. Recompute the ladder and request up to eight necessary cancellations. Acknowledgements do not release quantity or slots. Confirm cancellations/fills in subsequent normal cycles.
4. Replenish up to eight eligible child slots, inner levels first and alternating sides. A pending cancellation keeps its own slot and risk reservation; it does not impose a global stop on unrelated slots. All independent combinations of outstanding fills are reserved before dispatch.
5. Pace the next cycle to leave capacity for writes. At the default 60 total requests/second and eight-order submission batch, the minimum target cycle interval is 0.20 seconds, including work already done. Extra I/O, rate limits or server latency can make it longer.

Four independent keep-alive sessions provide bounded concurrent I/O. Workers never mutate the ledger. New order submissions have freshness/end-of-case deadlines checked immediately before dispatch. Successful siblings are accounted for even when another request fails. An uncertain **order submission** stops new orders and is never replayed. An uncertain **cancellation** stays pending for confirmation; it does not permanently disable submissions by itself.

HTTP 429 cooldown is shared, with one 20% cap reduction per cooldown and gradual recovery after successful traffic without further rate limits. No server capacity is assumed: instructors can control API submission speed ([RIT overview](https://rotman.utoronto.ca/faculty-and-research/education-labs/bmo-financial-group-finance-research-and-trading-lab/rit-market-simulator/overview--features/)).

## Ladder and quantity

| Distance on each side of adjusted fair | Total shares per side at this level | 500-share children per side |
|---|---:|---:|
| 2 cents | 5,000 | 10 |
| 4 cents | 4,000 | 8 |
| 8 cents | 2,500 | 5 |
| 15 cents | 1,000 | 2 |

Up to **50 resting orders**, totaling **12,500 shares per side** from flat, are staged over several cycles. These are per-side distances: a symmetric 2-cent bid/ask pair is four cents wide. Splitting orders does not increase aggregate quantity, improve queue priority or create counterparties.

Fully filled children replenish independently; partial children retain their remaining quantity and queue position. Child IDs within each level remain stable even as available capacity changes. Prices preserve a fee-adjusted minimum edge and stay passive against the external book. Inventory skew, volatility, adverse-fill penalties and duplicate-price coalescing can change prices or reduce the number of eligible slots. The nearest quote is moved to the edge floor rather than dropped solely because inventory skew moved it too close to fair.

RY maker fee is $0.002/share and taker rebate is $0.0014/share under the supplied case settings. A completed passive buy/sell cycle subtracts $0.004/share in maker fees from its actual price difference. The screenshots motivated the strategy; candle highs/lows do not establish executable fills.

From tick 50 onward, fewer than 1,000 confirmed RY shares over the trailing 20 ticks triggers a one-time switch to **2/3/5/8-cent offsets** with unchanged allocations. The 20-tick interval is a measurement window, not a delay between submissions. Old children transition through confirmed cancellations at their own slots; independent empty slots remain eligible.

## Limits and exits

- Stock, gross and net budgets are 25,000 shares, clamped to any lower live limits. Each individual order is at most 5,000 shares. Pending cancellations remain fully reserved. At the hard position ceiling, additional inventory-increasing orders must wait for capacity to be freed.
- No default wind-down at ticks 275/280 and no default 15,000-share inventory brake, age-based exit or inventory-based quantity reduction. `scheduled_closeout` and `inventory_throttle` remain optional. Price skew still reflects inventory.
- A six-cent midpoint move over two seconds temporarily blocks the adverse inventory-adding side for three seconds. Fee and stale-data checks remain active.
- A $500 run loss reduces new exposure; a $1,500 loss or $900 drawdown after a $500 profit peak triggers an explicit `RISK STOP` and liquidation. These are trigger thresholds, not guaranteed loss caps.
- Ctrl+C once requests liquidation; twice exits immediately with best-effort cleanup. Case end stops new orders and cancels outstanding orders. Without scheduled liquidation, residual inventory can remain at close.
- A reset prevents cancellation of old IDs in a new round. Persistently unresolved order/position accounting stops new exposure; fills are never inferred from a missing order or cancellation acknowledgement.

The account must have one writer. Restarting the script to reset a loss stop or running another copy concurrently is not a substitute for resolving the reported state.

## Read the live output

`LIVE` lines appear every wall-clock second after completed cycles, even if simulation ticks do not advance. They show actual accepted orders per second (`sent/s`), confirmed filled shares per second, open-order count, position, cycle duration and the latest reason. A separate five-second heartbeat reports the current operation during slow I/O.

| Reason | Meaning |
|---|---|
| `quotes_resting` | Desired quotes are present; no replacement is needed. Zero new submissions is normal here. |
| `submitted` | At least one submission batch completed; it does not imply a fill. |
| `queued_quotes_expired` / `decision_expired` | The decision became stale before dispatch. Inspect cycle time and effective request cap. |
| `reconciliation_pending` | Order/position snapshots disagree; the next cycle retries without guessing fills. |
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

`child_order_size` defaults to 500; 5,000 restores one child per level. `max_submit_batch` and `max_cancel_batch` default to eight. Child plans are limited to 100 orders across both sides. `request_cap` defaults to 60 and `io_workers` to four. `reconcile_timeout` defaults to five elapsed seconds, `cancel_retry_seconds` to one, and `telemetry_seconds` to one.

The benchmark uses the real paced worker transport with an in-memory exchange and artificial latency; it never contacts RIT. Fill events are injected to test replenishment. Stress tests validate mechanics and accounting, not profitability. See [REVIEW.md](REVIEW.md) and [VALIDATION.md](VALIDATION.md) for review findings and measured results.

The strategy originated from [ALGO2e-rebuilt](https://github.com/TR4-io/ALGO2e-rebuilt), source commit `faafa32`. The supplied one-pager's “Building it” section was excluded.
