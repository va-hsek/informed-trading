"""Market dashboard: energy, metals, FX, rates/debt, equity. Fail closed on stale or missing data.

Each row: latest value, as-of date, 1-day / 5-day / 1-month change, source. A row whose newest
observation is older than its limit shows 'n/a (newest data N days old)' instead of a number.
Writes data/dashboard_latest.json for the news report's 'Markets at a glance'.
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

import yaml

from collectors import fred
from collectors.http import FetchError
from collectors.metrics import clean, fmt_date, num
from core.prices import PriceDataError, PriceStore, read_csv
from core.secrets import redact

LAGS = {"1d": 1, "5d": 5, "1m": 21}


def _chg(series, n: int, kind: str) -> Optional[float]:
    if len(series) < n + 1:
        return None
    v1, v0 = series[-1][1], series[-1 - n][1]
    if kind == "yield":
        return v1 - v0
    return None if v0 == 0 else (v1 / v0 - 1) * 100


def _chg_text(x: Optional[float], kind: str) -> str:
    if x is None:
        return "n/a"
    return f"{x:+.2f} pp" if kind == "yield" else f"{x:+.2f}%"


def _manual_series(path: Path, name: str):
    if not path.exists():
        return []
    items = ((yaml.safe_load(path.read_text()) or {}).get("dashboard_manual") or {}).get(name) or []
    out = []
    for it in items:
        d = it["date"] if isinstance(it["date"], date) else date.fromisoformat(str(it["date"]))
        out.append((d, float(it["value"])))
    return out


def load_series(src: str, root: Path, store: PriceStore, secrets: dict, now: datetime, cache: dict,
                http_json=None, http_raw=None):
    kind, _, name = src.partition(":")
    if kind == "fred":
        if name not in cache:
            from collectors.http import get, get_json
            cache[name] = fred.fetch_fred(name, secrets.get("FRED_API_KEY"), now.date() - timedelta(days=75),
                                          http_json or get_json, http_raw or get)
        return cache[name]
    if kind == "price":
        df = store.continuous(name)
        return [(i.date(), float(r["close"])) for i, r in df.iterrows()]
    if kind == "csv":
        p = store.root / "index" / f"{name}.csv"
        if not p.exists():
            raise FetchError(f"{p.relative_to(store.root.parent.parent) if store.root.parent.parent in p.parents else p} not supplied")
        df = read_csv(p)
        return [(i.date(), float(r["close"])) for i, r in df.iterrows()]
    if kind == "india10y":
        from collectors.india_yield import read_history
        h = read_history(root / "data" / "history" / "india_10y_benchmark.csv")
        if not h:
            raise FetchError("no India 10-year readings stored yet (the run fetches the AMC tracker; see collectors/india_yield.py)")
        return h
    if kind == "manual":
        s = _manual_series(root / "config" / "manual_inputs.yaml", name)
        if not s:
            raise FetchError("not entered in config/manual_inputs.yaml")
        return s
    raise FetchError(f"unknown source type '{kind}'")


def build_rows(root: Path, now: datetime, secrets: dict, http_json=None, http_raw=None) -> list[dict]:
    cfg = yaml.safe_load((root / "config" / "dashboard.yaml").read_text())
    ucfg = yaml.safe_load((root / "config" / "user.yaml").read_text())
    store = PriceStore(root / ucfg.get("data_dir", "data") / "prices")
    cache: dict = {}
    rows = []
    today = now.date()
    for group, items in cfg["groups"].items():
        for it in items:
            row = {"group": group, "label": it["label"], "source": it["source"], "note": it.get("note", ""),
                   "value": None, "value_text": "n/a", "as_of": None, "chg": {}, "chg_text": {}, "problem": None}
            try:
                s = clean(load_series(it["source"], root, store, secrets, now, cache, http_json, http_raw))
                if not s:
                    raise FetchError("no observations")
                d, v = s[-1]
                age = (today - d).days
                if age < 0:
                    raise FetchError(f"newest observation is dated {d}, in the future")
                if age > int(it.get("max_age_days", 5)):
                    raise FetchError(f"newest data {age} days old ({fmt_date(d)})")
                row.update(value=v, value_text=num(v, 2), as_of=d.isoformat())
                for k, n in LAGS.items():
                    c = _chg(s, n, it["kind"])
                    row["chg"][k] = c
                    row["chg_text"][k] = _chg_text(c, it["kind"])
            except (FetchError, PriceDataError, ValueError, KeyError) as e:
                row["problem"] = redact(str(e), secrets)
                row["value_text"] = f"n/a ({row['problem']})"
            rows.append(row)
    return rows


def render_dashboard(rows: list[dict]) -> list[str]:
    L = ["Changes are 1 trading observation / 5 / 21 observations earlier (so '1m' is about a month). "
         "Yields and spreads change in percentage points. 'n/a' means the data was missing or too old to call current.", ""]
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r["group"], []).append(r)
    for g, rs in groups.items():
        L += [f"### {g}", "", "| Metric | Latest | As of | 1d | 5d | 1m | Source |", "|---|---|---|---|---|---|---|"]
        for r in rs:
            if r["problem"]:
                L.append(f"| {r['label']} | {r['value_text']} | | | | | {r['source']} |")
            else:
                d = date.fromisoformat(r["as_of"])
                src = r["source"] + (f" ({r['note']})" if r["note"] else "")
                L.append(f"| {r['label']} | {r['value_text']} | {fmt_date(d)} | {r['chg_text']['1d']} | "
                         f"{r['chg_text']['5d']} | {r['chg_text']['1m']} | {src} |")
        L.append("")
    return L


def write_latest(rows: list[dict], now: datetime, path: Path) -> None:
    out = {"generated": now.isoformat(), "rows": [
        {"label": r["label"], "value": r["value"], "value_text": r["value_text"], "as_of": r["as_of"],
         "chg_text": r["chg_text"].get("5d", "n/a") if r["chg_text"] else "n/a", "source": r["source"]}
        for r in rows if not r["problem"]]}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=1))
