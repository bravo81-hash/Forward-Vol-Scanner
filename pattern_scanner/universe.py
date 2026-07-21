"""Build the scan universe: S&P 500 + NASDAQ 100 + liquid ETFs.

Primary source for the index constituents is Wikipedia (current membership).
If that fetch fails (offline / page-layout change) a bundled curated large-cap
subset is used instead so the scanner always runs. Requires network for the
Wikipedia fetch; the ETF + fallback lists are local.
"""
import io

import pandas as pd

# Wikipedia 403s the default urllib user-agent; use a browser UA.
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}


def _read_tables(url):
    """Fetch HTML tables with a real user-agent (Wikipedia blocks the default)."""
    try:
        import requests  # bundled with yfinance
        r = requests.get(url, headers=_HEADERS, timeout=20)
        r.raise_for_status()
        return pd.read_html(io.StringIO(r.text))
    except ImportError:
        # pandas can pass headers via storage_options on newer versions
        return pd.read_html(url, storage_options=_HEADERS)


# Wikipedia 403s datacenter IPs (GitHub Actions) even with a browser UA. This
# Frictionless dataset is reliably reachable from cloud runners and stays current.
_GH_SP500_CSV = ("https://raw.githubusercontent.com/datasets/"
                 "s-and-p-500-companies/main/data/constituents.csv")


def _github_sp500():
    """Cloud-reachable S&P 500 fallback source. Returns a symbol list or None."""
    try:
        import requests
        r = requests.get(_GH_SP500_CSV, headers=_HEADERS, timeout=20)
        r.raise_for_status()
        df = pd.read_csv(io.StringIO(r.text))
        col = "Symbol" if "Symbol" in df.columns else df.columns[0]
        syms = [str(s).replace(".", "-").strip().upper() for s in df[col].tolist()]
        syms = [s for s in syms if s and s.isascii() and 1 <= len(s) <= 6]
        return syms if len(syms) > 400 else None
    except Exception:
        return None

LIQUID_ETFS = [
    "SPY", "QQQ", "IWM", "DIA", "XLF", "XLK", "XLE", "XLV", "XLI", "XLY",
    "XLP", "XLU", "XLB", "XLRE", "XLC", "SMH", "SOXX", "ARKK", "GLD", "SLV",
    "TLT", "HYG", "LQD", "EEM", "EFA", "FXI", "GDX", "GDXJ", "KRE", "XBI",
    "XOP", "XHB", "ITB", "IBB", "VNQ", "USO", "UNG", "DBA", "XME", "TAN",
    "JETS", "KWEB", "VWO", "VEA", "EWZ", "EWW", "INDA", "BITO", "ARKG",
]

# Curated liquid large-caps used only if the Wikipedia fetch fails.
FALLBACK_STOCKS = [
    "AAPL", "MSFT", "AMZN", "GOOGL", "GOOG", "META", "NVDA", "TSLA", "AVGO",
    "AMD", "NFLX", "ADBE", "CRM", "ORCL", "CSCO", "INTC", "QCOM", "TXN",
    "MU", "AMAT", "INTU", "NOW", "PANW", "SNOW", "PLTR", "UBER", "ABNB",
    "SHOP", "SQ", "PYPL", "COIN", "MRVL", "ASML", "ARM", "SMCI", "DELL",
    "JPM", "BAC", "WFC", "GS", "MS", "C", "SCHW", "BLK", "AXP", "V", "MA",
    "BRK-B", "UNH", "JNJ", "LLY", "PFE", "MRK", "ABBV", "TMO", "ABT", "DHR",
    "BMY", "AMGN", "GILD", "CVS", "MDT", "ISRG", "VRTX", "REGN", "HUM",
    "XOM", "CVX", "COP", "SLB", "EOG", "MPC", "PSX", "OXY", "HAL", "DVN",
    "WMT", "COST", "HD", "LOW", "TGT", "NKE", "MCD", "SBUX", "PG", "KO",
    "PEP", "PM", "MDLZ", "CL", "DIS", "CMCSA", "T", "VZ", "TMUS", "CAT",
    "DE", "BA", "GE", "HON", "UPS", "FDX", "LMT", "RTX", "MMM", "EMR",
    "F", "GM", "RIVN", "LCID", "NEE", "DUK", "SO", "LIN", "FCX", "NUE",
    "DOW", "ENPH", "FSLR", "MARA", "RIOT", "DKNG", "ROKU", "ZM", "DDOG",
    "NET", "CRWD", "ZS", "MDB", "OKTA", "TTD", "SPOT", "PINS", "SNAP",
]

_SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
_NDX_URL = "https://en.wikipedia.org/wiki/Nasdaq-100"


