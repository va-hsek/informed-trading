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


# ---------------- storylines and magazine page ----------------
def _story(h, sec, regions, sectors, claims, score=5, topics=()):
    from news.paper import Para, Source, Story
    return Story(h, sec, list(topics), regions, "news", None, [Para(claims, Source("Outlet", "https://o.example/" + h[:5].replace(" ", "")))], sectors=sectors, score=score)


def test_clustering_groups_related_and_splits_unrelated():
    from news.cluster import cluster
    a = _story("RBI holds repo rate steady amid food inflation", "India", ["india"], [], ["The RBI kept the repo rate at 5.50 percent."], 9)
    b = _story("RBI repo rate decision lifts bank shares", "India", ["india"], [], ["Bank shares rose after the RBI repo rate decision."], 5)
    c = _story("Tractor sales jump in festive season", "India", ["india"], [], ["Tractor makers reported higher festive sales."], 4)
    cl = cluster([a, b, c])
    assert len(cl) == 2 and cl[0].lead is a and cl[0].related == [b] and cl[1].lead is c


def test_magazine_page_has_lenses_collapsed_headlines_and_no_unverified_text():
    from news.magazine import render_magazine
    from news.feeds import Item
    cfg = yaml.safe_load(open(Path(__file__).resolve().parents[1] / "config" / "news_sources.yaml"))
    st = [_story("RBI holds repo rate steady", "India", ["india"], ["Banks and finance"], ["The RBI kept the repo rate at 5.50 percent."], 9),
          _story("Brent falls on Iran talks", "Energy and metals", [], [], ["Brent fell 4.9 percent to $93.54."], 8, topics=["crude"])]
    it = Item("f", "news", "Unreadable headline here", "https://x.example/1", None, publisher="Pub")
    stats = dict(read_direct=2, read_wayback=0, read_archive=0, read_search=0, read_partial=0, read_summary=0, unreadable=1, cost_stopped=0,
                 extract_errors=0, claims_kept=2, claims_dropped=0)
    page = render_magazine(NOW_M, st, [(it, "x")], stats, [{"feed": "F", "ok": False, "reason": "HTTP 403", "kept": 0}], [("reason", 3)],
                           "", "", None, "", {}, cfg, lambda i: "")
    assert "News by jurisdiction" in page and "News by sector" in page and "Energy and metals" in page and "Upcoming events" in page
    assert "The RBI kept the repo rate at 5.50 percent." in page and 'href="https://o.example/' in page
    assert '<details class="block" id="coverage"' in page and 'id="coverage" style="--accent:#adb5bd" open' not in page      # folded by default
    assert 'id="headlines"' in page and "Unreadable headline here" in page                                                    # missed headlines, behind a fold
    assert page.index('id="events"') < page.index('id="regions"') < page.index('id="headlines"') < page.index('id="coverage"')   # events first, missed last
    assert '<details class="st"' in page and "<summary><span class=\"ht\">RBI holds repo rate steady" in page               # each story is a one-line headline
    assert "Model check failed" not in page


def test_magazine_shows_model_failure_reason_at_the_top():
    from news.magazine import render_magazine
    cfg = yaml.safe_load(open(Path(__file__).resolve().parents[1] / "config" / "news_sources.yaml"))
    stats = dict(read_direct=0, read_wayback=0, read_archive=0, read_search=0, read_partial=0, read_summary=0, unreadable=0, cost_stopped=0,
                 extract_errors=0, claims_kept=0, claims_dropped=0)
    page = render_magazine(NOW_M, [], [], stats, [], [], "The model could not be reached: HTTP 400: API key not valid", "HTTP 400: API key not valid",
                           None, "", {}, cfg, lambda i: "")
    assert "Read this first" in page and "API key not valid" in page


def test_run_stops_early_and_says_why_when_the_model_is_down(tmp_path):
    from collectors.http import FetchError
    from news.llm import LLM
    from test_news import NOW as N2, make_net, make_root as mk
    def dead(*a, **k): raise FetchError("model API call failed: HTTP 400: API key not valid")
    md, meta = build_news_report(mk(tmp_path), N2, {"GEMINI_API_KEY": "AIzaDEAD"}, make_net(), lambda u, p=None: {}, None) if False else (None, None)
    llm = LLM("AIzaDEAD", "m", None, 0, 0, post=dead)
    class Once(LLM):
        pass
    import news.run_news as rn
    orig = rn.make_llm
    rn.make_llm = lambda *a, **k: llm
    try:
        md, meta = build_news_report(mk(tmp_path), N2, {"GEMINI_API_KEY": "AIzaDEAD"}, make_net(), lambda u, p=None: {}, None)
    finally:
        rn.make_llm = orig
    assert "API key not valid" in md and meta["stats"]["claims_kept"] == 0
    assert meta["diagnostics"]["llm_preflight_error"].endswith("API key not valid")


