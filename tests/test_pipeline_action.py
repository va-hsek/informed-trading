import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from collectors.http import FetchError
from core.prices import PriceStore
from core.render_html import md_to_html
from pipeline.run_action import IST, build_action_report, margin_for, pick_stop, spec_problem, upcoming_events

NOW = datetime(2026, 11, 7, 8, 0, tzinfo=IST)


class Net:
    """Fake network. FRED series trend so that gold/silver/crude drivers fire; EIA data flat."""
    SLOPE = {"DTWEXBGS": (120.0, -0.2), "DEXINUS": (90.0, 0.2), "DFII10": (1.9, -0.05), "DGS2": (4.5, -0.05), "VIXCLS": (28.18, 0.0)}

    def __init__(self, crude_fails=False):
        self.crude_fails, self.calls = crude_fails, []

    def raw(self, url, params=None, headers=None, **kw):
        sid = dict(params)["id"]
        base, step = self.SLOPE[sid]
        rows = ["observation_date," + sid]
        for i in range(12):
            d = date(2026, 10, 27) + timedelta(days=i)
            rows.append(f"{d.isoformat()},{base + step * i:.4f}")
        return "\n".join(rows).encode()

    def json(self, url, params=None, headers=None, **kw):
        self.calls.append(url)
        if "api.eia.gov" in url:
            if self.crude_fails:
                raise FetchError("HTTP 500")
            rows, d = [], date(2026, 10, 30)
            for _ in range(340):
                rows.append({"period": d.isoformat(), "series": "WCESTUS1", "value": "420000", "units": "MBBL"})
                d -= timedelta(days=7)
            return {"response": {"data": rows}}
        if "ir.eia.gov" in url:
            return {"release_date": "2026-Nov-05 00:00:00", "current_week": "2026-10-30",
                    "series": [{"name": "Total Lower 48", "units": "Bcf", "unitsshort": "Bcf",
                                "calculated": {"5yr-avg": "3,618", "net_change": "80", "pct-chg_5yr-avg": "4.6"},
                                "data": [["2026-10-30", "3,785"]]}]}
        if "cftc" in url:
            code = dict(params)["cftc_contract_market_code"]
            name = {"088691": "GOLD - COMEX", "084691": "SILVER - COMEX", "067651": "WTI-PHYSICAL - NYMEX", "023651": "NAT GAS NYME - NYMEX"}[code]
            rows, d = [], date(2026, 11, 3)
            for i in range(160):
                rows.append({"report_date_as_yyyy_mm_dd": d.isoformat() + "T00:00:00.000", "market_and_exchange_names": name,
                             "m_money_positions_long_all": str(1000 + (160 - i)), "m_money_positions_short_all": "500", "open_interest_all": "5000"})
                d -= timedelta(days=7)
            return rows
        raise AssertionError(url)


def make_root(tmp_path, margins=None, silver_margin=60000):
    root = tmp_path / "root"
    shutil.copytree("config", root / "config")
    (root / "config" / "secrets.env").unlink(missing_ok=True)
    m = {"as_of": NOW.date().isoformat(),
         "margins_per_lot_inr": {"silver_micro": silver_margin, "gold_ten": 8000, "crude_mini": 6000, "natgas_mini": 5000}}
    (root / "config" / "margins.yaml").write_text(yaml.safe_dump(margins or m))
    store = PriceStore(root / "data" / "prices")
    rng = np.random.default_rng(3)
    for key, base in [("silver_micro", 150000.0), ("gold_ten", 120000.0), ("crude_mini", 6000.0), ("natgas_mini", 300.0)]:
        n = 300
        t = np.arange(n)
        c = base * (1 + 0.04 * np.sin(2 * np.pi * t / 50) + 0.0004 * t) + rng.normal(0, base * 0.002, n)
        idx = pd.date_range(end=pd.Timestamp(NOW.date()) - pd.Timedelta(days=1), periods=n, freq="D")
        df = pd.DataFrame({"open": c, "high": c * 1.004, "low": c * 0.996, "close": c, "volume": rng.integers(800, 1500, n)}, index=idx)
        store.save_contract(key, key.upper() + "_LIVE", date(2027, 1, 29), df)
    return root


def run(root, net=None):
    net = net or Net()
    return build_action_report(root, NOW, {"EIA_API_KEY": "k"}, net.json, net.raw)


def test_report_has_requested_trace_structure_and_correct_numbers(tmp_path):
    md, meta = run(make_root(tmp_path))
    gold = md.split("## GOLD")[1].split("## SILVER")[0]
    for token in ["R01 · Broad US dollar index", "**Data point:**", "**Rule:**", "**Today:**", "**Rationale:**",
                  "Broad US dollar index 117.8 on 7 Nov 2026, versus 118.8 on 2 Nov 2026 (5 observations earlier): change -0.84%",
                  "= -0.84% -> TRIGGERED (contribution +1.00)",
                  "US 10-year real yield 1.35% on 7 Nov 2026, versus 1.60% on 2 Nov 2026 (5 observations earlier): change -0.25 pp"]:
        assert token in gold, token
    # rule line wording and the trigger marker
    assert "IF 5-observation % change in the broad US dollar index < -0.5% THEN gold bullish (x1)  <- today" in gold
    assert "USDINR" in gold and "weaker rupee" in gold.lower() or "MCX prices are in rupees" in gold
    # independent recomputation of the dollar change
    last, ref = 120 - 0.2 * 11, 120 - 0.2 * 6
    assert f"{(last / ref - 1) * 100:+.2f}%" in gold
    assert meta["assets"]["gold"]["state"] == "UP"


