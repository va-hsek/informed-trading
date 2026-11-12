"""RSS/Atom parsing and Google News search feeds. Network is injected, so this is testable offline."""
from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, Optional

try:
    from defusedxml import ElementTree as ET
except ImportError:                                    # pragma: no cover
    import xml.etree.ElementTree as ET

from collectors.http import FetchError

GNEWS = "https://news.google.com/rss/search"


@dataclass
class Item:
    source: str
    category: str
    title: str
    link: str
    published: Optional[datetime]
    summary: str = ""
    paywalled: bool = False
    headline_only: bool = False          # True when the article text cannot be read (Google News links)
    publisher: str = ""                  # real publisher for Google News items
    origin_url: str = ""                 # the original article URL when known (not a redirect)
    topics: list[str] = field(default_factory=list)
    regions: list[str] = field(default_factory=list)
    feed_region: str = ""
    sectors: list[str] = field(default_factory=list)
    extra: bool = False                  # True for newsletter items: read on top of the normal quotas


def _clean(s: Optional[str]) -> str:
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    s = (s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'")
         .replace("&lt;", "<").replace("&gt;", ">"))
    return re.sub(r"\s+", " ", s).strip()


def _tzinfo(offset: Optional[str]):
    if not offset:
        return timezone.utc
    sign = -1 if offset.startswith("-") else 1
    hh, mm = offset.lstrip("+-").split(":")
    return timezone(sign * timedelta(hours=int(hh), minutes=int(mm)))


def _date(s: Optional[str], assume: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    s = s.strip()
    dt = None
    try:
        dt = parsedate_to_datetime(s)
    except Exception:  # noqa: BLE001
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:  # noqa: BLE001
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_tzinfo(assume))
    return dt.astimezone(timezone.utc)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text_of(fields: dict, *names: str) -> Optional[str]:
    """First non-empty text among the named child elements. (Never test an Element's truth value:
    an element without children is falsy even when it has text.)"""
    for n in names:
        el = fields.get(n)
        if el is not None and el.text and el.text.strip():
            return el.text
    return None


def parse_feed(xml: bytes, src: dict) -> list[Item]:
    try:
        root = ET.fromstring(xml)
    except Exception as e:  # noqa: BLE001
        raise FetchError(f"{src['name']}: not valid XML ({type(e).__name__})") from e
    kind = _local(root.tag).lower()
    if kind not in ("rss", "feed", "rdf"):
        raise FetchError(f"{src['name']}: unexpected root element <{kind}>")
    items: list[Item] = []
    for el in root.iter():
        t = _local(el.tag)
        if t not in ("item", "entry"):
            continue
        f = {_local(c.tag): c for c in el}
        title = _clean(f["title"].text) if "title" in f else ""
        link = ""
        if "link" in f:
            link = (f["link"].get("href") or f["link"].text or "").strip()
        if not title or not link:
            continue
        pub = _date(_text_of(f, "pubDate", "published", "updated", "date"), src.get("assume_tz"))
        summ = _clean(_text_of(f, "description", "summary", "content"))
        it = Item(src["name"], src.get("category", "world"), title, link, pub, summ, bool(src.get("paywalled")))
        it.feed_region = src.get("region", "")
        if src.get("mode") == "gnews":
            it.headline_only = True
            pub_el = f.get("source")
            it.publisher = _clean(pub_el.text) if pub_el is not None else ""
            if it.publisher and title.endswith(" - " + it.publisher):
                it.title = title[: -len(" - " + it.publisher)].strip()
            it.summary = ""                                    # Google's snippet is just the title again
        else:
            it.origin_url = link
        items.append(it)
    if not items:
        raise FetchError(f"{src['name']}: feed parsed but contains no items")
    return items


def feed_url(src: dict) -> str:
    if src.get("mode") == "gnews":
        q = urllib.parse.urlencode({"q": src["query"], "hl": "en-IN", "gl": "IN", "ceid": "IN:en"})
        return f"{GNEWS}?{q}"
    return src["url"]


def norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", t.lower()).strip()


IST = timezone(timedelta(hours=5, minutes=30))


def edition_window(now: datetime, anchor: str = "05:30", hours: float = 24) -> datetime:
    """Start of the news window, in UTC: `hours` before the most recent `anchor` o'clock (IST).

    Run at 05:30 IST -> the last 24 hours. Run at 11:39 IST -> from 05:30 IST yesterday (30 hours)."""
    hh, mm = (int(x) for x in anchor.split(":"))
    n = now.astimezone(IST)
    a = n.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if n < a:
        a -= timedelta(days=1)
    return (a - timedelta(hours=hours)).astimezone(timezone.utc)


def collect_items(feeds: list[dict], now: datetime, http_get: Callable, window_start: datetime, max_per_feed: int):
    """Returns (items, coverage). Only items PUBLISHED inside [window_start, now] are kept; undated items are dropped
    and counted, because their age cannot be checked."""
    items: list[Item] = []
    coverage: list[dict] = []
    seen_titles, seen_links = set(), set()
    for src in feeds:
        try:
            got = parse_feed(http_get(feed_url(src)), src)
        except FetchError as e:
            coverage.append({"feed": src["name"], "ok": False, "reason": str(e), "kept": 0})
            continue
        kept = undated = 0
        for it in got[:max_per_feed]:
            if it.published is None:
                undated += 1
                continue
            if it.published < window_start:
                continue
            if it.published > now + timedelta(hours=2):
                continue                                       # future-dated: ignore
            k = norm_title(it.title)
            if k in seen_titles or it.link in seen_links:
                continue
            seen_titles.add(k)
            seen_links.add(it.link)
            items.append(it)
            kept += 1
        coverage.append({"feed": src["name"], "ok": True, "reason": (f"{undated} undated item(s) dropped" if undated else ""), "kept": kept})
    return items, coverage


def tag_topics(item: Item, topics: dict[str, list[str]]) -> list[str]:
    text = f"{item.title} {item.summary}".lower()
    out = []
    for name, kws in topics.items():
        for kw in kws:
            if re.search(rf"(?<![a-z0-9]){re.escape(kw.lower())}(?![a-z0-9])", text):
                out.append(name)
                break
    return out
