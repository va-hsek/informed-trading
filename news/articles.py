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


def read_article(item, robots: RobotsCache, http_get: Callable, http_json: Callable,
                 min_chars: int = 600, max_chars: int = 12000) -> dict:
    """Returns {text, via, snapshot_date, reason}. via is 'direct', 'wayback', 'archive.today', 'feed summary' or None."""
    out = {"text": "", "via": None, "snapshot_date": None, "reason": ""}
    if item.headline_only:
        out["reason"] = "headline only (link is a search redirect, article text not readable)"
        return out
    url = item.origin_url or item.link
    why = ""
    if robots.allowed(url):
        try:
            text = extract_text(http_get(url).decode("utf-8", "replace"))
            why = looks_paywalled(text, min_chars) or ""
            if not why:
                out.update(text=text[:max_chars], via="direct")
                return out
        except FetchError as e:
            why = f"page fetch failed ({e})"
    else:
        why = "robots.txt disallows automated access"
    if item.paywalled or "paywall" in why or "characters" in why:
        try:
            snap = wayback_snapshot(url, item.published, http_json)
            if snap:
                text = extract_text(http_get(wayback_raw_url(snap[0])).decode("utf-8", "replace"))
                w = looks_paywalled(text, min_chars)
                if not w:
                    out.update(text=text[:max_chars], via="wayback", snapshot_date=snap[1].date().isoformat())
                    return out
                why += f"; Wayback copy unusable ({w})"
            else:
                why += "; no suitable Wayback snapshot"
        except FetchError as e:
            why += f"; Wayback lookup failed ({e})"
    if (item.paywalled or "paywall" in why) and getattr(robots, "archive_fails", 0) < ARCHIVE_MAX_FAILS:
        aurl = ARCHIVE_TODAY + url
        try:
            if not robots.allowed(aurl):
                raise FetchError("archive.today robots.txt disallows automated access")
            text = extract_text(http_get(aurl).decode("utf-8", "replace"))
            w = looks_paywalled(text, min_chars)
            if w or "captcha" in text[:400].lower() or "verify you are human" in text[:400].lower():
                raise FetchError(w or "CAPTCHA page")
            out.update(text=text[:max_chars], via="archive.today", snapshot_date=None)
            robots.archive_fails = 0
            return out
        except FetchError as e:
            robots.archive_fails = getattr(robots, "archive_fails", 0) + 1
            why += f"; archive.today unusable ({e})"
    if item.summary and len(item.summary) >= 120:
        out.update(text=item.summary[:max_chars], via="feed summary", reason=why)
        return out
    out["reason"] = why or "no readable text"
    return out