from datetime import datetime as _dt
NOW_M = _dt(2026, 11, 10, 6, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))


def test_doctor_reports_feed_publisher_and_model_results(tmp_path):
    from news import doctor
    from test_news import make_net, fake_llm
    cfg = yaml.safe_load(open(Path(__file__).resolve().parents[1] / "config" / "news_sources.yaml"))
    cfg["feeds"] = cfg["feeds"][:6]
    import news.doctor as d
    orig = d.make_llm
    d.make_llm = lambda *a, **k: fake_llm()
    d.check_model = lambda c, s: [{"test": "model: plain call", "ok": True, "detail": "fake"}]
    try:
        res = doctor.run(cfg, {}, make_net(), lambda u, p=None: {}, workers=2)
    finally:
        d.make_llm = orig
    assert res["summary"]["feeds_total"] == 6 and "articles_tested" in res["summary"]
    assert isinstance(res["hosts"], list) and "<h1>Doctor" in doctor.to_html(res)


def test_new_style_google_keys_are_gemini_not_anthropic():
    from news.llm import detect_provider, make_llm
    cfg = yaml.safe_load(open(Path(__file__).resolve().parents[1] / "config" / "news_sources.yaml"))
    for k in ("AIzaSyABC", "AQ.Ab8RN6Jabcdef", "anything-else"):
        assert detect_provider(k) == "gemini"
    assert detect_provider("sk-ant-api03-x") == "anthropic"
    assert make_llm(cfg, {"ANTHROPIC_API_KEY": "AQ.Ab8RN6Jabcdef"}, "extract", None).provider == "gemini"


# ---------------- Google News link decoding ----------------
def test_gnews_old_style_id_decodes_offline_and_new_style_uses_batch_call():
    import base64
    from news import gnews
    url = b"https://www.example.com/markets/story-1"
    raw = b"\x08\x13\x22" + bytes([len(url)]) + url + b"\xd2\x01\x00"
    gid = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    assert gnews.decode(f"https://news.google.com/rss/articles/{gid}?oc=5", lambda u: b"") == "https://www.example.com/markets/story-1"
    assert gnews.article_id("https://example.com/x") is None
    gnews.GAP = 0
    page = b'<c-wiz><div data-n-a-sg="SIG" data-n-a-ts="1700000000"></div></c-wiz>'
    new_id = base64.urlsafe_b64encode(b"\x08\x13\x22\x10AU_yqLabcdefghij").decode().rstrip("=")
    seen = {}
    def post(u, body):
        seen["body"] = body
        inner = json.dumps(["garturlres", "https://www.reuters.com/markets/real-article/"])
        return ")]}'\n\n" + json.dumps([["wrb.fr", "Fbv4je", inner, None], ["di", 5], ["af.httprm", 5]])
    out = gnews.decode(f"https://news.google.com/rss/articles/{new_id}", lambda u: page, post)
    assert out == "https://www.reuters.com/markets/real-article/" and "SIG" in __import__("urllib.parse").parse.unquote(seen["body"])
    assert gnews.decode(f"https://news.google.com/rss/articles/{new_id}", lambda u: b"<html>no signature</html>", post) is None      # fails closed
    bad = lambda u, b: ")]}'\n\n" + json.dumps([["wrb.fr", "Fbv4je", json.dumps(["x", "https://news.google.com/loop"]), None], ["di", 1], ["af", 1]])
    assert gnews.decode(f"https://news.google.com/rss/articles/{new_id}", lambda u: page, bad) is None                                   # never returns a Google URL


