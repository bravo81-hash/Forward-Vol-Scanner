"""Pure valuation, option-selection, scoring and stress-test logic."""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import date

RISK_FREE_RATE = 0.045

# Sector valuation multiples (bull P/E, bear P/E, bull EV/EBITDA, bear
# EV/EBITDA). These are a point-in-time calibration that goes stale through a
# sector re-rating, so they live in config/valuation.yaml with a
# `last_reviewed` date that the scanner surfaces on every card. The literals
# below remain the documented fallback.
_FALLBACK_MULTIPLES = {
    "Technology": (22.0, 16.0, 21.0, 15.0),
    "Communication Services": (20.0, 14.0, 19.0, 14.0),
    "Consumer Cyclical": (18.0, 12.0, 17.0, 12.0),
    "Consumer Defensive": (19.0, 15.0, 18.0, 14.0),
    "Healthcare": (20.0, 14.0, 19.0, 14.0),
    "Industrials": (18.0, 12.0, 17.0, 12.0),
    "Financial Services": (14.0, 10.0, 14.0, 10.0),
    "Energy": (13.0, 8.0, 12.0, 8.0),
    "Real Estate": (16.0, 11.0, 15.0, 10.0),
    "Utilities": (17.0, 13.0, 16.0, 12.0),
    "Basic Materials": (14.0, 9.0, 13.0, 9.0),
}
_FALLBACK_DEFAULT = (18.0, 12.0, 17.0, 12.0)


def _load_multiples() -> tuple[dict, tuple, str | None]:
    try:
        from config.loader import valuation_config
        cfg = valuation_config()
    except Exception:  # noqa: BLE001 - config problems must not break pricing
        return dict(_FALLBACK_MULTIPLES), _FALLBACK_DEFAULT, None
    sectors = cfg.get("sector_multiples") or {}
    parsed = {}
    for name, row in sectors.items():
        try:
            parsed[str(name)] = (float(row["pe_bull"]), float(row["pe_bear"]),
                                 float(row["ev_ebitda_bull"]), float(row["ev_ebitda_bear"]))
        except (KeyError, TypeError, ValueError):
            continue
    default = cfg.get("default_multiples") or {}
    try:
        fallback = (float(default["pe_bull"]), float(default["pe_bear"]),
                    float(default["ev_ebitda_bull"]), float(default["ev_ebitda_bear"]))
    except (KeyError, TypeError, ValueError):
        fallback = _FALLBACK_DEFAULT
    reviewed = cfg.get("last_reviewed")
    return (parsed or dict(_FALLBACK_MULTIPLES), fallback,
            str(reviewed) if reviewed else None)


SECTOR_MULTIPLES, DEFAULT_MULTIPLES, MULTIPLES_REVIEWED = _load_multiples()


def _finite(value, default=None):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _clamp(value: float, lo: float, hi: float) -> float:
    return min(max(value, lo), hi)


def _norm_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def put_delta(spot: float, strike: float, dte: int, iv: float,
              rate: float = RISK_FREE_RATE, dividend_yield: float = 0.0) -> float:
    """Absolute Black-Scholes-Merton put delta."""
    if min(spot, strike, dte, iv) <= 0:
        return 0.0
    tenor = dte / 365.0
    d1 = (math.log(spot / strike)
          + (rate - dividend_yield + 0.5 * iv * iv) * tenor) / (iv * math.sqrt(tenor))
    return abs(math.exp(-dividend_yield * tenor) * (_norm_cdf(d1) - 1.0))


def bs_put(spot: float, strike: float, dte: int, iv: float,
           rate: float = RISK_FREE_RATE, dividend_yield: float = 0.0) -> float:
    if min(spot, strike, dte, iv) <= 0:
        return 0.0
    tenor = dte / 365.0
    root_t = math.sqrt(tenor)
    d1 = (math.log(spot / strike)
          + (rate - dividend_yield + 0.5 * iv * iv) * tenor) / (iv * root_t)
    d2 = d1 - iv * root_t
    return (strike * math.exp(-rate * tenor) * _norm_cdf(-d2)
            - spot * math.exp(-dividend_yield * tenor) * _norm_cdf(-d1))


