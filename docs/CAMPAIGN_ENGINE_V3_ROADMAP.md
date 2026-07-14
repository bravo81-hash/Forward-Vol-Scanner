# Campaign Engine v3 — Implementation Roadmap

Status: **Approved for implementation planning**  
Branch: `upgrade/campaign-engine-v3`  
Base: `main`

## Product decision

Forward-Vol-Scanner will evolve from a selection-and-staging app into a campaign-management system covering selection, entries, portfolio risk, monitoring, adjustment proposals, exits, reconciliation, and audit history.

"Always-on" means continuous monitoring and ownership of open campaigns. It does **not** mean forced market exposure. Continuous deployment remains a hypothesis until it beats conditional deployment out of sample after costs and tail risk.

Every order remains `transmit=False`. The user reviews and transmits manually in TWS.

## Validation constraint

No historical options dataset will be purchased. Validation will use:

1. Existing market/context history and deterministic synthetic fixtures.
2. Daily point-in-time TWS snapshots captured by the application.
3. Shadow decisions with no orders.
4. Paper campaigns and staged paper orders.
5. Manual OptionNet Explorer testing when historical option-chain replay would otherwise be required.
6. Small live validation only after explicit user approval.

Unsupported rules remain `HYPOTHESIS` or `SHADOW`; missing historical data must never be hidden by optimistic modelling.

## Current baseline

The repository already provides:

- TWS, yfinance, and deterministic mock market contexts.
- Regime, VRP, forward-VRP, skew, and term-structure analytics.
- Six executable strategy families.
- Per-account candidate selection and TWS staging.
- Live NBBO/TWS Greek enrichment.
- Sentinel adjustment guidance.
- SMSF Gate S single-expiry selector.
- Risk budgets, stress rows, and mock/math tests.
- SQLite scan and staging logs.

## Gaps that drive this roadmap

1. Gate S ranks several reference structures that do not yet have executable candidate factories.
2. Hard gates remain advisory, while the staging API accepts browser-supplied legs and quantity.
3. Portfolio risk is evaluated per symbol rather than as one correlated account book.
4. Sizing omits structural max loss, cash, margin, gamma, stress, concentration, and drawdown.
5. Positions are not grouped into persistent campaigns with fills, adjustments, and outcomes.
6. Management is an entry-time template, not a live campaign policy engine.
7. Stress tests omit skew, term, gap, vol-of-vol, and correlated-book shocks.
8. Scan rows are not reconciled to actual trades and outcomes.
9. Dependency locking and continuous integration are absent.

## Target campaign lifecycle

`ELIGIBLE -> STAGED -> OPEN -> DEFENSIVE -> EXIT_PENDING -> CLOSED -> COOLDOWN`

A campaign stores:

- Account, underlying, sleeve, and mandate.
- Original thesis and selector inputs.
- Policy and rule versions.
- Structures, legs, fills, fees, and slippage.
- Current positions, Greeks, cash use, margin, and stress.
- Proposed, rejected, staged, filled, and externally executed actions.
- Management triggers and permitted adjustment families.
- Exit reason and realised outcome.

## Decision layers

The system must keep these separate:

1. **Recommendation** — an idea can be displayed.
2. **Eligibility** — the policy permits initiation.
3. **Risk approval** — the account has room.
4. **Staging approval** — the exact current order is valid and manually approved.

## Phase 0 — reproducible foundation

Deliverables:

- Dependency manifest and locked versions.
- GitHub Actions for unit, API, migration, deterministic replay, lint, and type checks.
- Versioned configuration.
- Architecture decision records.
- Hypothesis registry with evidence states.
- Golden baseline fixtures for current behaviour.

Exit gate: the current suite passes reproducibly without changing live selection behaviour.

## Phase 1 — staging safety boundary

Deliverables:

- Server-generated immutable candidate IDs.
- Maximum candidate age and stale-quote rejection.
- Reconstruct orders from stored candidates instead of trusting browser JSON.
- Re-run mandate, position, policy, and risk checks before staging.
- Reject STAND, blocked, expired, zero-lot, and invalid candidates.
- Exact-order whatIf verification.
- Idempotency key preventing duplicate staging.
- Central account/mandate configuration.
- Audit every denied staging attempt.

Exit gate: browser payload changes cannot alter the server-approved combo or stage a blocked candidate.

## Phase 2 — campaign ledger and TWS reconciliation

Structured records:

- Accounts and mandates.
- Campaigns, structures, and legs.
- Market/context snapshots.
- Decisions and ranked candidates.
- Order intents, TWS orders, fills, and commissions.
- Adjustments and daily campaign marks.
- Realised/unrealised P&L.
- Rule versions, manual overrides, exits, and outcomes.

Reconciliation states:

- Exact match.
- Partial fill.
- Quantity mismatch.
- Unassigned position.
- Missing or externally closed leg.
- TWS unavailable.

