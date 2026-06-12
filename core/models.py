"""Shared dataclasses — the contracts every module speaks."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date


@dataclass
class Leg:
    cp: str                 # 'C' / 'P'
    strike: float
    expiry: date
    qty: int                # signed
    iv: float = 0.0         # model IV used for pricing (decimal)

    def key(self) -> str:
        return f"{self.qty:+d} {self.expiry:%b%d} {self.strike:g}{self.cp}"


@dataclass
class Slice:
    """One expiry on the surface."""
    expiry: date
    dte: int
    atm_strike: float
    atm_iv: float           # decimal
    put25_iv: float = 0.0
    call25_iv: float = 0.0
    put25_strike: float = 0.0
    call25_strike: float = 0.0
    atm_spread_pct: float = 0.0   # NBBO spread / mid at ATM (liquidity)
    oi_atm: int = 0

    @property
    def rr25(self) -> float:      # put-over-call skew, vol pts
        return (self.put25_iv - self.call25_iv) * 100 if self.put25_iv and self.call25_iv else 0.0


@dataclass
class Suggestion:
    strategy: str           # family key: calendar/diagonal/condor/bwb/butterfly/double_calendar
    label: str              # human variant label
    legs: list[Leg]
    net_mid: float          # +debit / -credit, per spread, model or NBBO
    greeks: dict            # delta/gamma/theta/vega per spread
    max_profit: float
    max_loss: float
    breakevens: list[float]
    score: float
    rationale: list[str]
    margin: float | None = None
    liquidity_pen: float = 0.0
    fit: float = 0.0

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["score"] = round(self.score, 2)
        d["fit"] = round(self.fit, 2)
        d["legs"] = [l.key() for l in self.legs]
        d["legs_raw"] = [{"cp": l.cp, "strike": l.strike,
                          "expiry": l.expiry.isoformat(), "qty": l.qty, "iv": round(l.iv, 4)}
                         for l in self.legs]
        return d


@dataclass
class Context:
    """Everything a strategy needs, computed ONCE per ticker per run."""
    symbol: str
    spot: float
    today: date
    slices: list[Slice]                 # sorted by dte
    strikes: list[float]                # listed strikes near spot
    regime: dict = field(default_factory=dict)
    events: dict = field(default_factory=dict)
    gates: list[dict] = field(default_factory=list)
    pairs: list[dict] = field(default_factory=list)   # fwd-vol pair table
    book: dict = field(default_factory=dict)          # current book greeks
    mode: str = "mock"

    def slice_near(self, dte: int) -> Slice | None:
        if not self.slices:
            return None
        return min(self.slices, key=lambda s: abs(s.dte - dte))

    def snap(self, k: float) -> float:
        return min(self.strikes, key=lambda x: abs(x - k)) if self.strikes else k

    def hard_gated(self) -> bool:
        return any(g["hard"] for g in self.gates)
