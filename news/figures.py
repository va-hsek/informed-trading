"""'Figures differ': when two DIFFERENT outlets give a different number for what looks like the same measure, say so instead of picking one.
Deterministic. It compares only numbers that carry a unit (%, $, bps, billion...), of the same kind, whose words just before the number overlap
(at least two content words), and that differ by more than 2%. It only ever flags; it never decides which is right, and it can be wrong
about "same measure" (different dates, different bases), so the wording is 'check before relying'.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

NUM = re.compile(r"(?P<cur>US\$|[$₹€£]|Rs\.?\s?)?(?P<n>\d[\d,]*(?:\.\d+)?)\s?(?P<unit>%|percent|per\s?cent|pc|bps|basis points|trillion|billion|million|bn|mn|crore|lakh|a barrel|per barrel|barrels|bpd)?", re.I)
STOP = {"that", "this", "with", "from", "after", "have", "will", "said", "says", "over", "into", "amid", "than", "their", "about", "were", "been", "also", "year", "month",
        "week", "rose", "fell", "down", "higher", "lower", "to", "and", "the", "for", "its", "was", "has", "had", "are", "per", "by", "at"}
KIND = {"%": "pct", "percent": "pct", "per cent": "pct", "pc": "pct", "bps": "bps", "basis points": "bps",
        "trillion": "big", "billion": "big", "bn": "big", "million": "big", "mn": "big", "crore": "in", "lakh": "in", "a barrel": "bbl", "per barrel": "bbl", "barrels": "vol", "bpd": "vol"}
MULT = {"trillion": 1e12, "billion": 1e9, "bn": 1e9, "million": 1e6, "mn": 1e6, "crore": 1e7, "lakh": 1e5}


@dataclass
class Fig:
    value: float
    shown: str
    kind: str
    anchors: frozenset
    source: str
    claim: str


def figures(claim: str, source: str) -> list[Fig]:
    out = []
    for m in NUM.finditer(claim):
        unit = re.sub(r"\s+", " ", (m.group("unit") or "").lower()).strip()
        cur = m.group("cur")
        if not unit and not cur:
            continue                                   # a bare number carries no unit, so it is never compared
        kind = KIND.get(unit, "")
        if cur and kind in ("", "big", "in"):
            kind = "cur" + kind                        # currency amounts compare only with currency amounts of the same scale
        if not kind:
            continue
        try:
            v = float(m.group("n").replace(",", "")) * MULT.get(unit, 1)
        except ValueError:
            continue
        before = re.findall(r"[a-z]{3,}", claim[: m.start()].lower())[-5:]
        out.append(Fig(v, m.group(0).strip(), kind, frozenset(w for w in before if w not in STOP), source, claim))
    return out


def disagreements(pairs: list[tuple[str, str]], limit: int = 3) -> list[tuple[Fig, Fig]]:
    """pairs = [(outlet, claim)]. Returns up to `limit` (figure A, figure B) from different outlets."""
    figs = [f for o, c in pairs for f in figures(c, o)]
    out, seen = [], set()
    for i, a in enumerate(figs):
        for b in figs[i + 1:]:
            if a.source == b.source or a.kind != b.kind or len(a.anchors & b.anchors) < 2:
                continue
            hi = max(abs(a.value), abs(b.value))
            if hi == 0 or abs(a.value - b.value) / hi <= 0.02:
                continue
            key = (a.kind, frozenset((round(a.value, 4), round(b.value, 4))))        # one flag per pair of values, however many outlets repeat them
            if key in seen:
                continue
            seen.add(key)
            out.append((a, b))
            if len(out) >= limit:
                return out
    return out