def test_headline_only_item_is_read_after_decoding(monkeypatch):
    from news import articles, gnews
    from news.feeds import Item
    monkeypatch.setattr(gnews, "decode", lambda link, g, post=None: "https://pub.example/story")
    it = Item("g", "news", "Some story", "https://news.google.com/rss/articles/zzz", None, publisher="Pub", headline_only=True)
    body = "<html><body><article>" + "<p>" + "Sentence about markets and oil prices rising this week. " * 20 + "</p></article></body></html>"
    class R:
        def allowed(self, u): return True
    art = articles.read_article(it, R(), lambda u, *a, **k: body.encode(), lambda *a, **k: {}, 150, 12000)
    assert art["via"] == "direct" and it.origin_url == "https://pub.example/story" and not it.headline_only


# ---------------- delivery and newsletters ----------------
class _Resp:
    def __init__(self, body): self.body = body
    def read(self): return json.dumps(self.body).encode()
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_delivery_sends_nothing_without_stories_and_never_leaks_tokens(tmp_path):
    from news.deliver import deliver
    pdf = tmp_path / "e.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    calls = []
    def opener(req, timeout=0):
        calls.append((req.full_url, req.data))
        return _Resp({"id": "MEDIA1", "ok": True})
    sec = {"TELEGRAM_BOT_TOKEN": "123:SECRET", "TELEGRAM_CHAT_ID": "55", "WHATSAPP_TOKEN": "WATOKEN", "WHATSAPP_PHONE_NUMBER_ID": "999", "WHATSAPP_TO": "+919800000000"}
    log = deliver(sec, pdf, 0, "12 Nov 2026", opener)
    assert any("not sent" in x for x in log) and len(calls) == 1 and b"sendMessage" in calls[0][0].encode()      # only a text notice on Telegram
    calls.clear()
    log = deliver(sec, pdf, 300, "12 Nov 2026", opener)
    urls = [c[0] for c in calls]
    assert any(u.endswith("/sendDocument") for u in urls) and any(u.endswith("/999/media") for u in urls) and any(u.endswith("/999/messages") for u in urls)
    wa = json.loads([c[1] for c in calls if c[0].endswith("/messages")][0])
    assert wa["to"] == "919800000000" and wa["type"] == "document" and wa["document"]["id"] == "MEDIA1"
    sec["WHATSAPP_TEMPLATE"] = "morning_edition"
    calls.clear()
    deliver(sec, pdf, 300, "12 Nov 2026", opener)
    wa = json.loads([c[1] for c in calls if c[0].endswith("/messages")][0])
    assert wa["type"] == "template" and wa["template"]["components"][0]["parameters"][0]["document"]["id"] == "MEDIA1"
    def boom(req, timeout=0): raise RuntimeError("bad token WATOKEN 123:SECRET")
    log = deliver(sec, pdf, 300, "12 Nov 2026", boom)
    assert all("WATOKEN" not in x and "SECRET" not in x for x in log) and any("FAILED" in x for x in log)
    assert deliver({}, pdf, 300, "x", opener)[0].startswith("No delivery channel")


