"""Newsletters that arrive in your Gmail inbox become extra news items.

They are ADDITIONAL: they get their own reading quota (limits.newsletter_reads), on top of the feed quotas, and an item that duplicates a
feed story is dropped. Each linked headline in the email is followed to the publisher's page (tracking links are resolved; if the real
address cannot be found the item is dropped, so no personal tracking link ever appears in a report). The article is then read and verified
like any other; if the page cannot be read, the newsletter's own blurb is used (feed-summary rule). Reads mail over IMAP, read-only.

Needs GMAIL_USER and GMAIL_APP_PASSWORD (a Google app password, not your normal password) and an entry under `newsletters:` in config."""
from __future__ import annotations

import email
import hashlib
import email.policy
import imaplib
import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, Optional

from bs4 import BeautifulSoup

from news.feeds import Item, norm_title

SKIP_WORDS = ("unsubscribe", "preferences", "view in browser", "view online", "view this email", "manage subscription", "privacy", "terms of",
              "forward to a friend", "advertise", "sponsor", "download the app", "follow us", "share this", "sign up", "subscribe")
SKIP_HOSTS = ("facebook.com", "twitter.com", "x.com", "linkedin.com", "instagram.com", "youtube.com", "t.me", "wa.me", "whatsapp.com")
TRACK = re.compile(r"^(utm_|fbclid|gclid|mc_|mkt_tok|_hs|ref$|source$|cmpid|ncid|sid$|uid$|token|email|subscriber)", re.I)
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def parse_newsletter(html: str, max_links: int = 60) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style"]):
        t.decompose()
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        text, href = _clean(a.get_text(" ")), a["href"].strip()
        low = (text + " " + href).lower()
        if not href.startswith("http") or len(text) < 25 or len(text.split()) < 4:
            continue
        if any(w in low for w in SKIP_WORDS) or any(h in urllib.parse.urlparse(href).netloc.lower() for h in SKIP_HOSTS):
            continue
        k = norm_title(text)
        if k in seen:
            continue
        seen.add(k)
        block = a.find_parent(["p", "li", "td", "div"])
        blurb = _clean(block.get_text(" ")) if block else ""
        if len(blurb) < len(text) + 30 and block is not None:               # headline alone in its cell: the blurb is the next paragraph
            nxt = block.find_next(["p", "td", "li", "div"])
            while nxt is not None and nxt.find(["td", "p", "li", "div"]):          # skip wrappers: want the innermost next block
                nxt = nxt.find_next(["p", "td", "li", "div"])
            blurb = _clean(nxt.get_text(" ")) if nxt is not None else ""
            if nxt is not None and any(len(_clean(x.get_text(" "))) >= 25 for x in nxt.find_all("a")):
                blurb = ""                                                        # that is the next headline, not a blurb
        else:
            blurb = _clean(blurb.replace(text, "", 1))
        out.append({"title": text, "url": href, "blurb": blurb[:600]})
        if len(out) >= max_links:
            break
    return out


BOILER = ("read online", "together with", "powered by", "learn more", "subscribe", "sponsored", "advertis", "unsubscribe", "view in browser", "refer a friend",
          "forward this", "share this", "follow us", "click here", "sign up", "promo", "partner", "©", "all rights reserved", "mailing address", "privacy policy",
          "what we're watching", "full calendar", "let's dive in", "good morning", "presented by", "brought to you")
TRACK_HOSTS = ("beehiiv.com", "list-manage.com", "mailchi.mp", "substack.com/redirect", "sendgrid.net", "mandrillapp.com", "click.", "links.")


def parse_lines(html: str, max_lines: int = 70) -> list[dict]:
    """News written as short lines (one fact or headline per bullet or paragraph), as in beehiiv-style newsletters. Each leaf block of 8+ words becomes a line;
    the first link inside it (usually an outlet tag such as WSJ or FT) is kept in case it can be followed."""
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style"]):
        t.decompose()
    blocks, out, seen = ["p", "li", "td", "div", "h1", "h2", "h3"], [], set()
    for el in soup.find_all(blocks):
        if el.find(blocks):
            continue
        text = _clean(el.get_text(" ")).replace("\u200c", "").replace("\u200b", "").strip()
        text = re.sub(r"\s+", " ", text)
        if not (45 <= len(text) <= 700) or len(text.split()) < 8 or any(w in text.lower() for w in BOILER):
            continue
        k = norm_title(text[:120])
        if k in seen:
            continue
        seen.add(k)
        first = re.split(r"(?<=[.!?])\s+", text)[0]
        link = next((a["href"] for a in el.find_all("a", href=True) if a["href"].startswith("http")), "")
        out.append({"title": first[:200], "url": link, "blurb": text})
        if len(out) >= max_lines:
            break
    return out