@dataclass
class Company:
    symbol: str
    name: str
    sector: str
    spot: float
    market_cap: float | None = None
    normalized_eps: float | None = None
    fcf_per_share: float | None = None
    book_value_per_share: float | None = None
    net_debt_ebitda: float | None = None
    interest_coverage: float | None = None
    profit_margin: float | None = None
    return_on_equity: float | None = None
    beta: float | None = None
    dividend_yield: float = 0.0
    analyst_target: float | None = None
    realized_vol: float | None = None
    earnings_date: str | None = None
    data_warnings: list[str] = field(default_factory=list)


@dataclass
class OptionQuote:
    expiry: date
    strike: float
    bid: float
    ask: float
    iv: float
    open_interest: int = 0
    volume: int = 0
    delta: float | None = None
    atm_iv: float | None = None
    buying_power: float | None = None

    @property
    def dte(self) -> int:
        return max((self.expiry - date.today()).days, 1)


def value_company(company: Company, acquisition_override: float | None = None) -> dict:
    """Return a valuation range; never fabricate a precise single fair value."""
    pe_base, pe_bear, fcf_base, fcf_bear = SECTOR_MULTIPLES.get(
        company.sector, DEFAULT_MULTIPLES)
    base_methods: dict[str, float] = {}
    bear_methods: dict[str, float] = {}
    if _finite(company.normalized_eps, 0) > 0:
        base_methods["normalised earnings"] = company.normalized_eps * pe_base
        bear_methods["normalised earnings"] = company.normalized_eps * pe_bear
    if _finite(company.fcf_per_share, 0) > 0:
        base_methods["free cash flow"] = company.fcf_per_share * fcf_base
        bear_methods["free cash flow"] = company.fcf_per_share * fcf_bear
    if (company.sector == "Financial Services"
            and _finite(company.book_value_per_share, 0) > 0):
        base_methods["book value"] = company.book_value_per_share * 1.25
        bear_methods["book value"] = company.book_value_per_share * 0.90

    base = statistics.median(base_methods.values()) if base_methods else None
    bear = statistics.median(bear_methods.values()) if bear_methods else None
    if acquisition_override and acquisition_override > 0:
        acquisition = float(acquisition_override)
        source = "user override"
    elif base and bear:
        acquisition = min(bear, base * 0.75)
        source = "conservative model"
    else:
        acquisition = None
        source = "unavailable"

    bull = base * 1.22 if base else None
    confidence = 20 + 28 * len(base_methods)
    if company.analyst_target and base:
        dispersion = abs(company.analyst_target - base) / max(base, 0.01)
        confidence += 10 if dispersion <= 0.25 else -5
    if company.data_warnings:
        confidence -= min(20, 4 * len(company.data_warnings))
    confidence = int(_clamp(confidence, 10, 90))
    return {
        "bear": round(bear, 2) if bear else None,
        "base": round(base, 2) if base else None,
        "bull": round(bull, 2) if bull else None,
        "acquisition_price": round(acquisition, 2) if acquisition else None,
        "acquisition_source": source,
        "confidence": confidence,
        "methods": {key: round(value, 2) for key, value in base_methods.items()},
        "analyst_cross_check": round(company.analyst_target, 2)
        if company.analyst_target else None,
    }


