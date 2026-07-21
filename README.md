# TE Playbook + Campaign Engine v3

## Stock Opportunity Radar

Open `/stocks` for an automatic single-stock options watchlist. It scans the
curated liquid-options universe in `config/stock_universe.yaml` after each US
session. V2 displays five active names plus up to five reserves and retains the
top 20 internally for research. Friday's pool is also saved, but its technical
levels are refreshed from the latest completed session before it can be used.

Startup is adaptive. If the computer was off after the prior close, the first
monitor request builds the missing previous-close baseline automatically. At
14:45 ET, or immediately when the app starts later, it compares the wider pool
with the active five and then freezes the session list. Up to two materially
better challengers can alert in **shadow mode**; they cannot stage live orders
until their logged out-of-sample evidence is explicitly promoted.

The scanner first requires at least 220 daily bars, price of at least $15,
20-session average dollar volume of at least $50m, ATR/price no greater than 8%,
and membership in the maintained optionable universe.  It recognises only four
triggerable structures: aligned-trend breakout, pullback reclaim, breakdown,
and failed rally.  Candidates then receive a transparent 100-point score:

| Component | Maximum | Why it is included |
| --- | ---: | --- |
| Trend and structure | 25 | Avoid fighting the 50/200-day regime; ADX rewards a cleaner trend. |
| Trigger readiness | 20 | Prefer a setup close enough to act on, without entering early. |
| Liquidity | 15 | Underlying dollar-volume capacity proxy; the exact option NBBO is checked at trigger time. |
| Relative strength vs SPY | 15 | Select leaders for bullish trades and laggards for bearish trades. |
| Momentum and volume | 10 | Require the move to have confirmation rather than price alone. |
| Defined-risk payoff | 10 | Require approximately 2:1 underlying reward/risk. |
| Confirming candle | 5 | Small timing confirmation; never allowed to dominate the setup. |

Friday ranking can add or subtract up to five points for 60-day relative
strength.  A two-point carry-over bonus reduces unnecessary daily churn.
Known earnings inside the intended 30-day hold do not consume a shortlist slot
unless the idea is explicitly classified as an event trade. Unknown earnings
dates remain visibly unverified and cannot be staged. The pool is capped at two
names per sector and correlated cluster.

Each row contains a TradingView chart link, an OptionStrat custom-combination
link, the exact price trigger/invalidation/target, earnings and Tier-1 macro
warnings, and a 40–85 DTE defined-risk debit-spread plan compatible with a
30-calendar-day maximum hold.  During 15:00–15:40 ET the monitor checks only the
top five or ten TWS underlying quotes.  A valid crossing flashes the row, sounds
an alert, and can raise a browser notification.  It refuses stale quotes,
breached invalidations, entries more than 0.35 ATR beyond the trigger, earnings
inside the hold or unverified earnings, illiquid option NBBOs, per-leg option
open interest below 100, per-leg daily volume below 10, debit above 45% of spread width,
an unspecified multi-account destination, and orders outside the risk or
available-funds budget.  An existing position or working order in the selected
account is flagged and blocked pending a separate portfolio review.

The staging governor permits at most two new entries per session, 0.5% NLV
structural risk per trade, 1% total new risk per session, and one new entry per
correlated factor cluster. Same-day pre-release and next-day CPI/NFP/FOMC events
block new entries; the post-release session and events two calendar days away
halve the new-trade risk budget.

The **Stage TWS** button rechecks the underlying trigger, exact option NBBO,
earnings gate, account NLV/available funds and quantity on the server.  It runs
a what-if margin check and creates a combo with `transmit=False`.  The radar
never transmits an order; review the account, legs, limit and margin in TWS and
transmit manually only if they are correct.

Every static, reserve and challenger trigger is evaluated at 1/3/5/10/20
trading-day horizons. The durable evidence table records direction-adjusted
return, MFE, MAE, target/invalidation hits and false breakouts. A false breakout
means the next trading-day close returned through the trigger, or invalidation
was hit before target. This evidence determines whether five, ten or the
challenger model should eventually be promoted.

Price-Action supplies a separate, versioned US context feed after its scheduled
scan. Radar validates its schema and timestamp, then records S1/S2 directional
confirmation or conflict, S3 neutral research context and S4 experimental
context beside each matching ticker. These annotations are **shadow only**:
they do not change the primary score, rank, trigger, risk gate or staging
permission. The integration fails open if the feed is missing, malformed or
more than 96 hours old. Override the public feed URL with
`FVS_PRICE_ACTION_FEED`; set it to `off` to disable the context layer.

Run the desk and open it directly:

```text
radar.bat
# or
python webapp.py            # -> http://127.0.0.1:8765/stocks
```

While `webapp.py` is running, an internal New-York-time scheduler attempts the
daily scan after 16:10 ET and the weekly scan on Friday.  For unattended Windows
scheduling, run `install_radar_task.ps1` once in PowerShell.  It installs three
Melbourne-morning checks to cover both markets' DST combinations; the
`--due-only` New York guard makes the two irrelevant checks no-ops.  A manual or
Task Scheduler run is also available:

