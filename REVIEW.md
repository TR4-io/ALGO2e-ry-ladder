# Continuous-v2 execution review

The user's specific quiet-run incident could not be reconstructed without its console/log. The review did find reproducible structural problems in the old placement lifecycle. The default runner has been replaced; this is not a claim that additional open orders alone cure a placement failure.

| Finding in old default path | Rebuilt behavior | Evidence |
|---|---|---|
| Batched cancellations entered up to 51 confirmation-poll iterations. Even ordinary repricing of 50 children could require 50 DELETEs plus 50 detail reads before returning. | At most eight cancellations per cycle; terminal accounting is confirmed by normal subsequent snapshots. No internal confirmation sleep loop. | Delayed cancellation tests retain all reservations while unrelated empty slots submit. |
| A cancellation on any ladder level returned from management before replenishing unrelated slots. | Slot ownership and pending-cancel state are independent; unaffected slots can place in the same cycle. | A test leaves eight cancels pending and observes additional children accepted without releasing their slots. |
| Missing orders triggered detail lookups and potentially repeated status-list queries per order. | Two terminal-history requests cover the whole group; at most four detail fallbacks per cycle. Unresolved state yields to the next cycle, with an explicit timeout. | Fifty missing terminal children reconcile with two history reads and no detail polls. |
| Book age began at the start of the whole prefetched frame, including rate-budget queue wait and reconciliation. | Read the book after account reconciliation and timestamp actual HTTP dispatch. Response latency still counts toward staleness. | A paced-read test places despite 0.60 seconds spent reaching the book; a genuinely 0.70-second book response blocks new writes. |
| Unknown DELETE responses latched the same permanent transport guard used for uncertain POST submissions. | Only uncertain order submissions latch the submission guard. Uncertain DELETEs retain pending reservations and can be resolved from authoritative state. | Dedicated transport and runtime cancel-ack-loss tests. |
| Generic API-error handling launched another cancel-all path, introducing a second blocking lifecycle. | Retry via the next bounded frame; revalidate case, orders, positions and book before any new submission. | Default CLI uses the new runtime exclusively, with no nested cancel-all polling. |
| Spyder could combine new heartbeat code with cached old Bot classes. | Script entry refreshes the complete local module group. Missing heartbeat diagnostics produce a visible message rather than a dead thread. | A subprocess injects poisoned cached modules, launches the actual script entry, and verifies fresh base/subclass identities and activity initialization. |
| Status depended on simulation-tick progress and omitted actual submission rates. | One-second wall-clock LIVE telemetry plus a separate five-second I/O heartbeat. Submitted orders, filled shares and resting orders are separate measures. | Runtime telemetry tests and recorded offline placement benchmark. |

## What remains deliberately enforced

The 25,000-share stock/gross/net limits, lower live limits, 5,000-share per-order maximum, self-cross prevention, passive quote/fee checks, stale-data deadlines and configured loss stops remain. Pending cancels retain their full unfilled-share reservation. Accepted siblings are recorded before errors/interrupts are raised. Unknown accepted submissions are never replayed. Resets never cancel old IDs in a new round.

The current ladder retains 50 planned 500-share children, weighted 5,000/4,000/2,500/1,000 shares per side at 2/4/8/15-cent offsets. No timed wind-down or inventory brake was reintroduced. Risk controls can still prevent new inventory-increasing orders; a healthy full ladder can have zero new submissions while it rests.

## Verification scope

Tests cover both continuous-v2 and the retained legacy components. Five full-round new-runtime integration regimes and 25 seeded new-runtime stress rounds check independent-fill risk, exact final accounting and cleanup. Fills racing a cancellation may be unknown until the next frame; while pending, actual exposure must remain inside the reserved envelope, and confirmed volume must never exceed actual volume.

The offline timing harness uses the real paced thread-pool transport with artificial response latency and an in-memory exchange. It injects fills and measures acceptance of replacement orders. It does not contact RIT or demonstrate live profitability. The fixture's P&L metric was also corrected to include residual inventory marked at the synthetic midpoint; the older cash-only output was not valid ending P&L for non-flat runs.

See `validation/continuous-v2-benchmark.json`, `validation/continuous-v2-stress.json`, and `VALIDATION.md`. Live RIT timing and the original no-order incident remain unverified.
