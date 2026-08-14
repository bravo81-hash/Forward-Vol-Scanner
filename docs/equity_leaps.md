# FVS Upgrade — Equity LEAPS Engine

**Revision 3.** Scoped as an FVS extension. No Telegram. Fundamentals from yfinance ± IBKR. Single user, no auth, no multi-tenancy.

Working names: **Radar-B** (base screener), **Gate E** (equity structure selector, sibling to Gate S), **Weekend Card** (Saturday output view).

---

## 0. What already exists vs what's new

| Need | FVS today | Action |
|---|---|---|
| Candidate screening | Stock Opportunity Radar | **Extend** — add base-formation gates, don't parallel it |
| Pattern confirmation | Price-Action Pattern Scanner (shadow-only feed) | **Reuse** as confirmation input; respect fail-open/shadow constraint |
| Short-put-at-support | Value Entry Put Scanner | **Dedupe** — Gate E's short-put branch must delegate here, not reimplement |
| Structure selection | Gate S (SMSF, single-expiry, 7 variants) | **Clone the pattern** into Gate E for single-name equity |
| Chain + IV data | IBKR TWS ingestion, nightly parquet capture | **Reuse as-is** |
| Ranking | VRP ranker | **Extend** with the quality score in §1.3 |
| Position rotation | Campaign Engine v3, tranche laddering | **Integrate** — LEAPS positions join the rotation doctrine |
| Primary view | "Today" screen | **Extend** — Weekend Card as a Today section, not a separate app |
| Order staging | transmit=False broker staging | **Reuse as-is**; Gate E stages, never transmits |

Genuinely new code: base-formation gates, the fundamental layer, Gate E's selection matrix and strike solver, the portfolio governance check in §4, the card renderer.

---

## 1. Radar-B — base screener

Extends Stock Opportunity Radar. Produces a **watchlist**, not signals.

### 1.1 The architectural point

The screener as described surfaces NFLX today — deep pullback, consolidating, defensible fundamentals. And the honest read on NFLX was *don't*. So:

**Radar-B membership is silent. A separate trigger fires entries.**

Stage 1 bases fail to resolve more often than they resolve, and the failures look identical to the successes right up until they don't. A screener that pushes five names at you every Saturday, each rendered as a tidy card, is a machine for manufacturing conviction you haven't earned.

### 1.2 Structural gates (hard, boolean)

| Gate | Condition |
|---|---|
| Drawdown | 25% ≤ drawdown from 52w high ≤ 60% |
| Base age | ≥ 6 weeks since the 52w low |
| No new lows | No new 20-day low in the last 15 sessions |
| Range compression | 20d ATR / 100d ATR < 0.80 |
| Base width | 8-week high-to-low < 25% of price |
| Liquidity | 90d avg USD volume > $50M; OI > 500 at target strikes |
| Not a binary | Exclude pre-revenue biotech, going-concern flags |

### 1.3 Quality score (0–100), ranking only among gate survivors

| Component | Weight | Measure |
|---|---|---|
| Base tightness | 25 | Percentile rank of ATR compression ratio |
| Estimate revision trend | 25 | 90d change in forward EPS consensus (§3) |
| RS inflection | 20 | RS line slope over 20d |
| Volume dry-up | 15 | 20d avg vol / 100d avg vol, inverted |
| Base structure | 15 | Higher-lows count since the low |

Cap at 5. **Return fewer if fewer qualify.** A screener that always returns exactly 5 has stopped screening.

### 1.4 Trigger (separate module)

- Close above the 50-day, **and**
- 50-day slope ≥ 0, **and**
- Reclaim volume > 1.5× the 20d average, **and**
- No earnings within 5 sessions

Optional confirmation from the PA Confirm feed — but per the existing integration contract, that feed is shadow-only and fail-open. It may raise confidence; it must never gate or override.

---

## 2. Gate E — structure selector

Input: ticker + intended hold (1–2w / 4–6w / months). Output: structure, strikes, staged order, card.

### 2.1 Inputs

