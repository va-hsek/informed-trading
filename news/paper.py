"""Newspaper layout: stories made only of verified claims, paragraphs ending in hyperlinked source names,
a week-ahead calendar, and a markets-at-a-glance strip. Pure functions, no network."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from news.claims import norm, numbers

IST = timezone(timedelta(hours=5, minutes=30))
SECTIONS = ["Front page", "India", "USA and Americas", "Europe", "Markets and macro", "Energy and metals", "Sectors", "Tech and AI",
            "Deals and corporate", "Asia-Pacific", "Middle East", "Official releases"]
REGION_SECTION = {"india": "India", "apac": "Asia-Pacific", "europe": "Europe", "americas": "USA and Americas", "middleeast": "Middle East"}
TOPIC_SCORE = {"crude": 5, "natgas": 5, "gold": 5, "silver": 5, "rates_fed": 4, "usd_inr": 4, "defence": 3,
               "india": 3, "geopolitics": 3, "deals": 3, "tech": 2}


@dataclass
class Source:
    name: str
    url: str
    via: str = "direct"             # direct | wayback | archive | archive.today | partial | feed summary | headline search
    snap: Optional[str] = None
    archive: str = ""               # manual archive link for paywalled items
    orig: str = ""                  # outlet of the unreadable original, when the story was found by headline search


@dataclass
class Para:
    claims: list[str]
    source: Source


@dataclass
class Story:
    headline: str
    section: str
    topics: list[str]
    regions: list[str]
    category: str
    published: Optional[datetime]
    paras: list[Para] = field(default_factory=list)
    score: float = 0.0
    aka: list[str] = field(default_factory=list)      # headlines of stories merged into this one
    sectors: list[str] = field(default_factory=list)

    @property
    def sources(self) -> list[Source]:
        seen, out = set(), []
        for p in self.paras:
            if p.source.url not in seen:
                seen.add(p.source.url)
                out.append(p.source)
        return out

    @property
    def n_claims(self) -> int:
        return sum(len(p.claims) for p in self.paras)


def section_for(item) -> str:
    topics, regions = set(item.topics), list(item.regions)
    if item.category == "official":
        return "Official releases"
    if topics & {"crude", "natgas", "gold", "silver"}:
        return "Energy and metals"
    if "deals" in topics:
        return "Deals and corporate"
    if getattr(item, "sectors", None):
        return "Sectors"
    for r in ([item.feed_region] if item.feed_region else []) + regions:
        if r in REGION_SECTION:
            return REGION_SECTION[r]
    if topics & {"rates_fed", "usd_inr", "geopolitics"}:
        return "Markets and macro"
    if "tech" in topics or item.category == "tech":
        return "Tech and AI"
    return {"india": "India"}.get(item.category, "Markets and macro")


def item_score(item) -> float:
    s = max([TOPIC_SCORE.get(t, 0) for t in item.topics] or [0])
    return s + (3 if item.category == "official" else 0)


def _words(s: str) -> set[str]:
    return {w[:-1] if w.endswith("s") else w for w in re.findall(r"[a-z]{4,}", norm(s))}


def _same_story(a: Story, b: Story) -> bool:
    if a.section != b.section:
        return False
    wb = _words(b.headline)
    for h in [a.headline] + a.aka:
        wa = _words(h)
        if wa and wb and len(wa & wb) / min(len(wa), len(wb)) >= 0.6:
            return True
    na = set().union(*[numbers(c) for p in a.paras for c in p.claims]) if a.paras else set()
    nb = set().union(*[numbers(c) for p in b.paras for c in p.claims]) if b.paras else set()
    shared = {x for x in na & nb if len(x) >= 2}
    return len(shared) >= 3 and len(shared) >= 0.5 * min(len(na), len(nb))


def _dup_claim(c: str, existing: list[str]) -> bool:
    nc, wc = numbers(c), _words(c)
    for e in existing:
        if numbers(e) == nc and wc and len(_words(e) & wc) >= 0.7 * min(len(wc), max(1, len(_words(e)))):
            return True
    return False


def merge_stories(stories: list[Story]) -> list[Story]:
    """Merge stories about the same event. Attribution stays exact: each paragraph keeps its own source,
    and a second outlet only contributes claims the first did not already make."""
    out: list[Story] = []
    for s in sorted(stories, key=lambda x: -x.n_claims):
        for base in out:
            if _same_story(base, s):
                have = [c for p in base.paras for c in p.claims]
                for p in s.paras:
                    new = [c for c in p.claims if not _dup_claim(c, have)]
                    if new:
                        base.paras.append(Para(new, p.source))
                        have += new
                    else:
                        base.paras.append(Para([], p.source))      # corroboration only; shown as "also reported by"
                base.score += 2
                base.aka.append(s.headline)
                break
        else:
            out.append(s)
    return out


def _link(src: Source) -> str:
    lab = src.name
    out = f"[{lab}]({src.url})"
    if src.via == "archive.today":
        out += " (read from an archive.today copy)"
    elif src.via == "archive":
        out += " (read from a web-archive copy)"
    elif src.via == "partial":
        out += " (only part of the article was readable)"
    elif src.via == "headline search":
        out += f" (the original, {src.orig}, could not be read; same story found by searching its headline)"
    if src.via == "wayback":
        out += f" (Wayback copy of {src.snap})"
    elif src.via == "feed summary":
        out += " (feed summary only)"
    if src.archive:
        out += f" · [archive]({src.archive})"
    return out


def render_story(s: Story, per_para: int, level: int = 3, tag: str = "") -> str:
    lines = [f"{'#' * level} {s.headline}"]
    if tag:
        lines.append(f"*{tag}*")
    corroborating = []
    for p in s.paras:
        if not p.claims:
            corroborating.append(p.source)
            continue
        for i in range(0, len(p.claims), per_para):
            chunk = p.claims[i: i + per_para]
            cite = f" ({_link(p.source)})" if i + per_para >= len(p.claims) else ""
            lines.append(" ".join(c.rstrip() if c.rstrip().endswith((".", "!", "?")) else c.rstrip() + "." for c in chunk) + cite)
            lines.append("")
    if corroborating:
        lines.append("Also reported by " + ", ".join(_link(x) for x in corroborating) + ".")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ---------------- calendar ----------------
def _cal_date(e: dict) -> Optional[date]:
    try:
        return date.fromisoformat(str(e["date"]))
    except Exception:  # noqa: BLE001
        return None


def render_calendar(cal: dict, now: datetime, horizon_days: int, review_max_age_days: int) -> list[str]:
    today = now.astimezone(IST).date()
    L = ["## Week ahead: what is coming, what is expected, what it touches"]
    reviewed = None
    try:
        reviewed = date.fromisoformat(str(cal.get("last_reviewed")))
    except Exception:  # noqa: BLE001
        pass
    if reviewed is None:
        L += ["", "**Calendar has no review date: treat it as incomplete and check official calendars.**"]
    elif (today - reviewed).days > review_max_age_days:
        L += ["", f"**Calendar last reviewed {reviewed:%d %b %Y} ({(today - reviewed).days} days ago): events may be missing or changed. "
                  "Check official calendars.**"]
    evs = []
    for e in cal.get("events") or []:
        d = _cal_date(e)
        if d is None or d < today or d > today + timedelta(days=horizon_days):
            continue
        evs.append((d, e))
    if not evs:
        return L + ["", "No events in the calendar for this period."]
    evs.sort(key=lambda x: (x[0], str(x[1].get("time_ist", ""))))
    L += ["", "Times are IST. Cells marked 'unverified' were not confirmed against the official calendar.", "",
          "| When | Event | Expectation | What it can move | Source |", "|---|---|---|---|---|"]
    for d, e in evs:
        when = f"{d:%a %d %b}" + (f", {e['time_ist']}" if e.get("time_ist") else "")
        src = f"[{e['source_name']}]({e['source_url']})" if e.get("source_url") and e.get("source_name") else "n/a"
        ttl = e["title"] + ("" if e.get("verified", True) else " (unverified)")
        L.append(f"| {when} | {ttl} | {e.get('expectation') or 'n/a'} | {', '.join(e.get('affects') or []) or 'n/a'} | {src} |")
    L.append("")
    return L


# ---------------- markets at a glance ----------------
def render_glance(path: Path, now: datetime, max_age_days: int = 3) -> list[str]:
    L = ["## Markets at a glance"]
    if not path.exists():
        return L + ["", "Not available: the market dashboard has not been generated yet (it is written by the action report)."]
    try:
        data = json.loads(path.read_text())
        gen = datetime.fromisoformat(data["generated"])
        rows = data["rows"]
    except Exception as e:  # noqa: BLE001
        return L + ["", f"Not available: dashboard file unreadable ({type(e).__name__})."]
    age = (now - gen).total_seconds() / 86400
    if age > max_age_days:
        return L + ["", f"Not shown: the dashboard was generated {age:.1f} days ago (limit {max_age_days}). Run the action report again."]
    L += ["", f"From the dashboard generated {gen.astimezone(IST):%d %b %Y %H:%M IST}. Each row shows the date of its own latest data point.", "",
          "| Market | Latest | As of | Change (5 observations) | Source |", "|---|---|---|---|---|"]
    for r in rows:
        if r.get("value") is None:
            continue
        L.append(f"| {r['label']} | {r['value_text']} | {r['as_of']} | {r.get('chg_text') or 'n/a'} | {r['source']} |")
    L.append("")
    return L
