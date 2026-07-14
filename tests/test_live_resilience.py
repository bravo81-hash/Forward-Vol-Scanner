from datetime import date, datetime
from types import ModuleType, SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from core.events import trading_clock
from core.ib_client import _finish_result


def test_ny_melbourne_clock_handles_both_dst_seasons():
    winter_ny = datetime(2026, 1, 15, 15, 30, tzinfo=ZoneInfo("America/New_York"))
    summer_ny = datetime(2026, 7, 15, 15, 30, tzinfo=ZoneInfo("America/New_York"))
    jan = trading_clock(winter_ny)
    jul = trading_clock(summer_ny)
    assert (jan["melbourne_date"], jan["melbourne_time"]) == ("2026-01-16", "07:30:00")
    assert (jul["melbourne_date"], jul["melbourne_time"]) == ("2026-07-16", "05:30:00")
    assert jan["regular_session"] and jul["regular_session"]


def test_tws_missing_result_never_becomes_none_unpack_error():
    with pytest.raises(RuntimeError, match="ended without a result"):
        _finish_result({}, False)
    with pytest.raises(RuntimeError, match="timed out"):
        _finish_result({}, True)
    assert _finish_result({"result": None}, False) is None


class _Contract:
    def __init__(self, symbol, *args, tradingClass=None, **kwargs):
        self.symbol = symbol
        self.secType = "IND"
        self.tradingClass = tradingClass
        self.conId = 0


class _IB:
    def __init__(self, chains):
        self.chains = chains
        self.next_id = 1

    def qualifyContracts(self, *contracts):
        for contract in contracts:
            if not contract.conId:
                contract.conId = self.next_id
                self.next_id += 1
        return contracts

    def reqSecDefOptParams(self, *_args):
        return self.chains


def _install_fake_ib(monkeypatch):
    module = ModuleType("ib_insync")
    module.Index = module.Stock = module.Option = _Contract
    monkeypatch.setitem(__import__("sys").modules, "ib_insync", module)


def test_empty_tws_chain_has_clear_error_not_index_error(monkeypatch):
    import core.chain as chain_module

    _install_fake_ib(monkeypatch)
    monkeypatch.setattr(chain_module, "quote_many",
                        lambda _ib, contracts, **_kw: {contracts[0].conId: {"mid": 6000}})
    with pytest.raises(RuntimeError, match="no option-chain definitions"):
        chain_module.build_chain_live(_IB([]), "SPX", date(2026, 7, 14))


def test_off_hours_chain_uses_historical_iv_fallback(monkeypatch):
    import core.chain as chain_module

    _install_fake_ib(monkeypatch)
    chain_module.CHAIN_CACHE._d.clear()
    chain_module.PARAMS_CACHE._d.clear()
    calls = 0

    def quotes(_ib, contracts, **_kw):
        nonlocal calls
        calls += 1
        return ({contracts[0].conId: {"mid": 6000}} if calls == 1 else {})

    monkeypatch.setattr(chain_module, "quote_many", quotes)
    option_chain = SimpleNamespace(
        tradingClass="SPXW",
        expirations={"20260724", "20260731", "20260807", "20260821", "20260918"},
        strikes=set(range(5000, 7001, 5)),
    )
    diagnostics = {}
    spot, slices, strikes = chain_module.build_chain_live(
        _IB([option_chain]), "SPX", date(2026, 7, 14),
        fallback_spot=5990, fallback_iv=.20, diagnostics=diagnostics)
    assert spot == 6000
    assert len(slices) >= 2 and strikes
    assert all(s.atm_iv > .20 for s in slices)
    assert diagnostics["surface_source"] == "historical TWS IV fallback"
