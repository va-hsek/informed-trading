"""News report as a newspaper: feeds -> readable articles -> verified claims -> stories -> cited paper.

    python3 -m news.run_news

Every sentence in a story is a claim that passed the verification guard (excerpt present word for word in the article,
every number present in it, no copying of 15+ words). Each paragraph ends with the hyperlinked source name.
Anything that could not be read is listed as a headline with its link. Nothing is summarised from a headline.
"""
from __future__ import annotations

import argparse
import json
import re
import threading
from collections import Counter
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
from news.feeds import collect_items, edition_window, norm_title, tag_topics
from news.headline_search import find_alternative
from news.newsletter import fetch_newsletters
from news.llm import LLM, make_llm
from news.magazine import render_magazine
from news.paper import (SECTIONS, Para, Source, Story, item_score, merge_stories, render_calendar, render_glance, render_story,
                        section_for)

IST = timezone(timedelta(hours=5, minutes=30))
ROOT = Path(__file__).resolve().parent.parent


BROWSER_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36 informed-trading/1.0",
                   "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "Accept-Language": "en-IN,en;q=0.9"}


def news_get(url: str, params=None, headers=None, **kw) -> bytes:
    """Feeds and articles are fetched with a normal browser identity: many Indian and US publishers answer 403 to unknown clients.
    robots.txt is still honoured by the reader."""
    return get(url, params, {**BROWSER_HEADERS, **(headers or {})}, **kw)


def _reason_key(why: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"https?://\S+", "<url>", why or "no reason recorded"))[:140]


def _d(dt: Optional[datetime]) -> str:
    return dt.astimezone(IST).strftime("%d %b %Y") if dt else "date n/a"


def _relevant(it) -> bool:
    return bool(it.topics or it.regions or it.feed_region or it.sectors)


def _primary_region(it) -> str:
    return it.feed_region or (it.regions[0] if it.regions else "other")


