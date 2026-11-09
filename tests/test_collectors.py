from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from collectors import cftc, eia, fred, registry
from collectors.http import FetchError
from collectors.lags import visible_on, visible_series
from collectors.metrics import (MetricError, change_abs, change_pct, level, percentile_latest,
                                seasonal_change_vs_avg, seasonal_vs_avg)
from core.secrets import load_secrets, redact, require, MissingSecret


# ---------------- metrics: hand-calculated ----------------
def daily(values, start=date(2026, 10, 1)):
    return [(start + timedelta(days=i), v) for i, v in enumerate(values)]


def test_change_pct_hand_calc_and_text():
    s = daily([100, 101, 102, 103, 104, 106])       # latest 106 vs 5 observations earlier (100)
    v, d, det = change_pct(s, 5, "Broad US dollar index")
    assert abs(v - 6.0) < 1e-9 and d == date(2026, 10, 6)
    assert "106 on 6 Oct 2026" in det["text"] and "100 on 1 Oct 2026" in det["text"] and "+6.00%" in det["text"]


def test_change_abs_in_percentage_points():
    v, d, det = change_abs(daily([1.80, 1.85, 1.90, 1.95, 2.00, 1.92]), 5, "US 10-year real yield")
    assert abs(v - 0.12) < 1e-9 and "+0.12 pp" in det["text"]


def test_level_and_too_short_and_duplicates():
    v, d, det = level(daily([20.5, 28.18]), "CBOE VIX")
    assert v == 28.18 and "28.18" in det["text"]
    with pytest.raises(MetricError):
        change_pct(daily([1, 2, 3]), 5, "x")
    with pytest.raises(MetricError):
        change_pct([(date(2026, 1, 1), 1.0), (date(2026, 1, 1), 2.0)] * 4, 1, "x")


def weekly(start, end, fn):
    out, d, i = [], start, 0
    while d <= end:
        out.append((d, fn(d, i)))
        d += timedelta(days=7)
        i += 1
    return out


def test_seasonal_vs_avg_hand_calc():
    # same-week values by year: 2021..2025 = 100,110,120,130,140 (avg 120); latest 2026 = 132 -> +10%
    s = weekly(date(2020, 1, 3), date(2026, 10, 30), lambda d, i: 100 + 10 * (d.year - 2021) if d.year < 2026 else 132)
    v, d, det = seasonal_vs_avg(s, "US commercial crude stocks", " thousand bbl")
    assert abs(v - 10.0) < 1e-9
    assert "average of 120" in det["text"] and "5 prior years" in det["text"]


def test_seasonal_vs_avg_needs_enough_years():
    s = weekly(date(2024, 1, 5), date(2026, 10, 30), lambda d, i: 100.0)
    with pytest.raises(MetricError):
        seasonal_vs_avg(s, "x")


def test_seasonal_change_vs_avg_hand_calc():
    s = weekly(date(2020, 1, 3), date(2026, 10, 30), lambda d, i: 1000 + 5 * i)   # every weekly change is +5
    s[-1] = (s[-1][0], s[-1][1] + 20)                                              # latest change is +25
    v, d, det = seasonal_change_vs_avg(s, "stocks", " thousand bbl", scale=1.0, out_unit=" x")
    assert abs(v - 20.0) < 1e-9
    v2, _, _ = seasonal_change_vs_avg(s, "stocks", scale=1 / 1000)
    assert abs(v2 - 0.020) < 1e-12


def test_seasonal_change_rejects_non_weekly_latest():
    s = weekly(date(2020, 1, 3), date(2026, 10, 23), lambda d, i: 1000 + 5 * i) + [(date(2026, 11, 20), 9999.0)]
    with pytest.raises(MetricError):
        seasonal_change_vs_avg(s, "x")


def test_percentile_latest():
    s = [(date(2023, 1, 1) + timedelta(days=7 * i), float(i)) for i in range(156)]
    v, _, det = percentile_latest(s, 156, 100, "net")
    assert v == 100.0
    s2 = s[:-1] + [(s[-1][0], -5.0)]
    v2, _, _ = percentile_latest(s2, 156, 100, "net")
    assert abs(v2 - 100 / 156) < 1e-9
    with pytest.raises(MetricError):
        percentile_latest(s[:50], 156, 100, "net")


# ---------------- lags ----------------
def test_publication_lags():
    assert visible_on("h10_weekly", date(2026, 10, 30)) == date(2026, 11, 2)    # Friday -> Monday
    assert visible_on("h10_weekly", date(2026, 11, 2)) == date(2026, 11, 9)     # Monday -> next Monday
    assert visible_on("eia_petro", date(2026, 10, 30)) == date(2026, 11, 4)     # Friday -> Wednesday
    assert visible_on("eia_gas", date(2026, 10, 30)) == date(2026, 11, 5)       # Friday -> Thursday
    assert visible_on("cot", date(2026, 11, 3)) == date(2026, 11, 6)            # Tuesday -> Friday
    assert visible_on("daily_1d", date(2026, 11, 5)) == date(2026, 11, 6)
    s = [(date(2026, 10, 30), 1.0), (date(2026, 11, 2), 2.0)]
    assert visible_series(s, "h10_weekly", date(2026, 11, 3)) == [(date(2026, 10, 30), 1.0)]