def quality_company(company: Company) -> dict:
    score = 50.0
    flags: list[str] = []
    cap = _finite(company.market_cap, 0)
    if cap >= 50e9:
        score += 12
    elif cap >= 5e9:
        score += 7
    elif cap and cap < 2e9:
        score -= 15
        flags.append("small capitalisation")
    else:
        flags.append("market cap unavailable")

    if _finite(company.fcf_per_share, 0) > 0:
        score += 10
    else:
        score -= 18
        flags.append("positive normalised FCF not established")
    if _finite(company.normalized_eps, 0) > 0:
        score += 8
    else:
        score -= 15
        flags.append("positive normalised earnings not established")

    leverage = _finite(company.net_debt_ebitda)
    if leverage is None:
        flags.append("leverage incomplete")
    elif leverage <= 1.5:
        score += 10
    elif leverage <= 3.0:
        score += 2
    elif leverage <= 4.5:
        score -= 12
        flags.append("elevated leverage")
    else:
        score -= 28
        flags.append("high refinancing/leverage risk")

    coverage = _finite(company.interest_coverage)
    if coverage is None:
        flags.append("interest coverage unavailable")
    elif coverage >= 8:
        score += 7
    elif coverage < 3:
        score -= 15
        flags.append("weak interest coverage")

    if _finite(company.profit_margin, 0) > 0.15:
        score += 4
    elif _finite(company.profit_margin, 0) < 0:
        score -= 15
        flags.append("negative profit margin")
    if _finite(company.beta, 1) > 1.8:
        score -= 5
        flags.append("high equity volatility")

    score = round(_clamp(score, 0, 100), 1)
    grade = "A" if score >= 80 else "B" if score >= 68 else "C" if score >= 52 else "D"
    return {"score": score, "grade": grade, "flags": flags}


def executable_credit(quote: OptionQuote) -> float:
    """Conservative sell limit: one quarter of the spread above the bid."""
    bid, ask = max(quote.bid, 0), max(quote.ask, 0)
    if ask <= bid:
        return round(bid, 2)
    return round(bid + 0.25 * (ask - bid), 2)


def option_metrics(company: Company, quote: OptionQuote, valuation: dict,
                   mode: str, long_quote: OptionQuote | None = None) -> dict:
    short_credit = executable_credit(quote)
    long_cost = long_quote.ask if long_quote else 0.0
    credit = round(max(short_credit - long_cost, 0), 2)
    dte = max((quote.expiry - date.today()).days, 1)
    delta = quote.delta if quote.delta is not None else put_delta(
        company.spot, quote.strike, dte, quote.iv, dividend_yield=company.dividend_yield)
    net_basis = quote.strike - short_credit
    secured_capital = max(net_basis, 0.01) * 100
    annualised_yield = (short_credit / max(net_basis, 0.01)) * 365 / dte
    spread_pct = ((quote.ask - quote.bid) / max((quote.ask + quote.bid) / 2, 0.01))
    skew = ((quote.iv - quote.atm_iv) * 100) if quote.atm_iv else None
    vrp = ((quote.iv - company.realized_vol) * 100) if company.realized_vol else None
    max_loss = secured_capital
    width = None
    if mode == "defined_risk" and long_quote:
        width = quote.strike - long_quote.strike
        max_loss = max((width - credit) * 100, 0)
    cash_holding_return = short_credit / max(net_basis, 0.01)
    strategy_holding_return = (
        credit * 100 / max(max_loss, 0.01)
        if mode == "defined_risk" else cash_holding_return)
    strategy_annualised = strategy_holding_return * 365 / dte

    stress = []
    for shock in (0.15, 0.25, 0.40, 0.60):
        shocked = company.spot * (1 - shock)
        pnl = (short_credit - max(quote.strike - shocked, 0)) * 100
        if long_quote:
            pnl += (max(long_quote.strike - shocked, 0) - long_cost) * 100
        stress.append({"shock": int(shock * 100), "stock": round(shocked, 2),
                       "pnl": round(pnl, 2)})
    return {
        "expiry": quote.expiry.isoformat(), "dte": dte, "strike": quote.strike,
        "bid": quote.bid, "ask": quote.ask, "executable_credit": short_credit,
        "net_credit": credit, "net_basis": round(net_basis, 2),
        "discount_to_spot_pct": round((1 - net_basis / company.spot) * 100, 1),
        "discount_to_value_pct": round(
            (1 - net_basis / valuation["base"]) * 100, 1)
        if valuation.get("base") else None,
        "cash_secured_return_pct": round(cash_holding_return * 100, 2),
        "annualised_cash_return_pct": round(annualised_yield * 100, 2),
        "holding_return_pct": round(strategy_holding_return * 100, 2),
        "annualised_return_pct": round(strategy_annualised * 100, 2),
        "return_basis": "defined max loss" if mode == "defined_risk" else "cash-secured capital",
        "buying_power": quote.buying_power,
        "return_on_buying_power_pct": round(short_credit * 100 / quote.buying_power * 100, 1)
        if quote.buying_power else None,
        "delta": round(delta, 3), "iv_pct": round(quote.iv * 100, 1),
        "atm_iv_pct": round(quote.atm_iv * 100, 1) if quote.atm_iv else None,
        "put_skew_vol_points": round(skew, 1) if skew is not None else None,
        "vrp_vol_points": round(vrp, 1) if vrp is not None else None,
        "spread_pct": round(spread_pct * 100, 1),
        "open_interest": quote.open_interest, "volume": quote.volume,
        "assignment_capital": round(quote.strike * 100, 2),
        "max_loss": round(max_loss, 2), "long_strike": long_quote.strike if long_quote else None,
        "long_ask": long_cost if long_quote else None, "width": width,
        "stress": stress,
    }


