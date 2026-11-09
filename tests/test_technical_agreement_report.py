from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from core.agreement import compare
from core.report import render_asset, render_report
from core.sizing import build_scenario
from core.signals import AssetView
from core.technical import (
    TechnicalDataError, analyse, atr, classic_pivots, swing_points, validate_ohlcv, volume_profile,
)


def frame(highs, lows, closes, vols=None, start="2026-01-01"):
    n = len(closes)
    idx = pd.date_range(start, periods=n, freq="D")
    return pd.DataFrame({
        "open": closes, "high": highs, "low": lows, "close": closes,
        "volume": vols if vols is not None else [1000] * n,
    }, index=idx)


def wave(n=250, base=100.0, amp=10.0, period=40, drift=0.0, vol=1000):
    t = np.arange(n)
    c = base + amp * np.sin(2 * np.pi * t / period) + drift * t
    return frame(c + 1.0, c - 1.0, c, [vol] * n)


# ---------- indicators with hand-checkable answers ----------
def test_atr_constant_range_equals_range():
    df = frame([102] * 100, [100] * 100, [101] * 100)
    assert abs(atr(df, 14).iloc[-1] - 2.0) < 1e-9


def test_classic_pivots_hand_calc():
    p = classic_pivots(110, 90, 100)
    assert abs(p["P"] - 100) < 1e-9
    assert abs(p["R1"] - 110) < 1e-9 and abs(p["S1"] - 90) < 1e-9
    assert abs(p["R2"] - 120) < 1e-9 and abs(p["S2"] - 80) < 1e-9


def test_swing_points_zigzag():
    closes = [10, 11, 12, 13, 12, 11, 10, 11, 12, 13, 14, 13, 12, 11, 10]
    df = frame([c + 0.5 for c in closes], [c - 0.5 for c in closes], closes)
    highs, lows = swing_points(df, left=3, right=3)
    assert 14.5 in highs          # peak at the 14 close
    assert 9.5 in lows            # trough at the 10 close (index 6)


def test_volume_profile_poc_where_volume_is():
    n = 100
    highs = [101] * n
    lows = [99] * n
    closes = [100] * n
    vols = [10] * n
    # a wide bar with huge volume sitting at 140-141
    highs[50], lows[50], closes[50], vols[50] = 141, 140, 140.5, 100000
    df = frame(highs, lows, closes, vols)
    prof = volume_profile(df, bins=40)
    assert 139 <= prof["poc"] <= 142
    assert prof["val"] <= prof["poc"] <= prof["vah"]


def test_volume_profile_none_without_volume():
    df = frame([101] * 70, [99] * 70, [100] * 70, [0] * 70)
    assert volume_profile(df) is None


def test_validate_rejects_bad_data():
    df = wave()
    with pytest.raises(TechnicalDataError):
        validate_ohlcv(df.iloc[:20])
    bad = df.copy()
    bad.iloc[10, bad.columns.get_loc("high")] = bad.iloc[10]["low"] - 1
    with pytest.raises(TechnicalDataError):
        validate_ohlcv(bad)
    nan = df.copy()
    nan.iloc[5, nan.columns.get_loc("close")] = np.nan
    with pytest.raises(TechnicalDataError):
        validate_ohlcv(nan)
    with pytest.raises(TechnicalDataError):
        validate_ohlcv(df.iloc[::-1])
    with pytest.raises(TechnicalDataError):
        validate_ohlcv(df.drop(columns=["volume"]))


# ---------- analyse ----------
def test_analyse_basic_properties():
    df = wave()
    t = analyse(df)
    assert t.last_close == df["close"].iloc[-1]
    assert t.support.price < t.last_close < t.resistance.price
    assert t.support_dist_price > 0 and t.resistance_dist_price > 0
    assert abs(t.support_dist_atr * t.atr - t.support_dist_price) < 1e-9
    assert t.location in ("FLOOR", "MIDDLE", "CEILING")
    assert 0 <= t.range_pos <= 1
    assert t.stop_zone_long < t.last_close < t.stop_zone_short
    assert abs((t.last_close - t.stop_zone_long) - 1.5 * t.atr) < 1e-9
    assert t.ma["MA200"] is not None and t.profile is not None


def test_analyse_no_volume_is_reported_not_guessed():
    df = wave(vol=0)
    t = analyse(df)
    assert t.profile is None and "no volume" in t.volume_note


def test_location_floor_when_price_just_above_support():
    df = wave(n=250)
    t = analyse(df)
    # whatever location we got, it must agree with the range position thresholds
    expect = "FLOOR" if t.range_pos <= 0.25 else "CEILING" if t.range_pos >= 0.75 else "MIDDLE"
    assert t.location == expect


