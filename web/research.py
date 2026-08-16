"""Research desk: status, accounts, Direction, Gate S, suggest, Sentinel, payoff, legacy stage."""
from __future__ import annotations

from datetime import date, timedelta

from flask import Blueprint, jsonify, request, send_from_directory

import sentinel as S
from core.context import build_context
from core.events import trading_today
from core.ib_client import DEFAULT_HOST, DEFAULT_PORT
from core.models import Leg
from core.pricing import q_for, struct_value
from core.surface import term_stats
from core.walls import scan_walls
from portfolio.accounts import MOCK_ACCOUNTS, list_accounts
from portfolio.book import book_greeks, fetch_positions, stress_book
from portfolio.risk import book_warnings
from selection.ranker import shortlist
from store.log import log, log_scan
from web import shared
from web.shared import SENTINEL_INVESTING_ACCOUNTS, STATIC_DIR, SYMBOLS

bp = Blueprint("research", __name__)


def _smsf_funds(mode: str) -> tuple[float | None, float | None, str]:
    """Available funds for the Gate S affordability check.

    Precedence: an explicit ?cash= override (for planning a deposit or a
    what-if), then the live TWS AvailableFunds for the SMSF account, then the
    mock account book. Returns (cash, nlv, source) — a None cash makes Gate S
    say plainly that nothing was checked, which is the honest answer when the
    figure is unavailable rather than an assumed one.
    """
    account = request.args.get("account") or None
    override = request.args.get("cash", type=float)
    if override is not None:
        nlv = request.args.get("nlv", type=float)
        return override, nlv, "override"

    rows = []
    source = "unavailable"
    if mode == "live":
        try:
            rows = shared.with_ib(list_accounts)
            source = "tws"
        except Exception:  # noqa: BLE001 - never block the card on an account read
            rows, source = [], "unavailable"
    else:
        rows, source = MOCK_ACCOUNTS, "mock"

    row = None
    if account:
        row = next((r for r in rows if r.get("account") == account), None)
    if row is None:
        # No account named: prefer the SMSF/cash pool if config identifies one.
        investing = SENTINEL_INVESTING_ACCOUNTS
        row = next((r for r in rows if r.get("account") in investing), None)
        if row is None and len(rows) == 1:
            row = rows[0]
    if row is None:
        return None, None, "unavailable" if source != "override" else source
    return row.get("available_funds"), row.get("nlv"), source


@bp.get("/api/status")
def api_status():
    return jsonify({"symbols": SYMBOLS, "tws": f"{DEFAULT_HOST}:{DEFAULT_PORT}"})


@bp.get("/api/accounts")
def api_accounts():
    if request.args.get("mode", "mock") == "mock":
        return jsonify(MOCK_ACCOUNTS)
    try:
        return jsonify(shared.with_ib(list_accounts))
    except Exception as e:                       # noqa: BLE001
        return jsonify({"error": str(e)}), 500


@bp.get("/api/direction")
def api_direction():
    """Direction tab: objective structure selection for a stated intent.

    symbol: any ticker (SURFACE_CFG symbols usable live; anything else
            resolves via yfinance).
    intent: long | short | vol | auto   (auto = regime bias decides side)
    mode:   auto | live | yf | mock     (auto = TWS -> yfinance -> mock)
    """
    from core.chain import MOCK, SURFACE_CFG
    from core.yf_client import build_context_yf
    from selection.direction import direction_verdict

    symbol = request.args.get("symbol", "SPX").upper().strip()
    intent = request.args.get("intent", "auto").lower()
    mode = request.args.get("mode", "auto").lower()
    if intent not in ("long", "short", "vol", "auto"):
        return jsonify({"error": f"bad intent '{intent}'"}), 400

    errors, ctx = [], None
    order = {"live": ["live"], "yf": ["yf"], "mock": ["mock"],
             "auto": (["live", "yf", "mock"] if symbol in SURFACE_CFG
                      else ["yf"])}.get(mode)
    if order is None:
        return jsonify({"error": f"bad mode '{mode}'"}), 400
    for m in order:
        if m == "live" and symbol not in SURFACE_CFG:
            errors.append("live: symbol not in SURFACE_CFG")
            continue
        if m == "mock" and symbol not in MOCK:
            errors.append("mock: no synthetic surface for symbol")
            continue
        try:
            ctx = (build_context_yf(symbol) if m == "yf"
                   else build_context(symbol, m))
            break
        except Exception as e:                   # noqa: BLE001
            errors.append(f"{m}: {e}")
    if ctx is None:
        return jsonify({"error": "; ".join(errors) or "no data source"}), 502

    out = direction_verdict(ctx, intent)
    if errors:
        out["fallback_chain"] = errors
    log("direction", symbol, {"intent": intent, "mode": ctx.mode,
                              "play": out["play"], "side": out["side"],
                              "top": (out["structures"][0]["key"]
                                      if out["structures"] else None)})
    return jsonify(out)


