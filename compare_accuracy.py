"""Evidence harness: run pre-fix and post-fix; prints one table."""
import math, sys
from datetime import date, timedelta
sys.path.insert(0, ".")

from core import regime as R
from core.pricing import bs_price, bs_greeks
from core.models import Leg, Slice
from core.pricing import struct_metrics
from core.chain import k25
from core.surface import term_stats
import sentinel as S

TODAY = date(2026, 6, 15)
rows = []

# ---- E1: ATR true range on a -3% gap day (prev close 6000) -----------------
h, l, pc = 5865.0, 5820.0, 6000.0
tr_true = max(h - l, abs(h - pc), abs(l - pc))            # 180
tr_repo = max(h - l, abs(h - pc))                          # what regime.py uses
# post-fix regime exposes _tr; pre-fix it doesn't
tr_code = R._tr(h, l, pc) if hasattr(R, "_tr") else tr_repo
rows.append(("E1 ATR: TR on -3% gap bar (true 180.0)", f"{tr_code:.1f}"))

# ---- E2: ADX vs independent Wilder reference --------------------------------
def ref_wilder_adx(hs, ls, cs, n=14):
    trs, pd_, nd_ = [], [], []
    for i in range(1, len(cs)):
        trs.append(max(hs[i]-ls[i], abs(hs[i]-cs[i-1]), abs(ls[i]-cs[i-1])))
        up, dn = hs[i]-hs[i-1], ls[i-1]-ls[i]
        pd_.append(up if up > dn and up > 0 else 0.0)
        nd_.append(dn if dn > up and dn > 0 else 0.0)
    atr, p, q = sum(trs[:n]), sum(pd_[:n]), sum(nd_[:n])
    dx = []
    for i in range(n, len(trs)):
        atr = atr - atr/n + trs[i]; p = p - p/n + pd_[i]; q = q - q/n + nd_[i]
        pdi, ndi = 100*p/atr, 100*q/atr
        dx.append(100*abs(pdi-ndi)/max(pdi+ndi, 1e-9))
    a = sum(dx[:n]) / n
    for x in dx[n:]:
        a = (a*(n-1) + x) / n
    return a

bars = R.mock_bars("SPX", 6000.0, TODAY, n=300)
hs, ls, cs = [b[2] for b in bars], [b[3] for b in bars], [b[4] for b in bars]
rows.append(("E2 ADX: repo vs Wilder/Pine reference",
             f"{R._adx(hs, ls, cs):.2f} vs {ref_wilder_adx(hs, ls, cs):.2f}"))

# ---- E3: breakeven precision, single long call ------------------------------
iv, dte = 0.15, 30
K = 6000.0
entry = bs_price(6000.0, K, dte/365, iv, "C")
m = struct_metrics(6000.0, [Leg("C", K, TODAY+timedelta(days=dte), 1, iv)], TODAY)
be_true = K + entry
err = min(abs(b - be_true) for b in m["breakevens"]) if m["breakevens"] else float("nan")
rows.append(("E3 BE error, long call (pts; true BE = K+entry)", f"{err:.2f}"))

# ---- E5: term verdict, Fridays-only slices (event kink in 6d front) ---------
def slc(d, iv_):
    return Slice(expiry=TODAY+timedelta(days=d), dte=d, atm_strike=6000, atm_iv=iv_)
slices = [slc(6, 0.165), slc(13, 0.158), slc(27, 0.160), slc(34, 0.162)]
t = term_stats(slices)
# constant-maturity 9d/30d by variance interp (reference):
def cm(dte):
    ss = sorted(slices, key=lambda s: s.dte)
    for a, b in zip(ss, ss[1:]):
        if a.dte <= dte <= b.dte:
            va, vb = a.atm_iv**2*a.dte, b.atm_iv**2*b.dte
            return math.sqrt((va + (vb-va)*(dte-a.dte)/(b.dte-a.dte))/dte)
    return ss[0].atm_iv if dte < ss[0].dte else ss[-1].atm_iv
truth = "INVERTED FRONT" if cm(9)/cm(30) > 1.0 else "not-inverted"
rows.append((f"E5 term verdict (CM truth: {truth})", t["verdict"]))

# ---- E8: k25 — actual |delta| at the returned strike ------------------------
tt, iv8, r_, q_ = 30/365, 0.14, 0.04, 0.012
try:
    kp = k25(6000.0, iv8, tt, "P", q=q_); kc = k25(6000.0, iv8, tt, "C", q=q_)
except TypeError:                                   # pre-fix signature
    kp = k25(6000.0, iv8, tt, "P"); kc = k25(6000.0, iv8, tt, "C")
try:
    dp = abs(bs_greeks(6000.0, kp, tt, iv8, "P", q=q_)["delta"])
    dc = abs(bs_greeks(6000.0, kc, tt, iv8, "C", q=q_)["delta"])
except TypeError:
    dp = abs(bs_greeks(6000.0, kp, tt, iv8, "P")["delta"])
    dc = abs(bs_greeks(6000.0, kc, tt, iv8, "C")["delta"])
rows.append(("E8 |delta| at k25 strikes (target .250/.250)", f"P {dp:.3f} / C {dc:.3f}"))

# ---- E6: ATM 30d call delta, q=0 vs q=1.2% (Merton truth) --------------------
d_q0 = bs_greeks(6000.0, 6000.0, tt, iv8, "C")["delta"]
try:
    d_q = bs_greeks(6000.0, 6000.0, tt, iv8, "C", q=q_)["delta"]
except TypeError:
    d_q = float("nan")
rows.append(("E6 ATM call delta q=0 vs q=1.2%", f"{d_q0:.4f} vs {d_q:.4f}"))

# ---- E4: sentinel skew label at rr25 = +4.5 (put skew, equity norm) ----------
reg = {"symbol": "SPX", "trend": "RNG", "vol_state": "NRM", "iv_pctl": 50.0,
       "iv30": 14.0, "vrp": 1.0, "rv7": 13.0, "rv21": 13.0, "rv_falling": True,
       "gamma_score": 1, "gamma": "+g", "adx": 15.0, "bias": 0, "ac20": 0.0}
rv = S.RegimeView.from_fvs(reg, {"verdict": "CONTANGO", "rr25_30d": +4.5})
lab = "put-skew" if "put-skew" in rv.headline() else ("call-skew" if "call-skew" in rv.headline() else "flat")
rows.append(("E4 sentinel label at rr25=+4.5 (truth: put-skew)", lab))

# ---- E14: ranker verdict/order when VRP<0 and term FLAT ----------------------
from core.models import Context
from selection.ranker import family_priority
ctx = Context(symbol="SPX", spot=6000.0, today=TODAY, slices=slices, strikes=[6000.0],
              regime={**reg, "vrp": -1.0, "term": {"verdict": "FLAT"}, "rv21": 13.0},
              events={"fomc_dte": 999, "fomc_in_front": False, "opex_week": False,
                      "post_opex": False, "ex_div": False}, gates=[])
fams, verdict = family_priority(ctx)
rows.append(("E14 VRP<0/FLAT: verdict | first family",
             f"{verdict.split(' — ')[0]} | {fams[0][0]}"))

w = max(len(a) for a, _ in rows)
print(f"{'CHECK':<{w}}  RESULT")
for a, b in rows:
    print(f"{a:<{w}}  {b}")
