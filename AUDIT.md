# Forward-Vol-Scanner — Accuracy Audit & Fixes
Audited: every calculation path (pricing, regime, surface, chain, ranker, book,
reprice, strategies, walls) against implementation-independent references.
Verification: `pytest test_math.py tests/test_app.py` (35 pass) + `compare_accuracy.py`.

## Errors found & fixed (measured before → after)

| # | Where | Error | Before → After |
|---|-------|-------|----------------|
| E1 | `core/regime.py` ATR | True Range missing `|low−prevClose|` → ATR understated on gaps → `bias` z-score inflated | TR on −3% gap: **135 → 180** (true 180) |
| E2 | `core/regime.py` `_adx` | Final ADX = simple mean of last 14 DX, not Wilder RMA — drifts vs the Pine/TradingView console it claims parity with | **17.28 → 16.14** (= Wilder reference); now bit-exact to textbook Wilder |
| E3 | `core/pricing.py` `struct_metrics` | Breakevens quantized to the 0.2%-of-spot scan grid | BE error, long call: **7.09 pts → 0.00** (linear interpolation at sign change; scan window now always covers the strike envelope) |
| E4 | `sentinel.py` | Skew label inverted vs FVS convention (`rr25 = put25−call25`, positive = put skew) | rr25 +4.5 labeled **call-skew → put-skew** |
| E5 | `core/surface.py` | `iv9/iv30/iv45` = nearest listed Friday → term verdict computed on wrong maturities | Separating case verdict: **INVERTED FRONT → FLAT** (matches constant-maturity truth). New `iv_cm()` interpolates linearly in total variance; also feeds context `iv30` so IV-percentile compares like-for-like with TWS IV30 history |
| E6 | `core/pricing.py` | No dividend yield — model greeks biased (feeds `book_greeks`, your live per-account delta) | ATM 30d SPX call delta: **0.5406 → 0.5303** with q=1.2% (~1 delta pt/contract). Full Merton `q` threaded through pricing → strategies → book → reprice → webapp payoff; `DIV_YIELD` map + `q_for(symbol)` |
| E8 | `core/chain.py` `k25` | 25Δ strike solve missing drift `(r−q+σ²/2)T` → wings quoted off-delta → rr25 biased | Actual delta at wings: **.219P/.283C → .250/.250** |
| E14 | `selection/ranker.py` | VRP<0 + FLAT term: fallback overwrote "CAUTION — VRP negative" with weaker "MARGINAL" AND ranked condor (premium selling) first — exactly when selling is unpaid | **MARGINAL/condor → CAUTION/calendar** (verdict preserved; debit leads when VRP ≤ 0) |

## Reviewed and verified clean (left alone)
BSM theta/vega closed forms (now FD-verified for both puts and calls incl. q) ·
put-call parity · RV window/annualization (recovers GBM σ=20% within 1pt) ·
`_stdev` ddof · autocorr lag-1 windowing · Parkinson estimator · Wilder ±DM tie
rule · VRP horizon match (IV30 vs RV21) · IV percentile · forward-vol variance-time
math · `event_premium` baseline subtraction · pair-table FOMC exclusion · book
greek aggregation units & CAMPAIGN_MAX_DTE split · fit_score asymmetry · gates ·
OpEx date math · walls GEX sign proxy · `iv_at` extrapolation clamp · condor/BWB/
fly scoring heuristics (defensible weights, correct units).

## New permanent tests
`test_math.py` — 15 tests, all against independent references (parity identities,
central finite differences, GBM recovery, textbook Wilder, hand-computed
variance interpolation). Zero TWS. Keep in CI.

## Notes
* ADX values shift slightly (RMA is smoother). ADX_THR=20 was calibrated on the
  TradingView console — the engine now matches that scale, so the gate is MORE
  aligned, not less.
* Dividend yields hard-coded: SPX/SPY 1.2%, NDX/QQQ 0.6%, RUT/IWM 1.1%
  (`core/pricing.DIV_YIELD`). Live cards still NBBO-reprice with TWS greeks.
