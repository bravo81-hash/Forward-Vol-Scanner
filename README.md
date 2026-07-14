# TE Playbook + Campaign Engine v3

TWS-connected selection engine for the TE playbook: per ticker it builds
market context, reads the regime, picks the **top-2 strategy families**
(calendar, double calendar, diagonal, iron condor, put BWB, butterfly),
generates **2 concrete candidates each**, and stages the chosen combo to
TWS (`transmit=False`, whatIf margin first).

Campaign Engine v3 adds executable chat-derived single-expiry strategies,
an OptionNet testing laboratory, signed portfolio-governor checks, immutable
server-side candidates, campaign journaling, management advice, and manual
evidence capture. Open `/campaigns` for the v3 workflow. Hypothesis strategies
remain test-only until manual/paper evidence supports promotion.

## Run
```
pip install -r requirements.txt
python webapp.py            # -> http://127.0.0.1:8765
```
Mock/OptionNet mode works with no TWS. For live dependencies use
`pip install -r requirements-live.txt`. Live mode expects TWS on 127.0.0.1:7496
(edit `core/ib_client.py`).

All trading-date logic anchored to America/New_York via `core.events.trading_today()`.

## Layout
```
core/        models · pricing (BS) · ib_client (pacing/caches) · chain ·
             surface (fwd-vol pairs, term) · regime (TE Console port) ·
             events (FOMC/OpEx/ex-div) · context (single data touchpoint)
strategies/  one module per family, uniform propose(ctx) -> [Suggestion]
selection/   current ranker + Gate S + unified v3 selector + strategy lab
portfolio/   book greeks, signed governor, account aggregation, stress
execution/   immutable candidate validation + N-leg staging; never transmits
store/       scan audit + campaign/candidate/manual-test ledger
management/  deterministic campaign action engine
validation/  manual evidence and captured-context replay summaries
static/      TE browser UI + Campaign v3 testing UI
tests/       mock-mode suite: python -m pytest tests/
```

## Direction tab (`/api/direction`)
Objective structure selection for a stated intent — any ticker, three data
modes. Two gates (`selection/direction.py`), both advisory:
* **Gate 1 — play type**: forward VRP (`vrp_fwd`, IV30 vs HAR forecast)
  decides delta play vs vol play: `>= +3v` SELL VOL, `<= -2v` BUY VOL,
  FOMC event premium rich + inverted front → EVENT VOL, else DELTA.
* **Gate 2 — structure matrix**: for `long` / `short` delta intent, ranks
  credit vertical / debit vertical / OTM calendar / OTM butterfly from
  **IV band × 25Δ skew × term verdict**, with the index put-skew asymmetry
  handled explicitly on the short side (call credit is the cheap wing;
  put debit spreads are part skew-subsidised). `auto` intent resolves the
  side from the regime bias; `vol` intent maps the Gate-1 verdict to the
  existing strategy families.

Modes: `mode=auto` tries **TWS → yfinance → mock**. The yfinance fallback
(`core/yf_client.py`, `pip install yfinance`) works for **any ticker**
(AAPL etc.), is delayed ~15-20 min, proxies SPX/NDX/RUT chains via
SPY/QQQ/IWM, and takes IV-rank history from ^VIX/^VXN/^RVX; single names
with no IV history fall back to a flagged **IV30/HAR ratio** band.

## TWS request budget (per live refresh, per symbol)
* 1 underlying quote + ~4 x n_expiries option lines — **batched in groups
  of 40 and cancelled immediately** (`core/ib_client.quote_many`)
* chain expiries limited to **Fridays, 5–85 DTE** (supports 60–80 DTE Gate S rows)
* daily bars + IV30 history: 1 request each, **cached 1 h**
* chain surface cached **5 min**; secdef params cached 6 h
* staging: N qualifies + 1 whatIf + 1 placeOrder

## Selection guards
Guards are **advisory, never a block**: the engine always returns its
best-available candidates and the guards speak through gate warnings and
the verdict. "Stand aside" is advice shown to the user, not a suppression
of suggestions.
* **FOMC event harvest** — negative VRP normally argues against selling, but when
  VRP >= -1.5, the front is INVERTED and FOMC is <= 21d out, the implied
  event move (variance step across the event minus an rv21 baseline,
  `core/surface.event_premium`) is compared with the ~0.9% historical
  FOMC-day move; at >= 1.25x richness the app offers EVENT CAL cards that
  sell the first post-FOMC expiry, with an explicit override: exit within
  1-2 sessions after FOMC — normal hold and 7-DTE front-exit rules do not
  apply.
* **Friday cadence gate** — Monday close is the default entry day. Friday
  sessions raise gate W and rank net-debit long-vega structures (calendar /
  double calendar / diagonal) first, but no family is removed; Friday-close
  IVs are weekend-discounted and a Friday entry spans two weekends of gap
  risk.
* **Campaign scope** — book greeks, budget warnings and fit scores ignore
  legs more than `CAMPAIGN_MAX_DTE` (60) days out; those belong to the
  separate long-DTE campaign and are reported as excluded in the book bar.
* **Stress row** — the book bar shows scenario P&L for the FULL book
  (campaign legs included): -5% spot/IV+10/2d, -2%/IV+4/1d, +3%/IV-3/2d,
  valued with the same model pricing as the book greeks.

## Maintenance
* `core/events.py` — update FOMC dates each January
* `portfolio/risk.py` — tune vega/delta/theta budgets to account size

## Disclaimer
Model prices are skew-interpolated Black-Scholes from ATM/25Δ quotes —
confirm card pricing on the live chain before transmitting. Orders are
always staged untransmitted for manual review in TWS.