@bp.get("/api/smsf")
def api_smsf():
    """SMSF tab: single-expiry structure selection for the cash account.

    symbol: SPX / RUT (any SURFACE_CFG symbol accepted; index advisory)
    intent: auto | bull | neutral | bear   (auto = regime bias decides)
    mode:   auto | live | yf | mock
    """
    from core.chain import MOCK, SURFACE_CFG
    from core.yf_client import build_context_yf
    from selection.smsf import smsf_verdict

    symbol = request.args.get("symbol", "SPX").upper().strip()
    intent = request.args.get("intent", "auto").lower()
    mode = request.args.get("mode", "auto").lower()
    if intent not in ("auto", "bull", "neutral", "bear"):
        return jsonify({"error": f"bad intent '{intent}'"}), 400

    errors, ctx = [], None
    order = {"live": ["live"], "yf": ["yf"], "mock": ["mock"],
             "auto": (["live", "yf", "mock"] if symbol in SURFACE_CFG
                      else ["yf"])}.get(mode)
    if order is None:
        return jsonify({"error": f"bad mode '{mode}'"}), 400
    for m in order:
        if m == "live" and symbol not in SURFACE_CFG:
            errors.append("live: symbol not in SURFACE_CFG")
            continue
        if m == "mock" and symbol not in MOCK:
            errors.append("mock: no synthetic surface for symbol")
            continue
        try:
            ctx = (build_context_yf(symbol) if m == "yf"
                   else build_context(symbol, m))
            break
        except Exception as e:                   # noqa: BLE001
            errors.append(f"{m}: {e}")
    if ctx is None:
        return jsonify({"error": "; ".join(errors) or "no data source"}), 502

    cash, nlv, cash_source = _smsf_funds(ctx.mode)
    out = smsf_verdict(ctx, intent, cash=cash, nlv=nlv)
    out["cash"]["source"] = cash_source
    if errors:
        out["fallback_chain"] = errors
    log("smsf", symbol, {"intent": intent, "mode": ctx.mode,
                         "bias": out["bias"],
                         "top": (out["structures"][0]["key"]
                                 if out["structures"] else None)})
    return jsonify(out)



@bp.get("/api/suggest")
def api_suggest():
    symbol = request.args.get("symbol", "SPX").upper()
    mode = request.args.get("mode", "mock")
    account = request.args.get("account") or None
    nlv = request.args.get("nlv", type=float)
    try:
        ctx = build_context(symbol, mode)
        # F1: per-account mandate — SMSF/investing books cannot hold multi-expiry
        # combos on EU cash-settled indices; the ranker drops+flags those.
        investing = account in SENTINEL_INVESTING_ACCOUNTS
        ctx.mandate = {"account": account, "investing": investing,
                       "block_multi_expiry": investing and symbol in S.EU_CASH_INDEX}
        if mode == "live":
            def job(ib):
                return fetch_positions(ib, symbol, account, with_greeks=True)  # F2
            try:
                pos = shared.with_ib(job)
                ctx.book = book_greeks(ctx, pos)
                ctx.book["stress"] = stress_book(ctx, pos)
            except Exception as e:               # book optional, never fatal
                ctx.book = {"error": str(e)}
        if isinstance(ctx.book, dict):
            ctx.book["account"] = account
            ctx.book["nlv"] = nlv
        out = shortlist(ctx)
        if mode == "live" and out["cards"]:
            def enrich(ib):                      # one connection for both
                shared.reprice_cards(ib, symbol, ctx.spot, ctx.today, out["cards"])
                return scan_walls(ib, symbol, ctx, out["cards"])
            try:                                 # NBBO mids + OI walls
                out["walls"] = shared.with_ib(enrich)
            except Exception as e:               # keep model values on failure
                out["enrich_error"] = str(e)
        out["spot"] = ctx.spot
        out["mode"] = mode
        out["book"] = ctx.book
        out["book_warnings"] = book_warnings(ctx.book)
        log("shortlist", symbol, {"verdict": out["verdict"],
                                  "cards": [c["label"] for c in out["cards"]]})
        log_scan(out, account, mode)             # P3: structured, queryable row
        return jsonify(out)
    except Exception as e:                       # noqa: BLE001
        return jsonify({"error": str(e)}), 500



