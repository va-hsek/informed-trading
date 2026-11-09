import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from collectors.http import FetchError
from news.articles import RobotsCache, extract_text, looks_paywalled, read_article, wayback_raw_url, wayback_snapshot
from news.claims import copies_source, extract_claims, numbers, parse_claims, verify_claim
from news.feeds import Item, collect_items, edition_window, parse_feed, tag_topics
from news.run_news import build_news_report

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 11, 7, 8, 0, tzinfo=IST)

ARTICLE_P = [
    "Brent crude futures rose 1.8% to $84.20 a barrel on Friday after OPEC+ said it would keep output unchanged through December.",
    "US commercial crude inventories fell by 3.1 million barrels last week, the Energy Information Administration said on Wednesday.",
    "Analysts at one bank said they expect prices to hold between $80 and $90 over the next quarter, citing steady refinery demand in Asia.",
    "Refiners in Asia have been running at high rates and traders said buying interest from China and India remained firm through the week, supporting spot premiums.",
    "The dollar index was little changed while the rupee eased to 94.94 per dollar, according to data from the central bank this week.",
]
ARTICLE_HTML = "<html><body><nav>menu</nav><article>" + "".join(f"<p>{p}</p>" for p in ARTICLE_P) + "</article><footer>x</footer></body></html>"
ARTICLE_TEXT = "\n".join(ARTICLE_P)


def rss(items, ns=""):
    body = "".join(f"<item><title>{t}</title><link>{l}</link><pubDate>{d}</pubDate><description>{s}</description></item>" for t, l, d, s in items)
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>{body}</channel></rss>'.encode()


# ---------------- feeds ----------------
def test_parse_rss_atom_and_timezones():
    src = {"name": "RBI", "category": "official", "mode": "rss", "assume_tz": "+05:30"}
    its = parse_feed(rss([("RBI to conduct auction", "https://rbi.org/1", "Fri, 06 Nov 2026 19:10:00", "&lt;b&gt;desc&lt;/b&gt; text")]), src)
    assert its[0].published == datetime(2026, 11, 6, 13, 40, tzinfo=timezone.utc) and its[0].summary == "desc text"
    atom = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>A</title><link href="https://x/a"/><updated>2026-11-06T10:00:00Z</updated></entry></feed>'
    a = parse_feed(atom, {"name": "A", "mode": "rss"})
    assert a[0].link == "https://x/a" and a[0].published == datetime(2026, 11, 6, 10, 0, tzinfo=timezone.utc)
    with pytest.raises(FetchError):
        parse_feed(b"<html><body>hi</body></html>", src)
    with pytest.raises(FetchError):
        parse_feed(b"not xml at all", src)
    with pytest.raises(FetchError):
        parse_feed(b'<rss version="2.0"><channel></channel></rss>', src)


def test_gnews_items_are_headline_only_with_publisher_split():
    xml = (b'<rss version="2.0"><channel><item><title>Gold edges higher - Reuters</title>'
           b'<link>https://news.google.com/rss/articles/abc</link><pubDate>Fri, 06 Nov 2026 10:00:00 GMT</pubDate>'
           b'<description>Gold edges higher Reuters</description><source url="https://www.reuters.com">Reuters</source></item></channel></rss>')
    it = parse_feed(xml, {"name": "g", "category": "asset", "mode": "gnews"})[0]
    assert it.headline_only and it.title == "Gold edges higher" and it.publisher == "Reuters" and it.summary == "" and it.origin_url == ""