def pick_for_reading(items, lim, include_headline_only: bool = False) -> list:
    """Best items first, but with a per-region budget so India, USA and Europe get most of the reading."""
    caps = lim.get("read_caps") or {}
    pool = sorted([i for i in items if (include_headline_only or not i.headline_only) and _relevant(i)],
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


def build_news_report(root: Path, now: datetime, secrets: dict, http_get: Callable = news_get, http_json: Callable = get_json,
                      llm: Optional[LLM] = None) -> tuple[str, dict]:
    cfg = yaml.safe_load((root / "config" / "news_sources.yaml").read_text())
    lim = cfg["limits"]
    now_utc = now.astimezone(timezone.utc)
    win_start = edition_window(now_utc, lim.get("edition_anchor_ist", "05:30"), float(lim.get("window_hours", 24)))
    items, coverage = collect_items(cfg["feeds"], now_utc, http_get, win_start, lim["max_items_per_feed"])
    nl_items, nl_cov = fetch_newsletters(cfg.get("newsletters") or [], secrets, now_utc, win_start)
    have = {norm_title(i.title) for i in items} | {i.link for i in items}
    nl_items = [i for i in nl_items if norm_title(i.title) not in have and i.link not in have]       # additional only: no duplicate of a feed story
    items += nl_items
    coverage += nl_cov
    for it in items:
        it.topics = tag_topics(it, cfg["topics"])
        it.regions = tag_topics(it, cfg.get("regions") or {})
        it.sectors = tag_topics(it, cfg.get("sectors") or {})

    llm_note = ""
    made_here = False
    if llm is None:
        made_here = True
        llm = make_llm(cfg, secrets, "extract", root / "data" / "llm_usage.jsonl")
        if llm is None:
            llm_note = "No model API key is set (GEMINI_API_KEY or ANTHROPIC_API_KEY): articles were not read, so this edition lists headlines only."

    llm_error = ""
    if llm is not None and made_here:
        try:                                              # one tiny call first: if the model is unreachable, say exactly why and do not burn 900 calls
            llm.complete("Reply with exactly: {\"ok\": true}", "ping", max_tokens=20)
        except Exception as e:  # noqa: BLE001
            llm_error = f"{type(e).__name__}: {e}"
            llm_note = (f"The model could not be reached, so no article was read. Reason from the API: {llm_error}. "
                        "Headlines are listed instead. Fix the key, model name or quota and run again.")
            llm = None

    hs = lim.get("headline_search") or {}
    hs_on = bool(hs.get("enabled")) and llm is not None
    hs_left = int(hs.get("max_per_run", 150))
    max_cost = float(lim.get("max_cost_usd_per_run", 8.0))
    readable = pick_for_reading([i for i in items if not i.extra], lim, include_headline_only=True)
    readable += sorted(nl_items, key=lambda i: -item_score(i))[: int(lim.get("newsletter_reads", 120))]      # newsletters: extra quota
    robots = RobotsCache(http_get)

    stats = {"read_direct": 0, "read_wayback": 0, "read_summary": 0, "read_archive": 0, "read_search": 0, "read_partial": 0, "cost_stopped": 0, "unreadable": 0, "claims_kept": 0, "claims_dropped": 0,
             "extract_errors": 0}
    stories: list[Story] = []
    unread: list[tuple] = []
    drops: list[str] = []
    reasons: Counter = Counter()
    samples: list[dict] = []
    search_errors: list[str] = []
    brk = {"ok": 0, "errs": 0, "last": ""}
    from concurrent.futures import ThreadPoolExecutor
    lock = threading.Lock()
    hs_box = {"left": hs_left}
    via_stat = {"direct": "read_direct", "wayback": "read_wayback", "archive": "read_archive", "archive.today": "read_archive",
                "headline search": "read_search", "partial": "read_partial", "feed summary": "read_summary"}

    def process(it):
        """Reads one item end to end. Returns (kind, payload). Runs in a worker thread; shared state is only touched under `lock`."""
        if llm is None:
            return "unread", "model not configured"
        if llm.est_cost_usd() >= max_cost:
            return "cost", f"spending stop reached (${max_cost:.2f} per run)"
        if brk["ok"] == 0 and brk["errs"] >= 10:
            return "error", f"stopped early: the first {brk['errs']} model calls all failed (last: {brk['last']})"
        art = read_article(it, robots, http_get, http_json, lim["min_article_chars"], lim["max_article_chars"])
        alt = None
        if (not art["text"] or art.get("partial")) and hs_on:
            with lock:
                go = hs_box["left"] > 0 and len(search_errors) < 6          # six search errors in a run (e.g. quota): stop trying
                hs_box["left"] -= 1 if go else 0
            if go:
                alt = find_alternative(it.title, it.origin_url or it.link, llm, hs.get("tool_type", "web_search_20250305"), robots, http_get,
                                       lim["min_article_chars"], lim["max_article_chars"], errors=search_errors)
        if alt:
            art = {"text": alt["text"], "via": "headline search", "snapshot_date": None, "reason": "", "alt": alt}
        if not art["text"]:
            return "unreadable", art["reason"]
        kept, dropped, err = extract_claims(llm, it.title, art["text"], lim["max_claims_per_article"])
        with lock:
            if err:
                brk["errs"] += 1
                brk["last"] = err[:200]
            else:
                brk["ok"] += 1
        if err:
            return "error", err
        return "ok", (art, kept, dropped)

    workers = max(1, int(lim.get("parallel_workers", 8)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(process, readable))
    for it, (kind, payload) in zip(readable, results):
        if kind == "unread":
            unread.append((it, payload))
        elif kind == "cost":
            stats["cost_stopped"] += 1
            unread.append((it, payload))
        elif kind == "unreadable":
            stats["unreadable"] += 1
            unread.append((it, payload))
            reasons["not readable: " + _reason_key(payload)] += 1
            if len(samples) < 40:
                samples.append({"source": it.publisher or it.source, "title": it.title[:100], "reason": (payload or "")[:300]})
        elif kind == "error":
            stats["extract_errors"] += 1
            unread.append((it, f"claim extraction failed: {payload}"))
            reasons["model step failed: " + _reason_key(payload)] += 1
        else:
            art, kept, dropped = payload
            stats[via_stat[art["via"]]] += 1
            stats["claims_kept"] += len(kept)
            stats["claims_dropped"] += len(dropped)
            drops += [f"{it.source}: {why}" for _, why in dropped]          # reasons only: unverified text is never printed
            if not kept:
                continue
            if art["via"] == "headline search":
                src = Source(art["alt"]["publisher"], art["alt"]["url"], "headline search", None, _archive(it), it.publisher or it.source)
            else:
                src = Source(it.publisher or it.source, it.origin_url or it.link, art["via"], art["snapshot_date"], _archive(it))
            st = Story(it.title, section_for(it), it.topics, it.regions, it.category, it.published, [Para(kept, src)], sectors=it.sectors)
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
        if sec == "Sectors":
            per_sec = int(lim.get("sector_story_cap", 24))
            for sname in cfg.get("sectors") or {}:
                grp = [x for x in lst if x.sectors and x.sectors[0] == sname][:per_sec]
                if not grp:
                    continue
                L += [f"### {sname}", ""]
                for x in grp:
                    reg = {"india": "India", "americas": "USA and Americas", "europe": "Europe", "apac": "Asia-Pacific", "middleeast": "Middle East"}
                    tag = ", ".join(reg[r] for r in x.regions if r in reg)
                    L += [render_story(x, per, level=4, tag=tag)]
            continue
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
    L.append(f"- Articles read: {stats['read_direct']} directly, {stats['read_wayback']} via Wayback, {stats['read_archive']} via other archives, "
             f"{stats['read_search']} by finding the same story elsewhere via its headline, {stats['read_partial']} partially, {stats['read_summary']} from feed summaries; "
             f"{stats['unreadable']} could not be read, {stats['cost_stopped']} left unread by the spending stop; {stats['extract_errors']} extraction errors.")
    L.append(f"- Claims kept {stats['claims_kept']}, dropped by the guard {stats['claims_dropped']}.")
    if llm is not None:
        L.append(f"- Model use this run: {llm.tokens_in:,} input and {llm.tokens_out:,} output tokens (estimated ${llm.est_cost_usd():.3f} at the prices in config/news_sources.yaml).")
    if drops:
        L.append("- Dropped claims (reason): " + "; ".join(drops[:5]) + (" ..." if len(drops) > 5 else ""))

    meta = {"stats": stats, "coverage": coverage, "sections": {s: len(by_sec[s]) for s in SECTIONS},
            "stories": len(stories), "sources": len({p.source.url for s in stories for p in s.paras})}
    latest = {"generated": now.isoformat(),
              "stories": [{"headline": s.headline, "section": s.section, "topics": s.topics, "regions": s.regions, "sectors": s.sectors,
                           "sources": [{"name": x.name, "url": x.url} for x in s.sources],
                           "text": " ".join(c for p in s.paras for c in p.claims)} for s in stories],
              "headlines": [{"headline": it.title, "topics": it.topics, "regions": it.regions, "sectors": it.sectors, "source": it.publisher or it.source, "url": it.link}
                            for it, _ in hl]}
    meta["latest"] = latest
    meta["diagnostics"] = {"generated": now.isoformat(), "provider": getattr(llm, "provider", None), "model": getattr(llm, "model", None),
                           "llm_preflight_error": llm_error, "stats": stats,
                           "top_failure_reasons": reasons.most_common(15), "unreadable_samples": samples,
                           "headline_search_errors": [_reason_key(x) for x in search_errors[:10]],
                           "feed_failures": [{"feed": c["feed"], "reason": c["reason"][:160]} for c in coverage if not c["ok"]]}
    meta["mag"] = {"now": now, "stories": stories, "unread": hl, "stats": stats, "coverage": coverage, "reasons": reasons.most_common(12),
                   "llm_note": llm_note, "llm_error": llm_error, "llm": llm, "cal_note": cal_note, "cal": cal or {}, "cfg": cfg,
                   "archive": _archive}
    return "\n".join(L) + "\n", meta


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.parse_args(argv)
    now = datetime.now(IST)
    md, meta = build_news_report(ROOT, now, load_secrets())
    out = ROOT / "reports"
    out.mkdir(exist_ok=True)
    stem = f"news_{now:%Y-%m-%d_%H%M}"
    try:
        page = render_magazine(**meta["mag"])
    except Exception as e:  # noqa: BLE001                                   # never lose the edition because of a layout bug: fall back to the plain page
        print(f"magazine layout failed ({type(e).__name__}: {e}); wrote the plain layout instead")
        page = md_to_html(md, f"The Morning Edition {now:%d %b %Y}")
    (out / f"{stem}.html").write_text(page)
    nd = ROOT / "data" / "news"
    nd.mkdir(parents=True, exist_ok=True)
    (nd / "latest.json").write_text(json.dumps(meta["latest"], indent=1))
    (nd / "diagnostics.json").write_text(json.dumps(meta["diagnostics"], indent=1, default=str))
    print(f"wrote reports/{stem}.html  ({meta['stories']} stories, {meta['sources']} sources, {meta['stats']['claims_kept']} claims)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
