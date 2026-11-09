"""Summaries, calibration bands, constrained tuning and the A/B protocol (pre-registration sections 2-5)."""
from __future__ import annotations

import copy
import hashlib
import json
from statistics import mean
from typing import Optional

from .engine import CYCLE_A, CYCLE_B, Call, Engine
from .stats import wilson_lower

BANDS = [(0.70, 0.80), (0.80, 0.90), (0.90, 1.0001)]
MIN_CALLS_PER_BAND = 30
WEIGHT_GRID = (0.5, 1.0, 1.5, 2.0)
THRESHOLD_GRID = (0.75, 1.0, 1.25)


def band_name(lo: float, hi: float) -> str:
    return f"{lo:.2f}-{min(hi, 1.0):.2f}"


def summarise(res: dict) -> dict:
    calls: list[Call] = res["calls"]
    n = len(calls)
    out = {"asset_days": res["asset_days"], "no_view_days": res["no_view_days"], "unresolved": res["unresolved"],
           "raw_calls": len(res["raw_calls"]), "n": n}
    out["no_view_rate"] = res["no_view_days"] / res["asset_days"] if res["asset_days"] else None
    if not n:
        return out
    hits = sum(c.hit for c in calls)
    wins = [c.sim_net_pct for c in calls if c.sim_net_pct > 0]
    losses = [-c.sim_net_pct for c in calls if c.sim_net_pct <= 0]
    eq = peak = dd = 0.0
    for c in sorted(calls, key=lambda c: c.day):
        eq += c.sim_net_pct
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    out.update(
        hits=hits, hit_rate=hits / n, hit_wilson_low=wilson_lower(hits, n),
        mae_atr=mean(c.mae_atr for c in calls), mfe_atr=mean(c.mfe_atr for c in calls),
        stop_out_rate=sum(c.stopped for c in calls) / n,
        gross_expectancy_pct=mean(c.sim_gross_pct for c in calls), net_expectancy_pct=mean(c.sim_net_pct for c in calls),
        expectancy_r=mean(c.sim_r for c in calls),
        profit_factor=(sum(wins) / sum(losses)) if losses and sum(losses) > 0 else None,
        payoff_ratio=(mean(wins) / mean(losses)) if wins and losses and mean(losses) > 0 else None,
        win_rate_sim=len(wins) / n, max_drawdown_pct=dd, total_net_pct=eq,
    )
    return out


def by_asset(res: dict) -> dict:
    out = {}
    for a in sorted({c.asset for c in res["calls"]}):
        sub = {"calls": [c for c in res["calls"] if c.asset == a], "raw_calls": [], "asset_days": 0, "no_view_days": 0, "unresolved": 0}
        s = summarise(sub)
        out[a] = {k: s.get(k) for k in ("n", "hit_rate", "hit_wilson_low", "net_expectancy_pct", "stop_out_rate")}
    return out


def calibration(calls: list[Call]) -> list[dict]:
    rows = []
    for lo, hi in BANDS:
        sub = [c for c in calls if lo <= c.agreement < hi]
        n, h = len(sub), sum(c.hit for c in sub)
        rows.append({"band": band_name(lo, hi), "n": n, "hit_rate": h / n if n else None, "wilson_low": wilson_lower(h, n)})
    return rows


def calibration_verdict(rows_a: list[dict], rows_b: list[dict], bar: float = 0.70) -> list[dict]:
    """Pre-registration section 5: calibrated only if, in BOTH cycles, n >= 30 and hit-rate >= bar."""
    out = []
    for ra, rb in zip(rows_a, rows_b):
        ok = all(r["n"] >= MIN_CALLS_PER_BAND and r["hit_rate"] is not None and r["hit_rate"] >= bar for r in (ra, rb))
        reason = "meets the pre-registered test in both cycles" if ok else (
            "fewer than 30 independent calls in at least one cycle" if min(ra["n"], rb["n"]) < MIN_CALLS_PER_BAND
            else "hit-rate below 70% in at least one cycle")
        out.append({"band": ra["band"], "calibrated": ok, "reason": reason, "cycle_a": ra, "cycle_b": rb})
    return out


