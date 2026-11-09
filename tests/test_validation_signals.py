from datetime import datetime, timedelta, timezone

import yaml

from core.signals import evaluate_asset, rule_text
from core.validation import check_observation

NOW = datetime(2026, 11, 7, 8, 0, tzinfo=timezone.utc)
CFG = {"confidence_bar": 0.70, "min_coverage": 0.35, "min_firing_drivers": 2}


def drv(i, metric, weight, rules, stale=96, status="verified", horizon="swing", flag_only=False):
    return {
        "id": i, "name": metric, "metric": metric, "weight": weight, "rules": rules,
        "horizon": horizon, "stale_after_hours": stale, "flag_only": flag_only,
        "source": {"provider": "test", "status": status},
    }


def obs(v, hours_old=1, **kw):
    d = {"value": v, "as_of": (NOW - timedelta(hours=hours_old)).isoformat(), "source": "unit-test", "verified": True}
    d.update(kw)
    return d


# ---------- validation ----------
def test_fresh_verified_ok():
    c = check_observation(drv("A", "m", 1, []), obs(1.5), NOW)
    assert c.ok and c.value == 1.5


def test_stale_excluded():
    c = check_observation(drv("A", "m", 1, [], stale=24), obs(1.5, hours_old=25), NOW)
    assert not c.ok and "stale" in c.reason


def test_exactly_at_limit_is_ok():
    c = check_observation(drv("A", "m", 1, [], stale=24), obs(1.5, hours_old=24), NOW)
    assert c.ok


def test_source_not_verified_excluded():
    c = check_observation(drv("A", "m", 1, [], status="to_verify"), obs(1.5), NOW)
    assert not c.ok and "not verified" in c.reason


def test_missing_and_nan_and_bool():
    d = drv("A", "m", 1, [])
    assert not check_observation(d, None, NOW).ok
    assert not check_observation(d, obs(float("nan")), NOW).ok
    assert not check_observation(d, obs(True), NOW).ok
    assert not check_observation(d, obs(None), NOW).ok


def test_naive_timestamp_and_future_rejected():
    d = drv("A", "m", 1, [])
    assert not check_observation(d, {"value": 1, "as_of": "2026-11-07T07:00:00", "verified": True}, NOW).ok
    assert not check_observation(d, obs(1, hours_old=-5), NOW).ok


def test_two_extractions_must_agree():
    d = drv("A", "m", 1, [])
    assert check_observation(d, obs(100.0, second_value=100.2), NOW, tolerance_pct=0.5).ok
    c = check_observation(d, obs(100.0, second_value=105.0), NOW, tolerance_pct=0.5)
    assert not c.ok and "disagree" in c.reason


def test_collector_unverified_flag_excluded():
    assert not check_observation(drv("A", "m", 1, []), obs(1, verified=False), NOW).ok


def test_sanity_range():
    d = drv("A", "m", 1, [])
    assert not check_observation(d, obs(900), NOW, sanity_range=(0, 100)).ok


# ---------- signals ----------
def gold_drivers():
    return [
        drv("D1", "usd", 1.0, [{"op": ">", "threshold": 0.5, "effects": {"gold": -1}},
                               {"op": "<", "threshold": -0.5, "effects": {"gold": 1}}]),
        drv("D2", "real", 1.2, [{"op": ">", "threshold": 0.1, "effects": {"gold": -1}},
                                {"op": "<", "threshold": -0.1, "effects": {"gold": 1}}]),
        drv("D3", "inr", 0.8, [{"op": ">", "threshold": 0.3, "effects": {"gold": 1}},
                               {"op": "<", "threshold": -0.3, "effects": {"gold": -1}}]),
    ]


def run(drivers, values, asset="gold", stale_overrides=None):
    checked = {}
    for d in drivers:
        o = values.get(d["metric"])
        checked[d["id"]] = check_observation(d, o, NOW)
    return evaluate_asset(asset, drivers, checked, CFG)


def test_aligned_up_with_trace():
    v = run(gold_drivers(), {"usd": obs(-0.8), "real": obs(-0.2), "inr": obs(0.4)})
    assert v.state == "UP"
    assert v.up_weight == 1.0 + 1.2 + 0.8 and v.down_weight == 0
    assert v.agreement == 1.0 and v.coverage == 1.0
    assert len(v.firings) == 3
    txt = rule_text(v.firings[0])
    assert "D1" in txt and "usd < -0.5" in txt and "-0.8" in txt and "bullish" in txt


def test_disagreement_gives_neutral_not_opposite():
    # up weight 1.0+0.8=1.8, down 1.2 -> agreement 0.6 < 0.7
    v = run(gold_drivers(), {"usd": obs(-0.8), "real": obs(0.2), "inr": obs(0.4)})
    assert v.state == "NEUTRAL" and abs(v.agreement - 0.6) < 1e-9


def test_threshold_is_strict():
    # exactly at threshold must NOT fire for '>' and '<'
    v = run(gold_drivers(), {"usd": obs(0.5), "real": obs(0.1), "inr": obs(0.3)})
    assert v.firings == [] and v.state == "NEUTRAL"


