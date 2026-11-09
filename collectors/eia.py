"""EIA collectors: weekly crude stocks (API v2) and weekly natural gas storage (keyless report JSON)."""
from __future__ import annotations

from datetime import date, datetime
from typing import Callable, Optional

from .http import FetchError, get_json

CRUDE_URL = "https://api.eia.gov/v2/petroleum/stoc/wstk/data/"
CRUDE_SERIES = "WCESTUS1"
NGS_URL = "https://ir.eia.gov/ngs/wngsr.json"


def _num(x) -> float:
    if isinstance(x, bool):
        raise ValueError("bool is not a number")
    if isinstance(x, (int, float)):
        return float(x)
    return float(str(x).replace(",", "").replace("%", "").strip())


def parse_crude(payload: dict) -> list[tuple[date, float]]:
    """Returns (week-ending date, thousand barrels). Asserts series and units on every row."""
    try:
        rows = payload["response"]["data"]
    except Exception as e:  # noqa: BLE001
        raise FetchError("EIA crude: response has no response.data (schema changed?)") from e
    if not isinstance(rows, list) or not rows:
        raise FetchError("EIA crude: no rows returned")
    out = []
    for r in rows:
        if r.get("series") != CRUDE_SERIES:
            raise FetchError(f"EIA crude: unexpected series {r.get('series')!r}")
        if str(r.get("units", "")).upper() != "MBBL":
            raise FetchError(f"EIA crude: unexpected units {r.get('units')!r} (expected MBBL)")
        try:
            out.append((datetime.strptime(r["period"], "%Y-%m-%d").date(), _num(r["value"])))
        except Exception as e:  # noqa: BLE001
            raise FetchError(f"EIA crude: unparseable row {r!r}") from e
    return out


def fetch_crude(api_key: str, start: date, http_json: Callable = get_json) -> list[tuple[date, float]]:
    params = [
        ("api_key", api_key), ("frequency", "weekly"), ("data[0]", "value"),
        ("facets[series][]", CRUDE_SERIES), ("start", start.isoformat()),
        ("sort[0][column]", "period"), ("sort[0][direction]", "desc"),
        ("offset", "0"), ("length", "5000"),
    ]
    return parse_crude(http_json(CRUDE_URL, params))


def _parse_date(s: str) -> date:
    s = str(s).strip().split(" ")[0] if " " in str(s) and ":" in str(s) else str(s).strip()
    for fmt in ("%Y-%m-%d", "%Y-%b-%d", "%m/%d/%Y", "%b %d, %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognised date {s!r}")


def parse_ngs(payload: dict) -> dict:
    """Parse EIA's weekly storage report JSON. Cross-checks our own % against EIA's published %."""
    try:
        series = payload["series"]
        l48 = next(s for s in series if "lower 48" in str(s.get("name", "")).lower())
        calc = l48["calculated"]
        pair = l48["data"][0]
        cur_date = _parse_date(payload.get("current_week") or pair[0])
        cur = _num(pair[1])
        avg5 = _num(calc["5yr-avg"])
        published = _num(calc["pct-chg_5yr-avg"])
        released = _parse_date(payload["release_date"])
    except StopIteration as e:
        raise FetchError("EIA gas storage: no 'Lower 48' series found (schema changed?)") from e
    except Exception as e:  # noqa: BLE001
        raise FetchError(f"EIA gas storage: unexpected schema ({type(e).__name__}: {e})") from e
    units = (str(l48.get("units", "")) + str(l48.get("unitsshort", ""))).lower()
    if "bcf" not in units and "billion cubic" not in units:
        raise FetchError(f"EIA gas storage: unexpected units {units!r}")
    if avg5 <= 0 or cur <= 0:
        raise FetchError("EIA gas storage: non-positive values")
    ours = (cur / avg5 - 1) * 100
    if abs(ours - published) > 0.2:
        raise FetchError(f"EIA gas storage: our % ({ours:.2f}) disagrees with EIA's published % ({published:.2f})")
    net = None
    try:
        net = _num(calc["net_change"])
    except Exception:  # noqa: BLE001
        pass
    return {"week": cur_date, "released": released, "current_bcf": cur, "avg5_bcf": avg5,
            "pct_vs_5y": ours, "published_pct": published, "net_change_bcf": net}


def fetch_ngs(http_json: Callable = get_json) -> dict:
    return parse_ngs(http_json(NGS_URL))
