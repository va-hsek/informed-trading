"""News report as a newspaper: feeds -> readable articles -> verified claims -> stories -> cited paper.

    python3 -m news.run_news

Every sentence in a story is a claim that passed the verification guard (excerpt present word for word in the article,
every number present in it, no copying of 15+ words). Each paragraph ends with the hyperlinked source name.
Anything that could not be read is listed as a headline with its link. Nothing is summarised from a headline.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

import yaml

from collectors.http import get, get_json
from core.render_html import md_to_html
from core.secrets import load_secrets
from news import calendar_refresh as cal_refresh
from news.articles import RobotsCache, read_article
from news.claims import extract_claims
from news.feeds import collect_items, tag_topics
from news.llm import LLM
from news.paper import (SECTIONS, Para, Source, Story, item_score, merge_stories, render_calendar, render_glance, render_story,
                        section_for)

IST = timezone(timedelta(hours=5, minutes=30))
ROOT = Path(__file__).resolve().parent.parent


def _d(dt: Optional[datetime]) -> str:
    return dt.astimezone(IST).strftime("%d %b %Y") if dt else "date n/a"


def _relevant(it) -> bool:
    return bool(it.topics or it.regions or it.feed_region)


def _primary_region(it) -> str:
    return it.feed_region or (it.regions[0] if it.regions else "other")


def pick_for_reading(items, lim) -> list:
    """Best items first, but with a per-region budget so India, USA and Europe get most of the reading."""
    caps = lim.get("read_caps") or {}
    pool = sorted([i for i in items if not i.headline_only and _relevant(i)],
                  key=lambda i: (-item_score(i), -(i.published.timestamp() if i.published else 0)))
    used: dict[str, int] = {}
    out = []
    for i in pool:
        r = _primary_region(i)
        if used.get(r, 0) < int(caps.get(r, caps.get("other", 40))):
            used[r] = used.get(r, 0) + 1
            out.append(i)
    return out[: lim["max_articles_read"]]


def _archive(it) -> str:
    return "https://archive.ph/newest/" + it.origin_url if (it.paywalled and it.origin_url) else ""


def build_news_report(root: Path, now: datetime, secrets: dict, http_get: Callable = get, http_json: Callable = get_json,
                      llm: Optional[LLM] = None) -> tuple[str, dict]:
    cfg = yaml.safe_load((root / "config" / "news_sources.yaml").read_text())
    lim = cfg["limits"]
    items, coverage = collect_items(cfg["feeds"], now.astimezone(timezone.utc), http_get, lim["max_age_hours"], lim["max_items_per_feed"])
    for it in items:
        it.topics = tag_topics(it, cfg["topics"])
        it.regions = tag_topics(it, cfg.get("regions") or {})

    llm_note = ""
    if llm is None:
        if secrets.get("ANTHROPIC_API_KEY"):
            llm = LLM(secrets["ANTHROPIC_API_KEY"], cfg["llm"]["extract_model"], root / "data" / "llm_usage.jsonl",
                      cfg["llm"]["price_input_per_mtok"], cfg["llm"]["price_output_per_mtok"])
        else:
            llm_note = "ANTHROPIC_API_KEY is not set: articles were not read, so this edition lists headlines only."

    readable = pick_for_reading(items, lim)
    robots = RobotsCache(http_get)

    stats = {"read_direct": 0, "read_wayback": 0, "read_summary": 0, "read_archive": 0, "unreadable": 0, "claims_kept": 0, "claims_dropped": 0,
             "extract_errors": 0}
    stories: list[Story] = []
    unread: list[tuple] = []
    drops: list[str] = []
    for it in readable:
        if llm is None:
            unread.append((it, "model not configured"))
            continue
        art = read_article(it, robots, http_get, http_json, lim["min_article_chars"], lim["max_article_chars"])
        if not art["text"]:
            stats["unreadable"] += 1
            unread.append((it, art["reason"]))
            continue
        stats[{"direct": "read_direct", "wayback": "read_wayback", "archive.today": "read_archive", "feed summary": "read_summary"}[art["via"]]] += 1
        kept, dropped, err = extract_claims(llm, it.title, art["text"], lim["max_claims_per_article"])
        if err:
            stats["extract_errors"] += 1
            unread.append((it, f"claim extraction failed: {err}"))
            continue
        stats["claims_kept"] += len(kept)
        stats["claims_dropped"] += len(dropped)
        drops += [f"{it.source}: {why}" for _, why in dropped]          # reasons only: unverified text is never printed
        if not kept:
            continue
        src = Source(it.publisher or it.source, it.link, art["via"], art["snapshot_date"], _archive(it))
        st = Story(it.title, section_for(it), it.topics, it.regions, it.category, it.published, [Para(kept, src)])
        st.score = item_score(it) + min(len(kept), 6) / 2
        stories.append(st)

    stories = merge_stories(stories)
    bonus = float(lim.get("trending_bonus_per_extra_source", 1.5))
    for s_ in stories:                                   # a story many outlets carry is trending
        s_.score += bonus * (len(s_.sources) - 1)
    stories.sort(key=lambda s: -s.score)
    front = stories[: lim["front_page_stories"]]
    for s in front:
        s.section = "Front page"
    by_sec: dict[str, list[Story]] = {sec: [] for sec in SECTIONS}
    for s in stories:
        by_sec[s.section].append(s)

    used = {id(p.source) for s in stories for p in s.paras}
    read_links = {p.source.url for s in stories for p in s.paras}
    for it in items:
        if it.link not in read_links and _relevant(it) and all(u[0] is not it for u in unread):
            unread.append((it, "headline only" if it.headline_only else "not selected for reading (cap reached)"))

    L = [f"# The Morning Edition", f"**{now.astimezone(IST):%A %d %B %Y}** · generated {now.astimezone(IST):%H:%M} IST", "",
         "> Every sentence below is a statement extracted from the linked article and checked by code: the supporting excerpt had to appear "
         "word for word in the article and every number had to be present in it. Statements are paraphrased, not copied. Source names are links. "
         "This is information, not advice.", ""]
    if llm_note:
        L += [f"**Note:** {llm_note}", ""]

    L += render_glance(root / cfg.get("dashboard_file", "data/dashboard_latest.json"), now) + [""]
    per = int(lim.get("max_paragraph_claims", 3))
    for sec in SECTIONS:
        lst = by_sec[sec]
        if not lst:
            continue
        cap = int((lim.get("section_caps") or {}).get(sec, lim.get("max_stories_per_section", 14)))
        L += [f"## {sec}", ""]
        for s in lst[:cap]:
            L += [render_story(s, per)]
        if len(lst) > cap:
            L.append(f"{len(lst) - cap} more stories in this section were not printed (cap {cap}).")
            L.append("")

    cal_note = cal_refresh.ensure_fresh(root, now, secrets, cfg, llm if isinstance(llm, LLM) else None)
    if cal_note:
        L += [f"**Calendar:** {cal_note}", ""]
    cal = yaml.safe_load((root / "config" / "calendar.yaml").read_text()) if (root / "config" / "calendar.yaml").exists() else {}
    L += render_calendar(cal or {}, now, int(lim.get("calendar_horizon_days", 21)), int(lim.get("calendar_review_max_age_days", 7)))

    hl = [(it, why) for it, why in unread if _relevant(it)]
    if hl:
        L += ["## In brief: headlines not read", "Headline only: the article text could not be read here, so nothing is summarised.", ""]
        for it, why in sorted(hl, key=lambda x: (-item_score(x[0]), -(x[0].published.timestamp() if x[0].published else 0)))[: int(lim.get('in_brief_max', 150))]:
            pub = it.publisher or it.source
            arch = f" · [archive]({_archive(it)})" if _archive(it) else ""
            L.append(f"- {it.title} ([{pub}]({it.link})){arch}")
        L.append("")

    L += ["## Coverage and quality", "| Feed | Status | Items kept |", "|---|---|---|"]
    for c in coverage:
        L.append(f"| {c['feed']} | {'ok' if c['ok'] else 'FAILED: ' + c['reason'][:90]} | {c['kept']} |")
    L.append("")
    L.append(f"- Articles read: {stats['read_direct']} directly, {stats['read_wayback']} via Wayback, {stats['read_archive']} via archive.today, {stats['read_summary']} from feed summaries; "
             f"{stats['unreadable']} could not be read; {stats['extract_errors']} extraction errors.")
    L.append(f"- Claims kept {stats['claims_kept']}, dropped by the guard {stats['claims_dropped']}.")
    if llm is not None:
        L.append(f"- Model use this run: {llm.tokens_in:,} input and {llm.tokens_out:,} output tokens (estimated ${llm.est_cost_usd():.3f} at the prices in config/news_sources.yaml).")
    if drops:
        L.append("- Dropped claims (reason): " + "; ".join(drops[:5]) + (" ..." if len(drops) > 5 else ""))

    meta = {"stats": stats, "coverage": coverage, "sections": {s: len(by_sec[s]) for s in SECTIONS},
            "stories": len(stories), "sources": len({p.source.url for s in stories for p in s.paras})}
    latest = {"generated": now.isoformat(),
              "stories": [{"headline": s.headline, "section": s.section, "topics": s.topics, "regions": s.regions,
                           "sources": [{"name": x.name, "url": x.url} for x in s.sources],
                           "text": " ".join(c for p in s.paras for c in p.claims)} for s in stories],
              "headlines": [{"headline": it.title, "topics": it.topics, "regions": it.regions, "source": it.publisher or it.source, "url": it.link}
                            for it, _ in hl]}
    meta["latest"] = latest
    return "\n".join(L) + "\n", meta


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.parse_args(argv)
    now = datetime.now(IST)
    md, meta = build_news_report(ROOT, now, load_secrets())
    out = ROOT / "reports"
    out.mkdir(exist_ok=True)
    stem = f"news_{now:%Y-%m-%d_%H%M}"
    (out / f"{stem}.html").write_text(md_to_html(md, f"The Morning Edition {now:%d %b %Y}"))
    nd = ROOT / "data" / "news"
    nd.mkdir(parents=True, exist_ok=True)
    (nd / "latest.json").write_text(json.dumps(meta["latest"], indent=1))
    print(f"wrote reports/{stem}.html  ({meta['stories']} stories, {meta['sources']} sources, {meta['stats']['claims_kept']} claims)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
