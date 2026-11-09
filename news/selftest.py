"""Pre-flight check, run before the news edition (and by hand when something looks wrong):

    python3 -m news.selftest

Tests the model key (plain call, JSON call, web-search call) and whether the runner can reach feeds, article pages and the archive
services. Prints a table and writes data/news/selftest.json so the result can be read from the repo. Never raises, never prints keys."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

from core.secrets import load_secrets
from news.llm import make_llm
from news.run_news import news_get

ROOT = Path(__file__).resolve().parent.parent
URLS = [
    ("BBC business feed", "https://feeds.bbci.co.uk/news/business/rss.xml"),
    ("Economic Times feed", "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
    ("Mint feed", "https://www.livemint.com/rss/markets"),
    ("Moneycontrol feed", "https://www.moneycontrol.com/rss/business.xml"),
    ("RBI press releases", "https://www.rbi.org.in/pressreleases_rss.xml"),
    ("Wayback availability API", "https://archive.org/wayback/available?url=ft.com"),
    ("Memento aggregator", "https://timetravel.mementoweb.org/timemap/json/https://www.ft.com/"),
    ("archive.today", "https://archive.ph/newest/https://www.bbc.com/news"),
    ("Google News RSS", "https://news.google.com/rss/search?q=sensex&hl=en-IN&gl=IN&ceid=IN:en"),
]


def check_model(cfg: dict, secrets: dict) -> list[dict]:
    rows = []
    llm = make_llm(cfg, secrets, "extract", None)
    if llm is None:
        return [{"test": "model key", "ok": False, "detail": "no GEMINI_API_KEY or ANTHROPIC_API_KEY set"}]
    rows.append({"test": "model key", "ok": True, "detail": f"provider={llm.provider} model={llm.model}"})
    for name, fn in (("model: plain call", lambda: llm.complete("Reply with exactly: {\"ok\": true}", "ping", max_tokens=20)),
                     ("model: JSON mode", lambda: llm.complete("Reply with a JSON object {\"claims\": []}", "ping", max_tokens=40, json_mode=True)),
                     ("model: web search", lambda: llm.complete_with_search("Reply briefly.", "What is the RBI repo rate today?", "web_search_20250305", 1, 200))):
        t = time.time()
        try:
            out = fn()
            rows.append({"test": name, "ok": bool(str(out).strip()), "detail": f"{time.time() - t:.1f}s, reply: {str(out).strip()[:80]!r}"})
        except Exception as e:  # noqa: BLE001
            rows.append({"test": name, "ok": False, "detail": f"{type(e).__name__}: {e}"[:300]})
    return rows


def check_network() -> list[dict]:
    rows = []
    for name, url in URLS:
        t = time.time()
        try:
            data = news_get(url, timeout=20, retries=0)
            rows.append({"test": f"fetch: {name}", "ok": True, "detail": f"{len(data):,} bytes in {time.time() - t:.1f}s"})
        except Exception as e:  # noqa: BLE001
            rows.append({"test": f"fetch: {name}", "ok": False, "detail": str(e)[:200]})
    return rows


def main() -> int:
    cfg = yaml.safe_load((ROOT / "config" / "news_sources.yaml").read_text())
    rows = check_model(cfg, load_secrets()) + check_network()
    for r in rows:
        print(f"{'OK  ' if r['ok'] else 'FAIL'} {r['test']:<32} {r['detail']}")
    out = ROOT / "data" / "news"
    out.mkdir(parents=True, exist_ok=True)
    (out / "selftest.json").write_text(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "results": rows}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
