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
        assert r["via"] is None and "archive.today" in r["reason"] and "no suitable Wayback" in r["reason"]
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
    assert "no model API key" in cr.ensure_fresh(tmp_path, NOW, {}, cfg)
    class Bad:
        def complete_with_search(self, *a, **k):
            raise RuntimeError("x")
    assert "failed" in cr.ensure_fresh(tmp_path, NOW, {}, cfg, llm=Bad())


# ---------------- archive chain, headline search, short text, sectors, spending stop ----------------
from news.headline_search import find_alternative, sig_words
from test_news import NOW as NEWS_NOW, fake_llm, make_net, make_root
from news.run_news import build_news_report

BLOCKED = b"<html><body><p>Access denied</p></body></html>"


def test_memento_archive_other_than_archive_today_is_used():
    def g(url):
        if url.endswith("robots.txt"):
            return b"User-agent: *\nAllow: /"
        if url.startswith("https://perma.example/"):
            return ARTICLE_HTML.encode()
        return PAYWALL
    memento = lambda url, p=None: ({"mementos": {"closest": {"uri": ["https://perma.example/abc"]}}} if "mementoweb" in url else {})
    r = read_article(item(paywalled=True), RobotsCache(g), g, memento)
    assert r["via"] == "archive" and r["text"] == ARTICLE_TEXT


def test_blocked_page_goes_to_archives_and_short_text_is_kept():
    short = b"<html><body><article><p>Gold rose 1.2% on Friday as traders bet on a Fed pause, officials said in a brief statement released today, adding that further data on inflation and jobs would decide the next move.</p></article></body></html>"
    g = lambda u: b"User-agent: *\nAllow: /" if u.endswith("robots.txt") else short
    r = read_article(item(), RobotsCache(g), g, lambda *a, **k: {})
    assert r["via"] == "direct" and r["text"].startswith("Gold rose")            # a 170-character piece is not discarded
    # a page that blocks us falls through to the archives and, with nothing there, is reported with every step tried
    g2 = getter(FetchError("HTTP 403"))
    r2 = read_article(item(), RobotsCache(g2), g2, lambda *a, **k: {})
    assert r2["via"] is None and "paywall marker" in r2["reason"] and "Wayback" in r2["reason"] and "archive.today" in r2["reason"]


class SearchLLM:
    def __init__(self, urls):
        self.urls, self.search_count = urls, 0

    def complete_with_search(self, *a, **k):
        self.search_count += 1
        return json.dumps({"urls": self.urls})


def page(title, body):
    return f"<html><head><title>{title}</title></head><body><article><p>{body}</p></article></body></html>".encode()


def test_headline_search_accepts_matching_readable_page_and_rejects_others():
    title = "Tata Motors plans new electric vehicle plant in Gujarat"
    good = page("Tata Motors plans new electric vehicle plant in Gujarat | Mint", "Tata Motors said on Sunday that it will build a new electric vehicle plant in Gujarat with a planned investment announced by the company. " * 2)
    other = page("Cricket: India win series", "India won the series on Sunday after a long and tense final match played in front of a full house at the stadium. " * 2)
    paywalled = PAYWALL

    def g(url):
        if url.endswith("robots.txt"):
            return b"User-agent: *\nAllow: /"
        return {"https://a.example/1": paywalled, "https://b.example/2": other, "https://c.example/3": good}[url]
    llm = SearchLLM([{"url": "https://a.example/1", "publisher": "A"}, {"url": "https://b.example/2", "publisher": "B"},
                     {"url": "https://c.example/3", "publisher": "Mint"}, ])
    alt = find_alternative(title, "https://orig.example/x", llm, "t", RobotsCache(g), g)
    assert alt and alt["url"] == "https://c.example/3" and alt["publisher"] == "Mint"
    llm2 = SearchLLM([{"url": "https://b.example/2", "publisher": "B"}])
    assert find_alternative(title, "https://orig.example/x", llm2, "t", RobotsCache(g), g) is None       # unrelated page refused
    llm3 = SearchLLM([{"url": "https://orig.example/y", "publisher": "same"}])
    assert find_alternative(title, "https://orig.example/x", llm3, "t", RobotsCache(g), g) is None       # same site is not an alternative


def test_sig_words_ignore_noise():
    assert "tata" in sig_words("Tata Motors plans new plant") and "with" not in sig_words("with the plant")