| Metric | Source |
|---|---|
| IV rank / percentile | **Your parquet archive**, not IBKR's three percentile numbers |
| IV vs RV spread | Chain IV vs 30d close-to-close RV |
| Term structure slope | Front vs ~90 DTE ATM IV |
| Skew | 25Δ put IV − 25Δ call IV |
| Trend state | Weinstein stage from 30w slope + price position |
| Liquidity | Spread as % of mid at target strikes; OI |
| Earnings proximity | Days to next report vs intended DTE |

### 2.2 Selection matrix

| Trend | IV rank | IV vs RV | Structure |
|---|---|---|---|
| Confirmed up | Low (<30) | IV < RV | Long 80Δ LEAPS, or ZEBRA |
| Confirmed up | Mid | IV ≈ RV | Diagonal (LEAPS + 30–45 DTE short call) |
| Confirmed up | High (>70) | IV > RV | Short put spread, or call debit spread |
| Basing, trigger fired | Low | IV < RV | Call debit spread, defined risk |
| Basing, trigger fired | High | IV > RV | Short put spread at base low → **delegate to Value Entry Put Scanner** |
| Basing, no trigger | Any | Any | **No trade.** Watchlist only |
| Down | Any | Any | **No trade.** The NFLX case |
| Rangebound | Low | IV < RV | Calendar / diagonal |
| Rangebound | High | IV > RV | Iron fly, iron condor, put BWB |

### 2.3 Hard block: never sell premium when IV < RV

NFLX priced 31.9% IV against 51.2% realized. The chart invited "sell puts into the fear"; there was no fear premium — only vol offered at a 19-point discount to what the stock was delivering.

Gate E blocks all premium-selling structures when IV/RV < 1.0, and surfaces the ratio on the card so the block is legible rather than mysterious.

### 2.4 Strike solver

Solve numerically. Never use nominal deltas.

- **ZEBRA:** solve long strike where `2 × extrinsic(K_long) = extrinsic(K_ATM)`. The NFLX 70/78 at 305 DTE delivered 87Δ, $3.15 net extrinsic, breakeven +4.1% — because $8 spacing is 0.35 SD at 32% vol. Correct strike was ~65.
- **Diagonal:** enforce `K_short > K_long + net_debit`. Reject if no listed strike satisfies it.
- **All:** recompute delta from the live chain. At 300+ DTE with r ≈ 4.5%, ATM call delta runs ~61, not 50 — large enough to break any design built on assumed deltas.

---

## 3. Fundamentals — yfinance first

**Correction to Revision 1:** I said forward estimate revisions needed a paid source. That was wrong. Recent yfinance exposes them directly:

| Need | yfinance field |
|---|---|
| **Forward EPS revision trend** | `ticker.eps_trend` — current vs 7d / 30d / 60d / 90d ago |
| Revision breadth | `ticker.eps_revisions` — up/down counts, 7d and 30d |
| Revenue growth | `ticker.income_stmt` (annual + quarterly) |
| FCF | `ticker.cashflow` |
| Leverage | `ticker.balance_sheet` |
| Margins | `ticker.info` |
| Earnings date | `ticker.calendar` |

`eps_trend` is the important one — it is exactly the "good future outlook" proxy, and it is free.

### 3.1 Gates

| Gate | Condition |
|---|---|
| Revenue | TTM revenue growth > 0 |
| Profitability | Positive FCF, or positive and improving operating margin |
| Balance sheet | Net debt / EBITDA < 3.5 |
| **Outlook** | 90d forward EPS estimate flat or rising |

A stock down 40% with *rising* forward estimates is multiple compression. Down 40% with falling estimates is a deteriorating business. Reject hard on falling revisions — this gate does more work than the other three combined.

### 3.2 Reliability mitigation

yfinance is unofficial scraping: it breaks on upstream changes and rate-limits under load. Two mitigations, both cheap:

1. **Nightly fundamental snapshot to parquet**, alongside the existing chain capture. You stop depending on the endpoint at scan time, and you accumulate your own revision history — which after a few months is better than what the endpoint returns, because it is point-in-time and not restated.
2. **Fail loud, not silent.** Consistent with the pa_scanner doctrine: a fundamental fetch failure marks the candidate `UNRATED` and drops it from ranking. It must never silently score as neutral — that quietly promotes exactly the names you have no information about.

IBKR's `reqFundamentalData` (ReportSnapshot / RESC) can supplement, but coverage depends on your market data subscriptions and the parsing is XML. Wire it as an optional second opinion, not a dependency.

