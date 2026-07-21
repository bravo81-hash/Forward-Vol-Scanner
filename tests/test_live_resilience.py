from datetime import date, datetime
from types import ModuleType, SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from core.events import trading_clock
from core.ib_client import (TWS_REQUEST_TIMEOUT, _connection_error,
                            _finish_result, with_ib)


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


def test_every_tws_connection_is_bounded_but_tolerates_missing_contracts(monkeypatch):
    module = ModuleType("ib_insync")

    class InstantIB:
        RequestTimeout = 0
        RaiseRequestErrors = False

        def connect(self, *_args, **_kwargs):
            self.connected = True

        def isConnected(self):
            return getattr(self, "connected", False)

        def disconnect(self):
            self.connected = False

    module.IB = InstantIB
    monkeypatch.setitem(__import__("sys").modules, "ib_insync", module)
    assert with_ib(lambda ib: (ib.RequestTimeout, ib.RaiseRequestErrors)) == (
        TWS_REQUEST_TIMEOUT, False)


def test_timeout_connection_error_identifies_endpoint_and_settings():
    message = _connection_error("127.0.0.1", 7496, 7100, TimeoutError())
    assert "handshake timed out" in message
    assert "127.0.0.1:7496" in message
    assert "client ID 7100" in message
    assert "Enable ActiveX and Socket Clients" in message


def test_timeout_connection_is_retried_with_a_new_client_id(monkeypatch):
    module = ModuleType("ib_insync")
    attempted = []

    class RetryIB:
        RequestTimeout = 0
        RaiseRequestErrors = False

        def connect(self, _host, _port, clientId, **_kwargs):
            attempted.append(clientId)
            if len(attempted) == 1:
                raise TimeoutError()
            self.connected = True

        def isConnected(self):
            return getattr(self, "connected", False)

        def disconnect(self):
            self.connected = False

    module.IB = RetryIB
    monkeypatch.setitem(__import__("sys").modules, "ib_insync", module)
    monkeypatch.setattr("core.ib_client.time.sleep", lambda _seconds: None)
    assert with_ib(lambda _ib: "connected") == "connected"
    assert len(attempted) == 2
    assert attempted[1] == attempted[0] + 1


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


class _HistorylessIB(_IB):
    def __init__(self):
        super().__init__([])
        self.market_data_type = None

    def reqHistoricalData(self, *_args, **_kwargs):
        return []

    def reqMarketDataType(self, value):
        self.market_data_type = value


def test_zero_tws_bars_automatically_use_free_regime_history(monkeypatch):
    import core.context as context_module
    from core.chain import build_chain_mock
    from core.regime import mock_bars, mock_iv_hist

    _install_fake_ib(monkeypatch)
    today = date(2026, 7, 14)
    bars = mock_bars("SPX", 6000, today, 300)
    ivh = mock_iv_hist(18, 252)
    context_module.BARS_CACHE._d.clear()
    monkeypatch.setattr(context_module, "daily_bars", lambda *_args, **_kw: [])
    monkeypatch.setattr(context_module, "free_daily_inputs", lambda *_args, **_kw: {
        "bars": bars, "ivh": ivh, "price_source": "yfinance ^GSPC",
        "iv_source": "yfinance ^VIX"})

    def chain(_ib, symbol, day, *, fallback_spot, fallback_iv, diagnostics):
        assert fallback_spot == bars[-1][4]
        assert fallback_iv == ivh[-1] / 100
        diagnostics["surface_source"] = "historical TWS IV fallback"
        return build_chain_mock(symbol, day)

    monkeypatch.setattr(context_module, "build_chain_live", chain)
    monkeypatch.setattr(context_module, "with_ib",
                        lambda fn, **_kw: fn(_HistorylessIB()))
    ctx = context_module.build_context("SPX", "live", today=today)
    assert ctx.data["tws_daily_bars"] == 0
    assert ctx.data["regime_bars_source"] == "yfinance ^GSPC preloaded"
    assert ctx.data["iv_history_source"] == "yfinance ^VIX preloaded"
    assert ctx.data["tws_history_requested"] is False
    assert ctx.slices and ctx.regime["iv30"] > 0


def test_zero_tws_bars_reports_combined_error_if_free_history_fails(monkeypatch):
    import core.context as context_module

    _install_fake_ib(monkeypatch)
    context_module.BARS_CACHE._d.clear()
    monkeypatch.setattr(context_module, "daily_bars", lambda *_args, **_kw: [])

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("internet unavailable")

    monkeypatch.setattr(context_module, "free_daily_inputs", unavailable)
    monkeypatch.setattr(context_module, "with_ib",
                        lambda fn, **_kw: fn(_HistorylessIB()))
    with pytest.raises(RuntimeError, match="free history failed.*internet unavailable.*0 daily bars"):
        context_module.build_context("SPX", "live", today=date(2026, 7, 14))
