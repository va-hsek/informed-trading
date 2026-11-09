import json
from datetime import date, datetime, timedelta, timezone

from news.feeds import Item, tag_topics
from news.paper import (Para, Source, Story, merge_stories, render_calendar, render_glance, render_story, section_for)

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 11, 9, 8, 0, tzinfo=IST)


def story(head, claims, name="A", url="https://a/1", section="Energy and metals", via="direct"):
    return Story(head, section, ["crude"], [], "world", None, [Para(claims, Source(name, url, via))])


def test_paragraphs_are_chunked_and_each_ends_with_its_source():
    s = story("Oil falls", [f"Fact number {i} happened with {i}0 units" for i in range(1, 8)])
    out = render_story(s, 3)
    paras = [p for p in out.split("\n\n") if p and not p.startswith("###")]
    assert len(paras) == 3 and out.count("([A](https://a/1))") == 3 or out.count("([A](https://a/1))") == 1
    assert out.strip().endswith("([A](https://a/1))")
    assert all(p.rstrip().endswith(".") or p.rstrip().endswith(")") for p in paras)


def test_every_sentence_gets_a_full_stop_and_citation_follows_it():
    s = story("T", ["Brent fell 9.73% on Wednesday"])
    assert "Brent fell 9.73% on Wednesday. ([A](https://a/1))" in render_story(s, 3)


def test_merge_keeps_attribution_exact_and_does_not_repeat_facts():
    a = story("Brent oil falls 16% on week as Iran deal hopes grow",
              ["Brent settled at $98.32 on Friday.", "Brent fell 16% on the week from $117.64."], "A", "https://a/1")
    b = story("Oil prices fall 16% in week on Iran deal hopes",
              ["Brent settled at $98.32 on Friday.", "Hormuz flows are below 60% of capacity."], "B", "https://b/1")
    c = story("Oil prices fall 16% in week on Iran deal hopes (again)", ["Brent settled at $98.32 on Friday."], "C", "https://c/1")
    out = merge_stories([a, b, c])
    assert len(out) == 1
    txt = render_story(out[0], 3)
    assert txt.count("98.32") == 1                                    # repeated fact printed once
    assert "Hormuz flows are below 60% of capacity. ([B](https://b/1))" in txt     # B's new fact cites B, not A
    assert "Also reported by [C](https://c/1)" in txt                 # corroboration is shown, not hidden


def test_unrelated_stories_are_not_merged():
    a = story("Gold rises", ["Gold rose 2% to 4,050."], section="Energy and metals")
    b = story("Silver slips", ["Silver fell 1% to 51.9."], "B", "https://b/1", section="Energy and metals")
    assert len(merge_stories([a, b])) == 2
    c = story("Gold rises", ["Gold rose 2% to 4,050."], "C", "https://c/1", section="India")
    assert len(merge_stories([a, c])) == 2                            # different sections never merge


def test_wayback_and_archive_labels():
    s = Story("T", "India", [], [], "world", None, [Para(["Fact 1 here."], Source("WSJ", "https://w/1", "wayback", "2026-11-05", "https://archive.ph/newest/https://w/1"))])
    out = render_story(s, 3)
    assert "(Wayback copy of 2026-11-05)" in out and "[archive](https://archive.ph/newest/https://w/1)" in out


def test_regions_and_sections():
    topics = {"crude": ["oil price"], "deals": ["acquire"], "rates_fed": ["fed"], "tech": ["ai"]}
    regions = {"apac": ["nikkei", "japan"], "india": ["india", "rbi"], "europe": ["ecb"]}
    def mk(title, cat="world", region=""):
        it = Item("s", cat, title, "l", None)
        it.feed_region = region
        it.topics, it.regions = tag_topics(it, topics), tag_topics(it, regions)
        return it
    assert section_for(mk("Nikkei plunges as AI stocks fall")) == "Asia-Pacific"
    assert section_for(mk("RBI holds; oil price jumps")) == "Energy and metals"       # energy beats region
    assert section_for(mk("Adani to acquire Evos in Europe")) == "Deals and corporate"
    assert section_for(mk("ECB signals hike")) == "Europe"
    assert section_for(mk("Fed officials split")) == "Markets and macro"
    assert section_for(mk("Chip startup raises money", cat="tech")) == "Tech and AI"
    assert section_for(mk("Anything", cat="official")) == "Official releases"
    assert section_for(mk("Local story", region="europe")) == "Europe"                # feed's own region wins


# ---------------- calendar ----------------
CAL = {"last_reviewed": "2026-11-09", "events": [
    {"date": "2026-11-08", "title": "Past event", "source_name": "S", "source_url": "https://s"},
    {"date": "2026-11-10", "time_ist": "7:00 pm", "title": "US CPI", "expectation": "+0.3%", "affects": ["gold", "rupee"], "source_name": "S", "source_url": "https://s/cpi"},
    {"date": "2026-11-09", "title": "China CPI", "verified": False},
    {"date": "2027-03-01", "title": "Far away", "source_name": "S", "source_url": "https://s"},
    {"date": "bad", "title": "Broken date"}]}


def test_calendar_filters_orders_and_marks_unverified():
    out = "\n".join(render_calendar(CAL, NOW, 21, 7))
    assert "Past event" not in out and "Far away" not in out and "Broken date" not in out
    assert out.index("China CPI (unverified)") < out.index("US CPI")                    # sorted by date
    assert "Tue 10 Nov, 7:00 pm" in out and "[S](https://s/cpi)" in out and "+0.3%" in out
    assert "last reviewed" not in out.lower()


def test_calendar_warns_when_review_is_old_or_missing():
    old = dict(CAL, last_reviewed="2026-10-20")
    assert "last reviewed 20 Oct 2026 (20 days ago)" in "\n".join(render_calendar(old, NOW, 21, 7))
    none = {"events": CAL["events"]}
    assert "no review date" in "\n".join(render_calendar(none, NOW, 21, 7)).lower()
    assert "No events in the calendar" in "\n".join(render_calendar({"last_reviewed": "2026-11-09", "events": []}, NOW, 21, 7))


# ---------------- glance ----------------
def test_glance_missing_stale_ok_and_unreadable(tmp_path):
    p = tmp_path / "d.json"
    assert "Not available" in "\n".join(render_glance(p, NOW))
    p.write_text("not json")
    assert "unreadable" in "\n".join(render_glance(p, NOW))
    rows = [{"label": "Nifty 50", "value": 25030.35, "value_text": "25,030.35", "as_of": "2026-11-06", "chg_text": "+2.01% (1d)", "source": "Alice Blue"},
            {"label": "Brent", "value": None, "value_text": "n/a", "as_of": "", "chg_text": "", "source": "x"}]
    p.write_text(json.dumps({"generated": (NOW - timedelta(days=5)).isoformat(), "rows": rows}))
    assert "Not shown" in "\n".join(render_glance(p, NOW))
    p.write_text(json.dumps({"generated": (NOW - timedelta(hours=3)).isoformat(), "rows": rows}))
    out = "\n".join(render_glance(p, NOW))
    assert "| Nifty 50 | 25,030.35 | 2026-11-06 | +2.01% (1d) | Alice Blue |" in out and "Brent" not in out
