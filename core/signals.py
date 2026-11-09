"""Fundamental layer: deterministic driver rules -> direction, agreement, trace.

No LLM. No price levels. Only: which rules fired, on which numbers, with what weight.
"""
from __future__ import annotations

import operator
from dataclasses import dataclass, field
from typing import Optional

from .validation import Checked

_OPS = {">": operator.gt, "<": operator.lt, ">=": operator.ge, "<=": operator.le}


@dataclass
class Firing:
    driver_id: str
    name: str
    metric: str
    value: float
    op: str
    threshold: float
    effect: float          # signed multiplier for THIS asset
    weight: float          # driver weight
    contribution: float    # weight * effect (signed)
    horizon: str
    as_of: str
    source: str
    why: str = ""


@dataclass
class DriverEval:
    """Everything the report needs to show one driver for one asset."""
    driver_id: str
    label: str
    metric_phrase: str
    unit: str
    value: float
    data_point: str                 # collector-built sentence with numbers and dates
    as_of: str
    age_hours: float
    source: str
    source_note: str
    weight: float
    horizon: str
    flag_only: bool
    rules: list[dict]               # [{text, op, threshold, effect, fired, why}]
    fired: bool
    contribution: float = 0.0


@dataclass
class AssetView:
    asset: str
    state: str                      # UP | DOWN | NEUTRAL | DEGRADED
    reason: str
    agreement: Optional[float]
    coverage: Optional[float]
    up_weight: float = 0.0
    down_weight: float = 0.0
    available_weight: float = 0.0
    firings: list[Firing] = field(default_factory=list)
    not_firing: list[str] = field(default_factory=list)
    excluded: list[Checked] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    horizon: Optional[str] = None
    evaluations: list[DriverEval] = field(default_factory=list)


_HORIZON_ORDER = {"intraday": 0, "swing": 1, "positional": 2}


def _fmt(x: float) -> str:
    return f"{x:g}"


def rule_text(f: Firing) -> str:
    sign = "bullish" if f.effect > 0 else "bearish"
    return (
        f"{f.driver_id}: IF {f.metric} {f.op} {f.threshold:g} "
        f"THEN {sign} (x{abs(f.effect):g}, weight {f.weight:g}). "
        f"Today {f.metric} = {f.value:g} -> contribution {f.contribution:+.2f}."
    )


def _rule_line(op: str, threshold: float, unit: str, effect: float, asset: str) -> str:
    if effect == 0:
        return f"IF value {op} {_fmt(threshold)}{unit} THEN flag only (no direction change)"
    word = "bullish" if effect > 0 else "bearish"
    return f"IF value {op} {_fmt(threshold)}{unit} THEN {asset} {word} (x{abs(effect):g})"