def test_gating_messages_and_need_additional(tmp_path):
    md, meta = run(make_root(tmp_path))
    crude = md.split("## CRUDE")[1].split("## NATGAS")[0]
    assert "Sizing withheld: Contract spec for crude_mini is 'single_source'" in crude
    nat = md.split("## NATGAS")[1].split("## GOLD")[0]
    assert "No directional view" in nat and "Only 1 driver(s) firing" in nat
    assert "Need Rs" in md and "additional" in md          # silver margin 60,000 > free cash 50,000
    assert "At a glance" in md and "Rs 50,000" in md
    assert "R07" in md and "source not verified" in md       # unverified drivers are listed, not used


def test_missing_and_stale_margin_withholds_sizing(tmp_path):
    root = make_root(tmp_path, margins={"as_of": None, "margins_per_lot_inr": {k: None for k in ("silver_micro", "gold_ten", "crude_mini", "natgas_mini")}})
    md, _ = run(root)
    assert "Margin per lot for gold_ten is not entered" in md
    old = (NOW.date() - timedelta(days=10)).isoformat()
    root2 = make_root(tmp_path / "b", margins={"as_of": old, "margins_per_lot_inr": {"silver_micro": 1, "gold_ten": 1, "crude_mini": 1, "natgas_mini": 1}})
    md2, _ = run(root2)
    assert "is 10 days old" in md2


def test_failed_source_is_reported_not_guessed(tmp_path):
    md, _ = run(make_root(tmp_path), Net(crude_fails=True))
    crude = md.split("## CRUDE")[1].split("## NATGAS")[0]
    assert "R05" in md and "fetch failed: HTTP 500" in md
    assert "R05 · " not in crude          # no data point is shown for a driver that failed


def test_stale_prices_withhold_technicals(tmp_path):
    root = make_root(tmp_path)
    for p in (root / "data" / "prices" / "raw" / "gold_ten").glob("*.csv"):
        df = pd.read_csv(p)
        df["date"] = pd.to_datetime(df["date"]) - pd.Timedelta(days=30)
        df.to_csv(p, index=False)
    md, _ = run(root)
    assert "gold: technicals withheld: newest price bar is" in md
    assert "TECH_UNAVAILABLE" in md.split("## GOLD")[1].split("## SILVER")[0]


def test_no_recommendation_wording_and_disclaimer(tmp_path):
    md, _ = run(make_root(tmp_path))
    low = md.lower()
    for banned in ["you should buy", "you should sell", "we recommend", "i recommend", "strong buy", "must buy"]:
        assert banned not in low
    assert "decision support, not a recommendation" in md


def test_html_is_mobile_friendly(tmp_path):
    md, _ = run(make_root(tmp_path))
    html = md_to_html(md, "t")
    assert "viewport" in html and "<table" in html and "overflow-x:auto" in html


# ---------------- helpers ----------------
def test_margin_and_spec_helpers():
    assert margin_for("a", {"as_of": "2026-11-06", "margins_per_lot_inr": {"a": 100}}, date(2026, 11, 7), 3) == (100.0, None)
    v, why = margin_for("a", {"as_of": date(2026, 11, 1), "margins_per_lot_inr": {"a": 100}}, date(2026, 11, 7), 3)
    assert v is None and "6 days old" in why
    assert spec_problem("x", {"spec_status": "conflicting", "multiplier": None, "lot_text": "?"})
    assert spec_problem("x", {"spec_status": "cross_checked", "multiplier": None})
    assert spec_problem("x", {"spec_status": "cross_checked", "multiplier": 1}) is None
    assert spec_problem("x", {"spec_status": "confirmed", "multiplier": 10}) is None


def test_pick_stop_uses_farther_stop():
    class T:  # minimal TechView stand-in
        stop_zone_long, stop_structure_long = 95.0, 98.0
        stop_zone_short, stop_structure_short = 105.0, 102.0
    assert pick_stop("long", T, 1.5)[0] == 95.0
    assert pick_stop("short", T, 1.5)[0] == 105.0
    T.stop_zone_long, T.stop_structure_long = 99.0, 96.0
    p, how = pick_stop("long", T, 1.5)
    assert p == 96.0 and "support" in how


def test_upcoming_events_window():
    ev = {"events": [{"name": "A", "at": "2026-11-07T12:00:00+05:30", "affects": ["gold"]},
                     {"name": "B", "at": "2026-12-25T12:00:00+05:30"}, {"name": "C", "at": "bad"}, {"name": "D", "at": "2026-11-07T12:00:00"}]}
    out = upcoming_events(ev, NOW)
    assert [e["name"] for e in out] == ["A"] and abs(out[0]["hours"] - 4.0) < 1e-9
