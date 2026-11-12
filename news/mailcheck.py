"""Checks the Gmail side of the newsletter feature without touching anything else.   python3 -m news.mailcheck

Logs in (read-only), and for each newsletter in config/news_sources.yaml reports: how many emails matched the sender in the last 7 days, their dates and
subjects, how many headline links were found in each, and whether the first few links resolve to a real page. It prints nothing about any other email,
because this runs on a public repository."""
from __future__ import annotations

import email
import email.policy
import imaplib
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import yaml

from core.secrets import load_secrets, redact
from news.newsletter import _html_of, parse_newsletter, resolve_url

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    cfg = yaml.safe_load((ROOT / "config" / "news_sources.yaml").read_text())
    sec = load_secrets()
    specs = cfg.get("newsletters") or []
    if not (sec.get("GMAIL_USER") and sec.get("GMAIL_APP_PASSWORD")):
        print("FAIL: GMAIL_USER / GMAIL_APP_PASSWORD not set")
        return 0
    try:
        M = imaplib.IMAP4_SSL("imap.gmail.com")
        M.login(sec["GMAIL_USER"], sec["GMAIL_APP_PASSWORD"])
        M.select("INBOX", readonly=True)
        print("OK: Gmail login works")
    except Exception as e:  # noqa: BLE001
        print("FAIL: Gmail login:", redact(f"{type(e).__name__}: {e}", sec))
        return 0
    since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%d-%b-%Y")
    for s in specs:
        key = s["from"]
        for label, needle in (("configured sender", key), ("sender name contains", key.split("@")[-1].split(".")[0])):
            typ, data = M.search(None, "SINCE", since, "FROM", f'"{needle}"')
            ids = (data[0] or b"").split() if typ == "OK" else []
            print(f"{s['name']}: {len(ids)} email(s) in the last 7 days from {label} '{needle}'")
            if ids and label == "configured sender":
                break
        for mid in ids[-3:]:
            typ, md = M.fetch(mid, "(RFC822)")
            msg = email.message_from_bytes(md[0][1], policy=email.policy.default)
            sent = parsedate_to_datetime(str(msg["Date"])).astimezone(timezone.utc)
            links = parse_newsletter(_html_of(msg))
            ok = sum(1 for l in links[:5] if resolve_url(l["url"]))
            print(f"  - {sent:%d %b %H:%M} UTC | from: {str(msg['From'])[:60]} | subject: {str(msg['Subject'])[:80]} | headline links found: {len(links)} | first 5 resolved: {ok}/5")
            for l in links[:3]:
                print(f"      · {l['title'][:90]} | blurb {len(l['blurb'])} chars")
    M.logout()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
