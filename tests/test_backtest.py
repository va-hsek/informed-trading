import copy
import math
import random
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from backtest.engine import CYCLE_A, CYCLE_B, Engine
from backtest.protocol import (apply_params, calibration, calibration_verdict, full_study, params_hash, run_protocol,
                               summarise, tune)
from backtest.stats import simulate_exit, wilson_lower, wilson_upper

CFG = {"confidence_bar": 0.70, "min_coverage": 0.35, "min_firing_drivers": 2, "atr_period": 14,
       "stop_atr_multiple": 1.5, "caution_distance_atr": 0.5, "min_room_atr": 1.0}


def driver(i, metric, thr, w=1.0):
    return {"id": i, "label": metric, "metric": metric, "metric_phrase": metric, "unit": "", "weight": w, "horizon": "swing",
            "stale_after_hours": 264, "source": {"status": "verified"},
            "rules": [{"op": ">", "threshold": thr, "effects": {"gold": -1}, "why": {"gold": "x"}},
                      {"op": "<", "threshold": -thr, "effects": {"gold": 1}, "why": {"gold": "x"}}]}


DRIVERS = [driver("T1", "us2y_5d_chg_pp", 0.08), driver("T2", "us10y_real_5d_chg_pp", 0.08)]


def bdays(a, b):
    d, out = a, []
    while d <= b:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def world(seed, signal=True, start=date(2019, 6, 1), end=date(2026, 10, 30), k=0.45):
    """Common factor drives two yield series; gold's later drift depends (or not) on the factor's 5-day change."""
    rng = random.Random(seed)
    days = bdays(start, end)
    f = [0.0]
    for _ in days[1:]:
        f.append(f[-1] + rng.gauss(0, 0.05))
    y2 = [3 + x + rng.gauss(0, 0.005) for x in f]
    r10 = [1.5 + x * 0.8 + rng.gauss(0, 0.005) for x in f]
    px, rows = 1000.0, []
    for i, d in enumerate(days):
        prev5 = (f[i - 1] - f[i - 6]) if i >= 6 else 0.0
        drift = (-k * prev5 * 0.2 if signal else 0.0)                  # per-day log drift
        o = px * (1 + rng.gauss(0, 0.001))
        c = o * math.exp(drift + rng.gauss(0, 0.008))
        h, l = max(o, c) * (1 + abs(rng.gauss(0, 0.003))), min(o, c) * (1 - abs(rng.gauss(0, 0.003)))
        rows.append((d, o, h, l, c, 1000.0))
        px = c
    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date")
    hist = {"fred:DGS2": list(zip(days, y2)), "fred:DFII10": list(zip(days, r10))}
    return hist, {"gold": df}


def engine(hist, prices, **kw):
    return Engine(DRIVERS, CFG, hist, prices, date(2020, 1, 1), date(2025, 12, 31), **kw)


# ---------- statistics ----------
def test_wilson_reference_values():
    assert wilson_lower(70, 100) == pytest.approx(0.604151, abs=1e-5)
    assert wilson_upper(70, 100) == pytest.approx(0.781051, abs=1e-5)
    assert wilson_lower(0, 10) == pytest.approx(0.0, abs=1e-9)
    assert wilson_lower(0, 0) is None
    assert wilson_lower(30, 30) < 1.0


# ---------- exit simulation ----------
def bars(rows):
    return [list(x) for x in zip(*rows)]      # o,h,l,c


def test_exit_stop_hit_long():
    o, h, l, c = bars([(100, 101, 99, 100), (100, 100.5, 96, 97), (97, 98, 96, 97)])
    ex, dist = simulate_exit(o, h, l, c, 0, 2, +1, atr_val=2.0)             # stop = 100 - 3 = 97
    assert dist == 3.0 and ex.reason == "stop" and ex.exit_idx == 1 and ex.exit_price == 97.0


def test_exit_gap_through_stop_fills_at_open():
    o, h, l, c = bars([(100, 101, 99, 100), (94, 95, 93, 94), (94, 95, 93, 94)])
    ex, _ = simulate_exit(o, h, l, c, 0, 2, +1, atr_val=2.0)
    assert ex.exit_idx == 1 and ex.exit_price == 94.0                       # worse than the stop: honest gap fill