def test_collect_items_age_future_dedupe_and_failures():
    feeds = [{"name": "ok", "mode": "rss", "url": "https://ok"}, {"name": "bad", "mode": "rss", "url": "https://bad"}]
    xml = rss([("Fresh gold story", "https://a/1", "Fri, 06 Nov 2026 10:00:00 +0000", ""),
               ("Fresh gold story", "https://a/2", "Fri, 06 Nov 2026 10:05:00 +0000", ""),         # duplicate title
               ("Old story", "https://a/3", "Mon, 26 Oct 2026 10:00:00 +0000", ""),               # too old
               ("Future story", "https://a/4", "Sun, 08 Nov 2026 23:00:00 +0000", ""),           # future-dated
               ("Undated story", "https://a/5", "", "")])                                         # no date: cannot be checked, dropped

    def get(url):
        if url == "https://ok":
            return xml
        raise FetchError("HTTP 404 for https://bad")
    items, cov = collect_items(feeds, NOW.astimezone(timezone.utc), get, NOW.astimezone(timezone.utc) - timedelta(hours=60), 25)
    assert [i.title for i in items] == ["Fresh gold story"]
    assert "1 undated" in cov[0]["reason"]
    assert cov[0]["ok"] and cov[0]["kept"] == 1 and not cov[1]["ok"] and "404" in cov[1]["reason"]


def test_topic_tagging_whole_words():
    topics = {"gold": ["gold"], "tech": ["ai"], "crude": ["oil price"]}
    t = lambda s: tag_topics(Item("s", "c", s, "l", None), topics)
    assert t("Gold hits record") == ["gold"] and t("Oil price jumps") == ["crude"]
    assert t("He said it was fine") == []              # 'ai' inside 'said' must not match
    assert t("AI chip startup raises funds") == ["tech"]
    assert t("Golden Gate bridge") == []


# ---------------- articles ----------------
def test_extract_and_paywall_detection():
    assert extract_text(ARTICLE_HTML) == ARTICLE_TEXT
    assert looks_paywalled(ARTICLE_TEXT, 600) is None
    assert "characters" in looks_paywalled("short text " * 5, 600)
    assert "paywall marker" in looks_paywalled(ARTICLE_TEXT + " Subscribe to read the full story", 100)


def test_robots_cache_rules():
    def get(url):
        if "closed.example" in url:
            return b"User-agent: *\nDisallow: /"
        if "open.example" in url:
            return b"User-agent: *\nDisallow: /private/"
        if "norobots.example" in url:
            raise FetchError("HTTP 404 for x")
        raise FetchError("HTTP 500 for x")
    r = RobotsCache(get)
    assert not r.allowed("https://closed.example/a")
    assert r.allowed("https://open.example/a") and not r.allowed("https://open.example/private/a")
    assert r.allowed("https://norobots.example/a")
    assert not r.allowed("https://broken.example/a")            # unknown state: do not guess


def test_wayback_snapshot_window_and_raw_url():
    pub = datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc)
    ok = {"archived_snapshots": {"closest": {"available": True, "status": "200", "timestamp": "20261105123000",
                                             "url": "http://web.archive.org/web/20261105123000/https://wsj.com/a"}}}
    snap = wayback_snapshot("https://wsj.com/a", pub, lambda u, p=None: ok)
    assert snap[1] == datetime(2026, 11, 5, 12, 30, tzinfo=timezone.utc)
    assert wayback_raw_url(snap[0]) == "http://web.archive.org/web/20261105123000id_/https://wsj.com/a"
    old = json.loads(json.dumps(ok))
    old["archived_snapshots"]["closest"]["timestamp"] = "20260101000000"
    assert wayback_snapshot("https://wsj.com/a", pub, lambda u, p=None: old) is None
    assert wayback_snapshot("https://wsj.com/a", pub, lambda u, p=None: {"archived_snapshots": {}}) is None
    bad = json.loads(json.dumps(ok)); bad["archived_snapshots"]["closest"]["status"] = "404"
    assert wayback_snapshot("https://wsj.com/a", pub, lambda u, p=None: bad) is None


def item(link="https://pub.example/a", paywalled=False, summary="", headline_only=False):
    return Item("Pub", "world", "Title", link, datetime(2026, 11, 5, 10, tzinfo=timezone.utc), summary, paywalled, headline_only, "", link)


