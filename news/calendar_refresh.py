"""Weekly, date-driven refresh of config/calendar.yaml.

    python3 -m news.calendar_refresh          # also runs automatically from the news report when the calendar is >7 days old

Steps: ask the model (with web search) for scheduled, market-moving events from today to today+28 days across India, the US,
the euro area/UK, China/Japan, central banks, OPEC/EIA and major earnings; then CODE verifies each one:
  - the date must parse and fall inside the window (past events are dropped, by date);
  - the source page is fetched and the quoted evidence must appear in it word for word;
  - every number in `expectation` must appear in that page, otherwise the expectation is replaced by 'n/a' and the event is
    marked verified: false.
Existing future events that were not re-found are kept (marked carried_over) so a thin search never erases a good calendar.
last_reviewed is advanced only when at least one event was verified. Nothing here is a forecast of our own.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

import yaml

from collectors.http import FetchError, get
from news.articles import extract_text
from news.claims import norm, numbers
from news.llm import LLM

IST = timezone(timedelta(hours=5, minutes=30))
ROOT = Path(__file__).resolve().parent.parent
WINDOW_DAYS = 28
HEADER = """# Upcoming-events calendar shown in the news report ("Week ahead").
# Maintained by news/calendar_refresh.py (weekly, date-driven). You may edit by hand; hand-written entries marked `manual: true` are never removed.
# last_reviewed: the date the list was last refreshed and verified. The report warns when it is older than 7 days.
# date (IST), time_ist (optional), title, expectation (only what a named source states), affects, source_name + source_url,
# verified: true only when the source page was fetched and the evidence excerpt was found in it.
# `at` (ISO-8601 with offset) and `affects_assets` (crude/natgas/gold/silver) are optional: events that have them also warn in the ACTION report.
"""

SYSTEM = (
    "You compile a calendar of SCHEDULED, market-moving events for an Indian retail trader who follows India, the USA and the euro area most, "
    "and energy and metals. Use web search. Cover: US data (CPI, PPI, jobs, retail sales, GDP, PCE, Fed meetings and speeches), Indian data and "
    "RBI/SEBI/NSE holidays and big Indian earnings, ECB/BoE/euro-area data, China and Japan data and central banks, OPEC/EIA/IEA releases, big "
    "US/EU/Indian company earnings, major summits or sanctions deadlines. Only include events that a web page you found states, with its date. "
    "Return ONLY JSON: {\"events\":[{\"date\":\"YYYY-MM-DD\",\"time_ist\":\"7:00 pm\" or \"\",\"title\":str,\"expectation\":str,"
    "\"affects\":[str],\"affects_assets\":[crude|natgas|gold|silver],\"source_name\":str,\"source_url\":str,\"evidence\":str}]}. "
    "`evidence` is an exact excerpt (5-40 words) copied from the page that shows the event and its date. `expectation` states only forecasts the page "
    "gives (with the numbers); use \"n/a\" if none. Never invent a date, a forecast or a URL."
)


def _slug(t: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{4,}", norm(t))}


def _same(a: dict, b: dict) -> bool:
    if str(a.get("date")) != str(b.get("date")):
        return False
    wa, wb = _slug(a["title"]), _slug(b["title"])
    return bool(wa and wb and len(wa & wb) / min(len(wa), len(wb)) >= 0.5)


def parse_events(raw: str) -> list[dict]:
    i, j = raw.find("{"), raw.rfind("}")
    if i < 0 or j <= i:
        raise ValueError("no JSON object in model output")
    ev = json.loads(raw[i: j + 1]).get("events")
    if not isinstance(ev, list):
        raise ValueError("'events' is not a list")
    return [e for e in ev if isinstance(e, dict)]


def verify_event(e: dict, today: date, http_get: Callable, page_cache: dict) -> tuple[Optional[dict], str]:
    """Returns (clean event or None, reason)."""
    try:
        d = date.fromisoformat(str(e["date"]))
        title = str(e["title"]).strip()
        url = str(e["source_url"]).strip()
    except Exception:  # noqa: BLE001
        return None, "missing or unparseable date/title/source_url"
    if not title or not url.startswith("http"):
        return None, "empty title or non-http source"
    if not (today <= d <= today + timedelta(days=WINDOW_DAYS)):
        return None, f"date {d} outside the window"
    out = {"date": d.isoformat(), "title": title, "expectation": str(e.get("expectation") or "n/a").strip() or "n/a",
           "affects": [str(x) for x in (e.get("affects") or [])][:8], "source_name": str(e.get("source_name") or "").strip(),
           "source_url": url, "verified": False}
    if e.get("time_ist"):
        out["time_ist"] = str(e["time_ist"]).strip()
    assets = [a for a in (e.get("affects_assets") or []) if a in ("crude", "natgas", "gold", "silver")]
    try:
        if url not in page_cache:
            page_cache[url] = norm(extract_text(http_get(url).decode("utf-8", "replace")))
        page = page_cache[url]
        ev = str(e.get("evidence") or "")
        ok = 5 <= len(ev.split()) <= 40 and norm(ev) in page
        if ok:
            missing = numbers(out["expectation"]) - numbers(page)
            if missing:
                out["expectation"] = "n/a"
                return out, f"kept, expectation dropped: numbers {sorted(missing)} not on the source page"
            out["verified"] = True
            if assets and e.get("at"):
                try:
                    at = datetime.fromisoformat(str(e["at"]))
                    if at.tzinfo is not None and at.astimezone(IST).date() == d:
                        out["at"], out["affects_assets"] = at.isoformat(), assets
                except ValueError:
                    pass
            return out, "verified"
        out["expectation"] = "n/a"
        return out, "kept unverified: evidence excerpt not found on the source page"
    except FetchError as ex:
        out["expectation"] = "n/a"
        return out, f"kept unverified: source page unreadable ({ex})"


def merge(existing: list[dict], fresh: list[dict], today: date) -> list[dict]:
    out = [dict(e) for e in fresh]
    for old in existing:
        try:
            d = date.fromisoformat(str(old["date"]))
        except Exception:  # noqa: BLE001
            continue
        if d < today:
            continue                                            # past by date: dropped
        if any(_same(old, f) for f in out):
            continue
        if old.get("manual") or d <= today + timedelta(days=WINDOW_DAYS):
            old = dict(old)
            old.setdefault("carried_over", True)
            out.append(old)
    out.sort(key=lambda e: (str(e["date"]), e.get("time_ist", "")))
    return out


def refresh_calendar(root: Path, now: datetime, llm: LLM, tool_type: str, http_get: Callable = get) -> dict:
    path = root / "config" / "calendar.yaml"
    cur = (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}
    today = now.astimezone(IST).date()
    user = (f"Today is {today:%A %d %B %Y} (IST). Find scheduled events from {today} to {today + timedelta(days=WINDOW_DAYS)}. "
            "Aim for 25-45 events, weighting India, the USA and the euro area.")
    raw = llm.complete_with_search(SYSTEM, user, tool_type, max_uses=15, max_tokens=6000)
    cands = parse_events(raw)
    cache: dict = {}
    fresh, notes = [], []
    for e in cands:
        ev, why = verify_event(e, today, http_get, cache)
        if ev is None:
            notes.append(f"dropped: {why}")
            continue
        if why != "verified":
            notes.append(f"{ev['title']}: {why}")
        fresh.append(ev)
    nver = sum(1 for e in fresh if e["verified"])
    events = merge(cur.get("events") or [], fresh, today)
    reviewed = today.isoformat() if nver > 0 else str(cur.get("last_reviewed") or "")
    path.write_text(HEADER + yaml.safe_dump({"last_reviewed": reviewed, "events": events}, sort_keys=False, allow_unicode=True, width=200))
    return {"candidates": len(cands), "kept": len(fresh), "verified": nver, "total_events": len(events), "notes": notes[:12],
            "last_reviewed": reviewed}


def is_stale(root: Path, now: datetime, max_age_days: int = 7) -> bool:
    p = root / "config" / "calendar.yaml"
    if not p.exists():
        return True
    lr = (yaml.safe_load(p.read_text()) or {}).get("last_reviewed")
    try:
        return (now.astimezone(IST).date() - date.fromisoformat(str(lr))).days > max_age_days
    except Exception:  # noqa: BLE001
        return True


def ensure_fresh(root: Path, now: datetime, secrets: dict, cfg: dict, llm: Optional[LLM] = None,
                 http_get: Callable = get) -> str:
    """Called by the news run. Returns a note for the report ('' when nothing to say). Never raises."""
    max_age = int(cfg["limits"].get("calendar_review_max_age_days", 7))
    if not is_stale(root, now, max_age):
        return ""
    cc = cfg.get("calendar_refresh") or {}
    if llm is None:
        if not secrets.get("ANTHROPIC_API_KEY"):
            return "Calendar is out of date and ANTHROPIC_API_KEY is not set, so it was not refreshed."
        llm = LLM(secrets["ANTHROPIC_API_KEY"], cc.get("model", "claude-sonnet-5-5"), root / "data" / "llm_usage.jsonl",
                  float(cc.get("price_input_per_mtok", 3.0)), float(cc.get("price_output_per_mtok", 15.0)))
    try:
        r = refresh_calendar(root, now, llm, cc.get("search_tool_type", "web_search_20250305"), http_get)
        return f"Calendar refreshed automatically: {r['verified']} of {r['kept']} events verified against their source pages."
    except Exception as e:  # noqa: BLE001
        return f"Calendar refresh failed ({type(e).__name__}); the old calendar is shown with its review warning."


def main() -> int:
    from core.secrets import load_secrets
    now = datetime.now(IST)
    cfg = yaml.safe_load((ROOT / "config" / "news_sources.yaml").read_text())
    s = load_secrets()
    if not s.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set in config/secrets.env")
        return 1
    cc = cfg.get("calendar_refresh") or {}
    llm = LLM(s["ANTHROPIC_API_KEY"], cc.get("model", "claude-sonnet-5-5"), ROOT / "data" / "llm_usage.jsonl",
              float(cc.get("price_input_per_mtok", 3.0)), float(cc.get("price_output_per_mtok", 15.0)))
    r = refresh_calendar(ROOT, now, llm, cc.get("search_tool_type", "web_search_20250305"))
    print(json.dumps(r, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
