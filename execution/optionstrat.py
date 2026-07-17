"""Exact custom-combo links for OptionStrat."""
from __future__ import annotations


OCC_ROOT = {"SPX": "SPXW", "RUT": "RUTW"}


def _strike(value: float) -> str:
    return f"{float(value):.8f}".rstrip("0").rstrip(".")


def optionstrat_url(symbol: str, legs: list[dict]) -> str:
    """Build the same custom-strategy URL format used by the original UI."""
    symbol = symbol.upper()
    root = OCC_ROOT.get(symbol, symbol)
    encoded = []
    for leg in legs:
        qty = int(leg["qty"])
        if qty == 0:
            continue
        expiry = str(leg["expiry"]).replace("-", "")[2:]
        cp = str(leg["cp"]).upper()
        amount = abs(qty)
        encoded.append(
            f"{'-' if qty < 0 else ''}.{root}{expiry}{cp}{_strike(leg['strike'])}"
            f"{'x' + str(amount) if amount > 1 else ''}"
        )
    return f"https://optionstrat.com/build/custom/{symbol}/{','.join(encoded)}"
