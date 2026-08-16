# Equity LEAPS Engine

This document describes the behavior currently implemented by Radar-B, Gate E,
the ZEBRA strategy, the Weekend Card pipeline and the `/equity` page. It is not
a roadmap and does not claim checks that are not present.

## 1. Safety contract

- Missing measurements block. They are never replaced with neutral scores or
  middle-volatility assumptions.
- Radar-B produces a watchlist, not entry signals.
- A Stage 1/3 candidate requires a separately verified reclaim trigger.
- Confirmed Stage 4 rejects every structure.
- IV and realised volatility must both be present and verified before Gate E
  returns any structure.
- IV below realised volatility forbids every structure classified as short
  premium.
- A thin or degraded ZEBRA solve returns no suggestion.
- Portfolio governance is not implemented in this subsystem. Cards state that
  it was not checked and never claim it passed.

## 2. Radar-B

### 2.1 Structural gates

| Gate | Implemented condition |
|---|---|
| History | At least 260 daily bars |
| Drawdown | 25% through 60% below the 52-week high |
| Base age | At least six weeks since the 52-week low |
| No new lows | No new 20-session low during the last 15 sessions |
| ATR compression | Price-normalised ATR20 / price-normalised ATR100 below 0.80 |
| Base width | Eight-week high-to-low range no more than 25% of current price |
| Underlying liquidity | Ninety-day average of daily close × volume at least $50M |
| Fundamentals | All gates in section 3 pass |

Option open interest and binary-event classification are not Radar-B gates.
They are deliberately not documented as implemented checks because the radar
does not fetch an option chain or a reliable binary-risk classification for
every member of the universe.

### 2.2 Ranking

Only gate survivors form the percentile pool. The score is used for ordering,
not permission to trade.

| Component | Weight |
|---|---:|
| ATR tightness percentile | 25 |
| Ninety-day forward EPS trend | 25 |
| Date-aligned relative-strength inflection versus SPY | 20 |
| Volume dry-up | 15 |
| Higher-low structure | 15 |

Missing benchmark alignment earns no relative-strength points. The result is
hard-capped at five even if an API or CLI caller asks for a larger limit.

### 2.3 Reclaim trigger

The trigger requires all of the following:

- Price closed above the 50-day average.
- The 50-day slope is flat or rising.
- Reclaim-day volume is at least 1.5 times the prior twenty-session average.
- The next earnings report is known and more than five US market sessions away.

Weekends and standard NYSE holidays are excluded when counting sessions.
Unknown earnings blocks the trigger.

## 3. Fundamentals

Yahoo is the current source. A fetch or schema failure marks the symbol
`UNRATED`, and Radar-B removes it rather than assigning a neutral score.

| Gate | Implemented condition |
|---|---|
| Revenue | Current four-quarter revenue exceeds the preceding four-quarter total |
| Profitability | Positive free cash flow, or an operating margin that is both positive and higher than the preceding four-quarter margin |
| Leverage | Net debt / EBITDA is known and strictly below 3.5 |
| Outlook | The next-year forward EPS estimate is flat or higher than 90 days ago |

Debt, cash and positive EBITDA must all be available to verify leverage.
Missing cash is not treated as zero. Revenue and margin calculations require
eight usable quarterly statement columns.

Snapshots are stored in monthly JSONL files under `data/fundamentals` by
default. A symbol/day row is replaced when the job is rerun, so the store does
not accumulate duplicate daily observations.

## 4. Stage classification

Gate E uses daily bars as the approximately 30-week view:

- Stage 2 requires price above a rising 150-day average and no confirmed series
  of lower highs.
- Stage 4 requires price below a falling 150-day average, a negative recent
  return and lower highs measured in three non-overlapping windows.
- Everything else is Stage 1/3 and requires the reclaim trigger.

The high windows are disjoint. Nested 13-, 26- and 52-week maxima are not used
because they create a descending sequence mechanically.

## 5. Gate E

Input is a ticker and hold:

| Hold | Accepted proposal tenor | Target |
|---|---:|---:|
| Short | 7–21 DTE | 14 DTE |
| Medium | 28–60 DTE | 45 DTE |
| Long | 120–400 DTE | 300 DTE |

The selector consults stage, verified IV/RV, measured IV rank when available,
the requested hold and actual proposal tenors. The IV side of IV/RV is ATM IV
at the requested hold tenor; a LEAPS surface is never extrapolated back to 30
DTE and presented as a measured 30-day value. Skew is displayed as context but
does not choose a structure. Term slope, earnings proximity, target-strike OI
and spread are not Gate E selection inputs.

### 5.1 Implemented matrix

`Cheap` means measured IV rank below 30 or IV/RV below 1. `Rich` means measured
IV rank above 70, or IV/RV above 1.15 when historical IV rank is unavailable.
IV/RV below 1 always activates the short-premium removal pass.

