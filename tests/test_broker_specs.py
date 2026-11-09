from collections import namedtuple
from datetime import date, datetime

import pandas as pd
import pytest

from collectors import india_yield
from collectors.broker_alice import AliceBroker, BrokerError, derive_spec, parse_margin, plausible_margin
from collectors.http import FetchError
from core import newslink
from pipeline.run_action import broker_margin

Ins = namedtuple("Ins", "exchange token symbol name expiry lot_size")


def test_parse_margin_documented_shape_and_refusals():
    ok = {"status": "Ok", "result": [{"preOrderMargin": "1000", "postOrderMargin": "13500.5", "currentOrderMargin": "12500.5", "fundShort": "0"}]}
    assert parse_margin(ok) == 12500.5
    assert parse_margin({"currentOrderMargin": 5000}) == 5000                      # no pre/post to cross-check: accepted on current alone
    with pytest.raises(BrokerError, match="inconsistent"):
        parse_margin({"currentOrderMargin": 12500, "preOrderMargin": 1000, "postOrderMargin": 20000})
    with pytest.raises(BrokerError, match="fields seen"):
        parse_margin({"somethingElse": 1})
    with pytest.raises(BrokerError):
        parse_margin({"currentOrderMargin": "0"})
    with pytest.raises(BrokerError):
        parse_margin("garbage")


def test_plausible_margin_bounds():
    assert plausible_margin(60000, 700000, 1)           # 8.6% of contract value
    assert not plausible_margin(100, 700000, 1)         # looks like the wrong quantity unit
    assert not plausible_margin(700000, 700000, 1)


def test_derive_spec_accepts_only_published_lot_sizes():
    gold = {"lot_size_candidates": [10], "units_per_quote": 10}
    assert derive_spec(gold, 10)[0] == 1.0
    assert derive_spec({"lot_size_candidates": [250, 2500], "units_per_quote": 1}, 2500)[0] == 2500
    assert derive_spec({"lot_size_candidates": [250, 2500], "units_per_quote": 1}, 100)[0] is None
    assert derive_spec(gold, None)[0] is None and derive_spec({}, 10)[0] is None


class FakeClient:
    def __init__(self, margin_by_qty, indices=(), equities=()):
        self.margin_by_qty, self.indices, self.equities, self.calls = margin_by_qty, indices, equities, []

    def get_session_id(self): return {"stat": "Ok"}
    def get_contract_master(self, ex): self.calls.append(("master", ex))

    def search_instruments(self, ex, text):
        pool = self.indices if ex == "INDICES" else self.equities
        return [i for i in pool if text.upper() in i.symbol.upper()]

    def basket_margin(self, orders):
        self.calls.append(("margin", orders[0]["qty"], orders[0]["transType"]))
        v = self.margin_by_qty.get(int(orders[0]["qty"]))
        if v is None:
            raise RuntimeError("bad")
        return {"result": [{"currentOrderMargin": v}]}

    def get_historical(self, ins, a, b, interval, indices):
        return [{"time": f"2026-11-0{d} 00:00:00", "open": 100, "high": 102, "low": 99, "close": 100 + d, "volume": 0} for d in range(2, 7)]


def mk_broker(client):
    b = AliceBroker("u", "k", lambda u, k: client)
    b.connect()
    return b


def test_margin_tries_lot_quantity_then_one_and_accepts_only_plausible():
    ins = Ins("MCX", 1, "CRUDEOILM", "CRUDEOILM", None, 10)
    # quantity 10 returns a tiny figure (wrong unit), quantity 1 returns a plausible one
    c = FakeClient({10: 150.0, 1: 5000.0})
    st = {"broker": mk_broker(c), "nearest": {"crude_mini": {"instrument": ins, "lot_size": 10.0, "symbol": "CRUDEOILM", "last_close": 6000}}, "cache": {}}
    m, prob, note = broker_margin("crude_mini", {"exchange": "MCX", "multiplier": 10}, "long", st, 6000.0)
    assert m == 5000.0 and prob is None and "Alice Blue" in note and "quantity 1" in note
    assert [x for x in c.calls if x[0] == "margin"] == [("margin", "10", "B"), ("margin", "1", "B")]


def test_margin_refused_when_nothing_plausible_or_not_connected():
    ins = Ins("MCX", 1, "CRUDEOILM", "CRUDEOILM", None, 10)
    st = {"broker": mk_broker(FakeClient({10: 3.0, 1: 2.0})), "nearest": {"k": {"instrument": ins, "lot_size": 10.0, "symbol": "S"}}, "cache": {}}
    m, prob, _ = broker_margin("k", {"exchange": "MCX", "multiplier": 10}, "short", st, 6000.0)
    assert m is None and "could not be used" in prob
    m2, prob2, _ = broker_margin("k", {"exchange": "MCX", "multiplier": 10}, "long", {"broker": None, "nearest": {}, "cache": {}}, 6000.0)
    assert m2 is None and "not connected" in prob2