---

## 4. Portfolio governance (new — and the most important addition)

You run delta-neutral short-premium index across SMSF, Margin, Borg and FA. Every Gate E position is directional long single-name delta layered on top of that. Without a budget check, the engine will happily hand you five longs into a book that is already implicitly long via short puts.

Gate E must, before staging:

- **Aggregate delta budget.** Compute beta-weighted portfolio delta across all four accounts. Reject or size down if the new position breaches the ceiling.
- **Correlation gate.** Five semis is one position. Reject candidates whose 60d correlation to an already-held name exceeds ~0.75.
- **Account routing.** Tag the target account. SMSF is American-style only — single-name equity options qualify, but the routing must be explicit rather than assumed.
- **Margin stress.** Estimate requirement under a −15% / +50% IV shock. Your index book's requirement inflates in exactly those conditions; the check is whether both survive together.

This is the check that stops the engine from concentrating risk you already carry.

---

## 5. Presentation format

Every rendered output — Radar-B rows, Gate E cards, Weekend Card entries — uses dot points, and every dot point is a complete sentence that states the value **and** what it implies. A bare figure makes the reader do the interpretation twice: once when they read it, and again when they try to remember why it mattered.

- Wrong: `IV rank: 31`
- Right: `IV rank is 31, sitting in the bottom third of the past year, so premium is cheap here and long-vega structures are favoured over short.`

Rules for the renderer:

- Each bullet is one full sentence, ending in a full stop, with the number embedded rather than prefixed as a label.
- Where a number triggered a rule, the bullet names the rule it triggered, so the logic is legible on the card rather than buried in the code.
- Bullets never exceed two sentences. If a point needs three, it belongs in a different section of the card.
- Blocked or rejected cards state the specific gate that failed and the condition that would clear it, because "no trade" without a reason is indistinguishable from a bug.

### 5.1 Card structure

```
TICKER — $spot (day change %)
STAGE · IV RANK · IV/RV · SKEW · DAYS TO EARNINGS
─────────────────────────────────────────────────
SETUP
STRUCTURE
ENTRY
EXIT
MANAGE
FLAGS
```

### 5.2 Worked example — actionable card

```
ACME — $142.30 (+1.8%)
Stage 2 · IV rank 24 · IV/RV 0.81 · Skew +4.2 · Earnings in 38 days
─────────────────────────────────────────────────────────────────
SETUP
· Price has pulled back 14% from the 52-week high and has now closed
  back above a rising 50-day, which is the trend-continuation case
  rather than a base-formation case.
· Volume contracted for six sessions into the low and expanded 1.9x
  on the reclaim day, which satisfies the trigger's volume condition.
· Relative strength versus SPY has been rising through the pullback,
  so this is a leader holding up rather than a laggard bouncing.

STRUCTURE
· Diagonal: long 1x Jun-27 $115 call at 81 delta, short 1x Sep-26
  $160 call at 27 delta.
· Net debit is $3,180, which is also the maximum loss, and net delta
  is 61 so the position tracks about six-tenths of the underlying.
· Short strike at $160 clears the headroom rule, sitting $13.20 above
  the long strike plus debit, so a gap higher cannot lock in a
  structural loss.
· IV rank of 24 with IV running below realised means you are buying
  the long leg cheaply, which is what makes the diagonal preferable
  to a short put spread here.

ENTRY
· Fill the long leg first at the mid, working the order, because the
  deep in-the-money strike carries the wide quote and paying the
  offer on 81 deltas of notional is the largest avoidable cost.
· Sell the short call into any strength on the same or next session,
  once the long leg is filled.
· Re-verify the headroom rule against your actual fills rather than
  these model prices, and lift the short strike if the fills eroded
  the clearance.

EXIT
· Take profit if the position reaches 60% of maximum modelled value
  before the short leg's expiry.
· Stop out on a decisive close back below the 50-day or below the
  pullback low at $128.40, and close the whole structure rather than
  keeping the orphaned long call.

MANAGE
· Roll the short call at 21 days to expiry, or once it decays to
  10-15% of the premium collected, whichever comes first.
· Roll the long leg out before it drops under 90 days to expiry,
  where its own decay begins to accelerate.
· Earnings fall 38 days out and therefore inside the short leg's
  tenor, so plan to be rolled past the date or closed before it.

FLAGS
· No flags raised. Liquidity, headroom and portfolio governance
  checks all passed.
```