```text
radar_after_close.bat
python stock_radar_scan.py --cadence auto --source yf --due-only
```

Use `--source mock` and Last-hour source **Practice trigger** to exercise the
entire scan/flash/stage-preview flow without TWS or a live order.

### GitHub Codespaces practice test

Create the Codespace from the radar branch.  Its dev-container installs the
requirements and forwards port 8765 automatically.  In the terminal run:

```text
python webapp.py
```

Open the forwarded **Stock Opportunity Radar** port, select **Practice data**,
**Practice trigger**, and **MOCK-A**, then run the after-close scan and start the
monitor. Codespaces can test the active/reserve labels, frozen selection, risk
gates, ranking, alerts, links and inert
mock-stage flow.  It cannot connect to TWS running on another computer because
Codespaces has its own isolated localhost.

TWS defaults to `127.0.0.1:7496`.  Override it before starting the app when
testing against paper TWS without editing source:

```powershell
$env:FVS_TWS_PORT = "7497"
python webapp.py
```

## Last Hour Trade Desk

This branch opens `/` as a focused 15:00–15:40 ET decision surface. It hides
the broad research registry and evaluates only three regime flies plus the
canonical TimeEdge and progression-gated TimeZone structures:

* bullish controlled-pullback put BWB;
* near-balanced put fly for chop;
* bearish protected debit put BWB;
* SPX TimeEdge one-sided put calendar; and
* RUT TimeZone 20-point PCS plus OTM put calendar.

The desk shows one primary ENTER/WAIT verdict, graphical bias/IV–RV/term/skew
and model risk profiles, exact legs, one-click OptionStrat combos,
PT/SL/time stop, one-defense rules, SPX/RUT
differences, and a 15:00–15:40 ET workflow. Live orders are staged
`transmit=False`; the server enforces freshness, eligibility, quantity and the
portfolio governor. The normal window is advisory: staging outside it remains
available with a visible special-situation warning. The full scanner remains
available at `/research` and the campaign laboratory at `/campaigns`.

Single-expiry fly graphs use exact intrinsic payoff at every strike. TimeEdge
and TimeZone graphs are front-expiry profiles: later-dated calendar legs retain
Black–Scholes value at their remaining tenor, with entry IV held constant and
the exact displayed debit/credit used as cost basis. Use the OptionStrat link
to stress a different future volatility surface before staging.

TWS-connected selection engine for the TE playbook: per ticker it builds
market context, reads the regime, picks the **top-2 strategy families**
(calendar, double calendar, diagonal, iron condor, put BWB, butterfly),
generates **2 concrete candidates each**, and stages the chosen combo to
TWS (`transmit=False`, whatIf margin first).

Campaign Engine v3 adds executable chat-derived single-expiry strategies,
a historical matched-date OptionNet testing laboratory, one conditional
cross-strategy ranker, signed portfolio-governor checks, immutable
server-side candidates, campaign journaling, management advice, and manual
evidence capture. Open `/campaigns` for the v3 workflow. Hypothesis strategies
remain test-only until manual/paper evidence supports promotion.

For a historical ONE test, normally enter only the date, underlying, account,
and an optional directional override. The app derives the price/volatility
regime from free daily history. VIX9D/VIX3M and SKEW are clearly labelled
proxies; actual historical option legs, fills, and outcomes still come from ONE.

The same `/campaigns` screen also has **Live TWS → ONE**. When TWS is running,
it discovers managed accounts, reads the current chain/regime/book, selects
currently listed legs, and attempts to replace model prices with live NBBOs.
Those positions can be recorded as prospective ONE forward tests. A live test
is out-of-sample evidence only when its rule and parameters were frozen before
the outcome was observed.

All market dates, DTE calculations, events, and execution windows are anchored
to `America/New_York`; live captures also store the converted
`Australia/Melbourne` date/time. Python `zoneinfo` handles daylight-saving
changes in both locations. Outside New York regular hours the TWS path requests
frozen quotes and can build the surface from TWS historical IV when current
option Greeks are unavailable.
Live mode preloads the same free yfinance underlying/volatility-index history
as the historical workflow before connecting to TWS, then uses TWS only for
the current listed option chain. TWS history is a fallback only when the free
preload is unavailable. Every source is shown on the live capture; a temporary
empty TWS response is never cached. IB requests have a 15-second per-request
limit, and a failed live scan identifies the connection or market-data stage.

## Run
```
pip install -r requirements.txt
python webapp.py            # -> http://127.0.0.1:8765
```
Historical OptionNet mode works with no TWS. Live mode expects TWS on 127.0.0.1:7496
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
* daily bars + IV-index history: **preloaded in parallel from yfinance**;
  TWS requests them only as a fallback, then caches non-empty results for 1 h
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
