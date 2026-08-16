"""Automated stock-universe discovery for the Value Entry Put Scanner."""
from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import yaml

from .engine import Company, quality_company, value_company
from .providers import MOCK_COMPANIES, is_transient, mock_company_snapshot, with_retry, yahoo_company_snapshot

ROOT = Path(__file__).resolve().parents[1]
UNIVERSE_FILE = ROOT / "config" / "stock_universe.yaml"
FINANCIAL_SECTORS = {"Financial Services", "Financials"}


def _finite(value, default=None):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _clamp(value: float, lo: float, hi: float) -> float:
    return min(max(value, lo), hi)


def curated_symbols() -> list[str]:
    """Return maintained liquid US single stocks, never ETFs."""
    with UNIVERSE_FILE.open("r", encoding="utf-8") as fh:
        rows = (yaml.safe_load(fh) or {}).get("stocks", [])
    symbols = [str(row.get("symbol") or "").strip().upper() for row in rows]
    symbols.extend(MOCK_COMPANIES)
    return list(dict.fromkeys(symbol for symbol in symbols if symbol))


def _reachability_score(company: Company, valuation: dict) -> tuple[float, float | None]:
    acquisition = _finite(valuation.get("acquisition_price"))
    if not acquisition or company.spot <= 0:
        return 0.0, None
    gap = (company.spot - acquisition) / company.spot
    # Long puts are most useful when the desired basis is meaningfully below
    # spot but not so remote that the chain is unlikely to compensate it.
    if gap <= 0:
        score = 12.0
    elif gap <= 0.25:
        score = 6.0 + gap / 0.25 * 9.0
    elif gap <= 0.45:
        score = 15.0 - (gap - 0.25) / 0.20 * 6.0
    else:
        score = max(0.0, 9.0 - (gap - 0.45) / 0.20 * 9.0)
    return score, gap


def evaluate_discovery_company(
    company: Company,
    metadata: dict,
    *,
    min_market_cap: float,
    min_average_dollar_volume: float,
    min_quality: float,
    max_leverage: float,
    max_price_to_fcf: float,
) -> dict:
    """Apply sector-aware quality gates before any option-chain request."""
    quality = quality_company(company)
    valuation = value_company(company)
    cap = _finite(company.market_cap)
    dollar_volume = _finite(metadata.get("average_dollar_volume"), 0.0)
    high_52w = _finite(metadata.get("high_52w"))
    drawdown = ((high_52w - company.spot) / high_52w
                if high_52w and high_52w > 0 else None)
    p_fcf = (company.spot / company.fcf_per_share
             if _finite(company.fcf_per_share, 0) > 0 else None)
    leverage = _finite(company.net_debt_ebitda)
    financial = company.sector in FINANCIAL_SECTORS

    blocks: list[str] = []
    cautions: list[str] = []
    if cap is None:
        cautions.append("market capitalisation unavailable")
    elif cap < min_market_cap:
        blocks.append("market capitalisation below the discovery floor")
    if company.spot < 10:
        blocks.append("share price below $10")
    if dollar_volume < min_average_dollar_volume:
        blocks.append("average dollar volume below the liquidity floor")
    if quality["score"] < min_quality:
        blocks.append("business-quality score below the selected minimum")

    if financial:
        if _finite(company.normalized_eps, 0) <= 0:
            blocks.append("positive earnings not established")
        if _finite(company.book_value_per_share, 0) <= 0:
            cautions.append("book value unavailable for financial-company model")
        if _finite(company.return_on_equity) is None:
            cautions.append("return on equity unavailable for financial-company review")
        elif company.return_on_equity < 0.08:
            blocks.append("return on equity below 8%")
    else:
        if _finite(company.normalized_eps, 0) <= 0:
            blocks.append("positive normalised earnings not established")
        if _finite(company.fcf_per_share, 0) <= 0:
            blocks.append("positive free cash flow not established")
        if leverage is None:
            cautions.append("leverage requires manual verification")
        elif leverage > max_leverage:
            blocks.append("net debt/EBITDA above the selected maximum")
        if p_fcf is not None and p_fcf > max_price_to_fcf:
            blocks.append("price/free-cash-flow above the selected maximum")

    if valuation.get("acquisition_price") is None:
        blocks.append("conservative acquisition price unavailable")
    if (valuation.get("base") and
            company.spot > valuation["base"] * 1.30):
        cautions.append("spot is more than 30% above the automated base value")

    reach_score, acquisition_gap = _reachability_score(company, valuation)
    base = _finite(valuation.get("base"))
    valuation_score = _clamp(
        ((base / company.spot) - 0.70) / 0.70 * 20, 0, 20
    ) if base and company.spot > 0 else 0
    drawdown_score = _clamp((drawdown or 0) / 0.30 * 8, 0, 8)
    liquidity_score = _clamp(
        math.log10(max(dollar_volume, 1) / 10_000_000) * 4, 0, 7
    )
    score = round(
        quality["score"] * 0.45
        + valuation["confidence"] * 0.05
        + valuation_score
        + reach_score
        + drawdown_score
        + liquidity_score,
        1,
    )
    if blocks:
        status = "EXCLUDED"
    elif cautions:
        status = "REVIEW"
    else:
        status = "ELIGIBLE"

    return {
        "symbol": company.symbol,
        "name": company.name,
        "sector": company.sector,
        "status": status,
        "discovery_score": score,
        "quality": quality,
        "valuation": valuation,
        "spot": round(company.spot, 2),
        "market_cap": round(cap, 2) if cap is not None else None,
        "average_dollar_volume": round(dollar_volume, 2),
        "price_to_fcf": round(p_fcf, 1) if p_fcf is not None else None,
        "net_debt_ebitda": round(leverage, 2) if leverage is not None else None,
        "drawdown_52w_pct": round(drawdown * 100, 1) if drawdown is not None else None,
        "acquisition_gap_pct": (
            round(acquisition_gap * 100, 1) if acquisition_gap is not None else None
        ),
        "earnings_date": company.earnings_date,
        "blocks": blocks,
        "cautions": cautions,
        "data_warnings": company.data_warnings,
        "model": "financial company" if financial else "operating company",
    }


