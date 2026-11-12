"""Free headline search through Google News RSS (no API quota): finds the SAME story at other outlets, decodes the link to the real page, reads it.

Used when an article cannot be read (blocked, robots.txt, paywalled, JavaScript-only) and for newsletter lines, which are only headlines.
Same safety rules as the model-based search: the candidate's title must share at least half of the headline's significant words, it must be a
different outlet, robots.txt is honoured, the page must pass the paywall/CAPTCHA checks, and its own title or first lines must match again.
The story is then credited to that outlet."""
from __future__ import annotations

import urllib.parse
from typing import Callable, Optional

from collectors.http import FetchError
from news import gnews
from news.articles import RobotsCache, _usable, extract_text
from news.feeds import parse_feed
from news.headline_search import host, page_title, sig_words


def candidates(title: str, http_get: Callable, limit: int = 8) -> list:
    words = sorted(sig_words(title), key=len, reverse=True)[:9]
    q = urllib.parse.quote(" ".join(words) + " when:3d")
    gnews._pace()
    raw = http_get(f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en")
    return parse_feed(raw, {"name": "gn-search", "mode": "gnews"})[:limit]


def find_alternative_gnews(title: str, original_url: str, robots: RobotsCache, http_get: Callable, min_chars: int = 150, max_chars: int = 12000,
                           min_overlap: float = 0.5, errors: Optional[list] = None) -> Optional[dict]:
    want = sig_words(title)
    if len(want) < 3:
        return None
    try:
        cands = candidates(title, http_get)
    except Exception as e:  # noqa: BLE001
        if errors is not None:
            errors.append(f"{type(e).__name__}: {e}")
        return None
    ranked = sorted(cands, key=lambda c: -len(want & sig_words(c.title)))
    for c in ranked:
        if len(want & sig_words(c.title)) / len(want) < min_overlap:
            continue
        url = gnews.decode(c.link, http_get)
        if not url or (original_url and host(url) == host(original_url)) or not robots.allowed(url):
            continue
        try:
            html = http_get(url).decode("utf-8", "replace")
        except FetchError:
            continue
        text = extract_text(html)
        if _usable(text, min_chars):
            continue
        if len(want & sig_words(page_title(html) + " " + text[:600])) / len(want) < min_overlap:
            continue
        return {"text": text[:max_chars], "url": url, "publisher": c.publisher or host(url)}
    return None