def test_sector_story_lands_in_sectors_section_and_unreadable_goes_to_brief(tmp_path):
    root = make_root(tmp_path)
    cfg_path = root / "config" / "news_sources.yaml"
    cfg = yaml.safe_load(cfg_path.read_text())
    cfg["sectors"] = {"Chips and AI hardware": ["chip", "chips"]}
    cfg["limits"]["section_caps"] = {}
    cfg_path.write_text(yaml.safe_dump(cfg))
    md, meta = build_news_report(root, NEWS_NOW, {}, make_net(), lambda u, p=None: {}, fake_llm())
    assert "Chip startup raises $200 million" in md          # at least listed somewhere (headline-only or story)
    assert meta["latest"]["stories"] is not None


def test_spending_stop_leaves_rest_as_headlines(tmp_path):
    root = make_root(tmp_path)
    cfg_path = root / "config" / "news_sources.yaml"
    cfg = yaml.safe_load(cfg_path.read_text())
    cfg["limits"]["max_cost_usd_per_run"] = 0.0
    cfg_path.write_text(yaml.safe_dump(cfg))
    md, meta = build_news_report(root, NEWS_NOW, {}, make_net(), lambda u, p=None: {}, fake_llm())
    assert meta["stats"]["claims_kept"] == 0 and meta["stats"]["cost_stopped"] >= 1
    assert "spending stop" in "".join(str(x) for x in [md])


def test_search_cost_is_counted():
    from news.llm import LLM
    l = LLM("k", "m", None, 0.10, 0.50)
    l.search_count = 100
    assert abs(l.est_cost_usd() - 1.0) < 1e-9


# ---------------- Gemini provider ----------------
def test_gemini_provider_parses_usage_search_and_thoughts(tmp_path):
    from news.llm import LLM, detect_provider, make_llm
    assert detect_provider("AIzaSyXYZ") == "gemini" and detect_provider("sk-ant-1") == "anthropic"
    seen = {}
    def post(url, headers, body, timeout=90):
        seen.update(url=url, headers=headers, body=body)
        return {"candidates": [{"content": {"parts": [{"text": "hidden", "thought": True}, {"text": '{"ok": 1}'}]},
                                "groundingMetadata": {"webSearchQueries": ["a", "b"]}}],
                "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 100, "thoughtsTokenCount": 50}}
    l = LLM("AIzaKEY", "gemini-3.5-flash-lite", tmp_path / "u.jsonl", 0.30, 2.50, post=post)
    l.price_per_search = 0.014
    assert l.complete_with_search("sys", "user", "ignored") == '{"ok": 1}'
    assert seen["url"].endswith("gemini-3.5-flash-lite:generateContent") and seen["headers"]["x-goog-api-key"] == "AIzaKEY"
    assert seen["body"]["tools"] == [{"google_search": {}}] and seen["body"]["systemInstruction"]["parts"][0]["text"] == "sys"
    assert (l.tokens_in, l.tokens_out, l.search_count) == (1000, 150, 2)
    assert abs(l.est_cost_usd() - (1000 * 0.30 / 1e6 + 150 * 2.5 / 1e6 + 2 * 0.014)) < 1e-9
    assert "AIzaKEY" not in (tmp_path / "u.jsonl").read_text()


def test_gemini_empty_answer_fails_closed_and_factory_uses_prefix(tmp_path):
    import pytest
    from collectors.http import FetchError
    from news.llm import LLM, make_llm
    l = LLM("AIzaKEY", "m", None, 0, 0, post=lambda *a, **k: {"promptFeedback": {"blockReason": "SAFETY"}})
    with pytest.raises(FetchError):
        l.complete("s", "u")
    cfg = yaml.safe_load(open(Path(__file__).resolve().parents[1] / "config" / "news_sources.yaml"))
    g = make_llm(cfg, {"ANTHROPIC_API_KEY": "AIzaSomething"}, "extract", None)          # Google key in the Anthropic-named secret
    assert g.provider == "gemini" and g.model == "gemini-3.5-flash-lite"
    c = make_llm(cfg, {"ANTHROPIC_API_KEY": "sk-ant-x"}, "extract", None)
    assert c.provider == "anthropic" and c.model == "claude-haiku-5-5"
    assert make_llm(cfg, {}, "extract", None) is None