def discover_value_universe(
    *,
    source: str = "mock",
    limit: int = 25,
    min_market_cap: float = 5_000_000_000,
    min_average_dollar_volume: float = 50_000_000,
    min_quality: float = 68,
    max_leverage: float = 3.0,
    max_price_to_fcf: float = 35.0,
) -> dict:
    """Discover and rank stocks without asking the user for ticker symbols."""
    if source not in {"mock", "yf"}:
        raise ValueError("source must be 'mock' or 'yf'")
    if not 1 <= int(limit) <= 25:
        raise ValueError("discovery limit must be between 1 and 25")
    if min_market_cap < 0 or min_average_dollar_volume < 0:
        raise ValueError("market-cap and liquidity floors cannot be negative")
    if not 0 <= min_quality <= 100:
        raise ValueError("minimum quality must be between 0 and 100")
    if max_leverage <= 0 or max_price_to_fcf <= 0:
        raise ValueError("leverage and price/free-cash-flow maxima must be positive")

    symbols = curated_symbols()
    # Practice mode stays quick but still exercises true universe discovery,
    # ranking and exclusion logic rather than a user-supplied watchlist.
    if source == "mock":
        symbols = list(dict.fromkeys(
            list(MOCK_COMPANIES) + symbols[:32]
        ))
    provider = mock_company_snapshot if source == "mock" else yahoo_company_snapshot
    rows: list[dict] = []
    errors: list[dict] = []
    workers = 1 if source == "mock" else 6
    with ThreadPoolExecutor(max_workers=workers) as pool:
        jobs = {pool.submit(with_retry, provider, symbol): symbol
                for symbol in symbols}
        for future in as_completed(jobs):
            symbol = jobs[future]
            try:
                company, metadata = future.result()
                rows.append(evaluate_discovery_company(
                    company,
                    metadata,
                    min_market_cap=min_market_cap,
                    min_average_dollar_volume=min_average_dollar_volume,
                    min_quality=min_quality,
                    max_leverage=max_leverage,
                    max_price_to_fcf=max_price_to_fcf,
                ))
            except Exception as exc:  # noqa: BLE001
                # Distinguish "this company failed the screen" from "we never
                # saw this company": a fetch failure is a hole in the
                # universe, not a clean exclusion.
                errors.append({"symbol": symbol, "error": str(exc),
                               "kind": "fetch_failed",
                               "transient": is_transient(exc)})

    status_order = {"ELIGIBLE": 0, "REVIEW": 1, "EXCLUDED": 2}
    rows.sort(key=lambda row: (
        status_order[row["status"]], -row["discovery_score"], row["symbol"]
    ))
    selected = [
        row["symbol"] for row in rows if row["status"] in {"ELIGIBLE", "REVIEW"}
    ][:int(limit)]
    for rank, row in enumerate(rows, 1):
        row["rank"] = rank
        row["selected"] = row["symbol"] in selected
    counts = {
        key.lower(): sum(row["status"] == key for row in rows)
        for key in status_order
    }
    return {
        "policy_id": "value-universe-discovery-v1",
        "source": source,
        "universe": "FVS maintained liquid US single stocks",
        "universe_size": len(symbols),
        "rows": rows,
        "selected_symbols": selected,
        "errors": errors,
        "summary": {
            "companies_scanned": len(rows),
            "selected": len(selected),
            **counts,
            "data_errors": len(errors),
            "fetch_failed": [e["symbol"] for e in errors],
            "coverage_pct": (round(100.0 * len(rows) / len(symbols), 1)
                             if symbols else None),
            "universe_complete": not errors,
        },
        "filters": {
            "min_market_cap": min_market_cap,
            "min_average_dollar_volume": min_average_dollar_volume,
            "min_quality": min_quality,
            "max_leverage": max_leverage,
            "max_price_to_fcf": max_price_to_fcf,
        },
        "selection_note": (
            "Discovery ranks stock quality, valuation support, acquisition-price "
            "reachability, drawdown and liquidity. Option chains are requested only "
            "after the shortlist is sent to the put scanner."
        ),
    }
