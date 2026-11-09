"""Hedge candidates by MEASURED relationship, never assumed.

For a commodity view, rank watchlist equities by the rolling relationship between their
daily returns and the commodity's daily returns. Reports facts only:
  correlation, R-squared, hedge ratio (variance-minimising notional of equity per 1 of
  commodity), and the residual risk left over (1 - R-squared).
Shorting a stock needs stock futures; lot sizes and margin for those are not checked here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd


@dataclass
class HedgeRow:
    symbol: str
    n: int
    corr: float
    r2: float
    ratio: float          # equity notional per 1 of commodity notional, variance-minimising (signed)
    residual: float       # share of commodity-return variance NOT explained (1 - r2)
    stable: bool          # sign of correlation agrees between the two halves of the window


def hedge_table(commodity: pd.DataFrame, equities: dict[str, pd.DataFrame], window: int = 60,
                min_obs: int = 40, min_abs_corr: float = 0.4) -> list[HedgeRow]:
    rc = commodity["close"].pct_change().dropna()
    rows: list[HedgeRow] = []
    for sym, df in equities.items():
        re_ = df["close"].pct_change().dropna()
        j = pd.concat([rc, re_], axis=1, join="inner").dropna().tail(window)
        j.columns = ["c", "e"]
        if len(j) < min_obs or j["e"].var() == 0 or j["c"].var() == 0:
            continue
        corr = float(j["c"].corr(j["e"]))
        if not np.isfinite(corr) or abs(corr) < min_abs_corr:
            continue
        half = len(j) // 2
        c1, c2 = j.iloc[:half]["c"].corr(j.iloc[:half]["e"]), j.iloc[half:]["c"].corr(j.iloc[half:]["e"])
        stable = bool(np.sign(c1) == np.sign(c2) == np.sign(corr))
        ratio = float(j["c"].cov(j["e"]) / j["e"].var())      # regress commodity on equity
        rows.append(HedgeRow(sym, len(j), corr, corr ** 2, ratio, 1 - corr ** 2, stable))
    rows.sort(key=lambda r: -abs(r.corr))
    return rows


def render_hedge(asset: str, direction: Optional[str], rows: list[HedgeRow], window: int) -> list[str]:
    out = ["### Hedge candidates (measured, not assumed)"]
    if not rows:
        out.append(f"No watchlist equity showed a stable relationship strong enough (|correlation| >= 0.4 over {window} days), "
                   "or equity price history is not loaded (data/prices/equity/<SYMBOL>.csv).")
        return out
    out.append(f"Daily-return relationship with {asset} over the last {window} days:")
    out.append("")
    out.append("| Equity | Correlation | R-squared | Hedge ratio | Residual risk | Stable across window |")
    out.append("|---|---|---|---|---|---|")
    for r in rows[:5]:
        out.append(f"| {r.symbol} | {r.corr:+.2f} | {r.r2:.2f} | {r.ratio:+.2f} | {r.residual:.0%} | {'yes' if r.stable else 'NO'} |")
    out.append("")
    if direction in ("long", "short"):
        for r in rows[:3]:
            same = (r.corr > 0)
            side = ("short" if same else "long") if direction == "long" else ("long" if same else "short")
            out.append(f"- {r.symbol} moves {'with' if same else 'against'} {asset}; an offsetting position for a {direction} "
                       f"{asset} view would be {side} {r.symbol}. Residual risk {r.residual:.0%}.")
    out.append("- A partial hedge reduces one risk, not all of them. Shorting needs stock futures: check lot size and margin.")
    return out
