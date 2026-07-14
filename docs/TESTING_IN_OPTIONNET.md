# Testing Campaign Engine strategies in OptionNet Explorer

1. In Campaign Engine v3 choose a historical date, underlying, and account.
   Direction is optional; **Auto** uses the trend/bias known on that date.
2. Press **Auto-build snapshot & rank strategies**. The app retrieves free
   daily price and volatility-index history, then derives spot, IV percentile,
   realised volatility, forward VRP, trend, term proxy, and skew proxy.
   To prevent look-ahead, automatic signals stop at the prior trading close.
3. Open **Advanced** only if the historical surface visible in ONE clearly
   disagrees with a proxy, or if a known FOMC/CPI/NFP event needs adding.
4. The normal view returns one best
   build per family; **Show every account-permitted strategy** adds the other
   variants without changing the ranking method.
5. Add at least the top strategy and its comparator to the same matched-date
   test session. Never choose dates after seeing the subsequent result.
6. Press **Copy ONE recipe**. In ONE use the nearest expiry and strikes that
   were actually listed on that historical date; model targets may not exactly
   match historical listings.
7. Commit the position in ONE at quantity one, advance time using the frozen
   management rules, and record actual legs, entry, exit date, result, maximum
   drawdown and observations in Campaign Engine.

The automatic snapshot does not claim historical option-chain precision.
VIX9D/VIX/VIX3M and SKEW are free market proxies; ONE remains the source of
the actual historical structure and price.

## What the ranking means

- **PRIMARY** — best conditional hypothesis for the supplied market state.
- **COMPARATOR** — the most useful matched-date alternative.
- **RESEARCH** — permitted, but the selected state provides weaker support.
- **NO EDGE** — displayed only in the expanded list; do not treat it as a recommendation.
- **INELIGIBLE** — blocked by account mandate, missing directional thesis, or
  the audit's de-gross rule.

An inverted front blocks every new carry or short-front family. With no
declared direction the correct output is **stand aside**. With a direction,
only debit participation rows are eligible.

All rankings remain hypotheses until matched-date OptionNet results and paper
campaigns provide evidence. Mock and OptionNet tests send nothing to TWS.
