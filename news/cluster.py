"""Groups verified stories into storylines (one lead story plus related coverage), by jurisdiction and by sector. Pure functions."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from news.claims import norm
from news.paper import REGION_SECTION, Story

REGION_ORDER = ["India", "USA and Americas", "Europe", "Asia-Pacific", "Middle East", "Global markets and macro"]
COMMODITY_ORDER = [("crude", "Crude oil"), ("natgas", "Natural gas"), ("gold", "Gold"), ("silver", "Silver")]
_STOP = set("this that with from have been will were says said after over into amid more than their about would could which while other "
            "first year week also what when where news today stocks shares market markets price prices india indian".split())


def _toks(s: str) -> set[str]:
    out = set()
    for w in re.findall(r"[a-z][a-z0-9]{3,}", norm(s)):
        if w in _STOP:
            continue
        out.add(w[:-1] if w.endswith("s") and len(w) > 4 else w)
    return out


def head_tokens(st: Story) -> set[str]:
    return _toks(" ".join([st.headline, *st.aka]))


def story_tokens(st: Story) -> set[str]:
    first = st.paras[0].claims[0] if st.paras and st.paras[0].claims else ""
    return _toks(" ".join([st.headline, *st.aka, first]))


@dataclass
class Cluster:
    lead: Story
    related: list[Story] = field(default_factory=list)
    tokens: set = field(default_factory=set)
    heads: set = field(default_factory=set)

    @property
    def score(self) -> float:
        return self.lead.score + 0.5 * sum(r.score for r in self.related)

    @property
    def stories(self) -> list[Story]:
        return [self.lead, *self.related]


def _similar(a: set, b: set, ha: set, hb: set) -> bool:
    """Same storyline: headlines share at least two distinctive words (40% of the shorter headline), or the stories share three or more."""
    hi = ha & hb
    if len(hi) >= 2 and len(hi) / max(1, min(len(ha), len(hb))) >= 0.4:
        return True
    inter = a & b
    return len(inter) >= 4 and len(inter) / max(1, min(len(a), len(b))) >= 0.4


def cluster(stories: list[Story], max_related: int = 5) -> list[Cluster]:
    out: list[Cluster] = []
    for st in sorted(stories, key=lambda s: -s.score):
        t, h = story_tokens(st), head_tokens(st)
        for c in out:
            if len(c.related) < max_related and _similar(c.tokens, t, c.heads, h):
                c.related.append(st)
                c.tokens |= t
                c.heads |= h
                break
        else:
            out.append(Cluster(st, [], set(t), set(h)))
    out.sort(key=lambda c: -c.score)
    return out


def region_of(st: Story) -> str:
    if st.section in REGION_SECTION.values():
        return st.section
    for r in st.regions:
        if r in REGION_SECTION:
            return REGION_SECTION[r]
    return "Global markets and macro"


def is_commodity(st: Story) -> bool:
    return bool(set(st.topics) & {"crude", "natgas", "gold", "silver"})


def by_region(stories: list[Story]) -> list[tuple[str, list[Cluster]]]:
    buckets: dict[str, list[Story]] = {r: [] for r in REGION_ORDER}
    for st in stories:
        buckets[region_of(st)].append(st)
    return [(r, cluster(buckets[r])) for r in REGION_ORDER if buckets[r]]


def by_sector(stories: list[Story], sector_order: list[str]) -> list[tuple[str, list[Cluster]]]:
    buckets: dict[str, list[Story]] = {s: [] for s in sector_order}
    for st in stories:
        if st.sectors and st.sectors[0] in buckets:
            buckets[st.sectors[0]].append(st)
    res = [(s, cluster(buckets[s])) for s in sector_order if buckets[s]]
    res.sort(key=lambda x: -sum(c.score for c in x[1]))
    return res


def by_commodity(stories: list[Story]) -> list[tuple[str, list[Cluster]]]:
    out = []
    for key, label in COMMODITY_ORDER:
        grp = [s for s in stories if next((k for k, _ in COMMODITY_ORDER if k in s.topics), None) == key]
        if grp:
            out.append((label, cluster(grp)))
    return out
