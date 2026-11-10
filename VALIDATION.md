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
