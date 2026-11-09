"""Run this ON YOUR MACHINE after filling config/secrets.env. It tests every live source and
prints PASS/FAIL with the exact reason, plus the data point text the report would show.

    python3 -m pipeline.verify_sources
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import yaml

from collectors import registry
from core.secrets import load_secrets
from pipeline.run_action import ROOT

IST = timezone(timedelta(hours=5, minutes=30))


def main() -> int:
    secrets = load_secrets()
    print("Secrets present (names only):", ", ".join(sorted(secrets)) or "none")
    for need in ("FRED_API_KEY", "EIA_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY", "ALICEBLUE_USER_ID", "ALICEBLUE_API_KEY"):
        print(f"  {need}: {'set' if secrets.get(need) else 'MISSING'}")
    drivers = yaml.safe_load((ROOT / "config" / "drivers.yaml").read_text())["drivers"]
    now = datetime.now(IST)
    obs, errors = registry.collect(drivers, secrets, now, ROOT / "config" / "manual_inputs.yaml")
    print("\nData sources")
    for d in drivers:
        m = d["metric"]
        if d["source"]["status"] != "verified":
            print(f"  SKIP {d['id']:<5} {m}: source not verified (excluded by design)")
        elif m in obs:
            print(f"  PASS {d['id']:<5} {m}: {obs[m]['detail']['text']}")
        else:
            print(f"  FAIL {d['id']:<5} {m}: {errors.get(m, 'no value and no error?')}")
    print("\nIf a FAIL says the response schema changed, send me that line and I will update the parser.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
