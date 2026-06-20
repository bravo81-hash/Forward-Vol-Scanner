"""
Sentinel — portfolio-level options ADJUSTMENT advisor.

The position-MANAGEMENT layer that sits on top of the existing ENTRY/SELECTION
apps (Forward-Vol-Scanner, Keystone). Given the current regime and per-account
aggregate Greeks — no manual input — it emits per-account guidance:

  1. what direction the book should express now  (bearish / neutral / bullish)
  2. whether to express it with DEBIT or CREDIT structures  (the vol-state switch)
  3. where the book's Greeks CONFLICT with the regime, and the specific
     structure family that closes the conflict

Design rule: this module RE-IMPLEMENTS NOTHING that the two apps already own.
It consumes their shapes and adds the decision matrix on top.

  regime   <- FVS  core.regime.compute_regime(bars, iv30_hist, iv30_now)
              FVS  core.surface.term_stats(slices)                  (term + rr25)
              Keystone regime.skew.build_skew(...)                  (optional rr25)
  book     <- FVS  portfolio.book.book_greeks(ctx, positions)       (per account/symbol)
  accounts <- Keystone portfolio.account_profiles                   (pool + SMSF block)

Two modes:
  * reference  — print the matrix + structure->Greek table (the standing guide)
  * connected  — feed real per-account books -> per-account guidance cards
                 (adapter at the bottom; guarded so this file runs with no TWS)

Run `python sentinel.py` for a self-contained demo (literal FVS-shaped inputs,
no TWS, no ib_insync).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Optional


# ============================================================ axes / vocab ==
class Direction(str, Enum):
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"
    BULLISH = "BULLISH"


class VolState(str, Enum):
    CHEAP = "CHEAP"   # buy premium  (low IVR / VRP<=0 / backwardation)
    FAIR = "FAIR"     # mixed
    RICH = "RICH"     # sell premium (high IVR / contango / VRP>0, rv falling)


class Side(str, Enum):
    DEBIT = "DEBIT"
    CREDIT = "CREDIT"
    EITHER = "EITHER"


_VOL_ORDER = [VolState.CHEAP, VolState.FAIR, VolState.RICH]

#: European cash-settled index tickers — the SMSF cash account cannot hold
#: multi-expiry combos on these (Keystone account_profiles.EU_CASH_INDEX_TICKERS).
EU_CASH_INDEX = frozenset({"SPX", "RUT", "NDX", "XSP"})


def _sign(x: float, tol: float = 0.0) -> int:
    return 1 if x > tol else -1 if x < -tol else 0


# ====================================================== structure -> Greeks ==
# Representative sign of each structure's net Greeks (per spread, ATM-ish).
# delta/gamma/vega/theta in {-1,0,+1}. `multi` = uses >1 expiry (SMSF-blockable
# on EU cash index). `pos_dependent` = signs shift with strike placement (flies,
# BWBs) — treat the row as the *typical* placement, confirm on the live chain.
@dataclass(frozen=True)
class GreekSig:
    delta: int
    gamma: int
    vega: int
    theta: int
    multi: bool = False
    pos_dependent: bool = False


STRUCTURE_GREEKS: dict[str, GreekSig] = {
    # debit, directional, long convexity
    "long_put":            GreekSig(-1, +1, +1, -1),
    "long_call":           GreekSig(+1, +1, +1, -1),
    "put_debit_spread":    GreekSig(-1, +1, +1, -1),
    "call_debit_spread":   GreekSig(+1, +1, +1, -1),
    # credit, directional, short vol
    "put_credit_spread":   GreekSig(+1, -1, -1, +1),
    "call_credit_spread":  GreekSig(-1, -1, -1, +1),
    "put_ratio":           GreekSig(-1, -1, -1, +1, pos_dependent=True),  # short-heavy
    "call_ratio":          GreekSig(+1, -1, -1, +1, pos_dependent=True),  # short-heavy
    # time structures: long back vega, short front gamma, +theta differential
    "calendar":            GreekSig(0, -1, +1, +1, multi=True),
    "put_calendar":        GreekSig(-1, -1, +1, +1, multi=True, pos_dependent=True),
    "call_calendar":       GreekSig(+1, -1, +1, +1, multi=True, pos_dependent=True),
    "double_calendar":     GreekSig(0, -1, +1, +1, multi=True),
    "diagonal":            GreekSig(0, -1, +1, +1, multi=True, pos_dependent=True),
    # harvest: short vol, +theta, short gamma
    "iron_condor":         GreekSig(0, -1, -1, +1),
    "iron_fly":            GreekSig(0, -1, -1, +1, pos_dependent=True),
    "short_strangle":      GreekSig(0, -1, -1, +1),
    "butterfly":           GreekSig(0, 0, -1, +1, pos_dependent=True),
    "put_fly":             GreekSig(-1, 0, -1, +1, pos_dependent=True),
    "call_bwb":            GreekSig(-1, 0, -1, +1, pos_dependent=True),  # OTM call broken-wing
}

GREEKS = ("delta", "gamma", "vega", "theta")


def _sig_get(sig: GreekSig, g: str) -> int:
    return getattr(sig, g)


# ============================================================ the matrix ====
@dataclass(frozen=True)
class Play:
    family: str
    side: Side
    intent: str         # the Greek/structural intent in one phrase
    note: str

    @property
    def sig(self) -> GreekSig:
        return STRUCTURE_GREEKS[self.family]


# (Direction, VolState) -> ranked adjustment families. Direction picks the SIDE
# of the book; vol-state picks DEBIT vs CREDIT. This grid IS the standing guide.
ADJUSTMENT_MATRIX: dict[tuple[Direction, VolState], list[Play]] = {
    (Direction.BEARISH, VolState.RICH): [
        Play("call_credit_spread", Side.CREDIT, "short vega + bearish delta", "sell the elevated call IV; defined risk"),
        Play("call_bwb",           Side.CREDIT, "bearish, skew-financed",     "OTM call broken-wing — sell the rip, vol-crush pays"),
        Play("put_ratio",          Side.CREDIT, "bearish, short vega",        "short-heavy put ratio; watch tail below"),
    ],
    (Direction.BEARISH, VolState.FAIR): [
        Play("call_credit_spread", Side.CREDIT, "bearish delta, mild -vega",  "primary if IV not cheap"),
        Play("put_debit_spread",   Side.DEBIT,  "bearish, +gamma/+vega",      "switch here if expansion risk rising"),
    ],
    (Direction.BEARISH, VolState.CHEAP): [
        Play("long_put",           Side.DEBIT,  "bearish, +gamma +vega",      "buy cheap convexity into a falling, vol-expanding tape"),
        Play("put_debit_spread",   Side.DEBIT,  "bearish, defined cost",      "cheaper than outright; capped"),
        Play("put_calendar",       Side.DEBIT,  "bearish, +vega front-cheap", "own the front if backwardation/expansion expected"),
        Play("put_fly",            Side.DEBIT,  "bearish to a target",        "if a downside magnet/level is in view"),
    ],
    (Direction.NEUTRAL, VolState.RICH): [
        Play("iron_condor",        Side.CREDIT, "delta-neutral harvest",      "sell elevated premium; +theta, -vega"),
        Play("iron_fly",           Side.CREDIT, "neutral, strong +theta",     "tighter body if pinning; -gamma"),
        Play("short_strangle",     Side.CREDIT, "neutral, max +theta",        "undefined — only where margin/mandate allow"),
    ],
    (Direction.NEUTRAL, VolState.FAIR): [
        Play("iron_condor",        Side.CREDIT, "neutral harvest",            "standard range structure"),
        Play("butterfly",          Side.DEBIT,  "neutral to a pin",           "cheap defined-risk if a level holds"),
        Play("calendar",           Side.DEBIT,  "neutral, +vega",             "if term in contango — own back vega"),
    ],
    (Direction.NEUTRAL, VolState.CHEAP): [
        Play("calendar",           Side.DEBIT,  "neutral, +vega +theta",      "own cheap back vega; theta differential"),
        Play("double_calendar",    Side.DEBIT,  "neutral, wider tent",        "two feet for a wider range"),
        Play("diagonal",           Side.DEBIT,  "lean + vega",                "if a small directional tilt is wanted"),
    ],
    (Direction.BULLISH, VolState.RICH): [
        Play("put_credit_spread",  Side.CREDIT, "short vega + bullish delta",  "sell the elevated put IV; defined risk"),
        Play("call_ratio",         Side.CREDIT, "bullish-capped, short vega",  "short-heavy call ratio above market"),
    ],
    (Direction.BULLISH, VolState.FAIR): [
        Play("put_credit_spread",  Side.CREDIT, "bullish delta, mild -vega",   "primary if IV not cheap"),
        Play("call_debit_spread",  Side.DEBIT,  "bullish, +gamma/+vega",       "switch here if IV cheapening"),
    ],
    (Direction.BULLISH, VolState.CHEAP): [
        Play("call_debit_spread",  Side.DEBIT,  "bullish, +gamma +vega",       "buy cheap upside; capped cost"),
        Play("long_call",          Side.DEBIT,  "bullish, +gamma +vega",       "outright if a squeeze/run is the thesis"),
        Play("call_calendar",      Side.DEBIT,  "bullish, +vega",              "own front if upside vol expansion expected"),
    ],
}


# ============================================================ regime view ===
@dataclass
class RegimeView:
    symbol: str
    direction: Direction
    vol_state: VolState
    vol_expanding: bool
    # raw fields retained for messaging + conflict logic
    trend: str            # RNG / UP / DN  (FVS)
    bias: int             # -2..2          (FVS)
    adx: float
    iv_pctl: float
    vrp: float
    rv_falling: bool
    gamma_score: int      # FVS gamma_score (negative = unstable/whippy tape)
    term_verdict: str     # INVERTED FRONT / STEEP CONTANGO / CONTANGO / FLAT
    rr25: float           # 25d risk reversal, vol pts (negative = put skew = equity norm)

    @classmethod
    def from_fvs(cls, regime: dict, term: Optional[dict] = None,
                 rr25: Optional[float] = None) -> "RegimeView":
        """Adapt FVS compute_regime() + term_stats() (+ optional rr25) -> axes.

        Direction comes straight from the FVS trend (which already encodes ADX:
        RNG when adx<ADX_THR). Vol-state buckets IV percentile, then VRP and the
        term verdict flip it toward DEBIT/CREDIT — this is the 'something in the
        context changed' switch.
        """
        term = term or {}
        verdict = term.get("verdict", regime.get("term", {}).get("verdict", "FLAT"))
        if rr25 is None:
            rr25 = term.get("rr25_30d", regime.get("rr25", 0.0))

        direction = {"UP": Direction.BULLISH, "DN": Direction.BEARISH,
                     "RNG": Direction.NEUTRAL}.get(regime["trend"], Direction.NEUTRAL)

        ivp = regime["iv_pctl"]
        base = VolState.RICH if ivp >= 60 else VolState.FAIR if ivp >= 25 else VolState.CHEAP
        idx = _VOL_ORDER.index(base)
        if regime["vrp"] <= 0:            # realized >= implied -> selling unpaid -> lean cheap
            idx = max(0, idx - 1)
        vol_state = _VOL_ORDER[idx]
        if verdict == "INVERTED FRONT":   # backwardation -> expansion -> buy/own front
            vol_state = VolState.CHEAP

        vol_expanding = (not regime["rv_falling"]) or verdict == "INVERTED FRONT" or regime["vrp"] <= 0

        return cls(symbol=regime.get("symbol", "?"), direction=direction, vol_state=vol_state,
                   vol_expanding=vol_expanding, trend=regime["trend"], bias=regime.get("bias", 0),
                   adx=regime.get("adx", 0.0), iv_pctl=ivp, vrp=regime["vrp"],
                   rv_falling=regime["rv_falling"], gamma_score=regime.get("gamma_score", 0),
                   term_verdict=verdict, rr25=rr25 or 0.0)

    def cell(self) -> list[Play]:
        return ADJUSTMENT_MATRIX[(self.direction, self.vol_state)]

    def headline(self) -> str:
        skew = ("put-skew" if self.rr25 < -2 else "call-skew" if self.rr25 > 2 else "flat-skew")
        return (f"{self.symbol}: {self.direction.value} / vol {self.vol_state.value} "
                f"({'expanding' if self.vol_expanding else 'contracting'}) — "
                f"IVpctl {self.iv_pctl:.0f}, VRP {self.vrp:+.1f}, ADX {self.adx:.0f}, "
                f"term {self.term_verdict}, {skew} {self.rr25:+.1f}")


# ============================================================ book view =====
# Greek budgets per $100k NLV — mirrors FVS portfolio.risk so Sentinel stays
# standalone but consistent with the entry app.
BUDGET_PER_100K = {"vega": 12.0, "delta": 0.30, "theta_min": 0.0}


def budget_for(nlv: Optional[float]) -> dict:
    unit = max((nlv or 100_000.0) / 100_000.0, 0.25)
    return {"vega": BUDGET_PER_100K["vega"] * unit,
            "delta": BUDGET_PER_100K["delta"] * unit,
            "theta_min": BUDGET_PER_100K["theta_min"]}


@dataclass
class BookView:
    account: str
    label: str
    pool: str                 # 'trading' | 'investing'
    nlv: float
    delta: float
    gamma: float
    theta: float
    vega: float
    min_short_dte: Optional[int] = None
    gamma_flag: bool = False
    smsf_eu_cash_block: bool = False   # cannot do multi-expiry combos on EU cash index

    @classmethod
    def from_fvs(cls, account_meta: dict, book: dict, *, label: str = "",
                 pool: str = "trading", smsf_eu_cash_block: bool = False) -> "BookView":
        """Adapt FVS portfolio.book.book_greeks() output for one account/symbol."""
        g = book.get("greeks", {})
        return cls(account=account_meta.get("account", "?"),
                   label=label or account_meta.get("account", "?"),
                   pool=pool, nlv=float(account_meta.get("nlv") or 0.0),
                   delta=g.get("delta", 0.0), gamma=g.get("gamma", 0.0),
                   theta=g.get("theta", 0.0), vega=g.get("vega", 0.0),
                   min_short_dte=book.get("min_short_dte"),
                   gamma_flag=bool(book.get("gamma_flag")))


# ====================================================== conflict detector ===
@dataclass
class Conflict:
    name: str
    message: str
    need: dict          # greek -> desired sign of the ADJUSTMENT's contribution
    severity: str       # "warn" | "act"


def detect_conflicts(reg: RegimeView, book: BookView, bud: dict) -> list[Conflict]:
    """Diff (account Greeks) against (regime-prescribed posture)."""
    out: list[Conflict] = []
    d_tol, v_tol = bud["delta"], bud["vega"]

    # --- delta vs direction -------------------------------------------------
    want = {Direction.BEARISH: -1, Direction.BULLISH: +1, Direction.NEUTRAL: 0}[reg.direction]
    d_now = _sign(book.delta, d_tol * 0.5)
    if reg.direction is Direction.NEUTRAL:
        if abs(book.delta) > d_tol:
            out.append(Conflict("delta", f"book delta {book.delta:+.2f} off-neutral "
                                f"(±{d_tol:.2f}) in a range regime — recenter to flat",
                                {"delta": -_sign(book.delta)}, "act"))
    else:
        if d_now != 0 and d_now != want:
            out.append(Conflict("delta", f"book delta {book.delta:+.2f} fights the "
                                f"{reg.direction.value} tape — add {'short' if want<0 else 'long'} delta",
                                {"delta": want}, "act"))
        elif abs(book.delta) > d_tol:
            out.append(Conflict("delta", f"book delta {book.delta:+.2f} over ±{d_tol:.2f} "
                                f"budget — trim toward neutral", {"delta": -_sign(book.delta)}, "warn"))

    # --- vega vs vol regime -------------------------------------------------
    v_now = _sign(book.vega, v_tol * 0.5)
    long_vega_crush = (reg.vol_state is VolState.RICH and reg.rv_falling and reg.iv_pctl >= 60)
    if v_now < 0 and reg.vol_expanding:
        out.append(Conflict("vega", f"book vega {book.vega:+.1f} is SHORT into expanding vol "
                            f"({reg.term_verdict}, VRP {reg.vrp:+.1f}) — add long vega / shift credit->debit",
                            {"vega": +1}, "act"))
    elif v_now > 0 and long_vega_crush:
        out.append(Conflict("vega", f"book vega {book.vega:+.1f} is LONG into mean-reverting vol "
                            f"(IVpctl {reg.iv_pctl:.0f}, RV falling) — trim vega / harvest",
                            {"vega": -1}, "act"))
    elif abs(book.vega) > v_tol:
        out.append(Conflict("vega", f"book vega {book.vega:+.1f} over ±{v_tol:.0f} budget",
                            {"vega": -_sign(book.vega)}, "warn"))

    # --- gamma / theta ------------------------------------------------------
    rising_realized = (not reg.rv_falling) and reg.adx >= 20
    if book.gamma_flag or (book.gamma < 0 and rising_realized):
        dte = f" (short leg {book.min_short_dte}d)" if book.min_short_dte is not None else ""
        out.append(Conflict("gamma", f"book is SHORT gamma into rising realized{dte} — "
                            f"reduce gamma: roll fronts out / close near-expiry / widen, or buy gamma",
                            {"gamma": +1}, "act"))
    if reg.direction is Direction.NEUTRAL and reg.vol_state is VolState.RICH and book.theta < bud["theta_min"]:
        out.append(Conflict("theta", f"book theta {book.theta:+.2f} negative in a harvest regime — "
                            f"restructure toward +theta", {"theta": +1}, "warn"))

    return out


# ====================================================== structure picker ====
@dataclass
class Suggestion:
    family: str
    side: Side
    intent: str
    note: str
    fix_score: int
    blocked: bool = False
    block_reason: str = ""


def _score(fam: str, need: dict) -> int:
    sig = STRUCTURE_GREEKS[fam]
    s = 0
    for g, want in need.items():
        have = _sig_get(sig, g)
        s += 1 if have == want else -1 if have == -want else 0
    return s


def suggest(reg: RegimeView, need: dict, *, top_n: int = 3) -> list[Suggestion]:
    """Rank structures that (a) fit the regime cell and (b) supply the missing
    Greeks. If no in-cell family supplies a needed Greek sign, pull the best
    out-of-cell family that does — that surface is the debit<->credit flip."""
    cell = reg.cell()
    cell_fams = [p.family for p in cell]
    cand: dict[str, Play] = {p.family: p for p in cell}

    if need:
        for g, want in need.items():
            if want == 0:
                continue
            if not any(_sig_get(STRUCTURE_GREEKS[f], g) == want for f in cell_fams):
                ext = max(STRUCTURE_GREEKS, key=lambda f: _score(f, need))
                if ext not in cand and _score(ext, need) > 0:
                    sig = STRUCTURE_GREEKS[ext]
                    side = Side.CREDIT if (sig.vega < 0 and sig.theta > 0) else Side.DEBIT
                    cand[ext] = Play(ext, side, "context flip",
                                     "supplies a Greek the in-regime structures can't")

    ranked = sorted(cand.values(),
                    key=lambda p: (_score(p.family, need), p.family in cell_fams),
                    reverse=True)
    return [Suggestion(p.family, p.side, p.intent, p.note, _score(p.family, need))
            for p in ranked[:top_n]]


def _apply_block(reg: RegimeView, book: BookView, sugs: list[Suggestion]) -> list[Suggestion]:
    if not (book.smsf_eu_cash_block and reg.symbol.upper() in EU_CASH_INDEX):
        return sugs
    for s in sugs:
        if STRUCTURE_GREEKS[s.family].multi:
            s.blocked = True
            s.block_reason = "SMSF: no multi-expiry combo on EU cash-settled index"
    return sugs


# ============================================================ advisor =======
@dataclass
class AccountGuidance:
    account: str
    label: str
    pool: str
    regime_headline: str
    greeks: dict
    budget: dict
    aligned: bool
    conflicts: list[Conflict]
    suggestions: list[Suggestion]
    standing_plays: list[Play]   # the regime cell (shown when aligned / as reference)


def advise(reg: RegimeView, books: Iterable[BookView], *, top_n: int = 3) -> list[AccountGuidance]:
    cards: list[AccountGuidance] = []
    for b in books:
        bud = budget_for(b.nlv)
        conflicts = detect_conflicts(reg, b, bud)
        need_acc = {g: 0 for g in GREEKS}
        for c in conflicts:
            for g, want in c.need.items():
                need_acc[g] += want
        need = {g: _sign(v) for g, v in need_acc.items() if v != 0}
        sugs = _apply_block(reg, b, suggest(reg, need, top_n=top_n)) if conflicts else []
        cards.append(AccountGuidance(
            account=b.account, label=b.label, pool=b.pool,
            regime_headline=reg.headline(),
            greeks={"delta": b.delta, "gamma": b.gamma, "vega": b.vega, "theta": b.theta},
            budget=bud, aligned=not conflicts, conflicts=conflicts,
            suggestions=sugs, standing_plays=reg.cell()))
    return cards


# ============================================================ rendering =====
def render(cards: list[AccountGuidance]) -> str:
    L = []
    for c in cards:
        L.append("=" * 78)
        L.append(f"ACCOUNT {c.label}  [{c.pool}]")
        L.append(c.regime_headline)
        g, bud = c.greeks, c.budget
        L.append(f"  book greeks: d{g['delta']:+.2f} g{g['gamma']:+.3f} "
                 f"V{g['vega']:+.1f} t{g['theta']:+.2f}   "
                 f"budget +-d{bud['delta']:.2f} +-V{bud['vega']:.0f}")
        if c.aligned:
            L.append("  STATUS: aligned with regime — no adjustment forced.")
            L.append("  standing structures for this regime (reference):")
            for p in c.standing_plays:
                L.append(f"    - {p.family:<18} [{p.side.value}]  {p.intent}")
        else:
            L.append("  STATUS: CONFLICT(S):")
            for cf in c.conflicts:
                tag = "ACT " if cf.severity == "act" else "warn"
                L.append(f"    [{tag}] {cf.name}: {cf.message}")
            L.append("  suggested adjustment structures (regime-fit + Greek-fix):")
            for s in c.suggestions:
                blk = f"   << BLOCKED: {s.block_reason}" if s.blocked else ""
                L.append(f"    - {s.family:<18} [{s.side.value}] fix={s.fix_score:+d}  "
                         f"{s.intent} — {s.note}{blk}")
    return "\n".join(L)


def print_reference() -> str:
    """The standing matrix + structure->Greek table — the guide/reference."""
    L = ["ADJUSTMENT MATRIX  (direction x vol-state -> ranked structure families)",
         "direction sets the SIDE of the book; vol-state sets DEBIT vs CREDIT", ""]
    for d in Direction:
        for v in VolState:
            plays = ADJUSTMENT_MATRIX[(d, v)]
            head = f"{d.value:<8} x {v.value:<6} ->"
            fams = ", ".join(f"{p.family}[{p.side.value[0]}]" for p in plays)
            L.append(f"  {head} {fams}")
        L.append("")
    L.append("STRUCTURE -> GREEK SIGNATURE   (d g V t ; m=multi-expiry, *=strike-dependent)")
    for f, s in STRUCTURE_GREEKS.items():
        flags = ("m" if s.multi else " ") + ("*" if s.pos_dependent else " ")
        L.append(f"  {f:<20} {s.delta:+d} {s.gamma:+d} {s.vega:+d} {s.theta:+d}  {flags}")
    return "\n".join(L)


# ============================================================ live adapter ==
def from_live(ib, symbol: str, profiles: list, *,
              build_ctx_fn, term_stats_fn, book_greeks_fn, fetch_positions_fn) -> list[AccountGuidance]:
    """Connected mode. Wire the EXISTING app functions in — no re-implementation:

        from core.context   import build_context   as build_ctx_fn
        from core.surface   import term_stats       as term_stats_fn
        from portfolio.book import book_greeks      as book_greeks_fn
        from portfolio.book import fetch_positions  as fetch_positions_fn

    FVS `Context` already carries `.regime` (compute_regime output) and `.slices`,
    so Sentinel just adapts them. `profiles` are Keystone AccountProfile-likes
    (account_id, label, pool, nlv). One regime read per symbol; per-account book
    + advice. Import-free here so this module loads with no TWS / ib_insync.
    """
    ctx = build_ctx_fn(ib, symbol)
    term = term_stats_fn(ctx.slices)
    reg = RegimeView.from_fvs({**ctx.regime, "symbol": symbol}, term)
    books = []
    for p in profiles:
        positions = fetch_positions_fn(ib, symbol, account=p.account_id)
        bg = book_greeks_fn(ctx, positions)
        pool = getattr(p.pool, "value", str(p.pool))
        smsf_block = (symbol.upper() in EU_CASH_INDEX and pool == "investing")
        books.append(BookView.from_fvs({"account": p.account_id, "nlv": p.nlv}, bg,
                                       label=p.label, pool=pool, smsf_eu_cash_block=smsf_block))
    return advise(reg, books)


# ============================================================ demo ==========
def _demo() -> None:
    print(print_reference())
    print("\n" + "#" * 78)
    print("# DEMO — SPX, regime turned bearish into a vol expansion")
    print("#" * 78 + "\n")

    # FVS-shaped regime dict (exactly compute_regime()'s output keys).
    regime = {"symbol": "SPX", "trend": "DN", "vol_state": "CMP", "iv_pctl": 22.0,
              "iv30": 14.1, "vrp": -1.8, "rv7": 17.0, "rv21": 15.9, "rv_falling": False,
              "gamma_score": -2, "gamma": "-g", "adx": 28.0, "bias": -2, "ac20": -0.07}
    term = {"verdict": "INVERTED FRONT", "rr25_30d": -9.4, "skew_rich": True}
    reg = RegimeView.from_fvs(regime, term)

    # Synthetic per-account books (would come from FVS book_greeks per account).
    books = [
        BookView("U-TRAD-1", "Trading 1 (margin)", "trading", 250_000,
                 delta=+0.92, gamma=-0.020, theta=+0.41, vega=-19.0,
                 min_short_dte=6, gamma_flag=True),
        BookView("U-TRAD-2", "Trading 2 (margin)", "trading", 100_000,
                 delta=-0.05, gamma=-0.004, theta=+0.10, vega=+14.0,
                 min_short_dte=30, gamma_flag=False),
        BookView("U-SMSF", "SMSF (cash)", "investing", 92_000,
                 delta=+0.40, gamma=-0.006, theta=+0.05, vega=-6.0,
                 min_short_dte=20, gamma_flag=False, smsf_eu_cash_block=True),
    ]
    print(render(advise(reg, books)))


if __name__ == "__main__":
    _demo()
