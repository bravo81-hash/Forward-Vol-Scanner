"""Fundamental layer for Radar-B — yfinance first, IBKR optional.

Doctrine (mirrors pa_scanner's live-fail-loud mode): a fetch failure marks the
symbol UNRATED and drops it from ranking. It NEVER scores as neutral, because
a neutral score quietly promotes exactly the names we have no information on.

The single highest-value field is the 90-day forward EPS estimate trend. A
stock down 40% with rising forward estimates is multiple compression; the same
stock with falling estimates is a deteriorating business. yfinance exposes this
directly via `eps_trend`, so no paid data source is required.

Snapshots are appended to a local store so that after a few months we hold our
own point-in-time revision history, which beats the endpoint's restated view.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

OK, UNRATED = "OK", "UNRATED"

# Gate thresholds — see docs/equity_leaps.md §3.1
MAX_NET_DEBT_EBITDA = 3.5
MIN_REVENUE_GROWTH = 0.0
EPS_TREND_TOLERANCE = 0.0

SNAPSHOT_DIR = Path(os.environ.get("FVS_FUNDAMENTAL_DIR", "data/fundamentals"))


@dataclass
class Fundamentals:
    symbol: str
    status: str = UNRATED
    reason: str | None = None
    revenue_growth: float | None = None
    fcf: float | None = None
    operating_margin: float | None = None
    operating_margin_previous: float | None = None
    net_debt_ebitda: float | None = None
    eps_fwd_current: float | None = None
    eps_fwd_90d: float | None = None
    eps_trend_90d: float | None = None      # fractional change, current vs 90d ago
    revisions_up_30d: int | None = None
    revisions_down_30d: int | None = None
    as_of: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict:
        return asdict(self)


# ------------------------------------------------------------- helpers ----
def _num(v):
    try:
        f = float(v)
        return f if f == f and abs(f) != float("inf") else None
    except (TypeError, ValueError):
        return None


def _cell(frame, row_key: str, col):
    """Pull one cell from a yfinance DataFrame, tolerating layout drift."""
    if frame is None or getattr(frame, "empty", True):
        return None
    try:
        for idx in frame.index:
            if str(idx).strip().lower() == row_key.strip().lower():
                return _num(frame.loc[idx, col])
    except Exception:
        return None
    return None


def _latest_col(frame):
    if frame is None or getattr(frame, "empty", True):
        return None


def _row_values(frame, row_key: str) -> list[float]:
    """Newest-first numeric statement values for one named row."""
    if frame is None or getattr(frame, "empty", True):
        return []
    try:
        idx = next(i for i in frame.index
                   if str(i).strip().lower() == row_key.strip().lower())
        cols = sorted(frame.columns, reverse=True)
        return [v for c in cols if (v := _num(frame.loc[idx, c])) is not None]
    except Exception:
        return []


def _ttm_pair(frame, row_key: str) -> tuple[float | None, float | None]:
    values = _row_values(frame, row_key)
    if len(values) < 8:
        return None, None
    return sum(values[:4]), sum(values[4:8])
    try:
        return frame.columns[0]
    except Exception:
        return None


def _eps_trend(tkr) -> tuple[float | None, float | None, float | None]:
    """(current, 90d-ago, fractional change). yfinance `eps_trend` frame."""
    try:
        frame = tkr.eps_trend
    except Exception:
        return None, None, None
    if frame is None or getattr(frame, "empty", True):
        return None, None, None
    try:
        # Prefer the next-fiscal-year row; fall back to whatever is first.
        idx = "+1y" if "+1y" in frame.index else frame.index[0]
        cur = _num(frame.loc[idx, "current"])
        ago = _num(frame.loc[idx, "90daysAgo"])
    except Exception:
        return None, None, None
    if cur is None or ago is None or ago == 0:
        return cur, ago, None
    return cur, ago, (cur - ago) / abs(ago)


def _revisions(tkr) -> tuple[int | None, int | None]:
    try:
        frame = tkr.eps_revisions
    except Exception:
        return None, None
    if frame is None or getattr(frame, "empty", True):
        return None, None
    try:
        idx = "+1y" if "+1y" in frame.index else frame.index[0]
        up = _num(frame.loc[idx, "upLast30days"])
        dn = _num(frame.loc[idx, "downLast30days"])
    except Exception:
        return None, None
    return (int(up) if up is not None else None,
            int(dn) if dn is not None else None)


# ---------------------------------------------------------------- fetch ---
def fetch_fundamentals(symbol: str, *, ticker=None) -> Fundamentals:
    """Fetch one symbol. Never raises — returns UNRATED with a reason instead."""
    f = Fundamentals(symbol=symbol)
    try:
        if ticker is None:
            import yfinance as yf
            ticker = yf.Ticker(symbol)

        info = {}
        try:
            info = ticker.info or {}
        except Exception:
            info = {}

        quarterly = getattr(ticker, "quarterly_income_stmt", None)
        revenue_ttm, revenue_prior = _ttm_pair(quarterly, "Total Revenue")
        if revenue_ttm is not None and revenue_prior not in (None, 0):
            f.revenue_growth = (revenue_ttm - revenue_prior) / abs(revenue_prior)

        op_ttm, op_prior = _ttm_pair(quarterly, "Operating Income")
        if revenue_ttm not in (None, 0) and op_ttm is not None:
            f.operating_margin = op_ttm / revenue_ttm
        if revenue_prior not in (None, 0) and op_prior is not None:
            f.operating_margin_previous = op_prior / revenue_prior
        f.fcf = _num(info.get("freeCashflow"))

        if f.fcf is None:
            cf = getattr(ticker, "cashflow", None)
            col = _latest_col(cf)
            if col is not None:
                f.fcf = _cell(cf, "Free Cash Flow", col)

        ebitda = _num(info.get("ebitda"))
        total_debt = _num(info.get("totalDebt"))
        cash = _num(info.get("totalCash"))
        if ebitda and ebitda > 0 and total_debt is not None and cash is not None:
            f.net_debt_ebitda = (total_debt - cash) / ebitda

        f.eps_fwd_current, f.eps_fwd_90d, f.eps_trend_90d = _eps_trend(ticker)
        f.revisions_up_30d, f.revisions_down_30d = _revisions(ticker)

        # The outlook gate is the point of this module. Without it we are
        # UNRATED, however complete the rest of the payload looks.
        if f.eps_trend_90d is None:
            f.status, f.reason = UNRATED, "no forward EPS estimate trend available"
            return f
        if f.revenue_growth is None:
            f.status, f.reason = UNRATED, "no revenue growth available"
            return f

        f.status = OK
        return f
    except Exception as exc:                      # fail loud, never silent
        f.status, f.reason = UNRATED, f"fetch failed: {type(exc).__name__}: {exc}"
        return f


def fetch_many(symbols: list[str]) -> dict[str, Fundamentals]:
    return {s: fetch_fundamentals(s) for s in symbols}


# ---------------------------------------------------------------- gates ---
def fundamental_gates(f: Fundamentals) -> tuple[bool, list[str]]:
    """Return (passed, reasons). Reasons are full sentences for the card."""
    if f.status != OK:
        return False, [f"Fundamental data is unavailable for {f.symbol} "
                       f"({f.reason}), so the name is marked UNRATED and "
                       f"dropped from ranking rather than scored as neutral."]

    reasons: list[str] = []
    passed = True

    if f.revenue_growth is not None and f.revenue_growth <= MIN_REVENUE_GROWTH:
        passed = False
        reasons.append(
            f"Trailing revenue growth is {f.revenue_growth * 100:.1f}%, which is "
            f"not positive, so the business is shrinking rather than merely "
            f"de-rating.")

    margin_ok = (f.operating_margin is not None
                 and f.operating_margin_previous is not None
                 and f.operating_margin > 0
                 and f.operating_margin > f.operating_margin_previous)
    if not (f.fcf and f.fcf > 0) and not margin_ok:
        passed = False
        reasons.append(
            "Free cash flow is not positive and operating margin is not both "
            "positive and improving, so there is no verified profitability "
            "floor under the valuation.")

    if f.net_debt_ebitda is None:
        passed = False
        reasons.append(
            "Net debt to EBITDA cannot be verified because debt, cash or positive "
            "EBITDA is unavailable, so leverage is unknown and the balance-sheet "
            "gate blocks the name.")
    elif f.net_debt_ebitda >= MAX_NET_DEBT_EBITDA:
        passed = False
        reasons.append(
            f"Net debt to EBITDA is {f.net_debt_ebitda:.1f}x, at or above the "
            f"strict {MAX_NET_DEBT_EBITDA}x ceiling, so a further drawdown carries "
            f"balance-sheet risk on top "
            f"of price risk.")

    if f.eps_trend_90d is not None and f.eps_trend_90d < EPS_TREND_TOLERANCE:
        passed = False
        reasons.append(
            f"Forward EPS estimates have fallen {abs(f.eps_trend_90d) * 100:.1f}% "
            f"over the past 90 days, which makes this a deteriorating business "
            f"rather than multiple compression, and this gate rejects hard.")
    elif f.eps_trend_90d is not None:
        reasons.append(
            f"Forward EPS estimates have moved {f.eps_trend_90d * 100:+.1f}% over "
            f"the past 90 days, so the drawdown looks like multiple compression "
            f"rather than deteriorating fundamentals.")

    return passed, reasons


# ------------------------------------------------------------- snapshot ---
def snapshot(records: dict[str, Fundamentals], *, day: date | None = None,
             directory: Path | None = None) -> Path:
    """Append today's fundamentals to a local JSONL store.

    Point-in-time and never restated, so after a few months this is a better
    revision history than the endpoint returns.
    """
    directory = Path(directory or SNAPSHOT_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    day = day or date.today()
    path = directory / f"{day:%Y-%m}.jsonl"
    incoming = {(rec.symbol, day.isoformat()) for rec in records.values()}
    retained: list[str] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                old = json.loads(line)
            except json.JSONDecodeError:
                retained.append(line)
                continue
            if (old.get("symbol"), old.get("snapshot_date")) not in incoming:
                retained.append(line)

    with path.open("w", encoding="utf-8") as fh:
        for line in retained:
            fh.write(line + "\n")
        for rec in records.values():
            row = rec.to_dict()
            row["snapshot_date"] = day.isoformat()
            fh.write(json.dumps(row) + "\n")
    return path


def load_history(symbol: str, *, directory: Path | None = None) -> list[dict]:
    directory = Path(directory or SNAPSHOT_DIR)
    if not directory.exists():
        return []
    rows: list[dict] = []
    for path in sorted(directory.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("symbol") == symbol:
                rows.append(row)
    return sorted(rows, key=lambda r: r.get("snapshot_date", ""))
