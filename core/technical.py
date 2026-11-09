"""Technical layer: where is price relative to widely watched levels?

Input: pandas DataFrame with columns open, high, low, close, volume
and a DatetimeIndex sorted ascending. No LLM. No forecasting.

Reports facts only: levels, location, distances (price and ATR), stop zones,
volume confirmation. It never says buy or sell.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

REQUIRED = ["open", "high", "low", "close", "volume"]


class TechnicalDataError(ValueError):
    pass


def validate_ohlcv(df: pd.DataFrame, min_bars: int = 60) -> None:
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        raise TechnicalDataError(f"missing columns: {missing}")
    if len(df) < min_bars:
        raise TechnicalDataError(f"need at least {min_bars} bars, got {len(df)}")
    if not df.index.is_monotonic_increasing:
        raise TechnicalDataError("index is not sorted ascending")
    sub = df[["open", "high", "low", "close"]]
    if sub.isna().any().any():
        raise TechnicalDataError("NaN in price columns")
    if (df["high"] < df["low"]).any():
        raise TechnicalDataError("high < low on at least one bar")
    if ((df["close"] > df["high"]) | (df["close"] < df["low"])).any():
        raise TechnicalDataError("close outside high/low range on at least one bar")


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's ATR."""
    h, l, c = df["high"], df["low"], df["close"]
    prev_c = c.shift(1)
    tr = pd.concat([(h - l), (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    tr.iloc[0] = (h - l).iloc[0]
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def swing_points(df: pd.DataFrame, left: int = 3, right: int = 3) -> tuple[list[float], list[float]]:
    """Confirmed swing highs/lows. The last `right` bars cannot be confirmed yet."""
    highs, lows = [], []
    h, l = df["high"].to_numpy(), df["low"].to_numpy()
    n = len(df)
    for i in range(left, n - right):
        if h[i] == h[i - left : i + right + 1].max() and h[i] > h[i - left : i].max():
            highs.append(float(h[i]))
        if l[i] == l[i - left : i + right + 1].min() and l[i] < l[i - left : i].min():
            lows.append(float(l[i]))
    return highs, lows


def classic_pivots(prev_high: float, prev_low: float, prev_close: float) -> dict[str, float]:
    p = (prev_high + prev_low + prev_close) / 3
    return {
        "P": p,
        "R1": 2 * p - prev_low,
        "S1": 2 * p - prev_high,
        "R2": p + (prev_high - prev_low),
        "S2": p - (prev_high - prev_low),
    }


def volume_profile(df: pd.DataFrame, bins: int = 40, value_area: float = 0.70) -> Optional[dict]:
    """Approximate volume profile.

    Each bar's volume is spread evenly across the price bins its high-low range touches.
    Returns point of control (POC) and value area (VAL/VAH). Returns None if volume is
    missing or all zero (e.g. some index/spot series).
    """
    if df["volume"].fillna(0).sum() <= 0:
        return None
    lo, hi = float(df["low"].min()), float(df["high"].max())
    if hi <= lo:
        return None
    edges = np.linspace(lo, hi, bins + 1)
    vol = np.zeros(bins)
    for h, l, v in zip(df["high"].to_numpy(), df["low"].to_numpy(), df["volume"].fillna(0).to_numpy()):
        if v <= 0:
            continue
        i0 = min(max(np.searchsorted(edges, l, side="right") - 1, 0), bins - 1)
        i1 = min(max(np.searchsorted(edges, h, side="right") - 1, 0), bins - 1)
        span = i1 - i0 + 1
        vol[i0 : i1 + 1] += v / span
    centers = (edges[:-1] + edges[1:]) / 2
    poc_i = int(vol.argmax())
    total = vol.sum()
    # expand from POC until value_area of volume is covered
    covered = vol[poc_i]
    lo_i = hi_i = poc_i
    while covered < value_area * total and (lo_i > 0 or hi_i < bins - 1):
        down = vol[lo_i - 1] if lo_i > 0 else -1
        up = vol[hi_i + 1] if hi_i < bins - 1 else -1
        if up >= down:
            hi_i += 1
            covered += vol[hi_i]
        else:
            lo_i -= 1
            covered += vol[lo_i]
    return {
        "poc": float(centers[poc_i]),
        "val": float(edges[lo_i]),
        "vah": float(edges[hi_i + 1]),
        "bins": bins,
    }


def anchored_vwap(df: pd.DataFrame, anchor_pos: int) -> Optional[float]:
    sub = df.iloc[anchor_pos:]
    v = sub["volume"].fillna(0)
    if v.sum() <= 0:
        return None
    tp = (sub["high"] + sub["low"] + sub["close"]) / 3
    return float((tp * v).sum() / v.sum())


@dataclass
class Level:
    price: float
    tags: list[str]
    touches: int = 1


@dataclass
class TechView:
    last_close: float
    atr: float
    levels: list[Level]
    support: Optional[Level]
    resistance: Optional[Level]
    support_dist_price: Optional[float]
    resistance_dist_price: Optional[float]
    support_dist_atr: Optional[float]
    resistance_dist_atr: Optional[float]
    location: str                       # FLOOR | MIDDLE | CEILING | UNDEFINED
    range_pos: Optional[float]          # 0 at support, 1 at resistance
    caution: list[str]
    ma: dict[str, Optional[float]]
    profile: Optional[dict]
    volume_note: str
    stop_zone_long: Optional[float]     # informational: where a long stop would sit
    stop_zone_short: Optional[float]
    stop_structure_long: Optional[float]   # nearest swing/support below
    stop_structure_short: Optional[float]  # nearest swing/resistance above
    notes: list[str] = field(default_factory=list)


def _cluster(levels: list[tuple[float, str]], tol: float) -> list[Level]:
    levels = sorted(levels, key=lambda x: x[0])
    out: list[Level] = []
    for price, tag in levels:
        if out and abs(price - out[-1].price) <= tol:
            L = out[-1]
            n = L.touches
            L.price = (L.price * n + price) / (n + 1)
            L.touches = n + 1
            if tag not in L.tags:
                L.tags.append(tag)
        else:
            out.append(Level(price, [tag], 1))
    return out


def analyse(
    df: pd.DataFrame,
    atr_period: int = 14,
    stop_atr_multiple: float = 1.5,
    caution_distance_atr: float = 0.5,
) -> TechView:
    validate_ohlcv(df, min_bars=max(60, atr_period * 3))
    a = atr(df, atr_period)
    a_last = float(a.iloc[-1])
    if not np.isfinite(a_last) or a_last <= 0:
        raise TechnicalDataError("ATR could not be computed")
    last = float(df["close"].iloc[-1])

    raw: list[tuple[float, str]] = []
    sh, sl = swing_points(df)
    raw += [(p, "swing high") for p in sh[-8:]]
    raw += [(p, "swing low") for p in sl[-8:]]

    prev = df.iloc[-2]
    piv = classic_pivots(float(prev["high"]), float(prev["low"]), float(prev["close"]))
    raw += [(v, f"pivot {k}") for k, v in piv.items()]

    ma: dict[str, Optional[float]] = {}
    for n in (20, 50, 200):
        if len(df) >= n:
            ma[f"MA{n}"] = float(df["close"].tail(n).mean())
            raw.append((ma[f"MA{n}"], f"MA{n}"))
        else:
            ma[f"MA{n}"] = None

    prof = volume_profile(df.tail(120))
    if prof:
        raw += [(prof["poc"], "volume POC"), (prof["val"], "value-area low"), (prof["vah"], "value-area high")]

    # anchored VWAP from the most recent confirmed swing extreme (informational)
    if sh or sl:
        lows_idx = df["low"].tail(120).idxmin()
        pos = df.index.get_loc(lows_idx)
        av = anchored_vwap(df, pos)
        if av:
            raw.append((av, "anchored VWAP (from 120-bar low)"))

    tol = 0.25 * a_last
    levels = _cluster(raw, tol)

    below = [L for L in levels if L.price < last - 1e-9]
    above = [L for L in levels if L.price > last + 1e-9]
    support = max(below, key=lambda L: L.price) if below else None
    resistance = min(above, key=lambda L: L.price) if above else None

    sd = (last - support.price) if support else None
    rd = (resistance.price - last) if resistance else None
    sda = sd / a_last if sd is not None else None
    rda = rd / a_last if rd is not None else None

    notes: list[str] = []
    if support and resistance:
        pos = (last - support.price) / (resistance.price - support.price)
        location = "FLOOR" if pos <= 0.25 else "CEILING" if pos >= 0.75 else "MIDDLE"
    else:
        pos, location = None, "UNDEFINED"
        notes.append("No level found on one side of price; location cannot be placed in a range.")

    caution: list[str] = []
    if sda is not None and sda <= caution_distance_atr:
        caution.append(f"Within {sda:.2f} ATR of support {support.price:.2f} ({', '.join(support.tags)})")
    if rda is not None and rda <= caution_distance_atr:
        caution.append(f"Within {rda:.2f} ATR of resistance {resistance.price:.2f} ({', '.join(resistance.tags)})")

    # volume confirmation: last 5 bars vs prior 20, and direction of the 5-bar move
    vol = df["volume"].fillna(0)
    if vol.tail(25).sum() <= 0:
        vnote = "unknown (no volume data)"
    else:
        recent, base = vol.tail(5).mean(), vol.iloc[-25:-5].mean()
        move = df["close"].iloc[-1] - df["close"].iloc[-6]
        direction = "up" if move > 0 else "down" if move < 0 else "flat"
        if base <= 0:
            vnote = "unknown (no baseline volume)"
        else:
            ratio = recent / base
            vnote = (
                f"last 5 bars volume = {ratio:.2f}x prior 20-bar average; 5-bar move is {direction}. "
                + ("Volume is above average." if ratio >= 1.2 else "Volume is below average." if ratio <= 0.8 else "Volume is near average.")
            )

    stop_l = last - stop_atr_multiple * a_last
    stop_s = last + stop_atr_multiple * a_last
    struct_l = support.price - 0.25 * a_last if support else None
    struct_s = resistance.price + 0.25 * a_last if resistance else None

    return TechView(
        last_close=last, atr=a_last, levels=levels,
        support=support, resistance=resistance,
        support_dist_price=sd, resistance_dist_price=rd,
        support_dist_atr=sda, resistance_dist_atr=rda,
        location=location, range_pos=pos, caution=caution, ma=ma, profile=prof,
        volume_note=vnote,
        stop_zone_long=stop_l, stop_zone_short=stop_s,
        stop_structure_long=struct_l, stop_structure_short=struct_s,
        notes=notes,
    )
