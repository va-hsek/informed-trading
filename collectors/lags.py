"""Publication lags, so a backtest only uses data that was public at the decision date.

These are conservative approximations of the release calendars:
  daily_1d  : series published with a one business-day lag (FRED yields, VIX)
  h10_weekly: Fed H.10 data (broad dollar index, USDINR) is released on Mondays for the previous week
  eia_petro : EIA weekly petroleum status, released Wednesday for the week ending the prior Friday (+5d)
  eia_gas   : EIA weekly gas storage, released Thursday for the week ending the prior Friday (+6d)
  cot       : CFTC positions as of Tuesday, released Friday (+3d)
"""
from __future__ import annotations

from datetime import date, timedelta


def visible_on(kind: str, obs_date: date) -> date:
    if kind == "daily_1d":
        return obs_date + timedelta(days=1)
    if kind == "h10_weekly":
        return obs_date + timedelta(days=(7 - obs_date.weekday()) % 7 or 7)
    if kind == "eia_petro":
        return obs_date + timedelta(days=5)
    if kind == "eia_gas":
        return obs_date + timedelta(days=6)
    if kind == "cot":
        return obs_date + timedelta(days=3)
    raise ValueError(f"unknown lag kind {kind}")


def visible_series(series, kind: str, asof: date):
    """Observations that were public on `asof` (visible date <= asof)."""
    return [(d, v) for d, v in series if visible_on(kind, d) <= asof]
