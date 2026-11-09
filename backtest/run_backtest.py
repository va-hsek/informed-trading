"""Run the pre-registered study: tune on Cycle A, freeze, test on B; then the reverse.

    python3 -m backtest.run_backtest

Needs data/history/ (see backtest/history.py). Writes reports/backtest_<date>.html and data/backtest/calibration.json.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import yaml

from .engine import Engine
from .history import load_history
from .protocol import by_asset, full_study, label_breakdown, summarise

ROOT = Path(__file__).resolve().parent.parent


def pct(x, nd=1):
    return "n/a" if x is None else f"{x * 100:.{nd}f}%"


def f2(x):
    return "n/a" if x is None else f"{x:.2f}"


def _row(name, s):
    if not s.get("n"):
        return f"| {name} | 0 | n/a | n/a | n/a | n/a | n/a | n/a | {pct(s.get('no_view_rate'), 0)} |"
    return (f"| {name} | {s['n']} | {pct(s['hit_rate'])} | {pct(s['hit_wilson_low'])} | {pct(s['stop_out_rate'], 0)} | "
            f"{s['net_expectancy_pct']:+.2f}% | {f2(s['profit_factor'])} | {s['max_drawdown_pct']:.1f}% | {pct(s['no_view_rate'], 0)} |")


HEAD = ("| Run | Independent calls | Direction hit-rate | 95% Wilson low | ATR stop touched | Net expectancy per trade | "
        "Profit factor | Max drawdown | No-view days |\n|---|---|---|---|---|---|---|---|---|")


def render(study: dict, eng: Engine, notes: list[str], cost: float) -> str:
    fwd, rev = study["forward"], study["reverse"]
    L = ["# Backtest report (pre-registered protocol)", f"Window: {eng.start} to {eng.end}. Daily bars. Assets tested: {', '.join(eng.assets) or 'none'}.", "",
         "> Hit-rate: price at the horizon end vs the next-session open, in the called direction. Independent calls: overlapping calls on "
         "the same asset are collapsed to the first one. Net expectancy is a percentage of entry price per trade after an ASSUMED round-trip "
         f"cost of {cost:.2f}% (tick sizes and charges are not verified; see docs). Past results do not promise future results.", ""]
    if notes:
        L += ["## Not backtestable / missing"] + [f"- {n}" for n in notes] + [""]
    if eng.metric_days:
        L += ["## Driver data availability (days with a valid observation)"] + [f"- {m}: {n}" for m, n in sorted(eng.metric_days.items())] + [""]
    never = [d["id"] for d in eng.drivers if d["source"]["status"] == "verified" and d["metric"] not in eng.metric_days]
    if never:
        L += [f"- Verified drivers that never had data in this run (never fired): {', '.join(never)}", ""]
    for r in (fwd, rev):
        L += [f"## {r['label']}", "", f"Tuned parameters (frozen before the test run, hash {r['params_hash']}): " +
              (", ".join(f"{k}: {v}" for k, v in sorted(r['params'].items())) or "none (defaults kept)"), "", HEAD,
              _row("Tuning cycle (in-sample)", summarise(r["in_sample"])), _row("Test cycle (out-of-sample)", summarise(r["test"])), ""]
        ba = by_asset(r["test"])
        if ba:
            L += ["Out-of-sample by asset:", "", "| Asset | Calls | Hit-rate | Wilson low | Net expectancy |", "|---|---|---|---|---|"]
            L += [f"| {a} | {v['n']} | {pct(v['hit_rate'])} | {pct(v['hit_wilson_low'])} | {v['net_expectancy_pct']:+.2f}% |" for a, v in ba.items()]
            L.append("")
        lb = label_breakdown(r["test"]["calls"])
        if lb:
            L += ["Out-of-sample by technical agreement label:", ""] + [f"- {k}: {v['n']} calls, hit-rate {pct(v['hit_rate'])}, net expectancy {v['net_expectancy_pct']:+.2f}%" for k, v in lb.items()] + [""]
    L += ["## Calibration (pre-registration section 5)", "",
          "Out-of-sample calls only: Cycle B calls come from the model tuned on A, Cycle A calls from the model tuned on B.", "",
          "| Agreement band | Cycle A calls | A hit-rate | Cycle B calls | B hit-rate | B Wilson low | Verdict |", "|---|---|---|---|---|---|---|"]
    for v in study["calibration"]:
        a, b = v["cycle_a"], v["cycle_b"]
        L.append(f"| {v['band']} | {a['n']} | {pct(a['hit_rate'])} | {b['n']} | {pct(b['hit_rate'])} | {pct(b['wilson_low'])} | "
                 f"{'CALIBRATED >= 70%' if v['calibrated'] else 'UNCALIBRATED'}: {v['reason']} |")
    L += ["", "Weakness stated up front: adjacent months share market regimes, so Cycle B is a soft out-of-sample test. Forward paper-trading is the real validation.", ""]
    return "\n".join(L) + "\n"


def calibration_json(study: dict) -> dict:
    return {"generated": date.today().isoformat(),
            "bands": [{"band": v["band"], "calibrated": v["calibrated"], "reason": v["reason"]} for v in study["calibration"]]}


def main() -> int:
    cfg = yaml.safe_load((ROOT / "config" / "user.yaml").read_text())
    drivers = yaml.safe_load((ROOT / "config" / "drivers.yaml").read_text())["drivers"]
    data = ROOT / cfg.get("data_dir", "data")
    hist, prices, notes = load_history(data)
    if not prices:
        print("\n".join(notes))
        return 1
    y = date.today().year
    cost = float(cfg.get("backtest_cost_roundtrip_pct", 0.10))
    eng = Engine(drivers, cfg, hist, prices, date(y - 5, 1, 1), date(y - 1, 12, 31), cost_roundtrip_pct=cost)
    study = full_study(eng)
    md = render(study, eng, notes, cost)
    out = ROOT / "reports"
    out.mkdir(exist_ok=True)
    from core.render_html import md_to_html
    (out / f"backtest_{date.today():%Y-%m-%d}.html").write_text(md_to_html(md, "Backtest report"))
    (data / "backtest").mkdir(parents=True, exist_ok=True)
    (data / "backtest" / "calibration.json").write_text(json.dumps(calibration_json(study), indent=2))
    print(f"wrote reports/backtest_{date.today():%Y-%m-%d}.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