def test_read_article_paths():
    robots_ok = lambda url: b"User-agent: *\nAllow: /"
    # direct
    def get(url):
        if url.endswith("robots.txt"):
            return b"User-agent: *\nAllow: /"
        return ARTICLE_HTML.encode()
    r = read_article(item(), RobotsCache(get), get, lambda *a, **k: {})
    assert r["via"] == "direct" and r["text"] == ARTICLE_TEXT
    # paywalled direct page -> wayback copy
    snap = {"archived_snapshots": {"closest": {"available": True, "status": "200", "timestamp": "20261105123000",
                                               "url": "http://web.archive.org/web/20261105123000/https://pub.example/a"}}}

    def get2(url):
        if url.endswith("robots.txt"):
            return b"User-agent: *\nAllow: /"
        if "id_/" in url:
            return ARTICLE_HTML.encode()
        return b"<html><body><article><p>Subscribe to read the full story of this very long headline text here.</p></article></body></html>"
    r2 = read_article(item(paywalled=True), RobotsCache(get2), get2, lambda *a, **k: snap)
    assert r2["via"] == "wayback" and r2["snapshot_date"] == "2026-11-05"
    # robots disallow, no summary -> unreadable with a reason
    def get3(url):
        return b"User-agent: *\nDisallow: /"
    r3 = read_article(item(), RobotsCache(get3), get3, lambda *a, **k: {})
    assert r3["via"] is None and "robots.txt" in r3["reason"]
    # robots disallow but the feed gave a real summary -> summary used and labelled
    long_sum = "The central bank said it would hold rates steady at 5.5 percent and signalled no change in guidance for the next meeting. " * 2
    r4 = read_article(item(summary=long_sum), RobotsCache(get3), get3, lambda *a, **k: {})
    assert r4["via"] == "feed summary"
    # headline only
    r5 = read_article(item(headline_only=True), RobotsCache(get), get, lambda *a, **k: {})
    assert r5["via"] is None and "headline only" in r5["reason"]


# ---------------- claim guard ----------------
class FakeLLM:
    def __init__(self, replies):
        self.replies, self.tokens_in, self.tokens_out, self.calls = list(replies), 0, 0, 0

    def complete(self, system, user, max_tokens=800):
        self.calls += 1
        self.tokens_in += 100
        self.tokens_out += 50
        r = self.replies.pop(0)
        return r if isinstance(r, str) else json.dumps(r)

    def est_cost_usd(self):
        return 0.0


GOOD_EV = "US commercial crude inventories fell by 3.1 million barrels last week"


def test_verify_claim_rules():
    ok = {"claim": "US commercial crude stocks dropped 3.1 million barrels in the latest week, EIA reported.", "evidence": GOOD_EV}
    assert verify_claim(ok, ARTICLE_TEXT) == (True, "ok")
    assert not verify_claim({**ok, "evidence": "US crude stocks fell sharply by more than three million barrels last week"}, ARTICLE_TEXT)[0]
    bad_num = verify_claim({**ok, "claim": "US crude stocks dropped 4.7 million barrels last week."}, ARTICLE_TEXT)
    assert not bad_num[0] and "4.7" in bad_num[1]
    short_ev = verify_claim({**ok, "evidence": "crude fell"}, ARTICLE_TEXT)
    assert not short_ev[0] and "outside" in short_ev[1]
    copied = verify_claim({"claim": ARTICLE_P[0], "evidence": GOOD_EV.replace("US commercial crude inventories fell by 3.1 million barrels last week", "Brent crude futures rose 1.8% to $84.20 a barrel on Friday")}, ARTICLE_TEXT)
    assert not copied[0] and "copies" in copied[1]
    # normalisation: case, whitespace and curly quotes do not break a genuine match
    art = "The bank said it’s \"fully committed\" to the plan announced on Monday after the meeting ended."
    assert verify_claim({"claim": "The bank is committed to the plan.", "evidence": "the bank said it's \"fully committed\" to the plan announced on Monday"}, art)[0]
    # comma/decimal numbers compare as numbers
    assert numbers("rose 1,250.5 and 3%") == {"1250.5", "3"}


