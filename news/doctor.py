"""Deep diagnostic: can this machine read the news end to end?   python3 -m news.doctor

Run it from the Actions tab ('Doctor' workflow). For EVERY feed in config/news_sources.yaml it fetches the feed, takes the newest few
items, and pushes each through the real reading chain (direct page, Wayback, other archives, archive.today, partial text, feed summary,
headline search). It then runs the real model on a sample of the articles it could read. Output: data/news/doctor.json and reports/doctor.html,
with a verdict per feed, a verdict per publisher, the most common failure reasons, and the model result. Nothing here changes the edition."""
from __future__ import annotations

import html
import json
import re
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import yaml

from collectors.http import FetchError, get_json
from core.secrets import load_secrets
from news.articles import RobotsCache, read_article
from news.claims import extract_claims
from news.feeds import feed_url, parse_feed
from news.headline_search import find_alternative
from news.llm import make_llm
from news.run_news import _reason_key, news_get
from news.selftest import check_model

ROOT = Path(__file__).resolve().parent.parent
IST = timezone(timedelta(hours=5, minutes=30))
PER_FEED = 3


def _feed(src: dict, http_get) -> dict:
    t = time.time()
    try:
        items = parse_feed(http_get(feed_url(src)), src)
        return {"feed": src["name"], "ok": True, "items": items, "reason": "", "secs": round(time.time() - t, 1), "n": len(items)}
    except Exception as e:  # noqa: BLE001
        return {"feed": src["name"], "ok": False, "items": [], "reason": str(e)[:200], "secs": round(time.time() - t, 1), "n": 0}


def run(cfg: dict, secrets: dict, http_get=news_get, http_json=get_json, workers: int = 8, per_feed: int = PER_FEED, model_sample: int = 20,
        search_sample: int = 12) -> dict:
    lim = cfg["limits"]
    feeds = cfg["feeds"]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        fr = list(ex.map(lambda s: _feed(s, http_get), feeds))
    robots = RobotsCache(http_get)
    llm = make_llm(cfg, secrets, "extract", None)
    model_rows = check_model(cfg, secrets)
    model_ok = all(r["ok"] for r in model_rows if r["test"].startswith("model:") and "search" not in r["test"])
    search_ok = any(r["ok"] for r in model_rows if r["test"] == "model: web search")

    jobs = []
    for f, src in zip(fr, feeds):
        got = sorted([i for i in f["items"] if i.published], key=lambda i: i.published, reverse=True)[:per_feed] or f["items"][:per_feed]
        for it in got:
            jobs.append((src["name"], it))

    def read(job):
        name, it = job
        t = time.time()
        art = read_article(it, robots, http_get, http_json, lim["min_article_chars"], lim["max_article_chars"])
        row = {"feed": name, "title": it.title[:90], "host": urlparse(it.origin_url or it.link).netloc, "headline_only": it.headline_only,
               "via": art["via"], "chars": len(art["text"]), "reason": _reason_key(art["reason"]), "secs": round(time.time() - t, 1)}
        return row, art, it

    with ThreadPoolExecutor(max_workers=workers) as ex:
        res = list(ex.map(read, jobs))
    rows = [r for r, _, _ in res]

    # headline search on items that could not be read (this is how Google News and paywalled items are rescued)
    srch = []
    if llm is not None and search_ok:
        todo = [(r, a, it) for r, a, it in res if not a["text"] or a.get("partial")][:search_sample]
        errs: list[str] = []

        def one(x):
            r, a, it = x
            alt = find_alternative(it.title, it.origin_url or it.link, llm, "web_search_20250305", robots, http_get, lim["min_article_chars"],
                                   lim["max_article_chars"], errors=errs)
            return {"feed": r["feed"], "title": r["title"], "found": bool(alt), "publisher": (alt or {}).get("publisher"), "chars": len((alt or {}).get("text", ""))}, alt
        with ThreadPoolExecutor(max_workers=4) as ex:
            out = list(ex.map(one, todo))
        srch = [o for o, _ in out]
        for (r, a, it), (_, alt) in zip(todo, out):
            if alt:
                a["text"], a["via"] = alt["text"], "headline search"
                r["via"], r["chars"] = "headline search", len(alt["text"])
        search_errors = [_reason_key(e) for e in errs[:5]]
    else:
        search_errors = []

    # the model on a sample of readable articles
    ext = []
    if llm is not None and model_ok:
        readable = [(r, a, it) for r, a, it in res if a["text"]][:model_sample]

        def ex1(x):
            r, a, it = x
            kept, dropped, err = extract_claims(llm, it.title, a["text"], lim["max_claims_per_article"])
            return {"feed": r["feed"], "title": r["title"], "via": a["via"], "kept": len(kept), "dropped": len(dropped), "error": err[:200],
                    "sample_claim": kept[0] if kept else "", "drop_reasons": [w for _, w in dropped][:3]}
        with ThreadPoolExecutor(max_workers=4) as ex:
            ext = list(ex.map(ex1, readable))

    per_feed_stats = defaultdict(lambda: {"tested": 0, "read": 0})
    per_host = defaultdict(lambda: {"tested": 0, "read": 0, "reasons": Counter()})
    for r in rows:
        ok = r["chars"] > 0
        pf, ph = per_feed_stats[r["feed"]], per_host[r["host"] or "?"]
        pf["tested"] += 1
        ph["tested"] += 1
        if ok:
            pf["read"] += 1
            ph["read"] += 1
        else:
            ph["reasons"][r["reason"]] += 1
    feed_table = [{"feed": f["feed"], "feed_ok": f["ok"], "feed_reason": f["reason"], "items_in_feed": f["n"], **per_feed_stats.get(f["feed"], {"tested": 0, "read": 0})} for f in fr]
    hosts = [{"host": h, "tested": v["tested"], "read": v["read"], "top_reason": (v["reasons"].most_common(1) or [("", 0)])[0][0]} for h, v in per_host.items()]
    hosts.sort(key=lambda x: (x["read"] / max(1, x["tested"]), -x["tested"]))
    via = Counter(r["via"] or "unreadable" for r in rows)
    reasons = Counter(r["reason"] for r in rows if not r["chars"])
    summary = {"feeds_total": len(fr), "feeds_ok": sum(f["ok"] for f in fr), "articles_tested": len(rows), "articles_read": sum(1 for r in rows if r["chars"]),
               "via": dict(via), "model_all_ok": model_ok, "search_ok": search_ok,
               "claims_kept_in_sample": sum(e["kept"] for e in ext), "extractions": len(ext), "extraction_errors": sum(1 for e in ext if e["error"])}
    return {"at": datetime.now(IST).isoformat(), "summary": summary, "model": model_rows, "feeds": feed_table, "hosts": hosts, "top_failure_reasons": reasons.most_common(15),
            "headline_search": srch, "headline_search_errors": search_errors, "extraction_sample": ext,
            "llm": {"provider": getattr(llm, "provider", None), "model": getattr(llm, "model", None), "tokens_in": getattr(llm, "tokens_in", 0),
                    "tokens_out": getattr(llm, "tokens_out", 0), "searches": getattr(llm, "search_count", 0),
                    "est_cost_usd": round(llm.est_cost_usd(), 4) if llm else 0}}