def test_index_history_exact_match_only():
    idx = [Ins("INDICES", 1, "NIFTY 50", "NIFTY 50", None, 1), Ins("INDICES", 2, "NIFTY 500", "NIFTY 500", None, 1)]
    b = mk_broker(FakeClient({}, indices=idx))
    df = b.index_history("nifty 50", datetime(2026, 1, 1), datetime(2026, 11, 8))
    assert len(df) == 5 and df["close"].iloc[-1] == 106
    with pytest.raises(BrokerError, match="similar names"):
        b.index_history("NIFTY", datetime(2026, 1, 1), datetime(2026, 11, 8))


def test_symbol_resolution_and_news_link_status():
    eq = [Ins("NSE", 1, "ONGC-EQ", "ONGC", None, 1), Ins("NSE", 2, "INFY-EQ", "INFY", None, 1)]
    b = mk_broker(FakeClient({}, equities=eq))
    assert b.resolve_symbol("NSE", "ONGC")["symbol"] == "ONGC-EQ"
    assert b.resolve_symbol("NSE", "NOSUCH") is None
    txt = newslink.symbol_status(["ONGC", "NOSUCH"], "NSE", b.resolve_symbol)
    assert "confirmed in Alice Blue: ONGC-EQ" in txt and "NOT confirmed: NOSUCH (not found in Alice Blue)" in txt
    assert "not yet checked" in newslink.symbol_status(["ONGC"], "NSE", None)


def test_sector_story_links_to_sector_instruments():
    latest = {"generated": "2026-11-09T09:00:00+05:30", "headlines": [], "stories": [
        {"headline": "Lenders report strong loan growth", "topics": [], "sectors": ["Banks and finance"], "regions": ["india"],
         "sources": [{"name": "Mint", "url": "https://m/1"}], "text": "HDFC Bank said loans grew."}]}
    cfg = {"topics": {}, "names": [], "sectors": {"Banks and finance": [{"name": "Bank stocks", "symbols": ["SBIN"], "exchange": "NSE", "relation": "x"}]}}
    links = newslink.build_links(latest, cfg, [], lambda ex, s: {"symbol": s + "-EQ", "lot_size": 1})
    assert links[0]["instrument"] == "Bank stocks" and "SBIN-EQ" in links[0]["symbol_text"] and links[0]["items"][0]["source"] == "Mint"


# ---------------- India 10-year ----------------
TRACKER = ("Yield on the 10-year benchmark paper (6.94% GS 2036) fell by 2 bps to close at 7.17% \n"
           "Corporate Bonds/G-Sec 05-Nov-26 Previous close Week Ago Month Ago\n10 Year G-Sec 7.30 7.32 7.53 7.34\n")


def test_parse_tracker_uses_benchmark_close_and_reports_generic_separately():
    r = india_yield.parse_tracker(TRACKER)
    assert r["benchmark_close"] == 7.17 and r["generic_10y"] == 7.30 and r["as_of"] == date(2026, 11, 5) and r["benchmark_label"] == "6.94% GS 2036"


def test_parse_tracker_fails_closed():
    with pytest.raises(FetchError):
        india_yield.parse_tracker("nothing useful")
    with pytest.raises(FetchError, match="differ by more than"):
        india_yield.parse_tracker(TRACKER.replace("7.30 7.32", "9.30 9.32"))
    with pytest.raises(FetchError):
        india_yield.parse_tracker(TRACKER.replace("05-Nov-26", "xx"))


def test_fetch_latest_walks_back_over_weekend_and_stores_history(tmp_path, monkeypatch):
    monkeypatch.setattr(india_yield, "pdf_text", lambda raw: raw.decode())
    asked = []

    def g(url):
        asked.append(url)
        if "06-nov-2026" in url:
            return TRACKER.encode()
        raise FetchError("HTTP 404")
    rec = india_yield.fetch_latest(date(2026, 11, 8), g)          # Sunday: skips Sat/Sun, tries Fri 6 Nov first
    assert "06-nov-2026" in asked[0] and rec["benchmark_close"] == 7.17
    p = tmp_path / "h.csv"
    india_yield.append_history(p, rec)
    india_yield.append_history(p, rec)                             # same day twice: one row
    assert india_yield.read_history(p) == [(date(2026, 11, 5), 7.17)]
    with pytest.raises(FetchError):
        india_yield.fetch_latest(date(2026, 11, 8), lambda u: (_ for _ in ()).throw(FetchError("HTTP 404")), lookback_days=3)