# ---------------- FRED ----------------
def test_fred_json_parse_skips_missing_and_validates():
    p = {"observations": [{"date": "2026-10-29", "value": "."}, {"date": "2026-10-30", "value": "121.6656"}]}
    assert fred.parse_fred_json(p, "DTWEXBGS") == [(date(2026, 10, 30), 121.6656)]
    with pytest.raises(FetchError):
        fred.parse_fred_json({"error": "x"}, "DTWEXBGS")
    with pytest.raises(FetchError):
        fred.parse_fred_json({"observations": [{"date": "2026-10-30", "value": "abc"}]}, "DTWEXBGS")
    with pytest.raises(FetchError):
        fred.parse_fred_json({"observations": [{"date": "2026-10-29", "value": "."}]}, "DTWEXBGS")


def test_fred_csv_parse():
    txt = "observation_date,DEXINUS\n2026-10-29,.\n2026-10-30,94.94\n"
    assert fred.parse_fred_csv(txt, "DEXINUS") == [(date(2026, 10, 30), 94.94)]
    with pytest.raises(FetchError):
        fred.parse_fred_csv("DATE,OTHER\n2026-10-30,1\n", "DEXINUS")


# ---------------- EIA ----------------
def crude_rows(units="MBBL", series="WCESTUS1"):
    return {"response": {"data": [{"period": "2026-10-30", "series": series, "value": "421000", "units": units},
                                  {"period": "2026-10-23", "series": series, "value": "419500", "units": units}]}}


def test_eia_crude_parse_and_guards():
    assert eia.parse_crude(crude_rows())[0] == (date(2026, 10, 30), 421000.0)
    with pytest.raises(FetchError):
        eia.parse_crude(crude_rows(units="MMBBL"))
    with pytest.raises(FetchError):
        eia.parse_crude(crude_rows(series="WCSSTUS1"))
    with pytest.raises(FetchError):
        eia.parse_crude({"nope": 1})


def ngs_payload(pct_published=4.6, cur="3,785", avg="3,618"):
    return {"release_date": "2026-Nov-05 00:00:00", "current_week": "2026-10-30",
            "series": [{"name": "East", "units": "Bcf", "calculated": {}, "data": []},
                       {"name": "Total Lower 48", "units": "Bcf", "unitsshort": "Bcf",
                        "calculated": {"5yr-avg": avg, "net_change": "80", "pct-chg_5yr-avg": str(pct_published)},
                        "data": [["2026-10-30", cur], ["2026-10-23", "3,705"], ["2025-10-30", "3,679"]]}]}


def test_ngs_parse_cross_check():
    r = eia.parse_ngs(ngs_payload())
    assert r["week"] == date(2026, 10, 30) and abs(r["pct_vs_5y"] - (3785 / 3618 - 1) * 100) < 1e-9
    with pytest.raises(FetchError):
        eia.parse_ngs(ngs_payload(pct_published=9.9))          # EIA's own % disagrees with ours
    bad = ngs_payload()
    bad["series"][1]["units"] = "Tcf"
    bad["series"][1]["unitsshort"] = "Tcf"
    with pytest.raises(FetchError):
        eia.parse_ngs(bad)
    with pytest.raises(FetchError):
        eia.parse_ngs({"series": []})


# ---------------- CFTC ----------------
def cot_rows(name="GOLD - COMMODITY EXCHANGE INC.", lng="122508", sht="16946", oi="396810"):
    return [{"report_date_as_yyyy_mm_dd": "2026-10-27T00:00:00.000", "market_and_exchange_names": name,
             "m_money_positions_long_all": lng, "m_money_positions_short_all": sht, "open_interest_all": oi}]


def test_cot_parse_and_guards():
    s = cftc.parse_cot(cot_rows(), "GOLD", "088691")
    assert s[0][0] == date(2026, 10, 27) and abs(s[0][1] - (122508 - 16946) / 396810 * 100) < 1e-9
    with pytest.raises(FetchError):
        cftc.parse_cot(cot_rows(name="COPPER - COMEX"), "GOLD", "088691")
    with pytest.raises(FetchError):
        cftc.parse_cot(cot_rows(lng="999999"), "GOLD", "088691")     # long > open interest
    with pytest.raises(FetchError):
        cftc.parse_cot([{"foo": 1}], "GOLD", "088691")