| Stage | Volatility state | Shortlist before proposal validation |
|---|---|---|
| Stage 2 | Cheap | ZEBRA, diagonal |
| Stage 2 | Rich | Call debit spread, iron condor |
| Stage 2 | Middle | Diagonal, ZEBRA |
| Stage 1/3 with verified trigger | Cheap or IV<RV | Call debit spread |
| Stage 1/3 with verified trigger | Otherwise | Call debit spread, put BWB |
| Stage 1/3 without trigger | Any | No trade |
| Stage 4 | Any | No trade |

Rangebound-only calendar, iron-fly and short-put-delegation branches are not
implemented. Stage 1/3 is not treated as rangebound permission: it remains
blocked until the reclaim trigger fires.

If the selected strategy cannot build legs within the hold window, Gate E
returns `STAND ASIDE`. One valid expiry in the requested window is sufficient;
the engine does not invent a tenor shortfall merely because a second expiry is
absent.

## 6. ZEBRA

The structure is two long ITM calls against one short ATM call at the same
expiry. The solver searches actual candidate strikes below spot for:

`2 × extrinsic(long strike) ≈ extrinsic(ATM strike)`

The candidate is refused when:

- Fewer than eight strikes are available below spot.
- Absolute net extrinsic exceeds 10% of ATM extrinsic.
- A required IV reaches either the 2% or 300% safety boundary.
- Yahoo leg provenance is not a bid/ask mid or an IV cross-checked to bid/ask.
- The source is TWS but the deep-ITM strike/expiry contract lacks its own
  verified quote.

TWS currently supplies an ATM/wing surface and a chain-definition strike set,
not a verified deep-ITM quote. Therefore the equity subsystem refuses a TWS
ZEBRA rather than representing an interpolated theoretical price as NBBO.

Delta, theta and vega are computed from the built legs and displayed. Zero
current extrinsic does not imply zero theta or zero vega.

Breakeven comes from the actual piecewise payoff walk. Maximum loss for the
debit back ratio is the debit. Maximum profit is unbounded because the position
is net long one call above the short strike. Its exit instruction therefore
uses a return-on-debit or thesis target set at entry, not a percentage of a
finite maximum value.

### 6.1 Model limitation

The local Greek engine is European Black–Scholes. A deep-ITM call on a
dividend-paying US equity can have American early-exercise and discrete-dividend
effects. The card states this limitation when the long strike is at most 90% of
spot and the configured dividend yield is positive.

## 7. Data provenance and failure behavior

### Yahoo

- Placeholder or market-inconsistent IV is solved from bid/ask mid.
- A sane quoted IV is retained only when its Black–Scholes price lies inside
  the contemporaneous bid/ask, with a small rounding allowance.
- A `lastPrice` fallback is labelled explicitly and cannot authorize a ZEBRA
  leg.
- Rate-limit exceptions retain a structured `EquityThrottleError`; the web
  route does not infer throttling from message text.
- At most three explicitly selected expiry chains are requested; the builder
  does not fetch every monthly expiry lying between two DTE endpoints.
- The context is built locally without replacing shared yfinance functions, so
  concurrent Flask requests cannot exchange symbol-specific repair closures.

### TWS

- Context caching includes symbol, hold and trading date.
- A successful live request clears any stale fallback error for that symbol.
- A historical-IV fallback marks volatility inputs unverified and Gate E
  blocks.
- Cards describe context-surface provenance separately from per-leg price
  provenance.

Valid source values are `auto`, `live` and `yf`. Unknown values are rejected.

## 8. Cards and delivery

Cards contain setup, structure, entry, exit, management and flags sections.
They display computed Greeks and per-leg price provenance when a suggestion is
available. If no governance payload is supplied, FLAGS says governance was not
checked.

The implemented browser surface is `/equity`. Radar-B can be run there, and a
row can be passed to Gate E. Selecting trigger verification asks the server to
recompute the trigger from bars, volume and a known earnings date; the query
parameter is not accepted as proof that the trigger fired.

`weekend_card.py` writes a pull-based JSON watchlist. Structures are provisional
and must be recomputed when a trigger is verified. This subsystem does not
stage or transmit broker orders and is not integrated into the Today screen.

## 9. Running

```bash
python -m pytest tests/test_equity_leaps.py -q

python weekend_card.py --symbols AAPL,MSFT,NFLX --text --dry-run

curl "localhost:8790/api/equity/gate-e?symbol=NFLX&hold=long&source=yf&trigger=1"
curl "localhost:8790/api/equity/radar?symbols=AAPL,MSFT,NFLX"
```

The targeted suite contains independent pricing references and explicit
regressions for missing volatility, degraded solves, stage compatibility,
provenance, cache separation, trading-session earnings checks and the
documented fundamental gates.