def test_single_driver_not_enough():
    v = run(gold_drivers(), {"usd": obs(-0.8), "real": obs(0.0), "inr": obs(0.0)})
    assert v.state == "NEUTRAL" and "need at least 2" in v.reason


def test_stale_driver_excluded_and_listed():
    d = gold_drivers()
    vals = {"usd": obs(-0.8), "real": obs(-0.2, hours_old=500), "inr": obs(0.4)}
    v = run(d, vals)
    assert [c.driver_id for c in v.excluded] == ["D2"]
    assert v.available_weight == 1.0 + 0.8  # stale driver does not count as available
    assert v.state == "UP"


def test_no_fresh_data_is_degraded():
    v = run(gold_drivers(), {})
    assert v.state == "DEGRADED" and len(v.excluded) == 3


def test_low_coverage_blocks_view():
    d = gold_drivers() + [drv("D4", "x", 10.0, [{"op": ">", "threshold": 1000, "effects": {"gold": 1}}])]
    v = run(d, {"usd": obs(-0.8), "real": obs(-0.2), "inr": obs(0.0), "x": obs(0)})
    # firing 2.2 of available 13.0 -> coverage 17%
    assert v.state == "NEUTRAL" and "Coverage" in v.reason


def test_other_asset_drivers_ignored():
    d = gold_drivers() + [drv("C1", "eia", 1.0, [{"op": "<", "threshold": -2, "effects": {"crude": 1}}])]
    v = run(d, {"usd": obs(-0.8), "real": obs(-0.2), "inr": obs(0.4), "eia": obs(-5)})
    assert v.asset == "gold" and all(f.driver_id != "C1" for f in v.firings)


def test_flag_only_never_moves_direction():
    d = gold_drivers() + [drv("F1", "cot", 0.0, [{"op": ">", "threshold": 90, "effects": {"gold": 0}, "flag": "crowded long"}], flag_only=True)]
    v = run(d, {"usd": obs(-0.8), "real": obs(-0.2), "inr": obs(0.4), "cot": obs(95)})
    assert v.state == "UP" and v.available_weight == 3.0 and any("crowded long" in f for f in v.flags)


def test_horizon_is_shortest_of_supporting():
    d = gold_drivers()
    d[0]["horizon"] = "positional"
    d[1]["horizon"] = "intraday"
    v = run(d, {"usd": obs(-0.8), "real": obs(-0.2), "inr": obs(0.4)})
    assert v.horizon == "intraday"


# ---------- the real config ----------
def test_real_drivers_parse_and_fail_closed():
    cfg = yaml.safe_load(open("config/drivers.yaml"))
    ids = [d["id"] for d in cfg["drivers"]]
    assert len(ids) == len(set(ids)), "duplicate driver ids"
    for d in cfg["drivers"]:
        assert d["horizon"] in ("intraday", "swing", "positional")
        assert d["stale_after_hours"] > 0 and d["weight"] >= 0
        for r in d["rules"]:
            assert r["op"] in (">", "<", ">=", "<=") and isinstance(r["threshold"], (int, float))
            for a, e in r["effects"].items():
                assert a in ("gold", "silver", "crude", "natgas") and -1 <= e <= 1
        # rules of one driver must not overlap (only one may fire)
        ups = [r for r in d["rules"] if r["op"] in (">", ">=")]
        dns = [r for r in d["rules"] if r["op"] in ("<", "<=")]
        for u in ups:
            for w in dns:
                assert not (u["threshold"] < w["threshold"]), f"overlapping rules in {d['id']}"
    # every driver except the manual one is still 'to_verify' -> a run with real numbers must exclude them
    user = yaml.safe_load(open("config/user.yaml"))
    drivers = cfg["drivers"]
    checked = {d["id"]: check_observation(d, obs(0.0), NOW) for d in drivers}
    to_verify = {d["id"] for d in drivers if d["source"]["status"] != "verified"}
    assert to_verify, "expected some drivers to still be unverified"
    # every unverified driver must be excluded, for every asset it mentions
    for asset in ("gold", "silver", "crude", "natgas"):
        v = evaluate_asset(asset, drivers, checked, user)
        excluded_ids = {c.driver_id for c in v.excluded if "not verified" in c.reason}
        mentioned = {d["id"] for d in drivers if d["id"] in to_verify and any(asset in r["effects"] for r in d["rules"])}
        assert excluded_ids == mentioned, (asset, excluded_ids, mentioned)
        assert not any(f.driver_id in to_verify for f in v.firings)
    # every verified-source driver carries the fields the report needs
    for d in drivers:
        assert d.get("label") and d.get("metric_phrase") is not None
        if d["source"]["status"] == "verified" and not d.get("flag_only"):
            for r in d["rules"]:
                for a in r["effects"]:
                    assert r.get("why", {}).get(a), f"{d['id']} rule missing rationale for {a}"
