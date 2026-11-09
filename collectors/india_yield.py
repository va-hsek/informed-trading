"""India 10-year G-sec yield from the Aditya Birla Sun Life AMC "Daily Fixed Income Tracker" PDF.

Why this source: Alice Blue's API documents no bond-yield data; FBIL/CCIL/RBI pages block automated readers (robots.txt) and have no
free API; FRED's India series is monthly with a ~6-week lag. This tracker is published every business day by an SEBI-registered AMC,
citing RBI/CCIL/Refinitiv. It is still ONE source, so the dashboard marks it single-source.

Two different 10-year numbers appear in the same PDF and they DIFFER (on 5 Nov 2026: 7.17% vs 7.30%):
  - the closing yield of the benchmark paper (e.g. 6.94% GS 2036) from the text: "close at 7.17%"  <- we use this one
  - the "10 Year G-Sec" row of the Yield Monitor table (a generic 10-year quote): reported separately, never mixed in.
We store each day we read into data/history/india_10y_benchmark.csv so 1-day / 5-day / 1-month changes build up over time.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from collectors.http import FetchError

URL = "https://mutualfund.adityabirlacapital.com/-/media/knowledgecenter/pdf/all-about-market/daily-fixed-income-tracker-{d:02d}-{mon}-{y}.pdf"
MON = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def pdf_text(raw: bytes) -> str:
    try:
        from pypdf import PdfReader                      # lazy: listed in requirements.txt
    except ImportError as e:
        raise FetchError("pypdf is not installed (pip install pypdf)") from e
    try:
        return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(raw)).pages)
    except Exception as e:  # noqa: BLE001
        raise FetchError(f"PDF could not be read ({type(e).__name__})") from e


def parse_tracker(text: str) -> dict:
    """Returns {benchmark_label, benchmark_close, generic_10y, as_of}. Raises FetchError unless the benchmark line and its date are found."""
    t = re.sub(r"[ \t]+", " ", text)
    m = re.search(r"10-year benchmark paper \((\d+\.\d+% GS \d{4})\)[^%]*?(?:close at|closed at|ended at)\s*(\d+\.\d{2})%", t, re.I)
    if not m:
        raise FetchError("benchmark close line not found in the tracker")
    label, close = m.group(1), float(m.group(2))
    d = re.search(r"Corporate Bonds/G-Sec\s+(\d{2})-([A-Za-z]{3})-(\d{2})", t)
    if not d:
        raise FetchError("Yield Monitor date header not found")
    try:
        as_of = datetime.strptime(f"{d.group(1)}-{d.group(2).title()}-{d.group(3)}", "%d-%b-%y").date()
    except ValueError as e:
        raise FetchError("Yield Monitor date unreadable") from e
    g = re.search(r"10 Year G-Sec\s+(\d+\.\d{2})", t)
    generic = float(g.group(1)) if g else None
    if not 3.0 <= close <= 15.0:
        raise FetchError(f"benchmark yield {close} is outside a sane range")
    if generic is not None and abs(generic - close) > 0.6:
        raise FetchError(f"benchmark close {close} and generic 10-year {generic} differ by more than 0.6 pp; not trusted")
    return {"benchmark_label": label, "benchmark_close": close, "generic_10y": generic, "as_of": as_of}


def fetch_latest(today: date, http_get: Callable, lookback_days: int = 6) -> dict:
    last = "no attempt"
    for k in range(lookback_days + 1):
        d = today - timedelta(days=k)
        if d.weekday() >= 5:
            continue
        url = URL.format(d=d.day, mon=MON[d.month - 1], y=d.year)
        try:
            out = parse_tracker(pdf_text(http_get(url)))
        except FetchError as e:
            last = f"{d}: {e}"
            continue
        out["url"] = url
        return out
    raise FetchError(f"India 10-year tracker not found for the last {lookback_days} days (last error: {last})")


def append_history(path: Path, rec: dict) -> None:
    rows = {}
    if path.exists():
        for r in csv.DictReader(path.open()):
            rows[r["date"]] = r
    rows[rec["as_of"].isoformat()] = {"date": rec["as_of"].isoformat(), "benchmark_close": f"{rec['benchmark_close']:.2f}",
                                      "generic_10y": "" if rec["generic_10y"] is None else f"{rec['generic_10y']:.2f}",
                                      "benchmark": rec["benchmark_label"], "source": rec["url"]}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "benchmark_close", "generic_10y", "benchmark", "source"])
        w.writeheader()
        for k in sorted(rows):
            w.writerow(rows[k])


def read_history(path: Path) -> list[tuple[date, float]]:
    if not path.exists():
        return []
    return [(date.fromisoformat(r["date"]), float(r["benchmark_close"])) for r in csv.DictReader(path.open())]