# --- ASX ~100 (ASX 50 + liquid mid-caps). Bare codes; ".AX" added per market.
# Index membership guarantees liquidity; stale/renamed codes are dropped by the
# data layer (no Yahoo history -> skipped), so quarterly rebalances are tolerated.
ASX_100 = [
    "CBA", "BHP", "CSL", "NAB", "WBC", "ANZ", "MQG", "WES", "GMG", "FMG",
    "WDS", "TLS", "RIO", "TCL", "WOW", "ALL", "REA", "COL", "STO", "QBE",
    "RMD", "FPH", "ORG", "SUN", "AMC", "JHX", "COH", "BXB", "SHL", "IAG",
    "ASX", "S32", "MIN", "PME", "XRO", "CPU", "NST", "WTC", "CAR", "ALD",
    "MPL", "AIA", "EDV", "TWE", "RHC", "QAN", "SGP", "VCX", "GPT", "MGR",
    "DXS", "CHC", "LLC", "APA", "AGL", "AZJ", "ORI", "BSL", "NEM", "EVN",
    "RRL", "PDN", "LYC", "IGO", "SFR", "WHC", "BPT", "JBH", "HVN", "SUL",
    "FLT", "WEB", "A2M", "DOW", "ANN", "ARB", "BRG", "REH", "SOL", "TNE",
    "NXT", "CWY", "SEK", "SGM", "MND", "NHF", "MFG", "AMP", "BEN", "BOQ",
    "NWL", "HUB", "PNI", "CGF", "TPG", "NEC", "NWS", "TLC", "EBO", "CIA",
    "CMM", "PLS", "LTR", "NIC", "SGR", "TAH", "QUB", "ALX", "VNT",
    "GMD", "PNV", "IEL", "ALQ", "ORA",
]

# --- India ~100 (NIFTY 50 + Next 50). Bare NSE codes; ".NS" added per market.
INDIA_100 = [
    "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "ITC", "SBIN",
    "BHARTIARTL", "LT", "HINDUNILVR", "BAJFINANCE", "KOTAKBANK", "AXISBANK",
    "ASIANPAINT", "MARUTI", "SUNPHARMA", "TITAN", "ULTRACEMCO", "WIPRO",
    "NESTLEIND", "ONGC", "NTPC", "POWERGRID", "TATAMOTORS", "HCLTECH",
    "ADANIENT", "ADANIPORTS", "JSWSTEEL", "TATASTEEL", "COALINDIA",
    "BAJAJFINSV", "HDFCLIFE", "SBILIFE", "DRREDDY", "CIPLA", "GRASIM",
    "BRITANNIA", "EICHERMOT", "HEROMOTOCO", "APOLLOHOSP", "BPCL", "TATACONSUM",
    "INDUSINDBK", "HINDALCO", "TECHM", "LTIM", "SHRIRAMFIN", "TRENT", "BEL",
    "M&M", "BAJAJ-AUTO", "DIVISLAB", "DMART", "PIDILITIND", "GODREJCP",
    "DABUR", "MARICO", "COLPAL", "BERGEPAINT", "HAVELLS", "SIEMENS", "ABB",
    "BANKBARODA", "PNB", "CANBK", "IOC", "GAIL", "VEDL", "AMBUJACEM", "ACC",
    "SHREECEM", "DLF", "GODREJPROP", "ICICIPRULI", "ICICIGI", "HDFCAMC",
    "SBICARD", "NAUKRI", "BIOCON", "LUPIN", "AUROPHARMA", "TORNTPHARM",
    "ALKEM", "MUTHOOTFIN", "CHOLAFIN", "PEL", "NMDC", "SAIL", "JINDALSTEL",
    "TATAPOWER", "ADANIGREEN", "ADANIPOWER", "NHPC", "IRCTC", "PFC", "RECLTD",
    "HAL", "BHEL", "UNITDSPR", "TVSMOTOR", "ZYDUSLIFE", "POLYCAB", "CGPOWER",
    "DIXON", "JIOFIN", "IRFC", "BOSCHLTD", "VBL",
]


def _wiki_symbols(url, candidate_cols):
    tables = _read_tables(url)
    for t in tables:
        for col in candidate_cols:
            if col in t.columns:
                syms = [str(s).replace(".", "-").strip().upper() for s in t[col].tolist()]
                syms = [s for s in syms if s and s.isascii() and 1 <= len(s) <= 6]
                if len(syms) > 50:
                    return syms
    raise ValueError(f"no symbol column in {url}")


def _wiki_safe(url, cols):
    try:
        return _wiki_symbols(url, cols)
    except Exception:
        return None


def build_universe(verbose=True):
    """S&P 500 + NASDAQ-100 + liquid ETFs, resilient to a Wikipedia 403.

    Order of preference for the S&P 500: Wikipedia (authoritative, current) ->
    GitHub Frictionless dataset (cloud-reachable) -> bundled large-cap subset.
    NASDAQ-100 comes from Wikipedia when reachable; otherwise the bundled
    growth/large-cap list supplements the major non-S&P names (ASML, ARM, ...).
    """
    syms = set(LIQUID_ETFS)

    sp = _wiki_safe(_SP500_URL, ["Symbol", "Ticker"])
    if sp:
        src = "wikipedia"
    else:
        sp = _github_sp500()
        if sp:
            src = "github-dataset (wikipedia 403)"
        else:
            sp, src = FALLBACK_STOCKS, "bundled subset (all fetches failed)"
    syms |= set(sp)

    ndx = _wiki_safe(_NDX_URL, ["Ticker", "Symbol"])
    if ndx:
        syms |= set(ndx)
    else:
        syms |= set(FALLBACK_STOCKS)   # covers major NDX/growth names off-Wikipedia

    if verbose:
        print(f"[universe] {len(syms)} symbols (S&P 500 via {src})")
    return sorted(syms)


def universe_for(market="us", verbose=True):
    """Return the yfinance-ready symbol list for a market (suffix applied)."""
    if market == "us":
        return build_universe(verbose=verbose)
    if market == "asx":
        syms = sorted({s + ".AX" for s in ASX_100})
    elif market == "in":
        syms = sorted({s + ".NS" for s in INDIA_100})
    else:
        raise ValueError(f"unknown market: {market}")
    if verbose:
        print(f"[universe] {len(syms)} {market.upper()} symbols (curated index list)")
    return syms
