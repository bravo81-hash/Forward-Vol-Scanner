"""Sentinel unit tests — run: python -m pytest test_sentinel.py -v"""
import unittest

from sentinel import (
    Direction, VolState, Side, RegimeView, BookView, budget_for,
    detect_conflicts, suggest, advise, print_reference, STRUCTURE_GREEKS,
)


def reg(trend="DN", iv_pctl=22.0, vrp=-1.8, rv_falling=False, adx=28.0,
        verdict="INVERTED FRONT", gamma_score=-2, symbol="SPX"):
    d = {"symbol": symbol, "trend": trend, "vol_state": "CMP", "iv_pctl": iv_pctl,
         "iv30": 14.0, "vrp": vrp, "rv7": 17.0, "rv21": 15.9, "rv_falling": rv_falling,
         "gamma_score": gamma_score, "gamma": "-g", "adx": adx, "bias": -2, "ac20": -0.07}
    return RegimeView.from_fvs(d, {"verdict": verdict, "rr25_30d": 9.4})


class TestAxisDerivation(unittest.TestCase):
    def test_direction_mapping(self):
        self.assertIs(reg(trend="UP").direction, Direction.BULLISH)
        self.assertIs(reg(trend="DN").direction, Direction.BEARISH)
        self.assertIs(reg(trend="RNG").direction, Direction.NEUTRAL)

    def test_vol_rich_high_ivr_contango(self):
        r = reg(iv_pctl=80, vrp=+3.0, rv_falling=True, verdict="CONTANGO")
        self.assertIs(r.vol_state, VolState.RICH)
        self.assertFalse(r.vol_expanding)

    def test_vrp_negative_nudges_cheap(self):
        r = reg(iv_pctl=50, vrp=-2.0, verdict="FLAT")   # base FAIR -> nudged CHEAP
        self.assertIs(r.vol_state, VolState.CHEAP)
        self.assertTrue(r.vol_expanding)

    def test_backwardation_forces_cheap(self):
        r = reg(iv_pctl=80, vrp=+1.0, verdict="INVERTED FRONT")  # high IVR but inverted
        self.assertIs(r.vol_state, VolState.CHEAP)
        self.assertTrue(r.vol_expanding)


class TestConflicts(unittest.TestCase):
    def setUp(self):
        self.r = reg()                       # SPX bearish / cheap / expanding
        self.bud = budget_for(250_000)

    def test_long_delta_into_bearish(self):
        b = BookView("A", "A", "trading", 250_000, delta=+90.0, gamma=-2.0,
                     theta=+40.0, vega=-1900, min_short_dte=6, gamma_flag=True)
        names = {c.name for c in detect_conflicts(self.r, b, self.bud)}
        self.assertEqual(names, {"delta", "vega", "gamma"})

    def test_short_vega_into_expansion(self):
        b = BookView("A", "A", "trading", 250_000, delta=0.0, gamma=+1.0,
                     theta=+10.0, vega=-2000, min_short_dte=30, gamma_flag=False)
        vega_c = [c for c in detect_conflicts(self.r, b, self.bud) if c.name == "vega"]
        self.assertTrue(vega_c and vega_c[0].need["vega"] == +1)

    def test_aligned_book_no_conflict(self):
        b = BookView("A", "A", "trading", 250_000, delta=-1.0, gamma=+1.0,
                     theta=+10.0, vega=+500, min_short_dte=30, gamma_flag=False)
        self.assertEqual(detect_conflicts(self.r, b, self.bud), [])

    def test_long_vega_crush_in_rich(self):
        r = reg(iv_pctl=80, vrp=+3.0, rv_falling=True, verdict="CONTANGO")  # RICH, contracting
        b = BookView("A", "A", "trading", 250_000, delta=0.0, gamma=-1.0,
                     theta=+20.0, vega=+2000, min_short_dte=30, gamma_flag=False)
        vega_c = [c for c in detect_conflicts(r, b, budget_for(250_000)) if c.name == "vega"]
        self.assertTrue(vega_c and vega_c[0].need["vega"] == -1)


class TestStructureSelection(unittest.TestCase):
    def test_long_put_fixes_bearish_expansion(self):
        r = reg()
        need = {"delta": -1, "vega": +1, "gamma": +1}
        sugs = suggest(r, need, top_n=3)
        fam = {s.family for s in sugs}
        self.assertIn("long_put", fam)
        lp = next(s for s in sugs if s.family == "long_put")
        self.assertEqual(lp.fix_score, 3)
        self.assertIs(lp.side, Side.DEBIT)

    def test_out_of_cell_reach_for_vega(self):
        # RICH/neutral cell is all short-vega; a +vega need must pull a calendar in
        r = reg(trend="RNG", iv_pctl=80, vrp=+3.0, rv_falling=True, verdict="CONTANGO")
        self.assertIs(r.vol_state, VolState.RICH)
        sugs = suggest(r, {"vega": +1}, top_n=4)
        self.assertTrue(any(STRUCTURE_GREEKS[s.family].vega == +1 for s in sugs))


class TestSMSFBlock(unittest.TestCase):
    def test_calendar_blocked_on_spx_smsf(self):
        r = reg(symbol="SPX")
        b = BookView("SMSF", "SMSF", "investing", 92_000, delta=+40.0, gamma=-0.6,
                     theta=+5.0, vega=-600, min_short_dte=20, smsf_eu_cash_block=True)
        card = advise(r, [b])[0]
        cal = [s for s in card.suggestions if s.family == "put_calendar"]
        self.assertTrue(cal and cal[0].blocked)

    def test_calendar_not_blocked_on_single_name(self):
        r = reg(symbol="AAPL")
        b = BookView("SMSF", "SMSF", "investing", 92_000, delta=+40.0, gamma=-0.6,
                     theta=+5.0, vega=-600, min_short_dte=20, smsf_eu_cash_block=True)
        card = advise(r, [b])[0]
        cal = [s for s in card.suggestions if s.family == "put_calendar"]
        self.assertTrue(all(not c.blocked for c in cal))


class TestBudget(unittest.TestCase):
    def test_scales_with_nlv(self):
        self.assertAlmostEqual(budget_for(200_000)["vega"], 2400.0)
        self.assertAlmostEqual(budget_for(100_000)["delta"], 5.0)
        self.assertAlmostEqual(budget_for(50_000)["vega"], 600.0)

    def test_floor(self):
        self.assertAlmostEqual(budget_for(10_000)["vega"], 1200.0 * 0.25)


class TestReference(unittest.TestCase):
    def test_prints(self):
        txt = print_reference()
        self.assertIn("ADJUSTMENT MATRIX", txt)
        self.assertIn("STRUCTURE -> GREEK", txt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
