"""Value Entry Put Scanner.

This package deliberately separates valuation-backed stock acquisition from
the volatility-strategy engines used elsewhere in FVS.
"""

from .service import scan_value_puts

__all__ = ["scan_value_puts"]