# ---------- tuning (Step 1; only weights and thresholds already in drivers.yaml) ----------
def _objective(eng: Engine, months, drivers) -> float:
    res = eng.run(months, drivers)
    n = len(res["calls"])
    if n == 0:
        return 0.0
    h = sum(c.hit for c in res["calls"])
    return (wilson_lower(h, n) or 0.0) * min(1.0, n / MIN_CALLS_PER_BAND)


def apply_params(drivers: list[dict], params: dict) -> list[dict]:
    out = copy.deepcopy(drivers)
    for d in out:
        p = params.get(d["id"])
        if not p or d.get("flag_only"):
            continue
        d["weight"] = float(d["weight"]) * p.get("w", 1.0)
        for r in d["rules"]:
            r["threshold"] = r["threshold"] * p.get("t", 1.0)
    return out


def tune(eng: Engine, months, sweeps: int = 2) -> tuple[dict, float]:
    """Coordinate search, objective = Wilson lower bound of the independent-call hit-rate, scaled down below 30 calls."""
    params: dict = {}
    best = _objective(eng, months, apply_params(eng.drivers, params))
    for _ in range(sweeps):
        for d in eng.drivers:
            if d.get("flag_only") or d["source"]["status"] != "verified":
                continue
            for kind, grid in (("w", WEIGHT_GRID), ("t", THRESHOLD_GRID)):
                cur = params.get(d["id"], {}).get(kind, 1.0)
                cand_best, cand_val = cur, best
                for g in grid:
                    if g == cur:
                        continue
                    trial = copy.deepcopy(params)
                    trial.setdefault(d["id"], {})[kind] = g
                    v = _objective(eng, months, apply_params(eng.drivers, trial))
                    if v > cand_val + 1e-9:                      # strict improvement only: ties keep the default
                        cand_best, cand_val = g, v
                if cand_best != cur:
                    params.setdefault(d["id"], {})[kind] = cand_best
                    best = cand_val
    params = {k: v for k, v in params.items() if any(x != 1.0 for x in v.values())}
    return params, best


def params_hash(params: dict) -> str:
    return hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:12]


def run_protocol(eng: Engine, tune_months, test_months, label: str, sweeps: int = 2) -> dict:
    """Tune on tune_months, FREEZE, then run test_months exactly once."""
    params, score = tune(eng, tune_months, sweeps)
    frozen = copy.deepcopy(params)
    h0 = params_hash(frozen)
    drivers = apply_params(eng.drivers, frozen)
    in_sample = eng.run(tune_months, drivers)
    oos = eng.run(test_months, drivers)
    assert params_hash(frozen) == h0                              # nothing changed after the test run
    eng.label_calls(oos["calls"])
    return {"label": label, "params": frozen, "params_hash": h0, "tune_score": score,
            "in_sample": in_sample, "test": oos}


def label_breakdown(calls: list[Call]) -> dict:
    out = {}
    for lab in sorted({c.agreement_label for c in calls}):
        sub = [c for c in calls if c.agreement_label == lab]
        h = sum(c.hit for c in sub)
        out[lab] = {"n": len(sub), "hit_rate": h / len(sub), "net_expectancy_pct": mean(c.sim_net_pct for c in sub)}
    return out


def full_study(eng: Engine, sweeps: int = 2) -> dict:
    fwd = run_protocol(eng, CYCLE_A, CYCLE_B, "tune A, test B", sweeps)
    rev = run_protocol(eng, CYCLE_B, CYCLE_A, "tune B, test A (reverse)", sweeps)
    # calibration compares the SAME frozen model in both cycles' out-of-sample runs: A's model on B, B's model on A
    ca, cb = calibration(rev["test"]["calls"]), calibration(fwd["test"]["calls"])
    return {"forward": fwd, "reverse": rev, "calibration": calibration_verdict(ca, cb)}