def evaluate_asset(
    asset: str,
    drivers: list[dict],
    checked: dict[str, Checked],
    cfg: dict,
) -> AssetView:
    """checked: driver_id -> Checked. Drivers that do not mention `asset` are ignored."""
    bar = cfg["confidence_bar"]
    min_cov = cfg["min_coverage"]
    min_n = cfg["min_firing_drivers"]

    firings: list[Firing] = []
    not_firing: list[str] = []
    excluded: list[Checked] = []
    flags: list[str] = []
    evals: list[DriverEval] = []
    available = 0.0

    for d in drivers:
        mentioned = any(asset in r.get("effects", {}) for r in d["rules"])
        if not mentioned:
            continue
        c = checked.get(d["id"])
        if c is None or not c.ok:
            excluded.append(
                c or Checked(d["id"], d["metric"], False, None, "no observation supplied", None, None, None)
            )
            continue

        flag_only = bool(d.get("flag_only", False))
        unit = d.get("unit", "")
        label = d.get("label") or d.get("name") or d["metric"]
        if not flag_only:
            available += float(d["weight"])

        rules_view: list[dict] = []
        fired_any = False
        fired_contrib = 0.0
        for r in d["rules"]:
            if asset not in r.get("effects", {}):
                continue
            eff = float(r["effects"][asset])
            hit = (not fired_any) and _OPS[r["op"]](c.value, r["threshold"])
            why = (r.get("why") or {}).get(asset, "") if isinstance(r.get("why"), dict) else (r.get("why") or "")
            rules_view.append({
                "text": _rule_line(r["op"], r["threshold"], unit, eff, asset),
                "op": r["op"], "threshold": r["threshold"], "effect": eff, "fired": hit,
                "why": why, "flag": r.get("flag"),
            })
            if hit:
                fired_any = True
                if flag_only:
                    if r.get("flag"):
                        flags.append(f"{d['id']}: {r['flag']} ({label}: {_fmt(c.value)})")
                    continue
                contrib = float(d["weight"]) * eff
                fired_contrib = contrib
                firings.append(
                    Firing(
                        d["id"], label, d["metric"], c.value, r["op"], r["threshold"],
                        eff, float(d["weight"]), contrib,
                        d["horizon"], c.as_of or "", c.source or "", why,
                    )
                )
        if not fired_any and not flag_only:
            not_firing.append(f"{d['id']} ({d['metric']} = {c.value:g}): no rule triggered")

        detail = c.detail or {}
        evals.append(DriverEval(
            driver_id=d["id"], label=label, metric_phrase=d.get("metric_phrase", d["metric"]),
            unit=unit, value=c.value,
            data_point=detail.get("text") or f"{label}: {c.value:g}",
            as_of=c.as_of or "", age_hours=c.age_hours or 0.0, source=c.source or "",
            source_note=(d.get("source") or {}).get("lag_note", ""),
            weight=float(d["weight"]), horizon=d["horizon"], flag_only=flag_only,
            rules=rules_view, fired=fired_any, contribution=fired_contrib,
        ))

    up = sum(f.contribution for f in firings if f.contribution > 0)
    down = -sum(f.contribution for f in firings if f.contribution < 0)
    total_fired = up + down
    evals.sort(key=lambda e: (not e.fired, e.driver_id))

    view = AssetView(
        asset=asset, state="NEUTRAL", reason="", agreement=None, coverage=None,
        up_weight=up, down_weight=down, available_weight=available,
        firings=firings, not_firing=not_firing, excluded=excluded, flags=flags,
        evaluations=evals,
    )

    if available <= 0:
        view.state = "DEGRADED"
        view.reason = "No driver for this asset has fresh, verified data. View withheld."
        return view

    view.coverage = total_fired / available if available else 0.0
    n_distinct = len({f.driver_id for f in firings})

    if total_fired <= 0:
        view.reason = "No driver is firing. No directional view."
        return view

    dominant = "UP" if up >= down else "DOWN"
    dom_w = max(up, down)
    view.agreement = dom_w / total_fired

    if n_distinct < min_n:
        view.reason = f"Only {n_distinct} driver(s) firing; need at least {min_n}. No directional view."
        return view
    if view.coverage < min_cov:
        view.reason = (
            f"Coverage {view.coverage:.0%} is below the minimum {min_cov:.0%}: "
            "too few of the available drivers are saying anything."
        )
        return view
    if view.agreement < bar:
        view.reason = (
            f"Agreement {view.agreement:.0%} is below the bar {bar:.0%}: drivers disagree. "
            "No directional view."
        )
        return view

    view.state = dominant
    view.reason = (
        f"Agreement {view.agreement:.0%} (bar {bar:.0%}), coverage {view.coverage:.0%}, "
        f"{n_distinct} drivers firing."
    )
    dom_sign = 1 if dominant == "UP" else -1
    hs = [f.horizon for f in firings if f.contribution * dom_sign > 0]
    view.horizon = min(hs, key=lambda h: _HORIZON_ORDER[h]) if hs else None
    return view