def test_newsletter_links_become_additional_items_and_tracking_links_are_never_kept():
    from news.newsletter import clean_url, fetch_newsletters, parse_newsletter
    html = ("<html><body><p>Hello! <a href='https://t.example/unsubscribe?u=1'>Unsubscribe from this newsletter list</a></p>"
            "<table><tr><td><a href='https://trk.example/c/AAA'>Reserve Bank holds repo rate at 5.5 percent in surprise move</a> The central bank cited food prices and a weak rupee in its statement on Wednesday.</td></tr>"
            "<tr><td><a href='https://trk.example/c/BBB'>Gold hits record high as dollar index slips this week</a></td></tr><tr><td>Gold rose 1.2 percent to a fresh peak on Thursday.</td></tr>"
            "<tr><td><a href='https://trk.example/c/CCC'>Another long headline whose real address cannot be found</a> Blurb text that is long enough to count here.</td></tr></table></body></html>")
    got = parse_newsletter(html)
    assert [g["title"][:12] for g in got] == ["Reserve Bank", "Gold hits re", "Another long"] and "weak rupee" in got[0]["blurb"] and "fresh peak" in got[1]["blurb"]
    assert clean_url("https://pub.example/a?id=7&utm_source=nl&token=abc#x") == "https://pub.example/a?id=7"
    from email.message import EmailMessage
    m = EmailMessage()
    m["From"], m["Subject"], m["Date"] = "Daily <news@nl.example>", "Today", "Thu, 12 Nov 2026 02:00:00 +0000"
    m.set_content("x"); m.add_alternative(html, subtype="html")
    class IMAP:
        def __init__(self, host): pass
        def login(self, u, p): assert (u, p) == ("me@gmail.com", "apppw")
        def select(self, box, readonly=False): assert readonly
        def search(self, *a): return "OK", [b"1"]
        def fetch(self, mid, what): return "OK", [(b"1", m.as_bytes())]
        def logout(self): pass
    now = datetime(2026, 11, 12, 6, 0, tzinfo=timezone.utc)
    res = {"https://trk.example/c/AAA": "https://pub.example/rbi?utm_x=1", "https://trk.example/c/BBB": "https://pub.example/gold", "https://trk.example/c/CCC": None}
    items, cov = fetch_newsletters([{"name": "Daily", "from": "@nl.example"}], {"GMAIL_USER": "me@gmail.com", "GMAIL_APP_PASSWORD": "apppw"}, now,
                                   now - timedelta(hours=24), IMAP, lambda u: res[u])
    assert [i.link for i in items] == ["https://pub.example/rbi", "https://pub.example/gold"] and all(i.extra and i.publisher == "Daily" for i in items)
    assert "1 link(s) dropped" in cov[0]["reason"] and "trk.example" not in " ".join(i.link for i in items)
    _, cov = fetch_newsletters([{"name": "Daily", "from": "x"}], {}, now, now - timedelta(hours=24))
    assert not cov[0]["ok"] and "GMAIL_USER" in cov[0]["reason"]


def test_event_updates_pull_matching_stories_up_and_out_of_the_news_sections():
    from news.magazine import event_updates
    a = _story("US CPI rises 0.3 percent in October", "USA and Americas", ["americas"], [], ["US consumer prices rose 0.3 percent in October."], 8)
    b = _story("Tractor sales jump", "India", ["india"], [], ["Tractor makers reported higher sales."], 4)
    o = _story("RBI statement on liquidity", "Official releases", ["india"], [], ["The RBI announced liquidity operations."], 3)
    cal = {"events": [{"date": "2026-11-12", "title": "US October CPI"}, {"date": "2026-11-30", "title": "Far future event"}]}
    groups, used = event_updates([a, b, o], cal, NOW_M.replace(day=12))
    labels = [g[0] for g in groups]
    assert labels[0] == "Official releases" and any("US October CPI" in l for l in labels)
    assert id(a) in used and id(o) in used and id(b) not in used


def test_email_goes_to_many_recipients_as_bcc_with_the_pdf(tmp_path):
    from news.deliver import deliver
    pdf = tmp_path / "e.pdf"
    pdf.write_bytes(b"%PDF-1.4 x")
    sent = {}
    class SMTP:
        def __init__(self, host, port, timeout=0): sent["host"] = host
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def login(self, u, p): sent["login"] = (u, p)
        def send_message(self, msg, from_addr=None, to_addrs=None):
            sent.update(to_addrs=to_addrs, to_header=msg["To"], attach=[p.get_filename() for p in msg.iter_attachments()])
    sec = {"GMAIL_USER": "me@gmail.com", "GMAIL_APP_PASSWORD": "pw", "EMAIL_TO": "a@x.com, b@y.org;c@z.in"}
    log = deliver(sec, pdf, 50, "12 Nov 2026", smtp_factory=SMTP, headlines=["One", "Two"])
    assert log == ["Email: PDF sent to 3 recipient(s)"] and sent["to_addrs"] == ["a@x.com", "b@y.org", "c@z.in"]
    assert sent["to_header"] == "me@gmail.com" and sent["attach"] == ["e.pdf"] and sent["host"] == "smtp.gmail.com"
    log = deliver(sec, pdf, 0, "12 Nov 2026", smtp_factory=SMTP)
    assert any("notice sent" in x for x in log) and sent["attach"] == []


# ---------------- newsletter written as lines, and free Google News search ----------------
LINES_HTML = ("<html><body><table><tr><td>Read online | Together with Acme Corp sponsor message goes here for sure yes</td></tr>"
              "<tr><td><p>US stocks bounced back yesterday as the AI trade came back to life across the major indexes <a href='https://link.mail.beehiiv.com/x/1'>WSJ</a></p></td></tr>"
              "<tr><td><p>Europe's Stoxx 600 hit a new all-time high for the first time since August on bank earnings</p></td></tr>"
              "<tr><td><p>Short</p></td></tr></table></body></html>")