def score_candidate(company: Company, valuation: dict, quality: dict, metrics: dict,
                    hurdle_rate: float, mode: str) -> dict:
    acquisition = valuation.get("acquisition_price")
    basis = metrics["net_basis"]
    quality_component = quality["score"] / 100 * 25
    if acquisition:
        margin = (acquisition - basis) / max(acquisition, 0.01)
        value_component = _clamp(12.5 + margin * 75, 0, 25)
    else:
        margin = None
        value_component = 0
    ann = metrics["annualised_return_pct"] / 100
    yield_component = _clamp((ann - max(hurdle_rate - 0.03, 0)) / 0.12 * 15, 0, 15)
    vol_edge = max(metrics.get("put_skew_vol_points") or 0, 0)
    vol_edge += max(metrics.get("vrp_vol_points") or 0, 0) * 0.5
    volatility_component = _clamp(vol_edge / 8 * 15, 0, 15)
    spread = metrics["spread_pct"]
    liquidity_component = _clamp(10 - max(spread - 5, 0) * 0.45, 0, 10)
    if metrics["open_interest"] < 100:
        liquidity_component *= 0.45
    stress_40 = next(x["pnl"] for x in metrics["stress"] if x["shock"] == 40)
    risk_base = metrics["max_loss"] or 1
    stress_component = _clamp(10 + stress_40 / risk_base * 8, 0, 10)
    total = round(quality_component + value_component + yield_component
                  + volatility_component + liquidity_component + stress_component, 1)

    blocks: list[str] = []
    cautions: list[str] = []
    if acquisition is None:
        blocks.append("valuation confidence is insufficient; set a reviewed acquisition price")
    elif basis > acquisition:
        blocks.append("net basis is above the conservative acquisition price")
    if quality["score"] < 52:
        blocks.append("company quality is below the minimum acquisition threshold")
    elif quality["score"] < 68:
        cautions.append("speculative company quality")
    if metrics["annualised_return_pct"] < hurdle_rate * 100:
        blocks.append(("defined-risk annualised return" if mode == "defined_risk"
                       else "cash-secured yield") + " is below the hurdle rate")
    if not 0.06 <= metrics["delta"] <= 0.28:
        blocks.append("put delta is outside the 0.06–0.28 selection range")
    if metrics["spread_pct"] > 20:
        blocks.append("option spread exceeds 20% of midpoint")
    elif metrics["spread_pct"] > 12:
        cautions.append("wide option market")
    if metrics["open_interest"] < 50:
        blocks.append("open interest is below 50 contracts")
    if mode != "defined_risk" and metrics["discount_to_spot_pct"] < 15:
        blocks.append("net basis is less than 15% below spot")

    if blocks:
        status = "REJECTED"
    elif quality["score"] < 68 or total < 62:
        status = "SPECULATIVE"
    elif total >= 75:
        status = "QUALIFIED"
    else:
        status = "WATCH"
    components = {
        "quality": round(quality_component, 1),
        "valuation": round(value_component, 1),
        "yield": round(yield_component, 1),
        "volatility": round(volatility_component, 1),
        "liquidity": round(liquidity_component, 1),
        "stress": round(stress_component, 1),
    }
    return {"score": total, "status": status, "components": components,
            "blocks": blocks, "cautions": cautions, "margin_to_acquisition_pct":
            round(margin * 100, 1) if margin is not None else None}


