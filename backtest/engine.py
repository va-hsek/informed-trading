"""Backtest engine. Reuses the production rule engine (core.signals) and metric code (collectors.metrics),
so what is tested is what runs. See docs/backtest_preregistration.md for the rules this follows.

No look-ahead:
  * a driver observation is used on decision day D only if its publication date (collectors.lags) <= D
  * technicals use bars up to and including D; entry is the NEXT session open
Daily bars only (the pre-registration allows this when intraday history is shallow).
"""
from __future__ import annotations

import bisect
import copy
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional

import pandas as pd

from collectors.lags import visible_on
from collectors.metrics import MetricError, make_obs, percentile_latest, seasonal_change_vs_avg, seasonal_vs_avg
from collectors.registry import COT_METRICS, FRED_METRICS, compute_cot_metric, compute_fred_metric
from core import technical
from core.agreement import compare
from core.signals import evaluate_asset
from core.validation import check_observation

from .stats import simulate_exit, wilson_lower

HORIZON_BARS = {"intraday": 1, "swing": 10, "positional": 20}   # positional: see amendment in the pre-registration
H10 = {"DTWEXBGS", "DEXINUS"}
CYCLE_A = (1, 3, 5, 7, 9, 11)
CYCLE_B = (2, 4, 6, 8, 10, 12)


def metric_sources() -> dict[str, tuple[str, str, int]]:
    """metric -> (history key, lag kind, tail length of visible observations needed; 0 = all)."""
    out = {}
    for m, (sid, *_rest) in FRED_METRICS.items():
        out[m] = (f"fred:{sid}", "h10_weekly" if sid in H10 else "daily_1d", 30)
    out["eia_crude_stocks_vs_5y_pct"] = ("eia:crude", "eia_petro", 0)
    out["eia_crude_weekly_chg_vs_5y_mb"] = ("eia:crude", "eia_petro", 0)
    out["eia_natgas_storage_vs_5y_pct"] = ("eia:ngs", "eia_gas", 0)
    for m in COT_METRICS:
        out[m] = (f"cot:{m}", "cot", 160)
    return out


def compute_metric(metric: str, series: list) -> tuple[float, date, dict, str]:
    if metric in FRED_METRICS:
        v, d, det = compute_fred_metric(metric, series)
        return v, d, det, f"FRED {FRED_METRICS[metric][0]}"
    if metric == "eia_crude_stocks_vs_5y_pct":
        v, d, det = seasonal_vs_avg(series, "US commercial crude stocks", " thousand bbl")
        return v, d, det, "EIA crude"
    if metric == "eia_crude_weekly_chg_vs_5y_mb":
        v, d, det = seasonal_change_vs_avg(series, "US commercial crude stocks", " thousand bbl", scale=1 / 1000, out_unit=" million bbl")
        return v, d, det, "EIA crude"
    if metric == "eia_natgas_storage_vs_5y_pct":
        v, d, det = seasonal_vs_avg(series, "US Lower-48 working gas in storage", " bcf")
        return v, d, det, "EIA storage"
    if metric in COT_METRICS:
        v, d, det = compute_cot_metric(metric, series)
        return v, d, det, "CFTC"
    raise KeyError(metric)


@dataclass
class Call:
    asset: str
    day: date
    direction: int                  # +1 / -1
    agreement: float
    horizon: str
    bars: int
    entry_date: date = None
    entry: float = 0.0
    atr: float = 0.0
    hit: bool = False
    ret_pct: float = 0.0            # close at horizon vs entry open, in the called direction
    mae_atr: float = 0.0
    mfe_atr: float = 0.0
    mae_pct: float = 0.0
    mfe_pct: float = 0.0
    stopped: bool = False           # ATR stop touched at any time before the horizon ended
    sim_reason: str = ""
    sim_gross_pct: float = 0.0
    sim_net_pct: float = 0.0
    sim_r: float = 0.0
    firing_ids: tuple = ()
    agreement_label: str = ""