def test_exit_time_stop_and_breakeven_then_trail():
    # price rises 3.5 (>= 1x stop distance 3) -> break-even armed, trail = best - 2*ATR(4)
    o, h, l, c = bars([(100, 101, 99.5, 100.5), (100.5, 103.5, 100, 103), (103, 103.2, 99.0, 99.5), (99.5, 100, 99, 99.5)])
    ex, _ = simulate_exit(o, h, l, c, 0, 3, +1, atr_val=2.0)
    # after bar1: best=103.5, stop=max(97,100)=100 then trail 103.5-4=99.5 -> max=100 ; bar2 low 99.0 <= 100 -> exit at 100
    assert ex.reason == "breakeven_or_trail_stop" and ex.exit_price == 100.0 and ex.exit_idx == 2
    o2, h2, l2, c2 = bars([(100, 101, 99.5, 100.5), (100.5, 101.5, 100, 101), (101, 101.5, 100.2, 101.2)])
    ex2, _ = simulate_exit(o2, h2, l2, c2, 0, 2, +1, atr_val=2.0)
    assert ex2.reason == "time" and ex2.exit_price == 101.2


def test_exit_short_mirrors_long():
    o, h, l, c = bars([(100, 101, 99, 100), (100, 103.5, 99.5, 103), (103, 104, 102, 103)])
    ex, dist = simulate_exit(o, h, l, c, 0, 2, -1, atr_val=2.0)             # short stop = 103
    assert ex.reason == "stop" and ex.exit_price == 103.0 and ex.exit_idx == 1


def test_stop_and_target_same_bar_counts_stop_first():
    # bar spans both a big favourable move and the stop: stop is applied first (no intrabar look-ahead)
    o, h, l, c = bars([(100, 101, 99, 100), (100, 110, 96, 109)])
    ex, _ = simulate_exit(o, h, l, c, 0, 1, +1, atr_val=2.0)
    assert ex.reason == "stop" and ex.exit_price == 97.0


# ---------- engine: no look-ahead, signal detection ----------
def test_no_lookahead_calls_do_not_change_when_the_future_is_destroyed():
    hist, prices = world(1)
    e1 = engine(hist, prices)
    cut = date(2023, 6, 30)
    r1 = [c for c in e1.run(CYCLE_A + CYCLE_B)["raw_calls"] if c.day <= cut - timedelta(days=40)]
    hist2 = {k: [(d, v if d <= cut else 1e6) for d, v in s] for k, s in hist.items()}
    p2 = {"gold": prices["gold"].copy()}
    p2["gold"].loc[p2["gold"].index > pd.Timestamp(cut), ["open", "high", "low", "close"]] = 1.0
    e2 = engine(hist2, p2)
    r2 = [c for c in e2.run(CYCLE_A + CYCLE_B)["raw_calls"] if c.day <= cut - timedelta(days=40)]
    assert len(r1) > 20
    assert [(c.day, c.direction, round(c.agreement, 9)) for c in r1] == [(c.day, c.direction, round(c.agreement, 9)) for c in r2]


def test_publication_lag_is_respected():
    # a value dated D is not visible on D under daily_1d; it appears the next day
    hist, prices = world(2)
    e = engine(hist, prices)
    d = date(2022, 3, 9)
    vis = e._visible("fred:DGS2", "daily_1d", d, 30)
    assert vis[-1][0] == date(2022, 3, 8)


def test_signal_world_beats_null_world_and_null_is_uncalibrated():
    hs, ps = world(3, signal=True)
    hn, pn = world(3, signal=False)
    s = summarise(engine(hs, ps).run(CYCLE_A + CYCLE_B))
    n = summarise(engine(hn, pn).run(CYCLE_A + CYCLE_B))
    assert s["n"] >= 30 and n["n"] >= 30
    assert s["hit_rate"] > 0.60
    assert n["hit_rate"] < 0.62 and n["hit_wilson_low"] < 0.70
    assert s["net_expectancy_pct"] > n["net_expectancy_pct"]


def test_independent_calls_do_not_overlap():
    hs, ps = world(4)
    e = engine(hs, ps)
    res = e.run(CYCLE_A + CYCLE_B)
    calls = sorted(res["calls"], key=lambda c: c.day)
    assert len(res["raw_calls"]) > len(calls)
    for a, b in zip(calls, calls[1:]):
        assert b.day > e._end_day(a)


def test_missing_history_means_no_view_not_a_guess():
    hs, ps = world(5)
    e = engine({}, ps)
    res = e.run(CYCLE_A)
    assert res["calls"] == [] and res["no_view_days"] == res["asset_days"] > 0