# ---------------- secrets ----------------
def test_secrets_file_env_override_and_redaction(tmp_path):
    p = tmp_path / "s.env"
    p.write_text("# c\nFRED_API_KEY=abc123456789\nEIA_API_KEY=\nANTHROPIC_API_KEY='sk-test-xyz-123456'\n")
    s = load_secrets(p, environ={})
    assert s == {"FRED_API_KEY": "abc123456789", "ANTHROPIC_API_KEY": "sk-test-xyz-123456"}
    s2 = load_secrets(p, environ={"FRED_API_KEY": "override-999999"})
    assert s2["FRED_API_KEY"] == "override-999999"
    with pytest.raises(MissingSecret) as e:
        require(s, ["EIA_API_KEY"])
    assert "abc123456789" not in str(e.value)
    red = redact("GET https://x/y?api_key=abc123456789&z=1 failed", s)
    assert "abc123456789" not in red and "api_key=***" in red


def test_real_secrets_file_is_private_and_blank():
    p = Path("config/secrets.env")
    assert p.exists() and (p.stat().st_mode & 0o077) == 0, "secrets.env must be chmod 600"
    assert load_secrets(p, environ={}) == {}, "shipped secrets.env must contain no real keys"
    assert "secrets.env" in Path(".gitignore").read_text()


# ---------------- registry end to end with a fake network ----------------
class FakeNet:
    def __init__(self):
        self.calls = []

    def raw(self, url, params=None, headers=None, **kw):
        self.calls.append(("raw", url, params))
        sid = dict(params)["id"]
        base = {"DTWEXBGS": 120.0, "DEXINUS": 90.0, "DFII10": 1.9, "DGS2": 4.5, "VIXCLS": 28.18}[sid]
        rows = ["observation_date," + sid]
        for i in range(12):
            d = date(2026, 10, 19) + timedelta(days=i)
            rows.append(f"{d.isoformat()},{base + 0.1 * i:.4f}")
        return "\n".join(rows).encode()

    def json(self, url, params=None, headers=None, **kw):
        self.calls.append(("json", url, params))
        if "stlouisfed" in url:
            raise AssertionError("FRED API must not be used without a key")
        if "api.eia.gov" in url:
            raise FetchError("HTTP 403 for https://api.eia.gov/...?api_key=SECRET1234567")
        if "ir.eia.gov" in url:
            return ngs_payload()
        if "cftc" in url:
            code = dict(params)["cftc_contract_market_code"]
            name = {"088691": "GOLD - COMEX", "084691": "SILVER - COMEX", "067651": "WTI-PHYSICAL - NYMEX",
                    "023651": "NAT GAS NYME - NYMEX"}[code]
            rows = []
            for i in range(160):
                d = date(2023, 12, 5) + timedelta(days=7 * i)
                rows.append({"report_date_as_yyyy_mm_dd": d.isoformat() + "T00:00:00.000", "market_and_exchange_names": name,
                             "m_money_positions_long_all": str(1000 + i), "m_money_positions_short_all": "500",
                             "open_interest_all": "5000"})
            return rows
        raise AssertionError(f"unexpected URL {url}")


def test_registry_collect_with_partial_failure_and_no_unverified_calls(tmp_path):
    drivers = yaml.safe_load(open("config/drivers.yaml"))["drivers"]
    net = FakeNet()
    manual = tmp_path / "manual.yaml"
    manual.write_text("india_gold_duty_chg_pp:\n  value: -2.0\n  as_of: '2026-10-15'\n  note: 'test notification'\n")
    now = datetime(2026, 11, 7, 8, 0, tzinfo=timezone.utc)
    obs, errors = registry.collect(drivers, {"EIA_API_KEY": "SECRET1234567"}, now, manual, net.json, net.raw)

    # produced
    for m in ["usd_broad_5d_pct", "us10y_real_5d_chg_pp", "us2y_5d_chg_pp", "usdinr_5d_pct", "vix_level",
              "eia_natgas_storage_vs_5y_pct", "cot_gold_pctile_3y", "cot_natgas_pctile_3y", "india_gold_duty_chg_pp"]:
        assert m in obs, (m, errors)
        assert obs[m]["verified"] and obs[m]["detail"]["text"]
    assert abs(obs["usd_broad_5d_pct"]["value"] - ((120 + 0.1 * 11) / (120 + 0.1 * 6) - 1) * 100) < 1e-6
    assert obs["india_gold_duty_chg_pp"]["value"] == -2.0
    # EIA crude failed -> reported, secret redacted, other sources unaffected
    assert "eia_crude_stocks_vs_5y_pct" in errors and "SECRET1234567" not in errors["eia_crude_stocks_vs_5y_pct"]
    assert "eia_crude_stocks_vs_5y_pct" not in obs
    # only verified sources were requested
    urls = " ".join(u for _, u, _ in net.calls)
    assert "brent" not in urls.lower() and "bakerhughes" not in urls.lower()
    # real drivers.yaml: every verified metric is either produced or explained, never silently missing
    verified = {d["metric"] for d in drivers if d["source"]["status"] == "verified"}
    assert verified <= set(obs) | set(errors), verified - set(obs) - set(errors)
