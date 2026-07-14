# Testing Campaign Engine strategies in OptionNet Explorer

1. Run `python webapp.py` and open <http://127.0.0.1:8765/campaigns>.
2. Select the account type, underlying, and directional intent.
3. Use **Build test strategies** for the system-ranked rows or enable **Show every permitted strategy** for the full laboratory.
4. Press **Copy ONE legs** and enter those exact legs in OptionNet Explorer at quantity one.
5. Inspect T+0/T+5 shape, entry debit/credit, cash or margin, Greeks, breakevens, tent movement under spot changes, and IV/skew changes.
6. Press **Create OptionNet test** in the app.
7. After testing, record rating, result, maximum drawdown, and observations under **Test campaigns**.

The app stores manual observations as evidence. They do not automatically activate a strategy rule.

## Suggested first comparisons

- Put BWB versus balanced ATM put fly in normal IV with rich put skew.
- Iron fly versus balanced put fly in high IV with flat skew.
- Wide OTM put fly versus put BWB in high IV with steep skew.
- M3-style BWB plus ITM call at different call depths and BWB widths.
- Call BWB as both an entry and an upside adjustment.
- Target fly at 0.75 and 1.0 expected-move targets.

## Safety

- Mock and OptionNet tests send nothing to TWS.
- Hypothesis strategies are blocked from non-mock TWS staging.
- The app never transmits an order automatically.