def clean_url(u: str) -> str:
    p = urllib.parse.urlparse(u)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True) if not TRACK.match(k)]
    return urllib.parse.urlunparse(p._replace(query=urllib.parse.urlencode(q), fragment=""))


def resolve_url(url: str, timeout: int = 15) -> Optional[str]:
    """Follow redirects (tracking links) to the real page without downloading it. None if it cannot be resolved."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            final = r.geturl()
    except Exception:  # noqa: BLE001
        return None
    if not final.startswith("http"):
        return None
    return clean_url(final)


def _html_of(msg) -> str:
    body = msg.get_body(preferencelist=("html", "plain"))
    if body is None:
        return ""
    text = body.get_content()
    return text if body.get_content_type() == "text/html" else "<p>" + text.replace("\n\n", "</p><p>") + "</p>"


def fetch_newsletters(specs: list[dict], secrets: dict, now: datetime, window_start: datetime,
                      imap_factory: Callable = imaplib.IMAP4_SSL, resolver: Callable = resolve_url) -> tuple[list[Item], list[dict]]:
    items: list[Item] = []
    coverage: list[dict] = []
    if not specs:
        return items, coverage
    user, pw = secrets.get("GMAIL_USER"), secrets.get("GMAIL_APP_PASSWORD")
    if not user or not pw:
        return items, [{"feed": f"Newsletter: {s['name']}", "ok": False, "reason": "GMAIL_USER / GMAIL_APP_PASSWORD not set", "kept": 0} for s in specs]
    try:
        M = imap_factory("imap.gmail.com")
        M.login(user, pw)
        M.select("INBOX", readonly=True)
    except Exception as e:  # noqa: BLE001
        why = f"Gmail login failed ({type(e).__name__}); check the app password and that IMAP is on"
        return items, [{"feed": f"Newsletter: {s['name']}", "ok": False, "reason": why, "kept": 0} for s in specs]
    try:
        for s in specs:
            label = f"Newsletter: {s['name']}"
            try:
                since = window_start.astimezone(timezone.utc).strftime("%d-%b-%Y")
                typ, data = M.search(None, "SINCE", since, "FROM", f'"{s["from"]}"')
                ids = (data[0] or b"").split() if typ == "OK" else []
                cand: list[tuple[dict, datetime, str]] = []
                for mid in ids[-10:]:
                    typ, md = M.fetch(mid, "(RFC822)")
                    if typ != "OK" or not md or not md[0]:
                        continue
                    msg = email.message_from_bytes(md[0][1], policy=email.policy.default)
                    sent = parsedate_to_datetime(str(msg["Date"])).astimezone(timezone.utc)
                    if sent < window_start or sent > now:
                        continue
                    if s.get("subject_contains") and s["subject_contains"].lower() not in str(msg["Subject"]).lower():
                        continue
                    parser = parse_lines if s.get("style") == "lines" else parse_newsletter
                    for link in parser(_html_of(msg)):
                        cand.append((link, sent, str(msg["Subject"])))
                def _res(c):
                    u = c[0]["url"]
                    if not u or any(t in urllib.parse.urlparse(u).netloc for t in TRACK_HOSTS):
                        return None                                     # trackers refuse automated clients; do not even try
                    return resolver(u)
                with ThreadPoolExecutor(max_workers=8) as ex:
                    real = list(ex.map(_res, cand))
                kept = unresolved = 0
                lines_style = s.get("style") == "lines"
                site = s.get("site") or "https://" + s["from"].split("@")[-1] + "/"
                for (link, sent, _subj), url in zip(cand, real):
                    if not url and not lines_style:
                        unresolved += 1                                     # never publish a tracking link
                        continue
                    if url:
                        url = clean_url(url)
                        it = Item(label, s.get("category", "world"), link["title"], url, sent, link["blurb"], bool(s.get("paywalled")))
                        it.origin_url = url
                    else:                                                   # a line with no followable link: only a headline; it is searched for elsewhere
                        it = Item(label, s.get("category", "world"), link["title"], f"{site.rstrip('/')}/#nl-{hashlib.md5(norm_title(link['title']).encode()).hexdigest()[:8]}", sent, link["blurb"])
                        it.headline_only = True
                    it.publisher, it.feed_region, it.extra = s["name"], s.get("region", ""), True
                    items.append(it)
                    kept += 1
                coverage.append({"feed": label, "ok": True, "kept": kept,
                                 "reason": f"{len(ids)} email(s) found" + (f"; {unresolved} link(s) dropped (real address not found)" if unresolved else "")})
            except Exception as e:  # noqa: BLE001
                coverage.append({"feed": label, "ok": False, "reason": f"{type(e).__name__}: {e}"[:160], "kept": 0})
    finally:
        try:
            M.logout()
        except Exception:  # noqa: BLE001
            pass
    return items, coverage
