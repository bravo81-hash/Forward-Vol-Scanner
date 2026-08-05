# FVS TradingView companion

`fvs_console_v3.pine` is the current Pine v6 market-state companion. It mirrors
the app's Gate 1 thresholds, HAR-lite forward VRP, high-level Gate 2 family
language, Friday/Monday doctrine and proxy warnings.

It does not reproduce the option chain. Use Forward Vol Scanner for exact
expiries, strikes, surface, liquidity, Greeks, sizing, account rules, portfolio
fit and TimeEdge/TimeZone eligibility.

`archive/te_console_v2_6.pine` is preserved for rollback and comparison.
`parity_fixture.json` is checked by the test suite so app doctrine changes flag
the Pine companion for review.
