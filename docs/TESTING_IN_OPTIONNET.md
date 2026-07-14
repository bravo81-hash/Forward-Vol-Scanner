# Testing Campaign Engine strategies in OptionNet Explorer

1. Open ONE and select a historical trading date and a time from 15:00–15:40 ET.
2. In ONE note the underlying price and approximately 30-DTE ATM IV.
3. In Campaign Engine v3 select that same date/time, account, bias, IV band,
   skew, term structure, forward-VRP state, trend and known event state.
4. Press **Rank strategies for this date**. The normal view returns one best
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