# ---------- agreement ----------
def fake_fund(state):
    return AssetView(asset="gold", state=state, reason="x", agreement=0.9, coverage=0.8)


def test_agreement_labels_for_non_directional_states():
    t = analyse(wave())
    assert compare(fake_fund("NEUTRAL"), t).label == "FUNDAMENTAL_NEUTRAL"
    assert compare(fake_fund("DEGRADED"), t).label == "DATA_DEGRADED"
    assert compare(fake_fund("UP"), None).label == "TECH_UNAVAILABLE"


def test_up_at_ceiling_is_conflicting_and_down_at_floor_is_conflicting():
    t = analyse(wave())
    t.location, t.resistance_dist_atr, t.support_dist_atr = "CEILING", 0.2, 5.0
    a = compare(fake_fund("UP"), t)
    assert a.label == "CONFLICTING" and any("ceiling" in c for c in a.conflicts) and a.resolution
    t.location, t.support_dist_atr, t.resistance_dist_atr = "FLOOR", 0.2, 5.0
    a = compare(fake_fund("DOWN"), t)
    assert a.label == "CONFLICTING" and any("floor" in c for c in a.conflicts)


def test_up_trend_with_room_is_aligned():
    df = wave(drift=0.3, amp=3)          # steady uptrend
    t = analyse(df)
    t.location, t.resistance_dist_atr = "MIDDLE", 3.0
    a = compare(fake_fund("UP"), t)
    px_, m50, m200 = t.last_close, t.ma["MA50"], t.ma["MA200"]
    assert px_ > m50 and px_ > m200, "synthetic uptrend should sit above both averages"
    assert a.label == "ALIGNED" and a.supports and not a.conflicts


def test_up_view_below_both_averages_is_conflict():
    df = wave(drift=-0.3, amp=2)         # steady downtrend
    t = analyse(df)
    t.location, t.resistance_dist_atr = "MIDDLE", 3.0
    assert t.last_close < t.ma["MA50"] and t.last_close < t.ma["MA200"]
    a = compare(fake_fund("UP"), t)
    assert a.label == "CONFLICTING" and any("averages" in c for c in a.conflicts)


# ---------- report ----------
def test_report_renders_and_has_required_sections():
    df = wave(drift=0.2, amp=4)
    t = analyse(df)
    f = AssetView(asset="gold", state="UP", reason="Agreement 100%", agreement=1.0, coverage=0.9, horizon="swing")
    a = compare(f, t)
    s = build_scenario("long", t.last_close, t.stop_zone_long, t.resistance.price, {"multiplier": 10},
                       margin_per_lot=50000, capital=100000)
    md = render_asset(f, t, a, s, "synthetic test", "n/a")
    for h in ["Agreement panel", "### Fundamental", "### Technical", "### Size and margin scenario",
              "### What could go wrong", "### What to watch", "Margin-call price"]:
        assert h in md
    for banned in ["you should buy", "you should sell", "we recommend"]:
        assert banned not in md.lower()
    full = render_report(datetime(2026, 11, 7, tzinfo=timezone.utc), [md], [], [])
    assert "Action report" in full and "decision support" in full


def test_report_warns_when_stop_is_inside_one_atr():
    df = wave()
    t = analyse(df)
    f = AssetView(asset="gold", state="UP", reason="r", agreement=1.0, coverage=1.0, horizon="swing")
    a = compare(f, t)
    tight = t.last_close - 0.3 * t.atr
    s = build_scenario("long", t.last_close, tight, None, {"multiplier": 10}, margin_per_lot=1000, capital=100000)
    md = render_asset(f, t, a, s, "synthetic test", "n/a")
    assert "ordinary daily volatility could reach it" in md
    wide = t.last_close - 2.0 * t.atr
    s2 = build_scenario("long", t.last_close, wide, None, {"multiplier": 10}, margin_per_lot=1000, capital=100000)
    assert "ordinary daily volatility" not in render_asset(f, t, a, s2, "synthetic test", "n/a")


def test_report_says_need_additional_when_unaffordable():
    df = wave()
    t = analyse(df)
    f = AssetView(asset="silver", state="DOWN", reason="r", agreement=1.0, coverage=1.0, horizon="swing")
    a = compare(f, t)
    s = build_scenario("short", t.last_close, t.stop_zone_short, t.support.price, {"multiplier": 10},
                       margin_per_lot=80000, capital=50000)
    md = render_asset(f, t, a, s, "synthetic test", "n/a")
    assert "Need Rs" in md and "additional" in md and "too big" not in md.lower()
