"""Embedded event calendar + OpEx/ex-div math. UPDATE FOMC EACH JANUARY."""
from __future__ import annotations
from datetime import date

FOMC_2026 = [date(2026, 1, 28), date(2026, 3, 18), date(2026, 4, 29),
             date(2026, 6, 17), date(2026, 7, 29), date(2026, 9, 16),
             date(2026, 10, 28), date(2026, 12, 9)]

ETFS = {"SPY", "QQQ", "IWM"}


def next_fomc_dte(today: date) -> int:
    fut = [(d - today).days for d in FOMC_2026 if d >= today]
    return min(fut) if fut else 999


def fomc_between(d1: date, d2: date) -> bool:
    return any(d1 < f <= d2 for f in FOMC_2026)


def fomc_within(d: date, today: date) -> bool:
    return any(today < f <= d for f in FOMC_2026)


def opex_day(y: int, m: int) -> date:
    first = date(y, m, 1)
    off = (4 - first.weekday()) % 7          # weekday(): Mon=0 .. Fri=4
    return date(y, m, 1 + off + 14)


def event_flags(today: date, symbol: str, front_max_dte: int) -> dict:
    ox = opex_day(today.year, today.month)
    post = ox < today <= ox.replace(day=min(ox.day + 5, 28))
    week = ox.day - 4 <= today.day <= ox.day and today.month == ox.month
    qtr = today.month in (3, 6, 9, 12)
    return {"fomc_dte": next_fomc_dte(today),
            "fomc_in_front": next_fomc_dte(today) <= front_max_dte,
            "opex_date": ox.isoformat(), "opex_week": week, "post_opex": post,
            "ex_div": symbol in ETFS and qtr and week}
