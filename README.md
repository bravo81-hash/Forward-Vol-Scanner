# TE Playbook — Trade Selection App

TWS-connected selection engine for the TE playbook: per ticker it builds
market context, reads the regime, picks the **top-2 strategy families**
(calendar, double calendar, diagonal, iron condor, put BWB, butterfly),
generates **2 concrete candidates each**, and stages the chosen combo to
TWS (`transmit=False`, whatIf margin first). Management is done in
OptionNet Explorer by design — this app only selects and stages.

## Run
```
pip install flask ib_insync
python webapp.py            # -> http://127.0.0.1:8765
```
Mock mode works with no TWS. Live mode expects TWS on 127.0.0.1:7496
(edit `core/ib_client.py`).

## Layout
```
core/        models · pricing (BS) · ib_client (pacing/caches) · chain ·
             surface (fwd-vol pairs, term) · regime (TE Console port) ·
             events (FOMC/OpEx/ex-div) · context (single data touchpoint)
strategies/  one module per family, uniform propose(ctx) -> [Suggestion]
selection/   ranker: regime matrix -> top-2 families -> 4 scored cards
portfolio/   live book greeks, risk budgets, portfolio-fit score
execution/   N-leg combo staging, whatIf margin, never auto-transmits
store/       sqlite audit log (every shortlist + staging)
static/      browser UI (cards, risk graphs, book bar)
tests/       mock-mode suite: python -m pytest tests/
```

## TWS request budget (per live refresh, per symbol)
* 1 underlying quote + ~4 x n_expiries option lines — **batched in groups
  of 40 and cancelled immediately** (`core/ib_client.quote_many`)
* chain expiries limited to **Fridays, 5–50 DTE**
* daily bars + IV30 history: 1 request each, **cached 1 h**
* chain surface cached **5 min**; secdef params cached 6 h
* staging: N qualifies + 1 whatIf + 1 placeOrder

## Maintenance
* `core/events.py` — update FOMC dates each January
* `portfolio/risk.py` — tune vega/delta/theta budgets to account size

## Disclaimer
Model prices are skew-interpolated Black-Scholes from ATM/25Δ quotes —
confirm card pricing on the live chain before transmitting. Orders are
always staged untransmitted for manual review in TWS.