### 5.3 Worked example — blocked card

```
NFLX — $78.10 (+5.2%)
Stage 4 · IV rank 39 · IV/RV 0.62 · Earnings in ~55 days
─────────────────────────────────────────────────────────────────
SETUP
· The 52-week, 26-week and 13-week highs are $126.71, $108.95 and
  $91.48 respectively, which is a descending sequence at every
  horizon and therefore a confirmed downtrend rather than a pullback.
· The 52-week low of $65.08 is also the 13-week low, meaning the
  worst price of the year was set within the last quarter and the
  decline has not yet demonstrably ended.
· Today's 5.2% gain arrives directly into the $78-81 shelf that acted
  as support in February and broke, so price is testing overhead
  supply rather than reclaiming a level.

STRUCTURE
· No structure recommended. The trend gate rejects all candidates in
  a confirmed downtrend regardless of the volatility picture.

FLAGS
· TREND BLOCK: requires a close above the 13-week high of $91.48 to
  clear, which would be the first genuine evidence of a trend change.
· PREMIUM-SELLING BLOCK: implied volatility of 31.9% is running well
  below 30-day realised of 51.2%, giving a ratio of 0.62, so any
  short-premium structure would be selling volatility at a steep
  discount to what the stock is actually delivering.
· Long-premium structures are not blocked on volatility grounds and
  would in fact be favourably priced, but remain blocked on trend.
```

That second card is the one that earns the format. It shows exactly which gate fired, what would clear it, and — importantly — that the volatility read and the trend read disagree, which is information you would lose entirely in a one-word verdict.

---

## 6. Step-by-step workflow

Times in AEST. The US close lands at roughly 06:00 AEST during US daylight saving, so the natural rhythm is: the app runs overnight, you read it with coffee, and you act in the final hour of the session that is still open.

### 6.1 Saturday — the weekend cycle

1. The launchd job fires at 09:00 Saturday, after Friday's US close has settled. You do nothing; this step is automatic.
2. It refreshes fundamentals from yfinance and writes the nightly snapshot to parquet.
3. It runs Radar-B across the universe, applying structural gates first and fundamental gates second.
4. It runs Gate E on each survivor to attach a provisional structure.
5. It runs the portfolio governance check, dropping or resizing anything that breaches the delta budget or the correlation gate.
6. It ranks, caps at five, and renders the Weekend Card onto the Today screen.
7. **You open the app and read the five cards.** For each one, note the trigger condition rather than the structure — the structure is provisional and will be recomputed when the trigger actually fires.
8. Arm alerts on the trigger conditions you want to act on. Skip any name whose thesis you do not independently believe; the ranking is a filter, not an instruction.

### 6.2 Weekday morning — responding to a trigger

1. Open the Today screen. Triggered names appear in the signals section, having fired on the prior session's close.
2. Re-run Gate E on the triggered name, because volatility and chain conditions have moved since Saturday and the provisional structure may no longer be the right one.
3. Read the refreshed card in full, paying particular attention to the FLAGS section and to whether the IV/RV ratio has crossed 1.0 in either direction.
4. Check the governance section: confirm the position still fits the aggregate delta budget given anything you have opened since Saturday.
5. If you are proceeding, use the staged order (transmit=False) as the starting point, adjust limits to live quotes, and transmit manually from TWS.
6. Fill the long leg first where the structure has one, then the short leg, per the entry notes on the card.
7. Log the fills back into the app so the shadow-mode performance record stays accurate. This step is easy to skip and destroys the value of everything in §9 if you do.

### 6.3 Ad-hoc — single ticker analysis

1. Enter the ticker and select the intended hold: 1-2 weeks, 4-6 weeks, or months.
2. The app pulls the chain, computes IV rank from your parquet archive, computes IV/RV, term structure and skew, and determines the Weinstein stage.
3. It applies the trend gate first. If the name is in a confirmed downtrend it returns a blocked card and stops, as in §5.3.
4. If the trend gate passes, it applies the selection matrix, solves strikes numerically, and runs the governance check.
5. It renders the card and stages the order.
6. **Read the FLAGS section first.** It is placed last on the card for reading flow but it is the section that most often changes the decision.

