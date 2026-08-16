"""Shared application seam for the route blueprints.

`webapp.py` had grown to ~1200 lines and 47 routes, which is why the CI ruff
line had to name individual paths. The routes now live in per-feature
blueprints under `web/`, and everything they share — the symbol list, the
context builders, and the TWS entry points — lives here.

This module is also the patch point for tests: patching
`web.shared.with_ib` or `web.shared.reprice_cards` reaches every blueprint,
because the blueprints resolve them through this module rather than binding
them at import time.
"""
from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path

from flask import request

import sentinel as S
from core.context import build_context
from core.events import trading_clock, trading_today
from core.ib_client import DEFAULT_HOST, DEFAULT_PORT, with_ib
from core.models import Leg
from core.pricing import q_for, struct_value
from core.reprice import reprice_cards
from core.surface import term_stats
from core.walls import scan_walls
from execution.stage import stage_suggestion
from portfolio.accounts import MOCK_ACCOUNTS, list_accounts
from portfolio.book import book_greeks, fetch_positions, stress_book
from portfolio.risk import book_warnings
from selection.ranker import shortlist
from store.campaigns import campaign_store
from store.log import log, log_scan

#: Absolute path to the browser UI. Blueprints live in web/, so a relative
#: "static" would resolve against the blueprint package, not the repo root.
STATIC_DIR = str(Path(__file__).resolve().parents[1] / "static")

SYMBOLS = ["SPX", "SPY", "QQQ", "RUT", "IWM"]

# Account id(s) that are SMSF / cash-settled — Sentinel applies the EU cash-index
# multi-expiry block to these. Add your real SMSF id here once; leave empty and
# every account is treated as a margin/trading book.
SENTINEL_INVESTING_ACCOUNTS: set[str] = {"U23260336"}


def truthy_env(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def v3_context(symbol: str, mode: str, account: str | None, nlv: float | None,
                as_of: date | None = None, manual: dict | None = None,
                mandate: str | None = None):
    """Build a v3 context with one central mandate and optional live book."""
    from config.loader import account_profile
    from core.chain import MOCK, SURFACE_CFG
    from core.yf_client import build_context_yf

    profile = account_profile(account, nlv)
    if mandate == "cash":
        profile.update(pool="investing", cash_account=True, block_multi_expiry=True)
    elif mandate == "margin":
        profile.update(pool="trading", cash_account=False, block_multi_expiry=False)
    errors, ctx = [], None
    order = {"mock": ["mock"], "live": ["live"], "yf": ["yf"],
             "auto": (["live", "yf", "mock"] if symbol in SURFACE_CFG else ["yf"])}.get(mode)
    if order is None:
        raise ValueError(f"bad mode '{mode}'")
    for source in order:
        if source == "mock" and symbol not in MOCK:
            continue
        if source == "live" and symbol not in SURFACE_CFG:
            continue
        try:
            if (as_of or manual) and source != "mock":
                raise ValueError("historical ONE sessions use manual/mock mode, not live/yfinance")
            ctx = (build_context_yf(symbol) if source == "yf" else
                   build_context(symbol, source, today=as_of, manual=manual))
            break
        except Exception as exc:                 # noqa: BLE001
            errors.append(f"{source}: {exc}")
    if ctx is None:
        raise RuntimeError("; ".join(errors) or "no data source")
    ctx.mandate = profile
    if ctx.mode == "live":
        try:
            pos = with_ib(lambda ib: fetch_positions(ib, symbol, account, with_greeks=True))
            ctx.book = book_greeks(ctx, pos)
            ctx.book["stress"] = stress_book(ctx, pos)
        except Exception as exc:                 # book optional for scan, explicit in output
            ctx.book = {"error": str(exc)}
    if isinstance(ctx.book, dict):
        ctx.book.update(account=account, nlv=profile["nlv"], symbol=symbol)
    if ctx.mode == "live":
        clock = trading_clock()
        ctx.data.update(session=clock["ny_date"], as_of_time=clock["ny_time"],
                        captured_at=clock["captured_at_ny"],
                        melbourne_date=clock["melbourne_date"],
                        melbourne_time=clock["melbourne_time"],
                        captured_at_melbourne=clock["captured_at_melbourne"],
                        market_phase=clock["market_phase"], source="tws_live")
    return ctx, profile, errors



def manual_one_context(intent: str) -> tuple[date | None, dict | None]:
    """Validate optional market-state fields copied from one historical ONE date."""
    raw = request.args.get("entry_date")
    if not raw:
        return None, None
    try:
        as_of = date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError("entry_date must be YYYY-MM-DD") from exc
    if as_of > trading_today():
        raise ValueError("entry_date cannot be in the future")
    if as_of.weekday() > 4:
        raise ValueError("entry_date must be a trading weekday")
    as_of_time = request.args.get("entry_time", "15:30")
    try:
        hh, mm = (int(x) for x in as_of_time.split(":"))
    except (ValueError, TypeError) as exc:
        raise ValueError("entry_time must be HH:MM New York time") from exc
    if not (15 * 60 <= hh * 60 + mm <= 15 * 60 + 40):
        raise ValueError("entry_time must be inside your 15:00-15:40 ET decision window")

    def number(name: str, default: float, lo: float, hi: float) -> float:
        value = request.args.get(name, default=default, type=float)
        if value is None or not lo <= value <= hi:
            raise ValueError(f"{name} must be between {lo:g} and {hi:g}")
        return float(value)

    iv_band = request.args.get("iv_band", "NRM").upper()
    term = request.args.get("term", "FLAT").upper()
    trend = request.args.get("trend", "RNG").upper()
    event = request.args.get("event", "NONE").upper()
    if iv_band not in {"CMP", "NRM", "ELV", "STR"}:
        raise ValueError("iv_band must be CMP, NRM, ELV, or STR")
    if term not in {"STEEP CONTANGO", "CONTANGO", "FLAT", "INVERTED FRONT"}:
        raise ValueError("invalid term state")
    if trend not in {"UP", "RNG", "DN"}:
        raise ValueError("trend must be UP, RNG, or DN")
    if event not in {"NONE", "FOMC", "MACRO", "OPEX"}:
        raise ValueError("event must be NONE, FOMC, MACRO, or OPEX")
    bias = {"bull": 1, "neutral": 0, "bear": -1}.get(intent, 0)
    return as_of, {"historical": True, "as_of_time": as_of_time,
                   "spot": number("spot", 6000, 1, 100000),
                   "iv30": number("iv30", 20, 1, 150),
                   "rv21": number("rv21", 16, 1, 150),
                   "vrp_fwd": number("vrp_fwd", 4, -100, 100),
                   "rr25_30d": number("rr25", 4, -50, 50),
                   "iv_band": iv_band, "term": term, "trend": trend,
                   "event": event, "bias": bias}

