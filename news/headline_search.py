"""Last resort for an article that could not be read (paywalled, blocked, headline-only): search the web for the SAME story
from an outlet whose page IS readable, and use that instead. Never skip silently, never guess.

  1. The model, with web search, proposes up to 4 URLs that cover the same story. It is told to return URLs only.
  2. CODE fetches each URL (robots.txt honoured) and accepts it only if
       - it is not the same page, and
       - at least `min_overlap` of the headline's significant words appear in the page title or first 600 characters, and
       - the text passes the same paywall/CAPTCHA checks.
  3. The story is then attributed to THAT outlet (named and linked), with a note that it was found by headline search.
The ordinary claim guard (verbatim excerpt, numbers present) still applies to the text read.
"""
from __future__ import annotations

import json
import re
import urllib.parse
from typing import Callable, Optional

from bs4 import BeautifulSoup

from collectors.http import FetchError
from news.articles import RobotsCache, _usable, extract_text

SYSTEM = ("You find web pages. Use web search to find up to 4 different news articles, from different outlets, reporting the same story as the "
          "given headline, published within the last 3 days. Prefer pages that are free to read. Return ONLY JSON: "
          '{"urls":[{"url":str,"publisher":str}]}. Never invent a URL; only return URLs that appeared in search results.')

STOP = {"that", "this", "with", "from", "after", "have", "will", "says", "said", "over", "into", "amid", "more", "than", "their", "about", "could", "would"}


def sig_words(t: str) -> set[str]:
    return {w[:-1] if w.endswith("s") else w for w in re.findall(r"[a-z]{4,}", t.lower()) if w not in STOP}


def page_title(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    h = soup.find("h1")
    return (soup.title.get_text(" ", strip=True) if soup.title else "") + " " + (h.get_text(" ", strip=True) if h else "")


def host(u: str) -> str:
    return urllib.parse.urlparse(u).netloc.lower().removeprefix("www.")


def parse_urls(raw: str) -> list[dict]:
    i, j = raw.find("{"), raw.rfind("}")
    if i < 0 or j <= i:
        return []
    try:
        arr = json.loads(raw[i: j + 1]).get("urls") or []
    except ValueError:
        return []
    return [u for u in arr if isinstance(u, dict) and str(u.get("url", "")).startswith("http")][:4]


def find_alternative(title: str, original_url: str, llm, tool_type: str, robots: RobotsCache, http_get: Callable,
                     min_chars: int = 150, max_chars: int = 12000, min_overlap: float = 0.5) -> Optional[dict]:
    """Returns {text, url, publisher} or None."""
    want = sig_words(title)
    if len(want) < 3:
        return None
    try:
        raw = llm.complete_with_search(SYSTEM, f"HEADLINE: {title}", tool_type, max_uses=2, max_tokens=500)
    except Exception:  # noqa: BLE001
        return None
    for cand in parse_urls(raw):
        u = cand["url"]
        if host(u) == host(original_url) or not robots.allowed(u):
            continue
        try:
            html = http_get(u).decode("utf-8", "replace")
        except FetchError:
            continue
        text = extract_text(html)
        if _usable(text, min_chars):
            continue
        have = sig_words(page_title(html) + " " + text[:600])
        if len(want & have) / len(want) < min_overlap:
            continue
        return {"text": text[:max_chars], "url": u, "publisher": str(cand.get("publisher") or host(u))}
    return None
