"""What a storyline is ABOUT decides whether it earns the top of the page. Deterministic, no model, nothing guessed.

score = strongest subject weight (+ a little for a second subject)
        + points for being on the reader's watchlist (the instruments they trade)
        + points for each extra outlet carrying it (capped), and for official confirmation
        - penalties for things that look like news but are not (retail price lists, 'stocks to buy' tips, horoscopes, live blogs)

The subject must be in the headline; a subject found only in the story text adds a small amount and never sets the topic.
Every storyline also gets `why`, the short reasons shown on the page, so the ranking can always be checked.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class Rating:
    score: float = 0.0
    key: str = ""                 # subject key; the top section shows at most `per_theme_cap` per key
    label: str = ""
    why: list[str] = field(default_factory=list)
    watch: list[str] = field(default_factory=list)


def _rx(words: list[str]):
    parts = [r"\b" + re.escape(w.strip().lower()).replace(r"\ ", r"\s+") + r"\b" for w in words if w and w.strip()]
    return re.compile("|".join(parts), re.I) if parts else None


class Importance:
    def __init__(self, cfg: dict | None):
        c = cfg or {}
        self.min_score = float(c.get("min_score", 9))
        self.per_theme_cap = int(c.get("per_theme_cap", 2))
        self.outlet_points = float(c.get("outlet_points", 1.0))
        self.outlet_max = float(c.get("outlet_max", 4.0))
        self.official_points = float(c.get("official_points", 3.0))
        self.watch_points = float(c.get("watch_points", 1.5))
        self.watch_max = float(c.get("watch_max", 3.0))
        self.themes = [(t["key"], t.get("label", t["key"]), float(t.get("weight", 1)), _rx(t.get("words", []))) for t in c.get("themes", []) if t.get("key")]
        self.penalties = [(p.get("label", "penalty"), float(p.get("points", -5)), _rx(p.get("words", []))) for p in c.get("penalties", [])]
        self.watch = [(w, _rx([w])) for w in c.get("watchlist", [])]

    def rate(self, headlines: list[str], body: str, outlets: int, official: bool) -> Rating:
        head = " . ".join(headlines)
        in_head, in_body = [], []                                          # (weight, key, label)
        for key, label, w, rx in self.themes:
            if rx is None:
                continue
            if rx.search(head):
                in_head.append((w, key, label))
            elif rx.search(body):
                in_body.append((w, key, label))
        in_head.sort(reverse=True)
        in_body.sort(reverse=True)
        r = Rating()
        if in_head:                                                         # the subject is what the headline says; body words only add a little
            r.score += in_head[0][0]
            r.key, r.label = in_head[0][1], in_head[0][2]
            r.why.append(in_head[0][2])
            rest = in_head[1:2] + in_body[:1]
            if rest:
                r.score += 0.3 * max(x[0] for x in rest)
        else:
            r.score += 1.0 + (0.25 * in_body[0][0] if in_body else 0)       # not clearly about a tracked subject: it can still be read below
        hits = [w for w, rx in self.watch if rx is not None and rx.search(head)]
        if hits:
            r.watch = hits[:3]
            r.score += min(self.watch_max, self.watch_points * len(hits))
            r.why.append("on your list: " + ", ".join(hits[:3]))
        extra = max(0, outlets - 1)
        if extra:
            r.score += min(self.outlet_max, self.outlet_points * extra)
            r.why.append(f"{outlets} outlets")
        if official:
            r.score += self.official_points
            r.why.append("official source")
        for label, pts, rx in self.penalties:
            if rx is not None and rx.search(head):
                r.score += pts
                r.why.append(f"-{label}")
        r.score = round(r.score, 2)
        return r


def rate_cluster(imp: Importance, c) -> Rating:
    lead = c.lead                                      # the subject is judged on the lead story (and the headlines merged into it), not on loosely related storylines
    heads = [lead.headline] + list(lead.aka)
    body = " ".join(cl for p in lead.paras[:2] for cl in p.claims[:2])
    names = {x.name for st in c.stories for x in st.sources}
    official = any(st.category == "official" for st in c.stories)
    return imp.rate(heads, body, len(names), official)


def pick_top(clusters: list, imp: Importance, floor: int = 3, cap: int = 15) -> list[tuple]:
    """[(cluster, rating)] for Today in X: best first, at most `per_theme_cap` per subject, never more than `cap`;
    only those at or above `min_score`, but always at least `floor` (the strongest, same theme limit) so a quiet day still has a front."""
    rated = sorted(((c, rate_cluster(imp, c)) for c in clusters), key=lambda x: -x[1].score)
    used: dict[str, int] = {}
    out, rest = [], []
    for c, r in rated:
        k = r.key or f"_{id(c)}"
        if used.get(k, 0) >= imp.per_theme_cap:
            continue
        if r.score >= imp.min_score and len(out) < cap:
            out.append((c, r))
            used[k] = used.get(k, 0) + 1
        else:
            rest.append((c, r))
    for c, r in rest:
        if len(out) >= floor:
            break
        k = r.key or f"_{id(c)}"
        if used.get(k, 0) < imp.per_theme_cap:
            out.append((c, r))
            used[k] = used.get(k, 0) + 1
    return out
