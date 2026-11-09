"""Link news to instruments. Reads data/news/latest.json (written by the news report) and
config/news_links.yaml. Shows the FACTUAL relation, the latest price from the dashboard where
one is connected, and the headline with a hyperlinked source. Never a direction or a recommendation.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml


def _kw_hit(text: str, kws: list[str]) -> bool:
    t = text.lower()
    return any(re.search(r"(?<![a-z0-9])" + re.escape(k.lower()) + r"(?![a-z0-9])", t) for k in kws)


def _link(name: str, url: str) -> str:
    return f"[{name}]({url})" if url else name


def build_links(latest: dict, links_cfg: dict, dash_rows: list[dict], resolve=None) -> list[dict]:
    """Return [{instrument, venue, kind, relation, price_text, items:[{headline, source, url}]}]."""
    cap = int(links_cfg.get("max_headlines_per_instrument", 2))
    by_label = {r["label"]: r for r in dash_rows}
    news = []
    for s in latest.get("stories", []):
        src = (s.get("sources") or [{}])[0]
        news.append({"headline": s["headline"], "topics": s.get("topics") or [], "sectors": s.get("sectors") or [], "text": s["headline"] + " " + s.get("text", ""),
                     "source": src.get("name", ""), "url": src.get("url", "")})
    for h in latest.get("headlines", []):
        news.append({"headline": h["headline"], "topics": h.get("topics") or [], "sectors": h.get("sectors") or [], "text": h["headline"],
                     "source": h.get("source", ""), "url": h.get("url", "")})

    out: dict[tuple, dict] = {}

    def add(inst: dict, n: dict):
        key = (inst["name"], inst.get("venue", ""))
        e = out.setdefault(key, {"instrument": inst["name"], "venue": inst.get("venue", ""), "kind": inst.get("kind", ""),
                                 "relation": inst.get("relation", ""), "row": inst.get("row"), "items": [],
                                 "symbols": inst.get("symbols") or [], "exchange": inst.get("exchange", "")})
        if all(i["headline"] != n["headline"] for i in e["items"]) and len(e["items"]) < cap:
            e["items"].append({"headline": n["headline"], "source": n["source"], "url": n["url"]})

    for n in news:
        for t in n["topics"]:
            for inst in (links_cfg.get("topics") or {}).get(t, []):
                add(inst, n)
        for sec in n["sectors"][:1]:
            for inst in (links_cfg.get("sectors") or {}).get(sec, []):
                add({**inst, "venue": inst.get("exchange", "NSE")}, n)
        for nm in links_cfg.get("names") or []:
            if _kw_hit(n["text"], nm["keywords"]):
                add({"name": nm["name"], "venue": nm.get("venue", ""), "relation": "Named in this news", "symbols": nm.get("symbols"), "exchange": nm.get("exchange", "")}, n)

    res = []
    for e in out.values():
        r = by_label.get(e.pop("row") or "")
        e["symbol_text"] = symbol_status(e.pop("symbols"), e.pop("exchange"), resolve)
        e["price_text"] = (f"{r['value_text']} as of {r['as_of']}, 5-obs change {r['chg_text']['5d']}" if r and not r["problem"]
                           else ("price not connected" if not r else f"price unavailable ({r['problem']})"))
        res.append(e)
    res.sort(key=lambda e: (-len(e["items"]), e["instrument"]))
    return res


def symbol_status(symbols: list, exchange: str, resolve) -> str:
    """Each symbol is checked against Alice Blue's contract master when connected; never trusted from memory."""
    if not symbols:
        return ""
    if resolve is None:
        return "symbols: " + ", ".join(symbols) + " (not yet checked: Alice Blue not connected)"
    ok, bad = [], []
    for sym in symbols:
        try:
            hit = resolve(exchange or "NSE", sym)
        except Exception:  # noqa: BLE001
            hit = None
            bad.append(f"{sym} (lookup failed)")
            continue
        if hit:
            ok.append(f"{hit['symbol']}" + (f" lot {hit['lot_size']:g}" if hit.get("lot_size") else ""))
        else:
            bad.append(f"{sym} (not found in Alice Blue)")
    return "; ".join(filter(None, ["confirmed in Alice Blue: " + ", ".join(ok) if ok else "", "NOT confirmed: " + ", ".join(bad) if bad else ""]))


def render_links(links: list[dict], latest_generated: str, now: datetime, max_age_hours: float = 36) -> list[str]:
    L = ["Instruments that have news today. The relation column states exposure only; it is not a view on direction. "
         "Symbols are checked against Alice Blue's contract master whenever the broker is connected.", ""]
    try:
        gen = datetime.fromisoformat(latest_generated)
        age = (now - gen).total_seconds() / 3600
    except Exception:  # noqa: BLE001
        return ["Not available: news file has no valid timestamp."]
    if age > max_age_hours:
        return [f"Not shown: the latest news edition is {age:.0f} hours old (limit {max_age_hours:.0f}). Run the news report first."]
    if not links:
        return L + ["No stories in the latest edition matched a tracked instrument."]
    L += ["| Instrument | Venue | Exposure | Symbols | Price (dashboard) | News |", "|---|---|---|---|---|---|"]
    for e in links:
        news = "<br>".join(f"{i['headline']} ({_link(i['source'], i['url'])})" for i in e["items"])
        tag = " (debt)" if e["kind"] == "debt" else ""
        L.append(f"| {e['instrument']}{tag} | {e['venue']} | {e['relation']} | {e['symbol_text'] or '-'} | {e['price_text']} | {news} |")
    L.append("")
    return L


def load_and_render(root: Path, now: datetime, dash_rows: list[dict], resolve=None) -> list[str]:
    p = root / "data" / "news" / "latest.json"
    if not p.exists():
        return ["Not available: no news edition has been generated yet (run `python3 -m news.run_news`)."]
    try:
        latest = json.loads(p.read_text())
        cfg = yaml.safe_load((root / "config" / "news_links.yaml").read_text())
        return render_links(build_links(latest, cfg, dash_rows, resolve), latest.get("generated", ""), now)
    except Exception as e:  # noqa: BLE001
        return [f"Not available: news link step failed ({type(e).__name__}: {e})."]