def to_html(d: dict) -> str:
    e = html.escape
    s = d["summary"]
    verdict = "READS NEWS" if s["articles_read"] and s["claims_kept_in_sample"] else "NOT WORKING"
    def tbl(head, rows):
        return "<table><tr>" + "".join(f"<th>{e(h)}</th>" for h in head) + "</tr>" + "".join("<tr>" + "".join(f"<td>{e(str(c))}</td>" for c in r) + "</tr>" for r in rows) + "</table>"
    return ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>Doctor</title>"
            "<style>body{font:14px/1.45 system-ui;margin:12px;max-width:1000px}table{border-collapse:collapse;display:block;overflow-x:auto}td,th{border:1px solid #8884;padding:3px 6px;text-align:left}</style>"
            f"<h1>Doctor: {verdict}</h1><p>{e(json.dumps(s))}</p><h2>Model</h2>{tbl(['test', 'ok', 'detail'], [[r['test'], r['ok'], r['detail']] for r in d['model']])}"
            f"<h2>Why articles failed</h2>{tbl(['count', 'reason'], [[n, k] for k, n in d['top_failure_reasons']])}"
            f"<h2>Publishers (worst first)</h2>{tbl(['host', 'tested', 'read', 'top reason'], [[h['host'], h['tested'], h['read'], h['top_reason']] for h in d['hosts']])}"
            f"<h2>Feeds</h2>{tbl(['feed', 'feed ok', 'items', 'tested', 'read', 'feed error'], [[f['feed'], f['feed_ok'], f['items_in_feed'], f['tested'], f['read'], f['feed_reason']] for f in d['feeds']])}"
            f"<h2>Headline search</h2>{tbl(['feed', 'title', 'found', 'publisher'], [[x['feed'], x['title'], x['found'], x['publisher']] for x in d['headline_search']])}"
            f"<h2>Model on real articles</h2>{tbl(['feed', 'title', 'via', 'kept', 'dropped', 'error', 'sample claim'], [[x['feed'], x['title'], x['via'], x['kept'], x['dropped'], x['error'], x['sample_claim']] for x in d['extraction_sample']])}")


def main() -> int:
    cfg = yaml.safe_load((ROOT / "config" / "news_sources.yaml").read_text())
    d = run(cfg, load_secrets())
    out = ROOT / "data" / "news"
    out.mkdir(parents=True, exist_ok=True)
    (out / "doctor.json").write_text(json.dumps(d, indent=1, default=str))
    (ROOT / "reports").mkdir(exist_ok=True)
    (ROOT / "reports" / "doctor.html").write_text(to_html(d))
    s = d["summary"]
    print(json.dumps(s, indent=1))
    for r in d["model"]:
        print(("OK  " if r["ok"] else "FAIL"), r["test"], r["detail"][:160])
    print("top failure reasons:", d["top_failure_reasons"][:6])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
