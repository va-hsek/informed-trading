"""Price storage, contract stitching and candle normalisation.

Futures history is stored PER CONTRACT (data/prices/raw/<instrument>/<contract>.csv) and
stitched into one continuous series at read time, so history accumulates run after run
even if the broker only serves live contracts. If you have a longer continuous history
from elsewhere, drop it in data/prices/continuous/<instrument>.csv (it takes precedence).

CSV columns: date,open,high,low,close,volume
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

COLS = ["open", "high", "low", "close", "volume"]


class PriceDataError(ValueError):
    pass


def normalize_candles(raw) -> pd.DataFrame:
    """Turn a broker candle response into a daily OHLCV frame. Refuses unknown shapes."""
    rows = raw
    if isinstance(raw, dict):
        for k in ("result", "data", "candles", "Result"):
            if isinstance(raw.get(k), list):
                rows = raw[k]
                break
        else:
            raise PriceDataError(f"candle response is a dict without a candle list; keys: {sorted(raw)[:10]}")
    if not isinstance(rows, list) or not rows:
        raise PriceDataError("candle response is empty")
    out = []
    for r in rows:
        if not isinstance(r, dict):
            raise PriceDataError(f"candle row is not a dict: {type(r).__name__}")
        low = {str(k).lower(): v for k, v in r.items()}
        tkey = next((k for k in ("datetime", "time", "date", "timestamp") if k in low), None)
        if tkey is None or any(k not in low for k in ("open", "high", "low", "close")):
            raise PriceDataError(f"candle row missing fields; keys: {sorted(low)[:10]}")
        t = low[tkey]
        try:
            if isinstance(t, (int, float)) or (isinstance(t, str) and t.isdigit()):
                x = float(t)
                x = x / 1000 if x > 1e11 else x
                d = datetime.fromtimestamp(x, tz=timezone.utc).date()
            else:
                d = pd.to_datetime(str(t)).date()
            out.append({"date": d, **{c: float(low.get(c, 0) or 0) for c in COLS}})
        except Exception as e:  # noqa: BLE001
            raise PriceDataError(f"unparseable candle {r!r}: {e}") from e
    df = pd.DataFrame(out).groupby("date").agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"), volume=("volume", "sum")
    )
    df.index = pd.to_datetime(df.index)
    return df.sort_index()


def read_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]
    if "date" not in df.columns or any(c not in df.columns for c in ("open", "high", "low", "close")):
        raise PriceDataError(f"{path}: needs columns date,open,high,low,close[,volume]")
    if "volume" not in df.columns:
        df["volume"] = 0.0
    df["date"] = pd.to_datetime(df["date"])
    df = df.drop_duplicates("date", keep="last").set_index("date").sort_index()
    return df[COLS].astype(float)


def write_csv(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    out = df.copy()
    out.index.name = "date"
    out.to_csv(path)


def stitch(contracts: list[tuple[str, date, pd.DataFrame]], roll_days: int = 5) -> pd.DataFrame:
    """Back-adjusted continuous series (difference method).

    Each contract is used until `roll_days` before its expiry. At each roll the older
    contracts are shifted by the price gap between the old and new contract on the last
    common date on or before the roll date, so the stitched series has no artificial jump.
    The most recent contract is never adjusted: its prices are the real tradable prices.
    """
    if not contracts:
        raise PriceDataError("no contracts to stitch")
    cs = sorted(contracts, key=lambda c: c[1])
    segs, adjust = [], []
    prev_roll: Optional[pd.Timestamp] = None
    for i, (label, expiry, df) in enumerate(cs):
        last = i == len(cs) - 1
        roll = pd.Timestamp(expiry - timedelta(days=roll_days))
        seg = df[(df.index > prev_roll) if prev_roll is not None else slice(None)]
        if not last:
            seg = seg[seg.index <= roll]
        if len(seg):
            segs.append((i, seg))
        if not last:
            nxt = cs[i + 1][2]
            common = df.index.intersection(nxt.index)
            common = common[common <= roll]
            if len(common) == 0:
                raise PriceDataError(f"contracts {label} and {cs[i + 1][0]} share no date on or before the roll date {roll.date()}")
            d = common.max()
            adjust.append(float(nxt.loc[d, "close"] - df.loc[d, "close"]))
        prev_roll = roll
    # cumulative adjustment for segment i = sum of gaps from i onwards
    total = [0.0] * len(cs)
    run = 0.0
    for i in range(len(cs) - 2, -1, -1):
        run += adjust[i]
        total[i] = run
    parts = []
    for i, seg in segs:
        s = seg.copy()
        for c in ("open", "high", "low", "close"):
            s[c] = s[c] + total[i]
        parts.append(s)
    out = pd.concat(parts).sort_index()
    return out[~out.index.duplicated(keep="last")]


class PriceStore:
    def __init__(self, root: Path | str):
        self.root = Path(root)

    def _raw(self, inst: str) -> Path:
        return self.root / "raw" / inst

    def save_contract(self, inst: str, label: str, expiry: date, df: pd.DataFrame) -> None:
        p = self._raw(inst) / f"{label}__{expiry.isoformat()}.csv"
        if p.exists():
            old = read_csv(p)
            df = pd.concat([old, df]).sort_index()
            df = df[~df.index.duplicated(keep="last")]
        write_csv(df, p)

    def load_contracts(self, inst: str) -> list[tuple[str, date, pd.DataFrame]]:
        out = []
        d = self._raw(inst)
        if d.exists():
            for p in sorted(d.glob("*.csv")):
                label, _, exp = p.stem.rpartition("__")
                out.append((label, date.fromisoformat(exp), read_csv(p)))
        return out

    def continuous(self, inst: str, roll_days: int = 5) -> pd.DataFrame:
        override = self.root / "continuous" / f"{inst}.csv"
        if override.exists():
            return read_csv(override)
        return stitch(self.load_contracts(inst), roll_days)