def test_stale_driver_is_excluded_in_backtest_too():
    hs, ps = world(6)
    hs = {k: [(d, v) for d, v in s if not (date(2022, 1, 1) <= d <= date(2022, 12, 31))] for k, s in hs.items()}
    e = engine(hs, ps)
    res = e.run((6,))
    assert not [c for c in res["raw_calls"] if c.day.year == 2022 and c.day.month == 6 and c.day.day > 20]


# ---------- calibration rules ----------
def test_calibration_requires_both_cycles_and_thirty_calls():
    good = [{"band": "0.70-0.80", "n": 40, "hit_rate": 0.75, "wilson_low": 0.6}] * 1
    bad_n = [{"band": "0.70-0.80", "n": 29, "hit_rate": 0.9, "wilson_low": 0.7}]
    low = [{"band": "0.70-0.80", "n": 50, "hit_rate": 0.69, "wilson_low": 0.55}]
    assert calibration_verdict(good, good)[0]["calibrated"]
    assert not calibration_verdict(good, bad_n)[0]["calibrated"]
    assert "fewer than 30" in calibration_verdict(good, bad_n)[0]["reason"]
    assert not calibration_verdict(low, good)[0]["calibrated"]


def test_calibration_band_edges():
    class C:
        def __init__(s, a, h): s.agreement, s.hit = a, h
    rows = calibration([C(0.70, True), C(0.7999, False), C(0.80, True), C(0.90, True), C(1.0, True)])
    assert [r["n"] for r in rows] == [2, 1, 2]


# ---------- protocol ----------
def test_tuning_is_deterministic_and_test_run_never_changes_params():
    hs, ps = world(7)
    e = engine(hs, ps)
    r1 = run_protocol(e, CYCLE_A, CYCLE_B, "x", sweeps=1)
    e2 = engine(hs, ps)
    r2 = run_protocol(e2, CYCLE_A, CYCLE_B, "x", sweeps=1)
    assert r1["params_hash"] == r2["params_hash"] == params_hash(r1["params"])
    assert r1["test"]["calls"] and all(c.agreement_label for c in r1["test"]["calls"])


def test_tune_only_touches_allowed_parameters_and_keeps_defaults_on_ties():
    hn, pn = world(8, signal=False)
    e = engine(hn, pn)
    params, _ = tune(e, CYCLE_A, sweeps=1)
    for did, p in params.items():
        assert set(p) <= {"w", "t"} and did in {"T1", "T2"}
    d2 = apply_params(DRIVERS, {})
    assert d2 == DRIVERS and d2 is not DRIVERS


def test_full_study_shapes():
    hs, ps = world(9)
    study = full_study(engine(hs, ps), sweeps=1)
    assert set(study) == {"forward", "reverse", "calibration"}
    assert len(study["calibration"]) == 3
    # reverse run tests on cycle A months only, forward on B only
    assert {c.day.month for c in study["forward"]["test"]["calls"]} <= set(CYCLE_B)
    assert {c.day.month for c in study["reverse"]["test"]["calls"]} <= set(CYCLE_A)


# ---------- history files, report, CLI pieces ----------
def test_history_roundtrip_and_rejects_bad_files(tmp_path):
    from backtest.history import load_history, read_series, write_series
    s = [(date(2024, 1, 2), 1.5), (date(2024, 1, 3), 2.25)]
    write_series(s, tmp_path / "history" / "fred_DGS2.csv")
    hist, prices, notes = load_history(tmp_path)
    assert hist["fred:DGS2"] == s and not prices and any("no price files" in n for n in notes)
    (tmp_path / "bad.csv").write_text("date,value\n2024-01-02,1\n2024-01-02,2\n")
    with pytest.raises(ValueError):
        read_series(tmp_path / "bad.csv")
    (tmp_path / "bad2.csv").write_text("day,val\n2024-01-02,1\n")
    with pytest.raises(ValueError):
        read_series(tmp_path / "bad2.csv")
    (tmp_path / "fred.csv").write_text("date,value\n2024-01-02,.\n2024-01-03,3\n")
    assert read_series(tmp_path / "fred.csv") == [(date(2024, 1, 3), 3.0)]


def test_report_renders_and_states_uncalibrated_for_null_world():
    from backtest.run_backtest import calibration_json, render
    hn, pn = world(11, signal=False)
    e = engine(hn, pn)
    study = full_study(e, sweeps=1)
    md = render(study, e, ["note one"], 0.10)
    assert "UNCALIBRATED" in md and "CALIBRATED >= 70%" not in md.replace("UNCALIBRATED", "")
    assert "tune A, test B" in md and "reverse" in md and "note one" in md
    assert all(not b["calibrated"] for b in calibration_json(study)["bands"])
