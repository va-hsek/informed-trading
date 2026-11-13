"""Market tiles for the top of the page from a free public source (Stooq daily CSV), so the strip no longer depends on the Alice Blue dashboard.
Fail closed: a symbol that cannot be fetched or parsed, or whose last close is older than `max_age_days`, simply gets no tile; each tile states its own
as-of date and source. Nothing is estimated or filled in. Whether Stooq answers from the cloud runner is only learned on a real run; failures are
listed in data/news/diagnostics.json (markets_errors)."""
from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta
from typing import Callable, Optional

URL = "https://stooq.com/q/d/l/?s={sym}&d1={d1}&d2={d2}&i=d"


def parse_closes(raw: str) -> list[tuple[str, float]]:
    """[(date, close)] oldest first from Stooq's CSV; anything unexpected gives []."""
    if not raw or "date" not in raw.lower().split("\n", 1)[0]:
        return []
    out = []
    for row in csv.DictReader(io.StringIO(raw)):
        try:
            out.append((row["Date"], float(row["Close"])))
        except (KeyError, ValueError, TypeError):
            continue
    return sorted(out)


def fetch_markets(specs: list[dict], now: datetime, http_get: Callable, max_age_days: int = 4) -> tuple[list[dict], list[str]]:
    rows, errors = [], []
    d2, d1 = now.strftime("%Y%m%d"), (now - timedelta(days=12)).strftime("%Y%m%d")
    for sp in specs or []:
        try:
            raw = http_get(URL.format(sym=sp["symbol"], d1=d1, d2=d2)).decode("utf-8", "replace")
            closes = parse_closes(raw)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{sp.get('label')}: {type(e).__name__}")
            continue
        if len(closes) < 2:
            errors.append(f"{sp.get('label')}: no usable data")
            continue
        (d_prev, prev), (d_last, last) = closes[-2], closes[-1]
        try:
            age = (now.replace(tzinfo=None) - datetime.strptime(d_last, "%Y-%m-%d")).days
        except ValueError:
            errors.append(f"{sp.get('label')}: bad date")
            continue
        if age > max_age_days or prev == 0:
            errors.append(f"{sp.get('label')}: stale or unusable (last close {d_last})")
            continue
        dec = int(sp.get("decimals", 2))
        pre = sp.get("prefix", "")
        pct = (last - prev) / prev * 100
        rows.append({"label": sp["label"], "value": last, "value_text": f"{pre}{last:,.{dec}f}", "chg_text": f"{pct:+.2f}%",
                     "as_of": f"close {datetime.strptime(d_last, '%Y-%m-%d'):%d %b}", "source": "Stooq"})
    return rows, errors
