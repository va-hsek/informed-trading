import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest
import yaml

from core import dashboard as dash
from core import newslink
from core.prices import PriceStore, write_csv
from pipeline.run_action import calendar_events, upcoming_events

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 11, 9, 9, 0, tzinfo=IST)


def fred_csv(sid, last, n=30, start=100.0, step=1.0):
    rows = ["observation_date," + sid]
    for i in range(n):
        d = last - timedelta(days=n - 1 - i)
        rows.append(f"{d},{start + i * step}")
    return "\n".join(rows)


def mk_root(tmp_path, groups):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "dashboard.yaml").write_text(yaml.safe_dump({"groups": groups}))
    (tmp_path / "config" / "user.yaml").write_text("data_dir: data\n")
    (tmp_path / "config" / "manual_inputs.yaml").write_text("dashboard_manual:\n  india_10y: []\n")
    return tmp_path


def http_raw_for(last):
    def raw(url, params=None, **kw):
        sid = dict(params)["id"]
        return fred_csv(sid, last).encode()
    return raw


def test_fred_row_values_and_changes(tmp_path):
    root = mk_root(tmp_path, {"G": [{"label": "X", "source": "fred:DGS10", "kind": "yield", "max_age_days": 6}]})
    rows = dash.build_rows(root, NOW, {}, http_json=lambda *a, **k: None, http_raw=http_raw_for(date(2026, 11, 6)))
    r = rows[0]
    assert r["problem"] is None and r["value"] == 129.0 and r["as_of"] == "2026-11-06"
    assert r["chg"]["1d"] == pytest.approx(1.0) and r["chg"]["5d"] == pytest.approx(5.0)
    assert "pp" in r["chg_text"]["5d"]


def test_stale_row_is_not_shown_as_current(tmp_path):
    root = mk_root(tmp_path, {"G": [{"label": "X", "source": "fred:DGS10", "kind": "price", "max_age_days": 3}]})
    rows = dash.build_rows(root, NOW, {}, http_json=lambda *a, **k: None, http_raw=http_raw_for(date(2026, 10, 30)))
    assert rows[0]["value"] is None and "days old" in rows[0]["value_text"]
    out = "\n".join(dash.render_dashboard(rows))
    assert "n/a" in out and "129" not in out


def test_missing_sources_fail_closed(tmp_path):
    root = mk_root(tmp_path, {"G": [{"label": "M", "source": "manual:india_10y", "kind": "yield"},
                                    {"label": "C", "source": "csv:nifty50", "kind": "price"},
                                    {"label": "P", "source": "price:crude_mini", "kind": "price"}]})
    rows = dash.build_rows(root, NOW, {}, http_json=lambda *a, **k: None, http_raw=http_raw_for(date(2026, 11, 6)))
    assert all(r["problem"] and r["value"] is None for r in rows)


def test_csv_and_price_sources(tmp_path):
    root = mk_root(tmp_path, {"G": [{"label": "N", "source": "csv:nifty50", "kind": "price", "max_age_days": 5}]})
    idx = pd.date_range("2026-10-01", "2026-11-06", freq="B")
    df = pd.DataFrame({"open": 1.0, "high": 2.0, "low": 0.5, "close": [100 + i for i in range(len(idx))], "volume": 0.0}, index=idx)
    write_csv(df, root / "data" / "prices" / "index" / "nifty50.csv")
    rows = dash.build_rows(root, NOW, {}, http_json=lambda *a, **k: None, http_raw=http_raw_for(date(2026, 11, 6)))
    assert rows[0]["problem"] is None and rows[0]["as_of"] == "2026-11-06"


def test_manual_series(tmp_path):
    root = mk_root(tmp_path, {"G": [{"label": "IN10", "source": "manual:india_10y", "kind": "yield", "max_age_days": 7}]})
    (root / "config" / "manual_inputs.yaml").write_text(
        "dashboard_manual:\n  india_10y: [{date: '2026-11-05', value: 6.50}, {date: '2026-11-06', value: 6.55}]\n")
    r = dash.build_rows(root, NOW, {}, None, None)[0]
    assert r["value"] == 6.55 and r["chg"]["1d"] == pytest.approx(0.05)


def test_write_latest_roundtrip_feeds_news_glance(tmp_path):
    from news.paper import render_glance
    root = mk_root(tmp_path, {"G": [{"label": "X", "source": "fred:DGS10", "kind": "price", "max_age_days": 6}]})
    rows = dash.build_rows(root, NOW, {}, http_json=lambda *a, **k: None, http_raw=http_raw_for(date(2026, 11, 6)))
    p = tmp_path / "data" / "dashboard_latest.json"
    dash.write_latest(rows, NOW, p)
    out = "\n".join(render_glance(p, NOW))
    assert "| X |" in out and "2026-11-06" in out


LATEST = {"generated": NOW.isoformat(), "stories": [
    {"headline": "Brent falls as Iran talks advance", "topics": ["crude", "geopolitics"], "regions": [],
     "sources": [{"name": "Reuters", "url": "https://r.example/1"}], "text": "Brent fell. Reliance said it would cut imports."}],
    "headlines": [{"headline": "Gold steadies before CPI", "topics": ["gold"], "regions": [], "source": "CNBC", "url": "https://c.example/2"}]}
CFG = {"max_headlines_per_instrument": 2, "topics": {
    "crude": [{"name": "MCX Crude Oil Mini", "venue": "MCX", "row": "R", "relation": "tracks crude"}],
    "gold": [{"name": "Gold ETFs", "venue": "NSE", "relation": "tracks gold"}]},
    "names": [{"keywords": ["reliance"], "name": "Reliance Industries", "venue": "NSE"}]}
ROWS = [{"label": "R", "value_text": "7,000", "as_of": "2026-11-06", "chg_text": {"5d": "+1.00%"}, "problem": None}]


def test_links_match_topics_names_and_prices():
    links = newslink.build_links(LATEST, CFG, ROWS)
    names = {l["instrument"]: l for l in links}
    assert set(names) == {"MCX Crude Oil Mini", "Gold ETFs", "Reliance Industries"}
    assert "7,000" in names["MCX Crude Oil Mini"]["price_text"]
    assert names["Gold ETFs"]["price_text"] == "price not connected"
    md = "\n".join(newslink.render_links(links, LATEST["generated"], NOW))
    assert "[Reuters](https://r.example/1)" in md and "[CNBC](https://c.example/2)" in md
    assert "buy" not in md.lower() and "sell" not in md.lower()


def test_links_word_boundary_and_stale_news():
    latest = {"generated": NOW.isoformat(), "stories": [{"headline": "Prereliance mania", "topics": [], "sources": [], "text": ""}], "headlines": []}
    assert newslink.build_links(latest, CFG, ROWS) == []
    old = (NOW - timedelta(hours=50)).isoformat()
    assert "Not shown" in "\n".join(newslink.render_links([], old, NOW))


def test_calendar_events_feed_action_warnings():
    cal = yaml.safe_load(Path("config/calendar.yaml").read_text())
    ev = calendar_events(cal)
    assert {e["name"] for e in ev} >= {"US October CPI", "US PPI (October)"}
    up = upcoming_events({"events": ev}, NOW, horizon_hours=72)
    assert [e["name"] for e in up] == ["US October CPI"] and "gold" in up[0]["affects"]


def test_load_and_render_without_news_file(tmp_path):
    assert "Not available" in newslink.load_and_render(tmp_path, NOW, [])[0]
