"""Application service for multi-symbol Value Entry Put scans."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

from .engine import MULTIPLES_REVIEWED, choose_candidates
from .providers import is_transient, mock_symbol, with_retry, yahoo_symbol

DEFAULT_SYMBOLS = ["AAPL", "MSFT", "GOOGL", "BAC", "JPM", "XOM", "AAL"]
VALID_MODES = {"cash_secured", "margin_efficient", "defined_risk"}


def _scan_one_retrying(symbol: str, **kwargs):
    """Retry transient upstream failures so a rate-limited sweep does not
    silently return a partial shortlist as if it were the whole universe."""
    return with_retry(_scan_one, symbol, **kwargs)


def _scan_one(symbol: str, *, source: str, mode: str, overrides: dict,
              hurdle_rate: float, nlv: float, available_cash: float,
              sector_capacity: float, min_dte: int, max_dte: int) -> dict:
    company, quotes = (mock_symbol(symbol) if source == "mock"
                       else yahoo_symbol(symbol, min_dte, max_dte))
    override = overrides.get(symbol, overrides.get(symbol.upper()))
    return choose_candidates(
        company, quotes, mode=mode, acquisition_override=override,
        hurdle_rate=hurdle_rate, nlv=nlv, available_cash=available_cash,
        sector_capacity=sector_capacity, min_dte=min_dte, max_dte=max_dte,
    )


def scan_value_puts(*, symbols: list[str] | None = None, source: str = "mock",
                    mode: str = "cash_secured", overrides: dict | None = None,
                    hurdle_rate: float = .08, nlv: float = 100_000,
                    available_cash: float = 50_000,
                    sector_limit_pct: float = .20,
                    min_dte: int = 45, max_dte: int = 390) -> dict:
    symbols = list(dict.fromkeys(
        str(value).strip().upper() for value in (symbols or DEFAULT_SYMBOLS)
        if str(value).strip()))[:25]
    if source not in {"mock", "yf"}:
        raise ValueError("source must be 'mock' or 'yf'")
    if mode not in VALID_MODES:
        raise ValueError(f"mode must be one of {sorted(VALID_MODES)}")
    if not 30 <= min_dte < max_dte <= 730:
        raise ValueError("DTE window must satisfy 30 <= minimum < maximum <= 730")
    if nlv <= 0 or available_cash < 0:
        raise ValueError("NLV must be positive and available cash cannot be negative")
    overrides = {str(key).upper(): float(value)
                 for key, value in (overrides or {}).items() if float(value) > 0}
    sector_capacity = nlv * max(min(sector_limit_pct, .50), 0)
    rows, errors = [], []
    workers = min(4, max(len(symbols), 1)) if source == "yf" else 1
    with ThreadPoolExecutor(max_workers=workers) as pool:
        jobs = {
            pool.submit(_scan_one_retrying, symbol, source=source, mode=mode,
                        overrides=overrides, hurdle_rate=hurdle_rate, nlv=nlv,
                        available_cash=available_cash, sector_capacity=sector_capacity,
                        min_dte=min_dte, max_dte=max_dte): symbol
            for symbol in symbols
        }
        for future in as_completed(jobs):
            try:
                rows.append(future.result())
            except Exception as exc:  # noqa: BLE001
                errors.append({"symbol": jobs[future], "error": str(exc),
                               "kind": "fetch_failed",
                               "transient": is_transient(exc)})
    status_order = {"QUALIFIED": 0, "WATCH": 1, "SPECULATIVE": 2, "REJECTED": 3}
    rows.sort(key=lambda row: (
        status_order.get((row.get("candidate") or {}).get("status"), 4),
        -(row.get("candidate") or {}).get("score", 0),
    ))
    counts = {key: sum((row.get("candidate") or {}).get("status") == key for row in rows)
              for key in status_order}
    return {
        "policy_id": "value-entry-put-v1", "source": source, "mode": mode,
        "rows": rows, "errors": errors,
        "valuation_multiples_reviewed": MULTIPLES_REVIEWED,
        "coverage": {"requested": len(symbols), "scanned": len(rows),
                     "fetch_failed": [e["symbol"] for e in errors],
                     "complete": not errors},
        "summary": {
            "symbols_requested": len(symbols), "symbols_scanned": len(rows),
            **{key.lower(): value for key, value in counts.items()},
            "assignment_capital": round(sum(
                (row.get("candidate") or {}).get("sizing", {}).get(
                    "full_assignment_cost", 0) for row in rows
                if (row.get("candidate") or {}).get("status") != "REJECTED"), 2),
        },
        "assumptions": {
            "hurdle_rate_pct": round(hurdle_rate * 100, 2), "nlv": nlv,
            "available_cash": available_cash,
            "sector_limit_pct": round(sector_limit_pct * 100, 1),
            "dte_window": [min_dte, max_dte],
            "execution_price": "bid plus 25% of bid/ask spread",
            "position_sizing": "full assignment exposure, not current broker buying power",
        },
        "score_weights": {
            "business_quality": 25, "valuation_margin": 25,
            "cash_yield": 15, "volatility_edge": 15,
            "liquidity": 10, "stress_survivability": 10,
        },
    }