def test_extract_claims_keeps_only_verified():
    llm = FakeLLM([{"claims": [
        {"claim": "US commercial crude stocks dropped 3.1 million barrels in the latest week, EIA reported.", "evidence": GOOD_EV},
        {"claim": "Brent rose to $99.99 a barrel.", "evidence": "Brent crude futures rose 1.8% to $84.20 a barrel on Friday"},       # fabricated number
        {"claim": "OPEC+ will cut output by 2 million barrels.", "evidence": "OPEC+ announced a deep cut in production for the winter months"},  # evidence not in article
    ]}])
    kept, dropped, err = extract_claims(llm, "Title", ARTICLE_TEXT, 4)
    assert err == "" and len(kept) == 1 and "3.1 million" in kept[0] and len(dropped) == 2


def test_extract_claims_handles_garbage_output():
    kept, dropped, err = extract_claims(FakeLLM(["sorry I cannot", "still not json"]), "T", ARTICLE_TEXT, 4)
    assert kept == [] and "unusable" in err
    kept2, _, err2 = extract_claims(FakeLLM(["no json first", json.dumps({"claims": []})]), "T", ARTICLE_TEXT, 4)
    assert kept2 == [] and err2 == ""                       # the retry succeeded with an empty list
    assert parse_claims('x {"claims":[{"claim":"a","evidence":"b"},{"bad":1}]} y') == [{"claim": "a", "evidence": "b"}]


# ---------------- end to end ----------------
def make_net(article_ok=True):
    rbi = rss([("RBI keeps policy repo rate unchanged", "https://pub.example/rbi", "Fri, 06 Nov 2026 19:10:00", "")])
    tc = rss([("Chip startup raises $200 million", "https://pub.example/tc", "Fri, 06 Nov 2026 16:00:00 +0000", "")])
    gold = (b'<rss version="2.0"><channel><item><title>Gold edges higher - Reuters</title><link>https://news.google.com/rss/articles/x</link>'
            b'<pubDate>Fri, 06 Nov 2026 10:00:00 GMT</pubDate><source url="https://reuters.com">Reuters</source></item></channel></rss>')
    oil = rss([("Oil price climbs as OPEC+ holds output", "https://pub.example/oil", "Fri, 06 Nov 2026 12:00:00 +0000", "")])

    def get(url, *a, **k):
        if url.endswith("robots.txt"):
            return b"User-agent: *\nAllow: /"
        if url == "https://www.rbi.org.in/pressreleases_rss.xml":
            return rbi
        if url == "https://techcrunch.com/feed/":
            return tc
        if url == "https://www.eia.gov/rss/todayinenergy.xml":
            return oil
        if url.startswith("https://news.google.com/rss/search") and "gold+price" in url:
            return gold
        if url.startswith("https://pub.example/oil"):
            return ARTICLE_HTML.encode() if article_ok else b"<html><body>nope</body></html>"
        if url.startswith("https://pub.example/rbi"):
            return ("<html><body><article>" + "".join(f"<p>{p}</p>" for p in [
                "The Reserve Bank of India kept the policy repo rate unchanged at 5.50 percent on Friday, according to the statement released.",
                "The central bank said consumer price inflation is projected to average 4.0 percent in the current financial year overall.",
                "Governor said the committee voted unanimously and that liquidity conditions would remain in a comfortable surplus for now.",
                "The next scheduled meeting of the monetary policy committee will take place in December according to the published calendar.",
                "Economists polled earlier this week had expected the central bank to leave rates on hold, citing food prices that have eased since the summer months."]) + "</article></body></html>").encode()
        raise FetchError("HTTP 404 for " + url)
    return get


