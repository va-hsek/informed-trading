"""FRED collector. Uses the API when a key is present, else the keyless CSV endpoint."""
from __future__ import annotations

import csv
import io
import math
from datetime import date, datetime
from typing import Callable, Optional

from .http import FetchError, get, get_json

API = "https://api.stlouisfed.org/fred/series/observations"
CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv"


def parse_fred_json(payload: dict, series_id: str) -> list[tuple[date, float]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("observations"), list):
        raise FetchError(f"FRED {series_id}: response has no 'observations' list (schema changed?)")
    out = []
    for row in payload["observations"]:
        if not isinstance(row, dict) or "date" not in row or "value" not in row:
            raise FetchError(f"FRED {series_id}: observation row missing date/value")
        if row["value"] in (".", "", None):
            continue                                   # FRED marks missing days with "."
        try:
            v = float(row["value"])
            d = datetime.strptime(row["date"], "%Y-%m-%d").date()
        except Exception as e:  # noqa: BLE001
            raise FetchError(f"FRED {series_id}: unparseable row {row!r}") from e
        if not math.isfinite(v):
            continue
        out.append((d, v))
    if not out:
        raise FetchError(f"FRED {series_id}: no usable observations")
    return out


def parse_fred_csv(text: str, series_id: str) -> list[tuple[date, float]]:
    rows = list(csv.reader(io.StringIO(text)))
    if len(rows) < 2 or len(rows[0]) < 2:
        raise FetchError(f"FRED {series_id}: CSV has no data")
    header = [h.strip().upper() for h in rows[0]]
    if series_id.upper() not in header:
        raise FetchError(f"FRED {series_id}: CSV header {header} does not contain the series")
    col = header.index(series_id.upper())
    out = []
    for r in rows[1:]:
        if len(r) <= col or r[col].strip() in (".", ""):
            continue
        try:
            out.append((datetime.strptime(r[0].strip(), "%Y-%m-%d").date(), float(r[col])))
        except Exception as e:  # noqa: BLE001
            raise FetchError(f"FRED {series_id}: unparseable CSV row {r!r}") from e
    if not out:
        raise FetchError(f"FRED {series_id}: no usable observations")
    return out


def fetch_fred(series_id: str, api_key: Optional[str], start: date,
               http_json: Callable = get_json, http_raw: Callable = get) -> list[tuple[date, float]]:
    if api_key:
        payload = http_json(API, [("series_id", series_id), ("api_key", api_key), ("file_type", "json"),
                                  ("observation_start", start.isoformat())])
        return parse_fred_json(payload, series_id)
    raw = http_raw(CSV, [("id", series_id), ("cosd", start.isoformat())])
    return parse_fred_csv(raw.decode("utf-8", "replace"), series_id)
