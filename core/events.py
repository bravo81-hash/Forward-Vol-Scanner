"""Embedded event calendar + OpEx/ex-div math. UPDATE FOMC EACH JANUARY."""
from __future__ import annotations
from datetime import date, datetime
from zoneinfo import ZoneInfo

US_TZ = ZoneInfo("America/New_York")


def trading_today() -> date:
    """Current date in New York — the date all DTE/event/cadence logic must
    use, regardless of server timezone."""
    return datetime.now(US_TZ).date()

FOMC_2026 = [date(2026, 1, 28), date(2026, 3, 18), date(2026, 4, 29),
             date(2026, 6, 17), date(2026, 7, 29), date(2026, 9, 16),
             date(2026, 10, 28), date(2026, 12, 9)]

# P5: official BLS release-calendar dates (bls.gov/schedule/2026), 08:30 ET.
# UPDATE EACH JANUARY alongside FOMC_2026.
CPI_2026 = [date(2026, 1, 13), date(2026, 2, 13), date(2026, 3, 11),
            date(2026, 4, 10), date(2026, 5, 12), date(2026, 6, 10),
            date(2026, 7, 14), date(2026, 8, 12), date(2026, 9, 11),
            date(2026, 10, 14), date(2026, 11, 10), date(2026, 12, 10)]
PPI_2026 = [date(2026, 1, 30), date(2026, 2, 27), date(2026, 3, 18),
            date(2026, 4, 14), date(2026, 5, 13), date(2026, 6, 11),
            date(2026, 7, 15), date(2026, 8, 13), date(2026, 9, 10),
            date(2026, 10, 15), date(2026, 11, 13), date(2026, 12, 15)]
NFP_2026 = [date(2026, 1, 9), date(2026, 2, 11), date(2026, 3, 6),
            date(2026, 4, 3), date(2026, 5, 8), date(2026, 6, 5),
            date(2026, 7, 2), date(2026, 8, 7), date(2026, 9, 4),
            date(2026, 10, 2), date(2026, 11, 6), date(2026, 12, 4)]
MACRO = {"CPI": CPI_2026, "PPI": PPI_2026, "NFP": NFP_2026}

ETFS = {"SPY", "QQQ", "IWM"}


def next_fomc_dte(today: date) -> int:
    fut = [(d - today).days for d in FOMC_2026 if d >= today]
    return min(fut) if fut else 999


def fomc_between(d1: date, d2: date) -> bool:
    return any(d1 < f <= d2 for f in FOMC_2026)


def fomc_within(d: date, today: date) -> bool:
    return any(today < f <= d for f in FOMC_2026)


def next_macro(today: date) -> tuple[int, str | None]:
    """P5: nearest of CPI/PPI/NFP, days-out + which one (ties -> CPI>PPI>NFP)."""
    best_dte, best_kind = 999, None
    for kind, dates in MACRO.items():
        fut = [(d - today).days for d in dates if d >= today]
        if fut and min(fut) < best_dte:
            best_dte, best_kind = min(fut), kind
    return best_dte, best_kind


def macro_between(d1: date, d2: date) -> list[str]:
    """P5: which macro releases fall strictly after d1, on/before d2 — the
    calendar-analogue of fomc_between, but a list since more than one type
    (e.g. CPI + NFP) can land inside a single pair window."""
    return [k for k, dates in MACRO.items() if any(d1 < d <= d2 for d in dates)]


def macro_within(d: date, today: date) -> list[str]:
    return [k for k, dates in MACRO.items() if any(today < x <= d for x in dates)]


def opex_day(y: int, m: int) -> date:
    first = date(y, m, 1)
    off = (4 - first.weekday()) % 7          # weekday(): Mon=0 .. Fri=4
    return date(y, m, 1 + off + 14)


def event_flags(today: date, symbol: str, front_max_dte: int) -> dict:
    ox = opex_day(today.year, today.month)
    post = ox < today <= ox.replace(day=min(ox.day + 5, 28))
    week = ox.day - 4 <= today.day <= ox.day and today.month == ox.month
    qtr = today.month in (3, 6, 9, 12)
    macro_dte, macro_type = next_macro(today)
    return {"fomc_dte": next_fomc_dte(today),
            "fomc_in_front": next_fomc_dte(today) <= front_max_dte,
            "macro_dte": macro_dte, "macro_type": macro_type,          # P5
            "macro_in_front": macro_dte <= front_max_dte,               # P5
            "opex_date": ox.isoformat(), "opex_week": week, "post_opex": post,
            "ex_div": symbol in ETFS and qtr and week}
