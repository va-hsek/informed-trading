"""Run on your machine (needs internet and your keys in config/secrets.env).

    python3 -m backtest.fetch_history

Downloads FRED, EIA crude and CFTC history into data/history/. It does NOT download prices:
put a continuous daily series per asset in data/history/prices/<asset>.csv
(date,open,high,low,close,volume; assets: gold, silver, crude, natgas).
Futures history must use the roll rule in the pre-registration (roll 5 trading days before expiry, difference back-adjusted;
core.prices.stitch does this from per-contract files).
"""
from __future__ import annotations

from datetime import date, timedelta

import yaml

from collectors import cftc, eia, fred
from collectors.http import FetchError
from collectors.registry import COT_METRICS, FRED_METRICS
from core.secrets import load_secrets, redact
from pipeline.run_action import ROOT

from .history import write_series


def main() -> int:
    sec = load_secrets()
    cfg = yaml.safe_load((ROOT / "config" / "user.yaml").read_text())
    out = ROOT / cfg.get("data_dir", "data") / "history"
    today = date.today()
    start = date(today.year - 5 - 1, 1, 1) - timedelta(days=400)        # window + warm-up for 3-year percentile / 5-year seasonal
    start_eia = date(today.year - 5 - 1, 1, 1) - timedelta(days=int(365 * 6.3))
    failures = 0
    for sid in sorted({v[0] for v in FRED_METRICS.values()}):
        try:
            s = fred.fetch_fred(sid, sec.get("FRED_API_KEY"), start)
            write_series(s, out / f"fred_{sid}.csv")
            print(f"OK   FRED {sid}: {len(s)} rows {s[0][0]} .. {s[-1][0]}")
        except FetchError as e:
            failures += 1
            print(f"FAIL FRED {sid}: {redact(str(e), sec)}")
    try:
        s = eia.fetch_crude(sec["EIA_API_KEY"], start_eia)
        write_series(s, out / "eia_crude.csv")
        print(f"OK   EIA crude: {len(s)} rows {s[0][0]} .. {s[-1][0]}")
    except (FetchError, KeyError) as e:
        failures += 1
        print(f"FAIL EIA crude: {redact(str(e), sec)}")
    for m, (code, name, _label) in COT_METRICS.items():
        try:
            s = cftc.fetch_cot(code, name, sec.get("CFTC_APP_TOKEN"), weeks=520)
            write_series(sorted(s), out / f"cot_{m}.csv")
            print(f"OK   CFTC {m}: {len(s)} rows {min(s)[0]} .. {max(s)[0]}")
        except FetchError as e:
            failures += 1
            print(f"FAIL CFTC {m}: {redact(str(e), sec)}")
    print("Not downloaded: eia_ngs.csv (natural gas storage history; R11 will be reported as not backtestable) and prices.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