Ambiguous position grouping requires user confirmation; the system never silently assigns uncertain legs.

Exit gate: paper TWS positions and fills reconcile into campaigns with complete cost and P&L history.

## Phase 3 — validation and replay harness

Two paths:

### Context replay

Re-run historical market/regime snapshots through the selector to test classification stability, recommendation frequency, and policy behaviour.

### Position validation

Evaluate proposed structures using captured option snapshots, paper fills, and manual OptionNet Explorer results. The system records manual results as evidence rather than pretending to have unavailable chain history.

Reports:

- Coverage and stand-aside frequency.
- Conditional versus continuously deployed campaigns.
- Performance by regime cell, underlying, and structure.
- Slippage sensitivity.
- Parameter sensitivity around thresholds.
- Chronological out-of-sample results.
- Stress and drawdown.
- Evidence cards linked to hypotheses.

Exit gate: deterministic evidence reports can promote, retain, or reject a hypothesis without changing code.

## Phase 4 — selector v2 and executable strategy factories

Unified contract:

`Context x AccountMandate x PortfolioState x Intent -> RankedCandidate[]`

Separate classification, eligibility, construction, scoring, portfolio fit, risk approval, and display ranking.

Factories to add:

- Balanced ATM put fly.
- Iron fly.
- Wide OTM put fly.
- Call BWB.
- M3-style put BWB plus same-expiry ITM call.
- Directional target fly.
- Same-expiry debit adjustment structures.

Every candidate supplies exact legs, live/model prices, Risk Navigator Greeks, max profit/loss, breakevens, cash, whatIf margin, liquidity, management policy, stress, and evidence status.

Exit gate: every actionable Gate S recommendation is executable; reference-only hypotheses are explicitly labelled and cannot receive automatic risk approval.

## Phase 5 — account-level portfolio governor

Evaluate:

- Delta and delta dollars.
- Gamma and gamma-dollar exposure.
- Vega by expiry bucket.
- Theta.
- Structural max loss.
- Cash and excess liquidity.
- TWS whatIf margin.
- Per-campaign and total risk.
- Underlying/expiry concentration.
- Correlated-equivalent SPX exposure.
- Full-book stress.
- Drawdown state.
- Event concentration.
- Liquidity and exit capacity.

Sizing becomes a whole-lot constrained search using signed post-trade exposure.

Exit gate: before/after portfolio risk and staged whatIf results agree with TWS within documented tolerances. The user will be guided through a small Risk Navigator comparison at this later stage.

## Phase 6 — monitoring and management engine

Daily process:

- Reconcile positions and fills.
- Mark campaigns.
- Recompute Greeks, P&L, cash, margin, and stress.
- Evaluate profit, loss, DTE, touch, event, and risk triggers.
- Produce ranked actions for the 15:00-15:40 ET review window.
- Never stage automatically.

Action hierarchy:

1. Close or reduce.
2. Hold.
3. Roll.
4. Re-centre.
5. Add a risk-reducing debit structure.
6. Add an income layer only when it independently passes fresh-entry and portfolio-risk gates.

An adjustment must improve a binding risk measure without unacceptable deterioration elsewhere. Sentinel becomes an input; actual proposed-leg Greeks replace static sign templates.

Exit gate: open paper campaigns produce reproducible, policy-versioned actions and complete audit records.

## Phase 7 — campaign dashboard

Retain Flask initially.

Views:

- Today.
- Campaigns.
- Portfolio.
- Opportunities.
- Order review.
- Reconciliation.
- Research.
- Journal.
- Settings.

Primary action states:

- NO ACTION
- REVIEW
- ENTER
- ADJUST
- REDUCE
- EXIT
- DATA INVALID

Exit gate: the complete paper workflow can be operated from the browser without terminal work.

## Phase 8 — shadow and paper rollout

Promotion order:

1. Context replay.
2. Live shadow mode.
3. Paper TWS staging and manual transmission.
4. Paper campaign lifecycle including adjustments and exits.
5. Small live pilot after explicit approval.
6. Broader activation only after sufficient evidence.

The paper sample must cover quiet, trending, high-IV, and event conditions; elapsed days alone are not sufficient.

## Phase 9 — release

- Recovery and migration tests.
- User operating guide.
- Known limitations and rejected hypotheses.
- Paper results and evidence review.
- Final user acceptance.
- Squash merge to `main`.

## Non-goals

- Automatic order transmission.
- Purchasing historical options data.
- Black-box machine-learning selection.
- Frontend rewrite before the risk and campaign engines are stable.
- Treating OptionNet Explorer manual tests as statistically complete historical evidence.

## User checkpoints

1. Roadmap approval — complete.
2. Historical-data decision — complete: no purchase; use capture, paper, and OptionNet manual testing.
3. Guided TWS/Risk Navigator comparison — deferred to the portfolio-governor phase.
4. Paper-results review and merge approval — deferred to release.