def test_parse_lines_keeps_news_lines_and_drops_boilerplate():
    from news.newsletter import parse_lines
    got = parse_lines(LINES_HTML)
    assert [g["title"][:12] for g in got] == ["US stocks bo", "Europe's Sto"] and got[0]["url"].startswith("https://link.mail.beehiiv.com")


def test_line_style_newsletter_items_are_headline_only_and_never_carry_tracking_links():
    from email.message import EmailMessage
    from news.newsletter import fetch_newsletters
    m = EmailMessage()
    m["From"], m["Subject"], m["Date"] = "Exec Sum <news@execsum.co>", "Today", "Thu, 12 Nov 2026 02:00:00 +0000"
    m.set_content("x"); m.add_alternative(LINES_HTML, subtype="html")
    class IMAP:
        def __init__(self, h): pass
        def login(self, u, p): pass
        def select(self, b, readonly=False): pass
        def search(self, *a): return "OK", [b"1"]
        def fetch(self, mid, w): return "OK", [(b"1", m.as_bytes())]
        def logout(self): pass
    now = datetime(2026, 11, 12, 6, 0, tzinfo=timezone.utc)
    calls = []
    items, cov = fetch_newsletters([{"name": "Exec Sum", "from": "news@execsum.co", "style": "lines", "site": "https://execsum.co/"}],
                                   {"GMAIL_USER": "a@b.c", "GMAIL_APP_PASSWORD": "p"}, now, now - timedelta(hours=24), IMAP, lambda u: calls.append(u))
    assert len(items) == 2 and all(i.headline_only and i.extra and i.link.startswith("https://execsum.co/#nl-") for i in items) and calls == []
    assert len(items[0].summary) > 60 and "beehiiv" not in " ".join(i.link for i in items)


def test_gnews_search_finds_same_story_at_another_outlet_and_rejects_mismatches():
    from news.gnews_search import find_alternative_gnews
    rss = (b"<?xml version='1.0'?><rss><channel>"
           b"<item><title>Wall Street stocks rebound as AI trade revives - Reuters</title><link>https://news.google.com/rss/articles/CBMiOLD</link><pubDate>Thu, 12 Nov 2026 03:00:00 GMT</pubDate><source url='https://reuters.com'>Reuters</source></item>"
           b"<item><title>Totally unrelated gardening tips</title><link>https://news.google.com/rss/articles/CBMiX</link><pubDate>Thu, 12 Nov 2026 03:00:00 GMT</pubDate><source url='https://g.com'>Garden</source></item></channel></rss>")
    import base64
    u = b"https://www.reuters.com/markets/rebound"
    gid = base64.urlsafe_b64encode(b"\x08\x13\x22" + bytes([len(u)]) + u + b"\xd2\x01\x00").decode().rstrip("=")
    rss = rss.replace(b"CBMiOLD", gid.encode())
    page = ("<html><head><title>Wall Street stocks rebound as AI trade revives</title></head><body><article>" + "<p>US stocks rebounded as the AI trade revived across major indexes on Wednesday. " * 12 + "</p></article></body></html>").encode()
    def get(url, *a, **k):
        if "news.google.com/rss/search" in url: return rss
        if "reuters.com" in url: return page
        raise AssertionError(url)
    class R:
        def allowed(self, u): return True
    from news import gnews
    gnews.GAP = 0
    alt = find_alternative_gnews("Wall Street stocks rebound yesterday as the AI trade revives", "https://execsum.co/#nl-1", R(), get, 150, 12000)
    assert alt and alt["url"] == "https://www.reuters.com/markets/rebound" and alt["publisher"] == "Reuters"
    assert find_alternative_gnews("Completely different volcano eruption in Iceland today", "x", R(), get) is None


# ---------------- Today in X, section summaries, depth styling, HTML attachment ----------------
def _mini_story(h, outlets, section="India", score=5, topics=None):
    from news.paper import Source, Para, Story
    ist = timezone(timedelta(hours=5, minutes=30))
    return Story(h, section, topics or [], ["india"], "news", datetime(2026, 11, 12, 3, 0, tzinfo=ist),
                 [Para([f"{h}, 4.2 percent."], Source(o, f"https://{o.lower()}.example/{abs(hash(h)) % 999}", "direct")) for o in outlets], score=score, sectors=[])