def fake_llm():
    ev = "US commercial crude inventories fell by 3.1 million barrels last week"
    rbi_ev = "kept the policy repo rate unchanged at 5.50 percent on Friday"

    class L(FakeLLM):
        def complete(self, system, user, max_tokens=800):
            self.tokens_in += 100
            self.tokens_out += 50
            if "Oil price climbs" in user:
                return json.dumps({"claims": [
                    {"claim": "US commercial crude stocks dropped 3.1 million barrels in the latest week, EIA reported.", "evidence": ev},
                    {"claim": "Brent crude hit $120 a barrel.", "evidence": "Brent crude futures rose 1.8% to $84.20 a barrel on Friday"}]})
            if "RBI keeps" in user:
                return json.dumps({"claims": [{"claim": "The RBI left its repo rate at 5.50 percent and projects inflation near 4.0 percent this year.", "evidence": rbi_ev}]})
            return json.dumps({"claims": []})
    return L([])


def make_root(tmp_path):
    root = tmp_path / "r"
    (root / "config").mkdir(parents=True)
    shutil.copy("config/news_sources.yaml", root / "config" / "news_sources.yaml")
    return root


def test_end_to_end_report(tmp_path):
    root = make_root(tmp_path)
    md, meta = build_news_report(root, NOW, {}, make_net(), lambda u, p=None: {}, fake_llm())
    assert "US commercial crude stocks dropped 3.1 million barrels in the latest week, EIA reported." in md
    assert "([EIA Today in Energy](https://pub.example/oil))" in md           # paragraph ends with the hyperlinked source name
    assert "The RBI left its repo rate at 5.50 percent" in md and "([RBI press releases](https://pub.example/rbi))" in md
    assert "$120" not in md                                                   # fabricated number was dropped
    assert meta["stats"]["claims_kept"] == 2 and meta["stats"]["claims_dropped"] == 1
    assert "## Front page" in md and "## Week ahead" in md and "## Coverage and quality" in md
    assert "- Gold edges higher ([Reuters](https://news.google.com/rss/articles/x))" in md and "headline only" in md.lower()
    assert "FAILED" in md                                                     # feeds not served by the fake are reported
    for p in ARTICLE_P + ["Governor said the committee voted unanimously"]:
        assert p not in md, "article text must not be reproduced"
    assert "Model use this run" in md
    latest = meta["latest"]
    assert {s["headline"] for s in latest["stories"]} == {"Oil price climbs as OPEC+ holds output", "RBI keeps policy repo rate unchanged"}
    assert latest["stories"][0]["sources"][0]["url"].startswith("https://pub.example/")
    assert any(h["headline"] == "Gold edges higher" for h in latest["headlines"])


def test_no_api_key_gives_headlines_only(tmp_path):
    md, meta = build_news_report(make_root(tmp_path), NOW, {}, make_net(), lambda u, p=None: {}, None)
    assert "ANTHROPIC_API_KEY is not set" in md and meta["stats"]["claims_kept"] == 0
    assert "Gold edges higher" in md


def test_unreadable_article_is_listed_not_summarised(tmp_path):
    md, meta = build_news_report(make_root(tmp_path), NOW, {}, make_net(article_ok=False), lambda u, p=None: {}, fake_llm())
    assert "US commercial crude stocks dropped" not in md
    assert meta["stats"]["unreadable"] >= 1


def test_edition_window_runs_from_0530_ist_yesterday():
    ist = timezone(timedelta(hours=5, minutes=30))
    f = lambda y, mo, d, h, mi: edition_window(datetime(y, mo, d, h, mi, tzinfo=ist).astimezone(timezone.utc)).astimezone(ist)
    assert f(2026, 11, 9, 5, 30) == datetime(2026, 11, 8, 5, 30, tzinfo=ist)       # 05:30 run: exactly 24 hours
    assert f(2026, 11, 9, 11, 39) == datetime(2026, 11, 8, 5, 30, tzinfo=ist)      # later run: from 05:30 yesterday
    assert f(2026, 11, 9, 3, 0) == datetime(2026, 11, 7, 5, 30, tzinfo=ist)        # before 05:30: previous edition's window