def _sentinel_mock_positions(spot: float) -> list[dict]:
    """Synthetic DEMO book: a put-heavy, near-dated short strangle (directional
    +delta, short vega, <=7 DTE -> gamma flag) so mock mode actually exercises
    Sentinel's conflict + suggestion UI. Only the positions are fabricated —
    Greeks still come from the real book_greeks pipeline."""
    f = (trading_today() + timedelta(days=6)).strftime("%Y%m%d")
    return [{"cp": "P", "strike": round(spot * 0.97), "expiry": f, "qty": -3, "conId": 0},
            {"cp": "C", "strike": round(spot * 1.05), "expiry": f, "qty": -1, "conId": 0}]


def _sentinel_payload(cards) -> list[dict]:
    """Serialize Sentinel guidance cards to JSON-safe dicts (enums -> values)."""
    def play(p):
        return {"family": p.family, "side": p.side.value,
                "intent": p.intent, "note": p.note}

    def conf(c):
        return {"name": c.name, "message": c.message,
                "severity": c.severity, "need": c.need}

    def sug(s):
        return {"family": s.family, "side": s.side.value, "intent": s.intent,
                "note": s.note, "fix_score": s.fix_score,
                "blocked": s.blocked, "block_reason": s.block_reason}

    return [{"account": c.account, "label": c.label, "pool": c.pool,
             "greeks": c.greeks, "budget": c.budget, "aligned": c.aligned,
             "conflicts": [conf(x) for x in c.conflicts],
             "suggestions": [sug(x) for x in c.suggestions],
             "standing_plays": [play(x) for x in c.standing_plays]}
            for c in cards]



@bp.get("/api/sentinel")
def api_sentinel():
    """Portfolio-level adjustment advisor: per-account guidance for one symbol.
    Reuses FVS regime + book greeks; adds Sentinel's decision matrix on top."""
    symbol = request.args.get("symbol", "SPX").upper()
    mode = request.args.get("mode", "mock")
    try:
        ctx = build_context(symbol, mode)
        reg = S.RegimeView.from_fvs({**ctx.regime, "symbol": symbol},
                                    term_stats(ctx.slices))
        if mode == "live":
            accts = shared.with_ib(list_accounts)
            pos_by = shared.with_ib(lambda ib: {a["account"]:
                             fetch_positions(ib, symbol, a["account"]) for a in accts})
        else:
            accts = MOCK_ACCOUNTS
            mp = _sentinel_mock_positions(ctx.spot)
            pos_by = {a["account"]: mp for a in accts}

        books = []
        for a in accts:
            bg = book_greeks(ctx, pos_by.get(a["account"], []))
            is_inv = (a["account"] in SENTINEL_INVESTING_ACCOUNTS
                      or (mode == "mock" and a is accts[-1]))   # demo: last = SMSF
            books.append(S.BookView.from_fvs(
                a, bg, label=a["account"],
                pool="investing" if is_inv else "trading",
                smsf_eu_cash_block=is_inv and symbol in S.EU_CASH_INDEX))

        cards = S.advise(reg, books)
        log("sentinel", symbol, {"accounts": len(books),
                                 "conflicts": sum(len(c.conflicts) for c in cards)})
        return jsonify({"symbol": symbol, "mode": mode, "spot": ctx.spot,
                        "headline": reg.headline(),
                        "cards": _sentinel_payload(cards)})
    except Exception as e:                       # noqa: BLE001
        return jsonify({"error": str(e)}), 500



@bp.post("/api/payoff")
def api_payoff():
    from core.pricing import risk_profile

    d = request.get_json(force=True)
    spot, today = float(d["spot"]), trading_today()
    q = q_for(d.get("symbol", ""))
    legs = [Leg(cp=leg["cp"], strike=float(leg["strike"]),
                expiry=date.fromisoformat(leg["expiry"]), qty=int(leg["qty"]),
                iv=float(leg.get("iv") or 0.18)) for leg in d["legs"]]
    entry = (float(d["net_mid"]) if d.get("net_mid") is not None
             else struct_value(spot, legs, today, q=q))
    profile = risk_profile(spot, legs, today, entry=entry, q=q)
    # Preserve the original endpoint field while exposing the richer profile.
    profile["expiry"] = profile["front_expiry"]
    return jsonify(profile)



@bp.post("/api/stage")
def api_stage():
    d = request.get_json(force=True)
    symbol = d["symbol"].upper()
    if d.get("mode") == "mock":
        log("stage_mock", symbol, d)
        return jsonify({"orderId": -1, "status": "MockStaged", "margin_change": None,
                        "note": "mock mode — nothing sent to TWS"})
    # The legacy endpoint trusted browser-supplied legs. Live use is disabled;
    # v3 requires a fresh immutable server-side candidate id.
    return jsonify({"error": "legacy live staging disabled; rescan in Campaign v3 and use /api/v3/stage"}), 410
