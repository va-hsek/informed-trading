from collections import namedtuple
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from collectors.broker_alice import AliceBroker, BrokerError, pick_contracts, refresh_store
from core.prices import PriceDataError, PriceStore, normalize_candles, read_csv, stitch, write_csv


def frame(start, n, base, step=1.0, vol=100):
    idx = pd.date_range(start, periods=n, freq="D")
    c = base + step * np.arange(n)
    return pd.DataFrame({"open": c, "high": c + 1, "low": c - 1, "close": c, "volume": vol}, index=idx)


# ---------------- stitching ----------------
def test_stitch_removes_jump_and_keeps_latest_contract_real():
    # old contract: dates Jan1..Feb9 at price ~100+; new contract trades 10 higher on the same dates
    old = frame("2026-01-01", 40, 100.0)                  # expires 2026-02-10
    new = frame("2026-01-20", 60, 100.0 + 19 + 10)        # on 2026-01-20 old closes 119, new closes 129 (+10)
    cont = stitch([("OLD", date(2026, 2, 10), old), ("NEW", date(2026, 3, 30), new)], roll_days=5)
    # roll date = 2026-02-05 ; last common date <= roll is 2026-02-05 itself
    assert cont.index.is_monotonic_increasing and not cont.index.has_duplicates
    # latest contract is untouched
    assert cont.loc["2026-03-01", "close"] == new.loc["2026-03-01", "close"]
    # no jump across the roll: close on roll date vs next day differs by the normal +1 step
    d_roll = pd.Timestamp("2026-02-05")
    step = cont.loc[d_roll + timedelta(days=1), "close"] - cont.loc[d_roll, "close"]
    assert abs(step - 1.0) < 1e-9
    # old contract data was shifted up by exactly the gap (10)
    assert abs(cont.loc["2026-01-10", "close"] - (old.loc["2026-01-10", "close"] + 10)) < 1e-9


def test_stitch_three_contracts_cumulative_adjustment():
    a = frame("2026-01-01", 45, 100.0)       # exp 2026-02-12
    b = frame("2026-01-15", 70, 110.0 + 14)  # +10 vs a on common days
    c = frame("2026-02-15", 90, 200.0)
    # compute expected gaps independently
    cont = stitch([("A", date(2026, 2, 12), a), ("B", date(2026, 3, 20), b), ("C", date(2026, 5, 30), c)], roll_days=5)
    assert cont.index.is_monotonic_increasing
    assert cont.loc["2026-05-01", "close"] == c.loc["2026-05-01", "close"]
    # across each roll the series must be continuous (step equals the contract's own +1 step)
    diffs = cont["close"].diff().dropna()
    assert diffs.abs().max() < 1.5 + 1e-9, "a jump survived the stitch"


def test_stitch_refuses_when_no_common_date():
    a = frame("2026-01-01", 10, 100.0)
    b = frame("2026-03-01", 30, 120.0)
    with pytest.raises(PriceDataError):
        stitch([("A", date(2026, 2, 10), a), ("B", date(2026, 4, 30), b)])


# ---------------- store ----------------
def test_store_accumulates_and_stitches(tmp_path):
    st = PriceStore(tmp_path)
    old = frame("2026-01-01", 40, 100.0)
    st.save_contract("silver_micro", "SILVERMIC26FEB", date(2026, 2, 10), old.iloc[:20])
    st.save_contract("silver_micro", "SILVERMIC26FEB", date(2026, 2, 10), old.iloc[15:])      # overlap merges, no dupes
    st.save_contract("silver_micro", "SILVERMIC26MAR", date(2026, 3, 30), frame("2026-01-20", 60, 129.0))
    cs = st.load_contracts("silver_micro")
    assert len(cs) == 2 and len(cs[0][2]) == 40 and not cs[0][2].index.has_duplicates
    assert len(st.continuous("silver_micro")) > 60


def test_store_continuous_override_wins(tmp_path):
    st = PriceStore(tmp_path)
    write_csv(frame("2025-01-01", 100, 50.0), tmp_path / "continuous" / "gold_ten.csv")
    assert len(st.continuous("gold_ten")) == 100


