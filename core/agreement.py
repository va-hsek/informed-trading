"""Agreement panel: compare the fundamental view with the technical picture.

States facts only. Never produces buy/sell. Labels:
  ALIGNED, CONFLICTING, FUNDAMENTAL_NEUTRAL, DATA_DEGRADED, TECH_UNAVAILABLE
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .signals import AssetView
from .technical import TechView


@dataclass
class Agreement:
    label: str
    conflicts: list[str] = field(default_factory=list)
    supports: list[str] = field(default_factory=list)
    watch: list[str] = field(default_factory=list)
    resolution: list[str] = field(default_factory=list)


def compare(fund: AssetView, tech: Optional[TechView], min_room_atr: float = 1.0) -> Agreement:
    if fund.state == "DEGRADED":
        return Agreement("DATA_DEGRADED", watch=["Key fundamental inputs are stale, missing or unverified."])
    if tech is None:
        return Agreement("TECH_UNAVAILABLE", watch=["Price history was not available or failed validation."])
    if fund.state == "NEUTRAL":
        a = Agreement("FUNDAMENTAL_NEUTRAL")
        a.watch.append("No directional view from fundamentals; technicals below are information only.")
        return a

    up = fund.state == "UP"
    a = Agreement("ALIGNED")
    dir_word = "up" if up else "down"

    # location vs direction
    if tech.location == "UNDEFINED":
        a.watch.append("Price location could not be placed between a support and a resistance.")
    elif up:
        if tech.location == "CEILING":
            a.conflicts.append(
                f"Fundamentals point up but price is near the ceiling: resistance "
                f"{tech.resistance.price:.2f} is only {tech.resistance_dist_atr:.2f} ATR away."
            )
            a.resolution.append(f"A close above {tech.resistance.price:.2f} on rising volume, or a pullback toward support {tech.support.price:.2f}.")
        elif tech.resistance_dist_atr is not None and tech.resistance_dist_atr < min_room_atr:
            a.conflicts.append(
                f"Fundamentals point up but resistance {tech.resistance.price:.2f} is only "
                f"{tech.resistance_dist_atr:.2f} ATR away (room needed: {min_room_atr:g} ATR)."
            )
            a.resolution.append(f"A close above {tech.resistance.price:.2f}.")
        else:
            a.supports.append(f"Price is in the {tech.location.lower()} of its range with {tech.resistance_dist_atr:.2f} ATR to resistance {tech.resistance.price:.2f}.")
    else:
        if tech.location == "FLOOR":
            a.conflicts.append(
                f"Fundamentals point down but price is near the floor: support "
                f"{tech.support.price:.2f} is only {tech.support_dist_atr:.2f} ATR away."
            )
            a.resolution.append(f"A close below {tech.support.price:.2f} on rising volume, or a bounce toward resistance {tech.resistance.price:.2f}.")
        elif tech.support_dist_atr is not None and tech.support_dist_atr < min_room_atr:
            a.conflicts.append(
                f"Fundamentals point down but support {tech.support.price:.2f} is only "
                f"{tech.support_dist_atr:.2f} ATR away (room needed: {min_room_atr:g} ATR)."
            )
            a.resolution.append(f"A close below {tech.support.price:.2f}.")
        else:
            a.supports.append(f"Price is in the {tech.location.lower()} of its range with {tech.support_dist_atr:.2f} ATR to support {tech.support.price:.2f}.")

    # trend vs direction (both MA50 and MA200 against the view)
    ma50, ma200 = tech.ma.get("MA50"), tech.ma.get("MA200")
    px = tech.last_close
    if ma50 is not None and ma200 is not None:
        against = (px < ma50 and px < ma200) if up else (px > ma50 and px > ma200)
        with_ = (px > ma50 and px > ma200) if up else (px < ma50 and px < ma200)
        if against:
            a.conflicts.append(
                f"Fundamentals point {dir_word} but price is {'below' if up else 'above'} both the 50-bar ({ma50:.2f}) and 200-bar ({ma200:.2f}) averages."
            )
            a.resolution.append(f"Price moving back {'above' if up else 'below'} the 50-bar average ({ma50:.2f}).")
        elif with_:
            a.supports.append(f"Price is {'above' if up else 'below'} both the 50-bar and 200-bar averages, in line with the fundamental view.")
        else:
            a.watch.append("Price is between its 50-bar and 200-bar averages: trend is mixed.")
    else:
        a.watch.append("Not enough history for a 200-bar average; trend check is partial.")

    # volume
    a.watch.append(f"Volume: {tech.volume_note}")
    a.watch += tech.caution

    if a.conflicts:
        a.label = "CONFLICTING"
    return a
