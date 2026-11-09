"""Small, dependency-free statistics and the hypothetical exit simulation (docs/backtest_preregistration.md section 6)."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

Z95 = 1.959963984540054


def wilson_lower(hits: int, n: int, z: float = Z95) -> Optional[float]:
    if n <= 0:
        return None
    p = hits / n
    d = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - margin) / d


def wilson_upper(hits: int, n: int, z: float = Z95) -> Optional[float]:
    if n <= 0:
        return None
    p = hits / n
    d = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre + margin) / d


@dataclass
class Exit:
    exit_idx: int
    exit_price: float
    reason: str          # stop | breakeven_or_trail_stop | time


def simulate_exit(o, h, l, c, entry_idx: int, last_idx: int, direction: int, atr_val: float,
                  stop_mult: float = 1.5, be_trigger_mult: float = 1.0, trail_mult: float = 2.0) -> tuple[Exit, float]:
    """Walk daily bars entry_idx..last_idx. Returns (Exit, initial_stop_distance).

    Entry at the open of entry_idx. Stop levels are only ever updated AFTER a bar has been
    checked against the previous level, so no intrabar look-ahead. If a bar opens beyond the
    stop, the fill is at the open (gap), not at the stop. If a bar could have hit both the stop
    and a favourable level, the stop counts first (conservative).
    Short trades are handled by mirroring prices so the same long logic applies.
    """
    sgn = 1 if direction > 0 else -1
    if sgn > 0:
        O, H, L, C = o, h, l, c
    else:
        O, H, L, C = [-x for x in o], [-x for x in l], [-x for x in h], [-x for x in c]
    entry = O[entry_idx]
    dist = stop_mult * atr_val
    stop = entry - dist
    best = entry
    be_done = False
    for j in range(entry_idx, last_idx + 1):
        if j > entry_idx and O[j] <= stop:
            return Exit(j, O[j] * sgn, "stop" if not be_done else "breakeven_or_trail_stop"), dist
        if L[j] <= stop:
            return Exit(j, stop * sgn, "stop" if not be_done else "breakeven_or_trail_stop"), dist
        if j == last_idx:
            return Exit(j, C[j] * sgn, "time"), dist
        best = max(best, H[j])
        if not be_done and best - entry >= be_trigger_mult * dist:
            be_done = True
            stop = max(stop, entry)
        if be_done:
            stop = max(stop, best - trail_mult * atr_val)
    raise AssertionError("unreachable")
