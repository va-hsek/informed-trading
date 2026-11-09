"""Fail-closed validation of observations.

An observation is a dict:
    {"value": float, "as_of": ISO-8601 str with tz, "source": str,
     "verified": bool, "second_value": float|None}

Rules (any failure => observation is EXCLUDED and reported, never guessed):
  - value must be a finite number
  - as_of must parse and carry a timezone
  - age must not exceed the driver's stale_after_hours
  - if a second independent value is given, it must agree within tolerance
  - the driver's source status must be 'verified'
  - optional sanity range must hold
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


@dataclass
class Checked:
    driver_id: str
    metric: str
    ok: bool
    value: Optional[float]
    reason: str          # why it was excluded, or "ok"
    as_of: Optional[str]
    age_hours: Optional[float]
    source: Optional[str]
    detail: Optional[dict] = None   # human-readable data point built by the collector


def _parse_ts(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        raise ValueError("timestamp has no timezone")
    return dt


def check_observation(
    driver: dict,
    obs: Optional[dict],
    now: datetime,
    tolerance_pct: float = 0.5,
    sanity_range: Optional[tuple[float, float]] = None,
    fetch_error: Optional[str] = None,
) -> Checked:
    did, metric = driver["id"], driver["metric"]

    def bad(reason, v=None, as_of=None, age=None, src=None):
        return Checked(did, metric, False, v, reason, as_of, age, src)

    if driver.get("source", {}).get("status") != "verified":
        return bad("source not verified yet (status != verified)")

    if obs is None:
        return bad(f"fetch failed: {fetch_error}" if fetch_error else "no observation supplied")

    src = obs.get("source")
    v = obs.get("value")
    if v is None or isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return bad("value missing or not a finite number", src=src)

    if not obs.get("as_of"):
        return bad("timestamp missing", v=v, src=src)
    try:
        ts = _parse_ts(obs["as_of"])
    except Exception as e:  # noqa: BLE001
        return bad(f"timestamp unusable: {e}", v=v, as_of=obs.get("as_of"), src=src)

    if now.tzinfo is None:
        raise ValueError("'now' must be timezone-aware")
    age_h = (now.astimezone(timezone.utc) - ts.astimezone(timezone.utc)).total_seconds() / 3600
    if age_h < -0.01:
        return bad("timestamp is in the future", v=v, as_of=obs["as_of"], age=age_h, src=src)
    if age_h > driver["stale_after_hours"]:
        return bad(
            f"stale: {age_h:.1f}h old, limit {driver['stale_after_hours']}h",
            v=v, as_of=obs["as_of"], age=age_h, src=src,
        )

    if not obs.get("verified", False):
        return bad("collector did not mark this value verified", v=v, as_of=obs["as_of"], age=age_h, src=src)

    second = obs.get("second_value")
    if second is not None:
        if not isinstance(second, (int, float)) or not math.isfinite(second):
            return bad("second value is not a finite number", v=v, as_of=obs["as_of"], age=age_h, src=src)
        denom = max(abs(v), abs(second), 1e-9)
        if abs(v - second) / denom * 100 > tolerance_pct:
            return bad(
                f"two independent extractions disagree ({v} vs {second})",
                v=v, as_of=obs["as_of"], age=age_h, src=src,
            )

    if sanity_range is not None and not (sanity_range[0] <= v <= sanity_range[1]):
        return bad(
            f"outside sanity range {sanity_range}", v=v, as_of=obs["as_of"], age=age_h, src=src
        )

    return Checked(did, metric, True, float(v), "ok", obs["as_of"], age_h, src, obs.get("detail"))
