"""Reading articles: robots.txt respected, paywall detection, Wayback Machine, then archive.today.

Order for a paywalled page: direct -> Wayback snapshot -> archive.today newest snapshot -> feed summary.
archive.today has no API and often answers bots with a CAPTCHA. We make ONE polite request per article,
honour its robots.txt, treat a CAPTCHA/short page as a failure (fail closed), and stop trying for the rest of the
run after 3 failures in a row. We never rotate mirrors or disguise the client. A paywalled item that stays unread
is printed as a headline with a manual archive link.
"""
from __future__ import annotations

import re
import urllib.parse
import urllib.robotparser
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from bs4 import BeautifulSoup

from news import gnews
from collectors.http import FetchError

BOT_NAME = "informed-trading"
PAYWALL_MARKERS = ["subscribe to read", "subscribers only", "subscribe to continue", "sign in to continue",
                   "already a subscriber", "create a free account to continue", "to continue reading",
                   "unlock this article", "this content is for subscribers"]
WAYBACK_API = "https://archive.org/wayback/available"
ARCHIVE_TODAY = "https://archive.ph/newest/"
ARCHIVE_MAX_FAILS = 3


class RobotsCache:
    def __init__(self, http_get: Callable):
        self.http_get, self.cache = http_get, {}

    def allowed(self, url: str) -> bool:
        p = urllib.parse.urlparse(url)
        host = f"{p.scheme}://{p.netloc}"
        if host not in self.cache:
            rp = urllib.robotparser.RobotFileParser()
            try:
                txt = self.http_get(f"{host}/robots.txt").decode("utf-8", "replace")
                rp.parse(txt.splitlines())
                self.cache[host] = rp
            except FetchError as e:
                # a missing robots.txt (404) means no restrictions; any other failure means we do not guess
                self.cache[host] = "open" if "HTTP 404" in str(e) else "closed"
        rp = self.cache[host]
        if rp == "open":
            return True
        if rp == "closed":
            return False
        return rp.can_fetch(BOT_NAME, url) and rp.can_fetch("*", url)


def extract_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "nav", "footer", "aside", "form", "header", "noscript", "figure"]):
        t.decompose()
    root = soup.find("article") or soup.find("main") or soup.body or soup
    paras = [re.sub(r"\s+", " ", p.get_text(" ", strip=True)) for p in root.find_all("p")]
    paras = [p for p in paras if len(p) >= 40]
    return "\n".join(paras)


def looks_paywalled(text: str, min_chars: int) -> Optional[str]:
    low = text.lower()
    for m in PAYWALL_MARKERS:
        if m in low:
            return f"paywall marker found ({m!r})"
    if len(text) < min_chars:
        return f"only {len(text)} characters of text (minimum {min_chars})"
    return None


def wayback_raw_url(snapshot_url: str) -> str:
    return re.sub(r"/web/(\d{14})/", r"/web/\1id_/", snapshot_url, count=1)


