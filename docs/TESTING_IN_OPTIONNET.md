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

## Prospective live testing with TWS

1. Start TWS, log in, and enable socket/API clients on port 7496.
2. Choose **Live TWS → ONE**. The app discovers the managed account and NLV.
3. Select the account mandate and press **Connect TWS & rank live strategies**.
4. Press **Copy ONE legs** on the chosen card. Create a new ONE position and
   enter each copied quantity, expiry, strike, and put/call line.
5. Add it as a forward test before advancing time or seeing the result.

**Copy ONE legs** copies a plain-text checklist; it does not automatically
create a ONE trade. Historical cards contain model targets and may need the
nearest listed contract. Live cards use contracts selected from the current
TWS chain. Keep the strategy rule frozen: merely using live data does not make
a result out-of-sample if the setup is changed after its outcome is known.

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