### 6.4 Position lifecycle

1. Open positions appear on the Today screen with current Greeks pulled from IBKR.
2. The app surfaces management actions when a roll condition is met: short leg at 21 DTE or decayed to 10-15% of premium collected, long leg approaching 90 DTE.
3. Stop conditions are monitored against the level recorded at entry, not recomputed, so that a drifting moving average cannot quietly widen your stop.
4. On close, the app writes the outcome to the performance log with the entry card attached, so that the record shows what you believed at entry rather than what you remember believing.

### 6.5 Monthly — the review that decides whether any of this works

1. Pull the shadow-mode log: every Radar-B candidate, whether it triggered, and its forward return at 4, 8 and 12 weeks.
2. Compare triggered-and-taken against triggered-and-skipped. A large gap in either direction is informative about your discretionary overlay.
3. Compare the whole set against SPY over the same windows. If the engine is not beating a passive benchmark, the structures are irrelevant.
4. Review which gates did the rejecting. A gate that never rejects anything is not a gate, and a gate that rejects almost everything is miscalibrated.

---

## 7. Delivery — no Telegram

Pull-based, since it's yours alone and you already open the app.

- **Weekend Card** as a section on the Today screen. The five names, each with its Gate E structure, trigger condition, and armed alert.
- **launchd job, Saturday 09:00 AEST** on the mini. Pipeline: refresh fundamentals → Radar-B → Gate E per survivor → governance check → rank → write JSON → render.
- **Trigger alerts:** write to a `signals` table the Today screen reads on load. If you want push, local SMTP to your own inbox is the lowest-friction option — no new service, no new account.
- **Staged orders** via the existing transmit=False path. The weekend output is a loaded gun; nothing enters Monday on the strength of a Saturday ranking.

---

## 8. Build order

1. **Backtest harness.** Radar-B gates + trigger over 5+ years. Measure trigger hit rate, forward returns at 4/8/12 weeks vs SPY, and the triggered-then-failed base rate. This step decides whether the rest is worth building.
2. Fundamental layer + nightly snapshot (useful standalone; starts accumulating history immediately, so build it early even if step 1 is still running).
3. Gate E metrics (IV rank from parquet, IV/RV, term structure, skew).
4. Strike solver + card renderer.
5. Portfolio governance check.
6. Radar-B live in shadow mode, logging only.
7. Weekend Card on Today.
8. Promote out of shadow only when the log evidences edge.

Test parity: the existing suite is at 141. New modules should land with tests, particularly the strike solver — it is the component where a silent error produces a plausible-looking position with wrong Greeks.

---

## 9. What this still doesn't fix

The gates above are reasonable priors, not validated rules. An engine that ranks by unvalidated rules produces output that looks rigorous — scores, ranks, clean cards — whether or not the ranking predicts anything. Interface quality has no relationship to edge quality, and a rendered card is more persuasive than a hand-drawn chart while being no more correct.

Sample size compounds it: five candidates a week across varied structures accumulates evidence slowly. Several months of shadow logging before the results mean anything — unless step 1 is built properly, in which case the timeline is however long the backtest takes to run.

---

## Appendix — running it

```bash
# tests
python -m pytest tests/test_equity_leaps.py -q

# single ticker, no TWS needed (yfinance path)
python -c "from core.yf_client import build_context_yf; \
           from selection import gate_e, cards_e; \
           print(cards_e.to_text(cards_e.render(gate_e.build(build_context_yf('NFLX'),'long'))))"

# weekend pipeline, restricted universe, printed not written
python weekend_card.py --symbols AAPL,MSFT,NFLX --text --dry-run

# API (webapp on :8765)
curl "localhost:8765/api/equity/gate-e?symbol=NFLX&hold=long&mode=yf"
curl "localhost:8765/api/equity/radar?symbols=AAPL,MSFT,NFLX"
```

launchd, Saturday 09:00 AEST:

```xml
<key>StartCalendarInterval</key>
<dict><key>Weekday</key><integer>6</integer>
      <key>Hour</key><integer>9</integer>
      <key>Minute</key><integer>0</integer></dict>
```
