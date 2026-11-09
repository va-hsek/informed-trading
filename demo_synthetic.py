"""Generates a DEMO action report from SYNTHETIC numbers, only to show the format.

Nothing here is real market data. Run:  python3 demo_synthetic.py
"""
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from core.agreement import compare
from core.report import render_asset, render_report
from core.signals import evaluate_asset
from core.sizing import build_scenario
from core.technical import analyse
from core.validation import check_observation

NOW = datetime(2026, 11, 7, 8, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
CFG = {"confidence_bar": 0.70, "min_coverage": 0.35, "min_firing_drivers": 2}


def drv(i, name, metric, w, rules, horizon="swing", stale=96):
    return {"id": i, "name": name, "metric": metric, "weight": w, "rules": rules, "horizon": horizon,
            "stale_after_hours": stale, "flag_only": False, "source": {"provider": "SYNTHETIC", "status": "verified"}}


def ob(v, h=2):
    return {"value": v, "as_of": (NOW - timedelta(hours=h)).isoformat(), "source": "SYNTHETIC-DEMO", "verified": True}


drivers = [
    drv("R01", "Broad USD 5d change", "usd", 1.0, [{"op": ">", "threshold": 0.5, "effects": {"silver": -1}}, {"op": "<", "threshold": -0.5, "effects": {"silver": 1}}]),
    drv("R02", "US 10Y real yield 5d change", "real", 1.2, [{"op": ">", "threshold": 0.1, "effects": {"silver": -0.8}}, {"op": "<", "threshold": -0.1, "effects": {"silver": 0.8}}]),
    drv("R04", "USDINR 5d change", "inr", 0.8, [{"op": ">", "threshold": 0.3, "effects": {"silver": 1}}, {"op": "<", "threshold": -0.3, "effects": {"silver": -1}}]),
    drv("R08", "Copper 20d change", "cu", 0.7, [{"op": ">", "threshold": 3, "effects": {"silver": 1}}, {"op": "<", "threshold": -3, "effects": {"silver": -1}}], horizon="positional"),
    drv("R13", "Stale example driver", "stale", 0.5, [{"op": ">", "threshold": 0, "effects": {"silver": 1}}]),
]
obs = {"usd": ob(-0.9), "real": ob(-0.18), "inr": ob(0.35), "cu": ob(1.2), "stale": ob(1.0, h=400)}
checked = {d["id"]: check_observation(d, obs[d["metric"]], NOW) for d in drivers}
fund = evaluate_asset("silver", drivers, checked, CFG)

rng = np.random.default_rng(11)
n = 260
t = np.arange(n)
close = 150000 + 6000 * np.sin(2 * np.pi * t / 55) + 25 * t + rng.normal(0, 400, n)
df = pd.DataFrame({"open": close, "high": close + 700, "low": close - 700, "close": close,
                   "volume": rng.integers(800, 1500, n)}, index=pd.date_range("2025-11-01", periods=n, freq="D"))
tech = analyse(df)
agree = compare(fund, tech)

scen = None
if fund.state in ("UP", "DOWN"):
    direction = "long" if fund.state == "UP" else "short"
    stop = tech.stop_structure_long if direction == "long" else tech.stop_structure_short
    nxt = tech.resistance.price if direction == "long" else tech.support.price
    scen = build_scenario(direction, tech.last_close, stop, nxt,
                          {"multiplier": 1.0},                      # SYNTHETIC multiplier
                          margin_per_lot=30000.0,                   # SYNTHETIC margin
                          capital=60000.0, margin_buffer=0.25)

block = render_asset(fund, tech, agree, scen, "SYNTHETIC instrument, SYNTHETIC prices",
                     "No directional view, so no scenario is shown.")
dq = [f"{c.driver_id} ({c.metric}): {c.reason}" for c in fund.excluded]
report = render_report(NOW, [block], dq, ["(demo) no events loaded"])

banner = ("# SYNTHETIC DEMO. NOT REAL MARKET DATA. DO NOT TRADE ON THIS.\n"
          "Numbers, multiplier and margin below are invented to show the report layout only.\n\n")
open("reports/DEMO_synthetic_action_report.md", "w").write(banner + report)
print("wrote reports/DEMO_synthetic_action_report.md")
