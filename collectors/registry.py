"""Builds observations for every VERIFIED driver. Never fetches to_verify sources.

Returns (observations, errors):
  observations: metric -> observation dict (value, as_of, source, verified, detail)
  errors:       metric -> plain-language reason the metric could not be produced
A failure of one source never stops the others, and never produces a guessed value.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

import yaml

from . import cftc, eia, fred
from .http import FetchError
from .metrics import (MetricError, change_abs, change_pct, level, make_obs, percentile_latest,
                      seasonal_change_vs_avg, seasonal_vs_avg, as_of_iso, fmt_date)
from core.secrets import redact

FRED_METRICS = {
    # metric: (series, kind, n, label, unit)
    "usd_broad_5d_pct": ("DTWEXBGS", "pct", 5, "Broad US dollar index", ""),
    "us10y_real_5d_chg_pp": ("DFII10", "abs", 5, "US 10-year real yield", "%"),
    "us2y_5d_chg_pp": ("DGS2", "abs", 5, "US 2-year yield", "%"),
    "usdinr_5d_pct": ("DEXINUS", "pct", 5, "USDINR (rupees per US dollar)", ""),
    "vix_level": ("VIXCLS", "level", 0, "CBOE VIX", ""),
}
COT_METRICS = {
    "cot_gold_pctile_3y": ("088691", "GOLD", "Gold managed-money net position (% of open interest)"),
    "cot_silver_pctile_3y": ("084691", "SILVER", "Silver managed-money net position (% of open interest)"),
    "cot_crude_pctile_3y": ("067651", "WTI", "WTI managed-money net position (% of open interest)"),
    "cot_natgas_pctile_3y": ("023651", "NAT GAS", "Natural gas managed-money net position (% of open interest)"),
}


def compute_fred_metric(metric: str, series) -> tuple[float, date, dict]:
    sid, kind, n, label, unit = FRED_METRICS[metric]
    if kind == "pct":
        return change_pct(series, n, label, unit)
    if kind == "abs":
        return change_abs(series, n, label, unit)
    return level(series, label, unit)


def compute_cot_metric(metric: str, net_pct_series) -> tuple[float, date, dict]:
    return percentile_latest(net_pct_series, window=156, min_n=100, label=COT_METRICS[metric][2])


def _manual(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    m = (yaml.safe_load(path.read_text()) or {}).get("india_gold_duty_chg_pp") or {}
    if m.get("value") is None:
        return None
    try:
        v = float(m["value"])
        d = datetime.strptime(str(m["as_of"]), "%Y-%m-%d").date()
    except Exception as e:  # noqa: BLE001
        raise FetchError(f"manual_inputs.yaml: value/as_of unusable ({e})") from e
    note = (m.get("note") or "").strip()
    text = f"India gold import duty change entered by you: {v:+g} pp, notified {fmt_date(d)}" + (f" ({note})" if note else "")
    return make_obs(v, d, "manual input (config/manual_inputs.yaml)", {"text": text, "kind": "manual"})


def collect(drivers: list[dict], secrets: dict, now: datetime, manual_path: Path,
            http_json: Optional[Callable] = None, http_raw: Optional[Callable] = None):
    from .http import get, get_json
    hj, hr = http_json or get_json, http_raw or get
    obs: dict[str, dict] = {}
    errors: dict[str, str] = {}
    wanted = {d["metric"]: d for d in drivers if d["source"]["status"] == "verified"}
    start = (now.date() - timedelta(days=60))

    fred_cache: dict[str, list] = {}
    for metric in [m for m in wanted if m in FRED_METRICS]:
        sid = FRED_METRICS[metric][0]
        try:
            if sid not in fred_cache:
                fred_cache[sid] = fred.fetch_fred(sid, secrets.get("FRED_API_KEY"), start, hj, hr)
            v, d, detail = compute_fred_metric(metric, fred_cache[sid])
            obs[metric] = make_obs(v, d, f"FRED {sid}", detail)
        except (FetchError, MetricError) as e:
            errors[metric] = redact(str(e), secrets)

    # EIA crude (two metrics from one series)
    crude_metrics = [m for m in wanted if m in ("eia_crude_stocks_vs_5y_pct", "eia_crude_weekly_chg_vs_5y_mb")]
    if crude_metrics:
        try:
            if not secrets.get("EIA_API_KEY"):
                raise FetchError("EIA_API_KEY is not set in config/secrets.env")
            s = eia.fetch_crude(secrets["EIA_API_KEY"], now.date() - timedelta(days=int(365 * 6.2)), hj)
            for m in crude_metrics:
                if m == "eia_crude_stocks_vs_5y_pct":
                    v, d, det = seasonal_vs_avg(s, "US commercial crude stocks", " thousand bbl")
                else:
                    v, d, det = seasonal_change_vs_avg(s, "US commercial crude stocks", " thousand bbl",
                                                       scale=1 / 1000, out_unit=" million bbl")
                obs[m] = make_obs(v, d, f"EIA {eia.CRUDE_SERIES}", det)
        except (FetchError, MetricError) as e:
            for m in crude_metrics:
                errors[m] = redact(str(e), secrets)

    if "eia_natgas_storage_vs_5y_pct" in wanted:
        try:
            r = eia.fetch_ngs(hj)
            text = (f"US Lower-48 working gas in storage {r['current_bcf']:,.0f} bcf for the week of {fmt_date(r['week'])}, "
                    f"versus a 5-year average of {r['avg5_bcf']:,.0f} bcf: {r['pct_vs_5y']:+.2f}% "
                    f"(EIA publishes {r['published_pct']:+.2f}%)")
            obs["eia_natgas_storage_vs_5y_pct"] = make_obs(r["pct_vs_5y"], r["week"], "EIA weekly storage report",
                                                           {"text": text, "kind": "ngs", **{k: str(v) for k, v in r.items()}})
        except (FetchError, MetricError) as e:
            errors["eia_natgas_storage_vs_5y_pct"] = redact(str(e), secrets)

    for metric in [m for m in wanted if m in COT_METRICS]:
        code, name, _ = COT_METRICS[metric]
        try:
            series = cftc.fetch_cot(code, name, secrets.get("CFTC_APP_TOKEN"), http_json=hj)
            v, d, det = compute_cot_metric(metric, series)
            obs[metric] = make_obs(v, d, f"CFTC disaggregated {code}", det)
        except (FetchError, MetricError) as e:
            errors[metric] = redact(str(e), secrets)

    if "india_gold_duty_chg_pp" in wanted:
        try:
            m = _manual(manual_path)
            if m:
                obs["india_gold_duty_chg_pp"] = m
            else:
                errors["india_gold_duty_chg_pp"] = "no duty change entered (nothing to report)"
        except FetchError as e:
            errors["india_gold_duty_chg_pp"] = str(e)

    return obs, errors
