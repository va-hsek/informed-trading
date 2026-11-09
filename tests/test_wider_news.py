import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

from collectors.http import FetchError
from news import calendar_refresh as cr
from news.articles import RobotsCache, read_article
from news.feeds import Item
from news.run_news import pick_for_reading
from test_news import ARTICLE_HTML, ARTICLE_TEXT, item

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 11, 9, 9, 0, tzinfo=IST)
PAYWALL = b"<html><body><article><p>Subscribe to read the full story of this very long headline text here.</p></article></body></html>"


def getter(archive_reply):
    def g(url):
        if url.endswith("robots.txt"):
            return b"User-agent: *\nAllow: /"
        if "archive.ph" in url:
            if isinstance(archive_reply, Exception):
                raise archive_reply
            return archive_reply
        return PAYWALL
    return g


def test_archive_today_used_for_paywalled_when_wayback_has_nothing():
    g = getter(ARTICLE_HTML.encode())
    r = read_article(item(paywalled=True), RobotsCache(g), g, lambda *a, **k: {})
    assert r["via"] == "archive.today" and r["text"] == ARTICLE_TEXT


def test_archive_today_captcha_or_block_fails_closed_and_trips_breaker():
    for reply in (b"<html><body><p>Please verify you are human to continue. " + b"x " * 400 + b"</p></body></html>", FetchError("HTTP 403")):
        g = getter(reply)
        rb = RobotsCache(g)
        r = read_article(item(paywalled=True), rb, g, lambda *a, **k: {})
        assert r["via"] is None and "archive.today unusable" in r["reason"]
    g = getter(FetchError("HTTP 403"))
    rb = RobotsCache(g)
    for _ in range(5):
        read_article(item(paywalled=True), rb, g, lambda *a, **k: {})
    assert rb.archive_fails == 3                         # stopped asking after 3 failures


def mk_item(i, region, score_topic="india"):
    it = Item(f"S{i}", "world", f"t{i}", f"https://x/{i}", NOW, "", False)
    it.topics, it.regions, it.feed_region = [score_topic], [], region
    return it


def test_read_quota_favours_india_usa_europe():
    items = [mk_item(i, "apac") for i in range(50)] + [mk_item(100 + i, "india") for i in range(50)]
    lim = {"max_articles_read": 200, "read_caps": {"india": 30, "apac": 5, "other": 10}}
    picked = pick_for_reading(items, lim)
    assert sum(1 for p in picked if p.feed_region == "india") == 30 and sum(1 for p in picked if p.feed_region == "apac") == 5


def test_region_feed_items_without_keywords_are_still_considered():
    it = Item("S", "world", "Quiet headline", "https://x/1", NOW, "", False)
    it.feed_region = "europe"
    assert pick_for_reading([it], {"max_articles_read": 10, "read_caps": {"europe": 5}}) == [it]


# ---------------- calendar refresh ----------------
PAGE = ("<html><body><article><p>The US consumer price index for October will be released on 10 November. Economists expect headline "
        "inflation of 0.3% for the month, the Bureau said in its schedule published this week.</p></article></body></html>").encode()
EV = {"date": "2026-11-10", "time_ist": "7:00 pm", "title": "US October CPI", "expectation": "Headline +0.3% m/m", "affects": ["gold"],
      "affects_assets": ["gold"], "at": "2026-11-10T19:00:00+05:30", "source_name": "BLS", "source_url": "https://bls.example/cpi",
      "evidence": "Economists expect headline inflation of 0.3% for the month, the Bureau said in its schedule"}


def test_verify_event_accepts_only_what_the_page_supports():
    ok, why = cr.verify_event(dict(EV), date(2026, 11, 9), lambda u: PAGE, {})
    assert why == "verified" and ok["verified"] and ok["at"] == "2026-11-10T19:00:00+05:30" and ok["affects_assets"] == ["gold"]
    bad_ev = dict(EV, evidence="Economists expect headline inflation of 0.9% for the month, the Bureau said")
    ok2, why2 = cr.verify_event(bad_ev, date(2026, 11, 9), lambda u: PAGE, {})
    assert not ok2["verified"] and ok2["expectation"] == "n/a" and "at" not in ok2
    ok3, _ = cr.verify_event(dict(EV, expectation="Headline +0.7% m/m"), date(2026, 11, 9), lambda u: PAGE, {})
    assert ok3["expectation"] == "n/a" and not ok3["verified"]
    gone, why4 = cr.verify_event(dict(EV, date="2026-11-08"), date(2026, 11, 9), lambda u: PAGE, {})
    assert gone is None
    far, _ = cr.verify_event(dict(EV, date="2027-01-30"), date(2026, 11, 9), lambda u: PAGE, {})
    assert far is None


def test_verify_event_unreadable_source_is_unverified():
    def boom(u):
        raise FetchError("HTTP 404")
    ev, why = cr.verify_event(dict(EV), date(2026, 11, 9), boom, {})
    assert not ev["verified"] and "unreadable" in why and ev["expectation"] == "n/a"


class FakeSearchLLM:
    tokens_in = tokens_out = 0

    def __init__(self, events):
        self.events = events

    def complete_with_search(self, *a, **k):
        return "Here you go: " + json.dumps({"events": self.events})


def test_refresh_merges_drops_past_and_sets_review_date(tmp_path):
    (tmp_path / "config").mkdir()
    old = [{"date": "2026-11-05", "title": "Old past event", "source_name": "x", "source_url": "https://x"},
           {"date": "2026-11-20", "title": "Hand added meeting", "manual": True, "source_name": "me", "source_url": "https://m"},
           {"date": "2026-11-10", "title": "US October CPI", "source_name": "old", "source_url": "https://o"}]
    (tmp_path / "config" / "calendar.yaml").write_text(yaml.safe_dump({"last_reviewed": "2026-10-01", "events": old}))
    r = cr.refresh_calendar(tmp_path, NOW, FakeSearchLLM([EV]), "web_search_20250305", lambda u: PAGE)
    out = yaml.safe_load((tmp_path / "config" / "calendar.yaml").read_text())
    titles = [e["title"] for e in out["events"]]
    assert titles == ["US October CPI", "Hand added meeting"] and out["last_reviewed"] == "2026-11-09"
    assert out["events"][0]["verified"] is True and r["verified"] == 1


def test_refresh_with_nothing_verified_does_not_advance_review_date(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "calendar.yaml").write_text(yaml.safe_dump({"last_reviewed": "2026-10-01", "events": []}))
    def boom(u):
        raise FetchError("down")
    cr.refresh_calendar(tmp_path, NOW, FakeSearchLLM([EV]), "t", boom)
    assert yaml.safe_load((tmp_path / "config" / "calendar.yaml").read_text())["last_reviewed"] == "2026-10-01"


def test_ensure_fresh_skips_when_recent_and_never_raises(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "calendar.yaml").write_text(yaml.safe_dump({"last_reviewed": "2026-11-08", "events": []}))
    cfg = {"limits": {"calendar_review_max_age_days": 7}}
    assert cr.ensure_fresh(tmp_path, NOW, {}, cfg) == ""
    (tmp_path / "config" / "calendar.yaml").write_text(yaml.safe_dump({"last_reviewed": "2026-10-01", "events": []}))
    assert "not set" in cr.ensure_fresh(tmp_path, NOW, {}, cfg)
    class Bad:
        def complete_with_search(self, *a, **k):
            raise RuntimeError("x")
    assert "failed" in cr.ensure_fresh(tmp_path, NOW, {}, cfg, llm=Bad())
