"""Turns a Google News redirect link (news.google.com/rss/articles/<id>) into the publisher's real article URL, without a search API.

Old-style ids carry the URL inside the base64 text. Newer ids (they begin 'AU_yqL' once decoded) need one call to Google's own page endpoint
(the same call the Google News web page makes) with a signature read from the article page. Fails closed: any surprise returns None and the
article falls through to the next step (headline search). Not an official API, so it can stop working; the Doctor reports its success rate."""
from __future__ import annotations

import base64
import json
import re
import threading
import time
import urllib.parse
import urllib.request
from typing import Callable, Optional

from collectors.http import FetchError

BATCH = "https://news.google.com/_/DotsSplashUi/data/batchexecute"
_gap_lock = threading.Lock()
_next = [0.0]
GAP = 0.25                                   # seconds between calls, across all workers, so Google is not hammered


def article_id(link: str) -> Optional[str]:
    p = urllib.parse.urlparse(link)
    if "news.google.com" not in p.netloc:
        return None
    m = re.search(r"/(?:rss/)?articles/([^/?]+)", p.path)
    return m.group(1) if m else None


def offline_decode(gid: str) -> Optional[str]:
    """Old-style ids hold the URL directly. Returns None for new-style ids (they need the online step)."""
    try:
        raw = base64.urlsafe_b64decode(gid + "=" * (-len(gid) % 4)).decode("latin1")
    except Exception:  # noqa: BLE001
        return None
    pre, suf = b"\x08\x13\x22".decode("latin1"), b"\xd2\x01\x00".decode("latin1")
    if raw.startswith(pre):
        raw = raw[len(pre):]
    if raw.endswith(suf):
        raw = raw[: -len(suf)]
    if not raw:
        return None
    n = raw.encode("latin1")[0]
    raw = raw[2: n + 1] if n >= 0x80 else raw[1: n + 1]
    if raw.startswith("AU_yqL") or not raw.startswith("http"):
        return None
    return raw


def _post_form(url: str, data: str, timeout: int = 25) -> str:
    req = urllib.request.Request(url, data=data.encode(), method="POST", headers={
        "content-type": "application/x-www-form-urlencoded;charset=UTF-8", "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _pace() -> None:
    with _gap_lock:
        now = time.monotonic()
        at = max(now, _next[0])
        _next[0] = at + GAP
    if at > now:
        time.sleep(at - now)


def decode(link: str, http_get: Callable, post: Optional[Callable] = None) -> Optional[str]:
    gid = article_id(link)
    if not gid:
        return None
    direct = offline_decode(gid)
    if direct:
        return direct
    post = post or _post_form
    try:
        _pace()
        page = http_get(f"https://news.google.com/rss/articles/{gid}").decode("utf-8", "replace")
        sg = re.search(r'data-n-a-sg="([^"]+)"', page)
        ts = re.search(r'data-n-a-ts="([^"]+)"', page)
        if not sg or not ts:
            return None
        inner = ('["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,null,0,1],"X","X",1,[1,1,1],1,1,null,0,0,null,0],'
                 f'"{gid}",{int(ts.group(1))},"{sg.group(1)}"]')
        body = "f.req=" + urllib.parse.quote(json.dumps([[["Fbv4je", inner, None, "generic"]]]))
        _pace()
        resp = post(BATCH, body)
        parsed = json.loads(resp.split("\n\n")[1])[:-2]
        url = json.loads(parsed[0][2])[1]
    except (FetchError, OSError, ValueError, IndexError, KeyError, TypeError):
        return None
    if isinstance(url, str) and url.startswith("http") and "news.google.com" not in urllib.parse.urlparse(url).netloc:
        return url
    return None