# ---------------- candle normalisation ----------------
def test_normalize_candles_formats():
    rows = [{"time": "2026-11-05 00:00:00", "open": "100", "high": "105", "low": "99", "close": "104", "volume": "10"},
            {"time": "2026-11-06 00:00:00", "open": 104, "high": 108, "low": 103, "close": 107, "volume": 12}]
    df = normalize_candles(rows)
    assert list(df["close"]) == [104.0, 107.0] and df.index[0] == pd.Timestamp("2026-11-05")
    df2 = normalize_candles({"result": [{"DateTime": 1793923200000, "Open": 1, "High": 2, "Low": 0.5, "Close": 1.5}]})
    assert len(df2) == 1
    # intraday rows collapse to one daily bar
    intr = [{"time": "2026-11-05 09:00:00", "open": 1, "high": 5, "low": 1, "close": 2, "volume": 1},
            {"time": "2026-11-05 15:00:00", "open": 2, "high": 6, "low": 0.5, "close": 4, "volume": 2}]
    d = normalize_candles(intr).iloc[0]
    assert (d.open, d.high, d.low, d.close, d.volume) == (1, 6, 0.5, 4, 3)


@pytest.mark.parametrize("bad", [None, [], {}, {"stat": "Not_ok", "emsg": "x"}, [{"foo": 1}], ["x"], [{"time": "garbage", "open": 1, "high": 1, "low": 1, "close": 1}]])
def test_normalize_candles_refuses_unknown(bad):
    with pytest.raises(PriceDataError):
        normalize_candles(bad)


# ---------------- broker with a fake pya3-shaped client ----------------
Ins = namedtuple("Ins", "exchange token symbol name expiry lot_size")


class FakeClient:
    def __init__(self, user_id, api_key, session=None):
        self.session = session or {"stat": "Ok", "sessionID": "x"}

    def get_session_id(self):
        return self.session

    def get_contract_master(self, exch):
        return None

    def search_instruments(self, exch, text):
        return [Ins("MCX", 1, "SILVERMIC DEC26", "SILVERMIC", "2026-12-04", 1),
                Ins("MCX", 2, "SILVERMIC FEB27", "SILVERMIC", "2027-02-04", 1),
                Ins("MCX", 3, "SILVERMIC OCT26", "SILVERMIC", "2026-10-05", 1),          # expired
                Ins("MCX", 4, "SILVERMIC DEC26 CE", "SILVERMIC", "2026-12-04", 1),       # option
                Ins("MCX", 5, "SILVER DEC26", "SILVER", "2026-12-04", 30)]               # different product

    def get_historical(self, ins, start, end, interval, indices):
        assert interval == "D" and indices is False
        n = 30
        return [{"time": (start + timedelta(days=i)).strftime("%Y-%m-%d 00:00:00"), "open": 100 + i, "high": 101 + i,
                 "low": 99 + i, "close": 100 + i, "volume": 5} for i in range(n)]

    def get_balance(self):
        return {"cash": 1}


def test_pick_contracts_filters_expired_options_and_other_products():
    picked = pick_contracts(FakeClient(1, 2).search_instruments("MCX", "SILVERMIC"), "SILVERMIC", date(2026, 11, 7))
    assert [p[0] for p in picked] == ["SILVERMIC DEC26", "SILVERMIC FEB27"]


def test_broker_flow_and_store(tmp_path):
    b = AliceBroker("u", "k", client_factory=FakeClient)
    b.connect()
    store = PriceStore(tmp_path)
    info = refresh_store(b, store, "silver_micro", "MCX", "SILVERMIC", date(2026, 11, 7))
    assert [c[0] for c in info["contracts"]] == ["SILVERMIC DEC26", "SILVERMIC FEB27"]
    assert info["lot_sizes"]["SILVERMIC DEC26"] == 1.0
    assert len(store.load_contracts("silver_micro")) == 2


def test_broker_login_failure_is_explained_without_secrets():
    b = AliceBroker("user-1", "secret-key-123", client_factory=lambda u, k: FakeClient(u, k, {"stat": "Not_ok", "emsg": "Session expired"}))
    with pytest.raises(BrokerError) as e:
        b.connect()
    assert "Session expired" in str(e.value) and "secret-key-123" not in str(e.value)


def test_broker_not_connected_and_missing_contract():
    b = AliceBroker("u", "k", client_factory=FakeClient)
    with pytest.raises(BrokerError):
        b.contracts("MCX", "SILVERMIC", date(2026, 11, 7))
    b.connect()
    with pytest.raises(BrokerError):
        b.contracts("MCX", "NOSUCH", date(2026, 11, 7))