class Engine:
    def __init__(self, drivers: list[dict], cfg: dict, history: dict[str, list], prices: dict[str, pd.DataFrame],
                 start: date, end: date, cost_roundtrip_pct: float = 0.10):
        self.drivers, self.cfg, self.history, self.prices = drivers, cfg, history, prices
        self.start, self.end, self.cost = start, end, cost_roundtrip_pct
        self.assets = sorted({a for d in drivers for r in d["rules"] for a in r.get("effects", {})} & set(prices))
        self._srcs = metric_sources()
        self._hist = {}
        for k, s in history.items():
            self._hist[k] = sorted(s, key=lambda x: x[0])
        self._vis_dates: dict[tuple[str, str], list[date]] = {}
        self._checked_cache: dict[date, dict] = {}
        self._outcome_cache: dict[tuple, Optional[Call]] = {}
        self._tech_cache: dict[tuple, object] = {}
        self._pos = {a: {ts.date(): i for i, ts in enumerate(df.index)} for a, df in prices.items()}
        self.metric_days: dict[str, int] = {}

    # ---------- fundamentals as of a day ----------
    def _visible(self, key: str, kind: str, day: date, tail: int) -> list:
        s = self._hist[key]
        ck = (key, kind)
        if ck not in self._vis_dates:
            self._vis_dates[ck] = [visible_on(kind, d) for d, _ in s]
        n = bisect.bisect_right(self._vis_dates[ck], day)
        return s[max(0, n - tail):n] if tail else s[:n]

    def checked_on(self, day: date) -> dict:
        if day in self._checked_cache:
            return self._checked_cache[day]
        now = datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc)
        out = {}
        for d in self.drivers:
            m = d["metric"]
            obs = None
            err = "no history supplied for this metric"
            if m in self._srcs and self._srcs[m][0] in self._hist:
                key, kind, tail = self._srcs[m]
                vis = self._visible(key, kind, day, tail)
                try:
                    v, od, det, src = compute_metric(m, vis)
                    obs = make_obs(v, od, src, det)
                    self.metric_days[m] = self.metric_days.get(m, 0) + 1
                except (MetricError, KeyError) as e:
                    err = str(e)
            out[d["id"]] = check_observation(d, obs, now, fetch_error=err)
        self._checked_cache[day] = out
        return out

    # ---------- days ----------
    def decision_days(self, asset: str, months: tuple[int, ...]) -> list[date]:
        return [d for d in (ts.date() for ts in self.prices[asset].index)
                if self.start <= d <= self.end and d.month in months and d.weekday() < 5]

    # ---------- outcome of a call ----------
    def outcome(self, asset: str, day: date, direction: int, horizon: str) -> Optional[Call]:
        key = (asset, day, direction, horizon)
        if key in self._outcome_cache:
            return self._outcome_cache[key]
        df = self.prices[asset]
        i = self._pos[asset][day]
        H = HORIZON_BARS[horizon]
        res = None
        if i + H < len(df) and i >= 60:
            a = technical.atr(df.iloc[: i + 1], int(self.cfg["atr_period"]))
            av = float(a.iloc[-1])
            if av == av and av > 0:
                sl = df.iloc[i + 1: i + H + 1]
                o, h, l, c = (sl[x].tolist() for x in ("open", "high", "low", "close"))
                entry = o[0]
                sgn = direction
                ret = sgn * (c[-1] - entry)
                mfe = max(h) - entry if sgn > 0 else entry - min(l)
                mae = entry - min(l) if sgn > 0 else max(h) - entry
                mult = float(self.cfg["stop_atr_multiple"])
                stop = entry - sgn * mult * av
                stopped = (min(l) <= stop) if sgn > 0 else (max(h) >= stop)
                ex, dist = simulate_exit(o, h, l, c, 0, H - 1, sgn, av, stop_mult=mult)
                gross = sgn * (ex.exit_price - entry)
                res = Call(asset, day, direction, 0.0, horizon, H, sl.index[0].date(), entry, av, ret > 0, ret / entry * 100,
                           mae / av, mfe / av, mae / entry * 100, mfe / entry * 100, stopped, ex.reason,
                           gross / entry * 100, gross / entry * 100 - self.cost, gross / dist)
        self._outcome_cache[key] = res
        return res

    # ---------- a full run ----------
    def run(self, months: tuple[int, ...], drivers: Optional[list[dict]] = None) -> dict:
        """Returns {'calls': independent calls, 'raw_calls': all, 'asset_days', 'no_view_days', 'unresolved'}."""
        drivers = drivers or self.drivers
        raw: list[Call] = []
        asset_days = no_view = unresolved = 0
        for asset in self.assets:
            for day in self.decision_days(asset, months):
                asset_days += 1
                view = evaluate_asset(asset, drivers, self.checked_on(day), self.cfg)
                if view.state not in ("UP", "DOWN"):
                    no_view += 1
                    continue
                hz = view.horizon or "swing"
                o = self.outcome(asset, day, 1 if view.state == "UP" else -1, hz)
                if o is None:
                    unresolved += 1
                    continue
                c = copy.copy(o)
                c.agreement = view.agreement
                c.firing_ids = tuple(sorted({f.driver_id for f in view.firings}))
                c._view = view  # type: ignore[attr-defined]
                raw.append(c)
        raw.sort(key=lambda c: (c.asset, c.day))
        indep: list[Call] = []
        busy_until: dict[str, date] = {}
        for c in raw:
            end_day = self._end_day(c)
            if c.asset in busy_until and c.day <= busy_until[c.asset]:
                continue
            busy_until[c.asset] = end_day
            indep.append(c)
        indep.sort(key=lambda c: c.day)
        return {"calls": indep, "raw_calls": raw, "asset_days": asset_days, "no_view_days": no_view, "unresolved": unresolved}

    def _end_day(self, c: Call) -> date:
        df = self.prices[c.asset]
        i = self._pos[c.asset][c.day]
        return df.index[i + c.bars].date()

    def label_calls(self, calls: list[Call]) -> None:
        """Agreement label (technical vs fundamental) for the given calls. Informational."""
        for c in calls:
            df = self.prices[c.asset]
            i = self._pos[c.asset][c.day]
            try:
                tv = technical.analyse(df.iloc[max(0, i - 399): i + 1], int(self.cfg["atr_period"]),
                                       float(self.cfg["stop_atr_multiple"]), float(self.cfg["caution_distance_atr"]))
            except Exception:  # noqa: BLE001
                tv = None
            c.agreement_label = compare(c._view, tv, float(self.cfg.get("min_room_atr", 1.0))).label  # type: ignore[attr-defined]
