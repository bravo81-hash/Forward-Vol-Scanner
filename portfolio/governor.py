"""Signed post-trade, account-level risk approval for Campaign Engine v3."""
from __future__ import annotations

from config.loader import risk_config

#: Static fallback beta-to-SPX. Used when no realised beta is available.
SPX_EQUIV = {"SPX": 1.0, "SPY": 1.0, "QQQ": 1.15, "NDX": 1.15,
             "RUT": 1.20, "IWM": 1.20}

#: Trailing window for realised beta. Short enough to track a regime change,
#: long enough not to be noise.
BETA_WINDOW = 120
BETA_MIN_OBS = 60
#: Realised beta is clamped: a bad or thin history must not silently inflate
#: the correlated-delta view.
BETA_BOUNDS = (0.5, 2.0)


def _returns(closes: list[float]) -> list[float]:
    return [(b / a) - 1.0 for a, b in zip(closes, closes[1:], strict=False)
            if a and b and a > 0]


def realised_beta(symbol_closes: list[float], spx_closes: list[float],
                  window: int = BETA_WINDOW) -> float | None:
    """Trailing beta of a symbol to SPX from daily closes.

    The static multipliers above are a point-in-time judgement that goes
    stale through a regime change — RUT/IWM beta in particular moves a long
    way. Returns None when there is not enough clean history, so the caller
    falls back rather than trusting a two-point regression.
    """
    a, b = _returns(symbol_closes[-(window + 1):]), _returns(spx_closes[-(window + 1):])
    n = min(len(a), len(b))
    if n < BETA_MIN_OBS:
        return None
    a, b = a[-n:], b[-n:]
    mean_a, mean_b = sum(a) / n, sum(b) / n
    var_b = sum((x - mean_b) ** 2 for x in b)
    if var_b <= 0:
        return None
    cov = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b, strict=False))
    beta = cov / var_b
    lo, hi = BETA_BOUNDS
    return round(min(max(beta, lo), hi), 3)


def equiv_factor(symbol: str, betas: dict | None = None) -> tuple[float, str]:
    """(factor, source) for the SPX-equivalent delta of one symbol."""
    symbol = symbol.upper()
    if betas and betas.get(symbol) is not None:
        try:
            return float(betas[symbol]), "realised"
        except (TypeError, ValueError):
            pass
    return SPX_EQUIV.get(symbol, 1.0), "static"


def _greeks(book: dict | None) -> dict:
    g = (book or {}).get("greeks", {})
    return {k: float(g.get(k, 0.0) or 0.0) for k in ("delta", "gamma", "theta", "vega")}


def aggregate_books(books: list[dict], betas: dict | None = None) -> dict:
    """Aggregate per-symbol books, retaining a conservative correlation view.

    `betas` maps symbol -> trailing realised beta (see `realised_beta`).
    Any symbol without one falls back to the static multiplier, and the
    source of each factor is reported so the correlated-delta figure can be
    read for what it is.
    """
    total = dict.fromkeys(("delta", "gamma", "theta", "vega"), 0.0)
    by_symbol = {}
    factors = {}
    equiv_delta = 0.0
    nlv = 0.0
    for b in books:
        symbol = str(b.get("symbol", "?")).upper()
        g = _greeks(b)
        by_symbol[symbol] = g
        for k in total:
            total[k] += g[k]
        factor, source = equiv_factor(symbol, betas)
        factors[symbol] = {"factor": round(factor, 3), "source": source}
        equiv_delta += g["delta"] * factor
        nlv = max(nlv, float(b.get("nlv") or 0.0))
    return {"greeks": {k: round(v, 2) for k, v in total.items()},
            "spx_equiv_delta": round(equiv_delta, 2),
            "equiv_factors": factors,
            "by_symbol": by_symbol, "nlv": nlv}


def _stress(card: dict, lots: int, spot: float, cfg: dict) -> list[dict]:
    g = card.get("greeks", {})
    out = []
    for row in cfg.get("stress", []):
        move = spot * float(row["spot_pct"])
        pnl = ((float(g.get("delta", 0)) * move)
               + .5 * float(g.get("gamma", 0)) * move * move
               + float(g.get("vega", 0)) * float(row["iv_points"])
               + float(g.get("theta", 0)) * float(row["days"])) * lots
        out.append({"name": row["name"], "pnl": round(pnl, 0)})
    return out


def evaluate_candidate(card: dict, book: dict | None, nlv: float | None,
                       spot: float, size: str = "FULL") -> dict:
    """Return maximum whole lots satisfying every configured constraint."""
    cfg = risk_config()
    nlv = float(nlv or (book or {}).get("nlv") or 100_000.0)
    unit = max(nlv / 100_000.0, .25)
    per = cfg["per_100k"]
    limits = {"delta": per["delta"] * unit,
              "gamma": per["gamma"] * unit,
              "vega": per["vega"] * unit,
              "theta_min": per.get("theta_min", 0.0)}
    before = _greeks(book)
    cg = {k: float(card.get("greeks", {}).get(k, 0.0) or 0.0)
          for k in before}
    structural = abs(float(card.get("max_loss", 0.0) or 0.0)) * 100
    cash = float(card.get("cash_required") or structural)
    if card.get("strategy") == "target_fly":
        risk_pct = cfg["limits"]["target_fly_risk_pct_nlv"]
    elif card.get("strategy") == "debit_spread":
        risk_pct = cfg["limits"]["directional_debit_risk_pct_nlv"]
    else:
        risk_pct = cfg["limits"]["max_campaign_risk_pct_nlv"]
    size_frac = {"FULL": 1.0, "HALF": .5, "QUARTER": .25, "STAND": 0.0}.get(size, 1.0)
    max_lots = int(cfg["limits"]["max_lots"] * size_frac)
    approved, binding = 0, []
    for lots in range(1, max_lots + 1):
        after = {k: before[k] + cg[k] * lots for k in before}
        stress = _stress(card, lots, spot, cfg)
        checks = {
            "delta": abs(after["delta"]) <= limits["delta"],
            "gamma": abs(after["gamma"]) <= limits["gamma"],
            "vega": abs(after["vega"]) <= limits["vega"],
            "theta": after["theta"] >= limits["theta_min"],
            "structural_risk": structural * lots <= nlv * risk_pct,
            "cash": cash * lots <= nlv * cfg["limits"]["max_campaign_cash_pct_nlv"],
            "stress": min((s["pnl"] for s in stress), default=0) >=
                      -nlv * cfg["limits"]["max_stress_loss_pct_nlv"],
        }
        failed = [k for k, ok in checks.items() if not ok]
        if failed:
            binding = failed
            break
        approved = lots
    if approved:
        after = {k: round(before[k] + cg[k] * approved, 2) for k in before}
        stress = _stress(card, approved, spot, cfg)
    else:
        after = {k: round(v, 2) for k, v in before.items()}
        stress = _stress(card, 1, spot, cfg)
    return {"approved_lots": approved, "binding": binding,
            "before": before, "after": after, "limits": limits,
            "risk_per_lot": round(structural, 2),
            "cash_per_lot": round(cash, 2), "stress": stress,
            "nlv": nlv, "size": size,
            "risk_approved": approved > 0}
