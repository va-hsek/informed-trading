"""Pure metric maths shared by live runs and the backtest. No network, no clock.

Every function returns (value, as_of_date, detail) where detail["text"] is a full
human sentence with the numbers and dates, so the report can show the data point
exactly as the user asked: current level, what it is compared with, from which
date, and the change.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timezone
from statistics import mean
from typing import Sequence

Series = Sequence[tuple[date, float]]


class MetricError(ValueError):
    pass


def fmt_date(d: date) -> str:
    return f"{d.day} {d.strftime('%b %Y')}"


def as_of_iso(d: date) -> str:
    """Observation date at 00:00 UTC. Conservative: it makes data look slightly older, never newer."""
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).isoformat()


def num(v: float, nd: int = 4) -> str:
    s = f"{v:,.{nd}f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def clean(series: Series) -> list[tuple[date, float]]:
    out = []
    for d, v in series:
        if v is None or isinstance(v, bool) or not math.isfinite(float(v)):
            continue
        out.append((d, float(v)))
    out.sort(key=lambda x: x[0])
    dates = [d for d, _ in out]
    if len(set(dates)) != len(dates):
        raise MetricError("duplicate dates in series")
    return out


def change_pct(series: Series, n: int, label: str, unit: str = ""):
    s = clean(series)
    if len(s) < n + 1:
        raise MetricError(f"need {n + 1} observations, have {len(s)}")
    (d1, v1), (d0, v0) = s[-1], s[-1 - n]
    if v0 == 0:
        raise MetricError("reference value is zero")
    pct = (v1 / v0 - 1) * 100
    text = (f"{label} {num(v1)}{unit} on {fmt_date(d1)}, versus {num(v0)}{unit} on {fmt_date(d0)} "
            f"({n} observations earlier): change {pct:+.2f}%")
    return pct, d1, {"text": text, "current": v1, "current_date": d1.isoformat(),
                     "reference": v0, "reference_date": d0.isoformat(), "kind": "change_pct"}


def change_abs(series: Series, n: int, label: str, unit: str = "%", change_unit: str = "pp"):
    s = clean(series)
    if len(s) < n + 1:
        raise MetricError(f"need {n + 1} observations, have {len(s)}")
    (d1, v1), (d0, v0) = s[-1], s[-1 - n]
    diff = v1 - v0
    text = (f"{label} {v1:.2f}{unit} on {fmt_date(d1)}, versus {v0:.2f}{unit} on {fmt_date(d0)} "
            f"({n} observations earlier): change {diff:+.2f} {change_unit}")
    return diff, d1, {"text": text, "current": v1, "current_date": d1.isoformat(),
                      "reference": v0, "reference_date": d0.isoformat(), "kind": "change_abs"}


def level(series: Series, label: str, unit: str = ""):
    s = clean(series)
    if not s:
        raise MetricError("empty series")
    d1, v1 = s[-1]
    return v1, d1, {"text": f"{label} was {num(v1)}{unit} on {fmt_date(d1)}",
                    "current": v1, "current_date": d1.isoformat(), "kind": "level"}


def _years_back(d: date, k: int) -> date:
    try:
        return d.replace(year=d.year - k)
    except ValueError:                      # 29 Feb
        return d.replace(year=d.year - k, day=28)


def _same_week(s: list[tuple[date, float]], target: date, tol: int):
    cand = [(abs((d - target).days), i) for i, (d, _) in enumerate(s) if abs((d - target).days) <= tol]
    return min(cand)[1] if cand else None


def seasonal_vs_avg(series: Series, label: str, unit: str = "", years: int = 5,
                    tol_days: int = 3, min_years: int = 4):
    """% deviation of the latest value from the average of the same week in prior years."""
    s = clean(series)
    if len(s) < 2:
        raise MetricError("series too short")
    d1, v1 = s[-1]
    comps = []
    for k in range(1, years + 1):
        i = _same_week(s, _years_back(d1, k), tol_days)
        if i is not None and i < len(s) - 1:
            comps.append(s[i])
    if len(comps) < min_years:
        raise MetricError(f"only {len(comps)} comparable prior years found; need {min_years}")
    avg = mean(v for _, v in comps)
    if avg == 0:
        raise MetricError("seasonal average is zero")
    pct = (v1 / avg - 1) * 100
    yrs = ", ".join(str(d.year) for d, _ in comps)
    text = (f"{label} {num(v1)}{unit} for the week of {fmt_date(d1)}, versus a same-week average of "
            f"{num(avg)}{unit} over {len(comps)} prior years ({yrs}): {pct:+.2f}%")
    return pct, d1, {"text": text, "current": v1, "current_date": d1.isoformat(), "average": avg,
                     "years_used": [d.isoformat() for d, _ in comps], "kind": "seasonal_pct"}


def seasonal_change_vs_avg(series: Series, label: str, unit: str = "", scale: float = 1.0,
                           out_unit: str = "", years: int = 5, tol_days: int = 3, min_years: int = 4):
    """This week's change minus the average change of the same week in prior years (scaled)."""
    s = clean(series)
    if len(s) < 3:
        raise MetricError("series too short")
    (dp, vp), (d1, v1) = s[-2], s[-1]
    if not 5 <= (d1 - dp).days <= 9:
        raise MetricError(f"latest two observations are {(d1 - dp).days} days apart, not weekly")
    cur = v1 - vp
    changes = []
    for k in range(1, years + 1):
        i = _same_week(s, _years_back(d1, k), tol_days)
        if i is None or i == 0:
            continue
        if 5 <= (s[i][0] - s[i - 1][0]).days <= 9:
            changes.append(s[i][1] - s[i - 1][1])
    if len(changes) < min_years:
        raise MetricError(f"only {len(changes)} comparable prior-year changes found; need {min_years}")
    avg = mean(changes)
    val = (cur - avg) * scale
    text = (f"{label} changed {cur * scale:+.2f}{out_unit} in the week of {fmt_date(d1)} "
            f"(from {num(vp)}{unit} to {num(v1)}{unit}); the same-week average change over {len(changes)} prior "
            f"years is {avg * scale:+.2f}{out_unit}; difference {val:+.2f}{out_unit}")
    return val, d1, {"text": text, "current_change": cur * scale, "avg_change": avg * scale,
                     "current_date": d1.isoformat(), "kind": "seasonal_change"}


def percentile_latest(series: Series, window: int, min_n: int, label: str):
    """Share (0-100) of the last `window` observations that are <= the latest one."""
    s = clean(series)
    w = s[-window:]
    if len(w) < min_n:
        raise MetricError(f"only {len(w)} observations in the window; need {min_n}")
    d1, v1 = w[-1]
    pct = 100.0 * sum(1 for _, v in w if v <= v1) / len(w)
    text = (f"{label} {num(v1, 2)} on {fmt_date(d1)}; that is higher than or equal to {pct:.0f}% of the last "
            f"{len(w)} weekly observations (range {num(min(v for _, v in w), 2)} to {num(max(v for _, v in w), 2)})")
    return pct, d1, {"text": text, "current": v1, "current_date": d1.isoformat(), "window": len(w), "kind": "percentile"}


def make_obs(value: float, d: date, source: str, detail: dict) -> dict:
    return {"value": float(value), "as_of": as_of_iso(d), "source": source, "verified": True, "detail": detail}