def size_candidate(metrics: dict, quality: dict, mode: str, nlv: float,
                   available_cash: float, sector_capacity: float) -> dict:
    company_pct = 0.05 if quality["score"] >= 75 else 0.025
    company_limit = nlv * company_pct
    sector_limit = max(sector_capacity, 0)
    if mode == "cash_secured":
        per_contract = metrics["assignment_capital"]
        capital_limit = available_cash
    elif mode == "defined_risk":
        per_contract = max(metrics["max_loss"], 1)
        capital_limit = min(available_cash, nlv * 0.01)
    else:
        per_contract = metrics["assignment_capital"]
        capital_limit = min(company_limit, available_cash * 2)
    allowed = min(company_limit, sector_limit, capital_limit)
    contracts = max(int(allowed // max(per_contract, 1)), 0)
    return {
        "max_contracts": contracts,
        "per_contract_capital": round(per_contract, 2),
        "full_assignment_cost": round(contracts * metrics["assignment_capital"], 2),
        "company_limit": round(company_limit, 2),
        "sector_capacity": round(sector_limit, 2),
        "cash_capacity": round(capital_limit, 2),
        "binding_limit": min(
            (("company", company_limit), ("sector", sector_limit), ("cash", capital_limit)),
            key=lambda item: item[1],
        )[0],
    }


def choose_candidates(company: Company, quotes: list[OptionQuote], *,
                      mode: str, acquisition_override: float | None,
                      hurdle_rate: float, nlv: float, available_cash: float,
                      sector_capacity: float, min_dte: int, max_dte: int) -> dict:
    valuation = value_company(company, acquisition_override)
    quality = quality_company(company)
    puts = [q for q in quotes if min_dte <= (q.expiry - date.today()).days <= max_dte]
    evaluated = []
    for short in puts:
        long_quote = None
        if mode == "defined_risk":
            lower = [q for q in puts if q.expiry == short.expiry
                     and short.strike * 0.78 <= q.strike <= short.strike * 0.94]
            if not lower:
                continue
            target = short.strike * 0.88
            long_quote = min(lower, key=lambda q: abs(q.strike - target))
        metrics = option_metrics(company, short, valuation, mode, long_quote)
        scored = score_candidate(company, valuation, quality, metrics, hurdle_rate, mode)
        sizing = size_candidate(metrics, quality, mode, nlv, available_cash, sector_capacity)
        if sizing["max_contracts"] == 0:
            scored["blocks"].append("portfolio limits approve zero contracts")
            scored["status"] = "REJECTED"
        evaluated.append({**metrics, **scored, "sizing": sizing})

    evaluated.sort(key=lambda row: (
        row["status"] != "QUALIFIED", row["status"] != "WATCH",
        row["status"] != "SPECULATIVE", -row["score"],
        -row["annualised_return_pct"]))
    best = evaluated[0] if evaluated else None
    return {
        "symbol": company.symbol, "name": company.name, "sector": company.sector,
        "spot": round(company.spot, 2), "quality": quality, "valuation": valuation,
        "earnings_date": company.earnings_date, "data_warnings": company.data_warnings,
        "candidate": best, "alternatives": evaluated[1:5],
    }