def wayback_snapshot(url: str, published: Optional[datetime], http_json: Callable) -> Optional[tuple[str, datetime]]:
    params = [("url", url)]
    if published:
        params.append(("timestamp", published.strftime("%Y%m%d")))
    data = http_json(WAYBACK_API, params)
    snap = ((data or {}).get("archived_snapshots") or {}).get("closest") or {}
    if not snap.get("available") or str(snap.get("status")) != "200":
        return None
    try:
        ts = datetime.strptime(snap["timestamp"], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        return None
    if published is not None and not (published - timedelta(days=1) <= ts <= published + timedelta(days=7)):
        return None                                   # a snapshot far from publication may be a different version
    return snap["url"], ts


MEMENTO_API = "https://timetravel.mementoweb.org/api/json/"
MIN_PARTIAL_CHARS = 150


def _usable(text: str, min_chars: int) -> Optional[str]:
    """None when text is usable. A CAPTCHA or block page is never usable."""
    low = text[:600].lower()
    if "captcha" in low or "verify you are human" in low or "access denied" in low or "enable javascript" in low:
        return "block or CAPTCHA page"
    return looks_paywalled(text, min_chars)


def memento_snapshots(url: str, published: Optional[datetime], http_json: Callable) -> list[str]:
    """Closest copy held by ANY web archive known to the Memento aggregator (Wayback, archive.today, Perma, national archives...)."""
    stamp = (published or datetime.now(timezone.utc)).strftime("%Y%m%d%H%M%S")
    data = http_json(MEMENTO_API + stamp + "/" + url, [])
    m = (data or {}).get("mementos") or {}
    uris = []
    for key in ("closest", "next", "prev"):
        u = (m.get(key) or {}).get("uri")
        for x in (u if isinstance(u, list) else [u]):
            if isinstance(x, str) and x.startswith("http") and x not in uris:
                uris.append(x)
    return uris[:3]


def read_article(item, robots: RobotsCache, http_get: Callable, http_json: Callable,
                 min_chars: int = MIN_PARTIAL_CHARS, max_chars: int = 12000) -> dict:
    """Returns {text, via, snapshot_date, reason, partial}.

    Order: the page itself -> Wayback -> any archive via Memento -> archive.today -> partial text / feed summary.
    Every failure (paywall, block, robots, error, short page) moves to the next step; nothing is skipped silently.
    via is 'direct', 'wayback', 'archive', 'archive.today', 'partial', 'feed summary' or None."""
    out = {"text": "", "via": None, "snapshot_date": None, "reason": "", "partial": False}
    if item.headline_only:
        real = None
        if gnews.article_id(item.link) is None:
            pass                                               # not a Google News link (e.g. a newsletter line): nothing to decode, and it must not trip the breaker
        elif getattr(robots, "gn_fails", 0) < 8:
            real = gnews.decode(item.link, http_get)
            if real is None:
                robots.gn_fails = getattr(robots, "gn_fails", 0) + 1
            else:
                robots.gn_fails = 0
        if not real:
            out["reason"] = "headline only (Google News link could not be turned into the publisher's address)"
            return out
        item.origin_url, item.headline_only = real, False          # from here on it is an ordinary article
    url = item.origin_url or item.link
    why = ""
    best = ""                                              # longest paywall-free text seen, kept as a last resort
    if robots.allowed(url):
        try:
            text = extract_text(http_get(url).decode("utf-8", "replace"))
            why = _usable(text, min_chars) or ""
            if not why:
                out.update(text=text[:max_chars], via="direct")
                return out
            if "paywall marker" not in why and len(text) >= MIN_PARTIAL_CHARS:
                best = text
        except FetchError as e:
            why = f"page fetch failed ({e})"
    else:
        why = "robots.txt disallows automated access"

    def attempt(label: str, fetch_url: str, via: str, snap: Optional[str] = None) -> bool:
        nonlocal why
        try:
            if not robots.allowed(fetch_url):
                why += f"; {label}: robots.txt disallows"
                return False
            text = extract_text(http_get(fetch_url).decode("utf-8", "replace"))
            w = _usable(text, min_chars)
            if w:
                why += f"; {label} unusable ({w})"
                return False
            out.update(text=text[:max_chars], via=via, snapshot_date=snap)
            return True
        except FetchError as e:
            why += f"; {label} failed ({e})"
            return False

    try:
        snap = wayback_snapshot(url, item.published, http_json)
        if snap:
            if attempt("Wayback", wayback_raw_url(snap[0]), "wayback", snap[1].date().isoformat()):
                return out
        else:
            why += "; no suitable Wayback snapshot"
    except FetchError as e:
        why += f"; Wayback lookup failed ({e})"
    try:
        if getattr(robots, "memento_fails", 0) >= ARCHIVE_MAX_FAILS:
            raise ValueError("Memento aggregator skipped after repeated failures")
        try:
            uris = memento_snapshots(url, item.published, http_json)
        except FetchError:
            robots.memento_fails = getattr(robots, "memento_fails", 0) + 1
            raise
        for uri in uris:
            if "web.archive.org" in uri:
                continue                                   # already tried above
            if "archive.ph" in uri or "archive.today" in uri or "archive.is" in uri:
                continue                                   # handled below with its own failure breaker
            if attempt("archive copy", uri, "archive"):
                return out
    except (FetchError, ValueError, AttributeError) as e:
        why += f"; archive lookup failed ({e})"
    if getattr(robots, "archive_fails", 0) < ARCHIVE_MAX_FAILS:
        if attempt("archive.today", ARCHIVE_TODAY + url, "archive.today"):
            robots.archive_fails = 0
            return out
        robots.archive_fails = getattr(robots, "archive_fails", 0) + 1
    if best:
        out.update(text=best[:max_chars], via="partial", partial=True, reason=why)
        return out
    if item.summary and len(item.summary) >= 60:
        out.update(text=item.summary[:max_chars], via="feed summary", partial=True, reason=why)
        return out
    out["reason"] = why or "no readable text"
    return out
