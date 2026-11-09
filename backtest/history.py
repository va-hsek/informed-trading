"""History files for the backtest: data/history/*.csv with columns date,value (prices: date,open,high,low,close,volume).

    fred_<SERIES>.csv      e.g. fred_DGS2.csv, fred_DTWEXBGS.csv
    eia_crude.csv          weekly US commercial crude stocks, thousand barrels
    eia_ngs.csv            weekly Lower-48 working gas, bcf (optional; without it R11 cannot be backtested)
    cot_<metric>.csv       managed-money net position as % of open interest, e.g. cot_cot_gold_pctile_3y.csv
    prices/<asset>.csv     continuous daily futures series per asset: gold, silver, crude, natgas
"""
from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

from core.prices import read_csv as read_prices


def read_series(path: Path) -> list[tuple[date, float]]:
    out, seen = [], set()
    with open(path, newline="") as f:
        rd = csv.DictReader(f)
        if not rd.fieldnames or [c.strip().lower() for c in rd.fieldnames][:2] != ["date", "value"]:
            raise ValueError(f"{path}: first two columns must be date,value")
        for row in rd:
            d = date.fromisoformat(row["date"].strip())
            if d in seen:
                raise ValueError(f"{path}: duplicate date {d}")
            seen.add(d)
            v = row["value"].strip()
            if v in ("", ".", "nan", "NaN"):
                continue                                  # FRED marks holidays with '.'
            out.append((d, float(v)))
    return sorted(out)


def write_series(series, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "value"])
        for d, v in series:
            w.writerow([d.isoformat(), repr(float(v))])


def load_history(root: Path) -> tuple[dict, dict, list[str]]:
    """Returns (history, prices, notes). Missing files are listed in notes, never invented."""
    h = root / "history"
    hist, prices, notes = {}, {}, []
    if not h.exists():
        return hist, prices, [f"{h} does not exist: run python3 -m backtest.fetch_history first"]
    for p in sorted(h.glob("fred_*.csv")):
        hist[f"fred:{p.stem[5:]}"] = read_series(p)
    if (h / "eia_crude.csv").exists():
        hist["eia:crude"] = read_series(h / "eia_crude.csv")
    else:
        notes.append("eia_crude.csv missing: R05 and R06 cannot be backtested")
    if (h / "eia_ngs.csv").exists():
        hist["eia:ngs"] = read_series(h / "eia_ngs.csv")
    else:
        notes.append("eia_ngs.csv missing: R11 (natural gas storage) cannot be backtested")
    for p in sorted(h.glob("cot_*.csv")):
        hist[f"cot:{p.stem[4:]}"] = read_series(p)
    for p in sorted((h / "prices").glob("*.csv")) if (h / "prices").exists() else []:
        prices[p.stem] = read_prices(p)
    if not prices:
        notes.append("no price files in history/prices/: nothing to test calls against")
    return hist, prices, notes