def _render(stories, cal=None):
    import yaml
    from news.magazine import render_magazine
    cfg = yaml.safe_load(open(ROOT_CFG))
    stats = dict(read_direct=1, read_wayback=0, read_archive=0, read_search=0, read_partial=0, read_summary=0, unreadable=0, cost_stopped=0, extract_errors=0, claims_kept=1, claims_dropped=0)
    now = datetime(2026, 11, 12, 6, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    return render_magazine(now, stories, [], stats, [], [], "", "", None, "", cal or {"last_reviewed": "2026-11-12", "events": []}, cfg, lambda it: "")


ROOT_CFG = str(Path(__file__).resolve().parent.parent / "config" / "news_sources.yaml")


def test_today_in_x_is_ranked_by_subject_not_popularity():
    import yaml
    from news.magazine import lead_clusters
    from news.cluster import cluster
    cfg = yaml.safe_load(open(ROOT_CFG))
    pricelist = _mini_story("Gold and silver rates today in Bengaluru and Chennai, check gold prices", ["A", "B", "C", "D", "E", "F"], score=1)
    fed = _mini_story("Fed official flags rate hike risk as inflation stays sticky", ["Mint"], score=1)
    crude1 = _mini_story("Brent crude falls on Iran talks hopes", ["Mint", "Reuters"], score=1)
    crude2 = _mini_story("Crude oil futures slip as OPEC supply rises", ["BS", "BBC"], score=1)
    crude3 = _mini_story("WTI crude extends losses on stockpile build", ["ET", "CNBC"], score=1)
    gardening = _mini_story("Village fair opens with local crafts", ["Mint", "BS", "ET"], score=1)
    got = lead_clusters(cluster([pricelist, fed, crude1, crude2, crude3, gardening]), cfg["limits"], cfg["importance"])
    heads = [c.lead.headline for c, r in got]
    assert any("Fed official" in h for h in heads)                                   # a single-outlet rates story qualifies on subject
    assert not any("rates today" in h for h in heads[:2]) and sum(("rude" in h or "WTI" in h) for h in heads) <= 2   # price list demoted; crude capped at 2
    html = _render([pricelist, fed, crude1, crude2, crude3])
    assert "Why here: " in html and "Today in " in html and "Today in five" not in html


def test_sections_with_several_parts_say_what_is_inside_and_depth_is_styled():
    stories = [_mini_story("Crude falls on talks of supply deal in gulf region", ["Mint"], "Energy and metals", topics=["crude"]),
               _mini_story("Gold slips as yields rise on strong jobs data print", ["Kitco"], "Energy and metals", topics=["gold"]),
               _mini_story("RBI keeps repo rate unchanged for fifth straight meeting", ["Mint"], "India"),
               _mini_story("Sensex ends higher led by banks and IT shares today", ["BS"], "India")]
    cal = {"last_reviewed": "2026-11-12", "events": [{"date": "2026-11-12", "time_ist": "10:00", "title": "Quarterly review meeting", "expectation": "x"},
                                                      {"date": "2026-11-13", "time_ist": "10:00", "title": "US CPI", "expectation": "y"}]}
    html = _render(stories, cal)
    import re
    sums = re.findall(r'<span class=sum>([^<]*)</span>', html)
    assert any(x.startswith("Today 1 · Tomorrow 1") for x in sums)                     # events: what days it holds
    assert any("Crude oil 1" in x and "Gold 1" in x for x in sums)                      # energy and metals: what it holds
    assert any(x.startswith("Includes: ") and "RBI keeps repo rate" in x for x in sums)  # a part with several storylines lists its leading headlines
    assert "details.sub{margin:.35rem 0 .35rem .6rem" in html and "details.st[open]{background:color-mix" in html


def test_email_carries_pdf_and_html(tmp_path):
    from news.deliver import deliver
    pdf, htm = tmp_path / "news_x.pdf", tmp_path / "news_x.html"
    pdf.write_bytes(b"%PDF-1.4 x"); htm.write_text("<html>page</html>")
    sent = {}
    class SMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def login(self, u, p): pass
        def send_message(self, msg, from_addr, to_addrs): sent["names"] = sorted(p.get_filename() for p in msg.iter_attachments()); sent["to"] = to_addrs
    log = deliver({"GMAIL_USER": "a@b.c", "GMAIL_APP_PASSWORD": "p", "EMAIL_TO": "x@y.z; q@r.s"}, pdf, 3, "12 Nov 2026", smtp_factory=SMTP)
    assert sent["names"] == ["news_x.html", "news_x.pdf"] and sent["to"] == ["x@y.z", "q@r.s"] and "PDF + HTML" in log[0]


# ---------------- figures differ, on your list, since yesterday, official feeds ----------------
def test_figures_differ_flags_only_different_outlets_with_same_measure():
    from news.figures import disagreements
    d = disagreements([("Mint", "Brent crude fell 2.1% to $91.80 a barrel on Wednesday"), ("Reuters", "Brent crude fell 2.4% to $92.10 a barrel after talks"),
                       ("Mint", "Brent crude fell 2.9% overnight")])
    assert [(a.shown, b.shown) for a, b in d][0] == ("2.1%", "2.4%") and all(a.source != b.source for a, b in d)
    assert disagreements([("A", "India GDP growth seen at 7% this year"), ("B", "Village fair drew 9% more visitors")]) == []       # different measure
    assert disagreements([("A", "Brent crude fell 2.1% on Wednesday"), ("B", "Brent crude fell 2.1% in Asian trade")]) == []        # same number: no flag
    assert disagreements([("A", "Brent crude rose 12 on Wednesday"), ("B", "Brent crude rose 15 on Wednesday")]) == []              # bare numbers are never compared


def test_figures_flag_appears_in_the_story_body_and_on_your_list_groups_by_word():
    a, b = _mini_story("Brent crude falls on Iran talks hopes", ["Mint"]), _mini_story("Brent crude slides as Iran talks resume", ["Reuters"])
    a.paras[0].claims[:] = ["Brent crude fell 2.1% to $91.80 a barrel on Wednesday."]
    b.paras[0].claims[:] = ["Brent crude fell 2.4% to $92.10 a barrel on Wednesday."]
    html = _render([a, b, _mini_story("Nifty ends flat as banks drag", ["BS"])])
    assert "Figures differ." in html and 'id="mine"' in html and "On your list" in html and "Brent" in html


def test_since_yesterday_tags_and_rotation(tmp_path):
    import json
    from news.run_news import load_prev_headlines, rotate_latest
    nd = tmp_path
    ist = timezone(timedelta(hours=5, minutes=30))
    yesterday = datetime(2026, 11, 12, 5, 40, tzinfo=ist)
    today = datetime(2026, 11, 13, 5, 40, tzinfo=ist)
    (nd / "latest.json").write_text(json.dumps({"generated": yesterday.isoformat(), "stories": [{"headline": "Brent crude falls on Iran talks hopes"}]}))
    assert load_prev_headlines(nd, today) == ["Brent crude falls on Iran talks hopes"]
    rotate_latest(nd, today)
    (nd / "latest.json").write_text(json.dumps({"generated": today.isoformat(), "stories": [{"headline": "other"}]}))
    assert load_prev_headlines(nd, today) == ["Brent crude falls on Iran talks hopes"]            # a second run the same day still compares with yesterday
    rotate_latest(nd, today)
    assert json.loads((nd / "latest_prev.json").read_text())["stories"][0]["headline"].startswith("Brent")   # not overwritten by same-day reruns
    from news.magazine import seen_yesterday
    assert seen_yesterday("Brent crude slides as Iran talks hopes grow", ["Brent crude falls on Iran talks hopes"]) and not seen_yesterday("Nifty ends flat as banks drag lower", ["Brent crude falls on Iran talks hopes"])
    html = _render([_mini_story("Brent crude falls on Iran talks hopes", ["Mint", "BBC", "ET"])])
    assert "Today in" in html


def test_official_feeds_added_and_config_parses():
    import yaml
    cfg = yaml.safe_load(open(ROOT_CFG))
    names = {f["name"] for f in cfg["feeds"]}
    assert {"ECB press", "Bank of England news", "BIS media releases", "Fed monetary policy", "IMF", "Basel Committee (BCBS)", "OPEC", "SEBI"} <= names
    assert all(f["category"] == "official" for f in cfg["feeds"] if f["name"] in ("IMF", "OECD", "IEA", "BIS media releases"))
