"""The Morning Edition as a phone-first magazine page. Everything starts as a one-line headline you can open or fold: sections, subsections,
storylines and single stories. Order: today in X (as many as deserve the top), upcoming events, event updates, energy and metals, news by jurisdiction, news by sector,
then what was missed and the coverage panel. One self-contained HTML file (a few lines of script open and fold sections). Pure rendering: it never fetches anything and never adds text of its own
to a story, so every sentence on the page is a verified claim followed by its source link."""
from __future__ import annotations

import html
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from news.cluster import Cluster, by_commodity, by_region, by_sector, cluster, is_commodity
from news.figures import disagreements, figures
from news.glossary import terms_in
from news.headline_search import sig_words
from news.importance import Importance, Rating, pick_top
from news.paper import IST, Source, Story

esc = html.escape

ACCENT = {"India": "#d9480f", "USA and Americas": "#1c4e9d", "Europe": "#5b3cc4", "Asia-Pacific": "#0b7285", "Middle East": "#a61e4d",
          "Global markets and macro": "#495057", "Crude oil": "#7a4a00", "Natural gas": "#0b7a75", "Gold": "#b8860b", "Silver": "#6c757d"}
SECTOR_HUES = [12, 205, 150, 280, 35, 340, 175, 60, 250, 100, 320, 190, 20]


def _accent_for(name: str, idx: int = 0) -> str:
    return ACCENT.get(name) or f"hsl({SECTOR_HUES[idx % len(SECTOR_HUES)]} 62% 40%)"


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


# ---------------------------------------------------------------- source links
def src_html(src: Source) -> str:
    note = ""
    if src.via == "archive.today":
        note = "read from an archive.today copy"
    elif src.via == "archive":
        note = "read from a web-archive copy"
    elif src.via == "wayback":
        note = f"Wayback copy of {src.snap}"
    elif src.via == "partial":
        note = "only part of the article was readable"
    elif src.via == "feed summary":
        note = "feed summary only"
    elif src.via == "headline search":
        note = f"{src.orig or 'the original'} could not be read; same story found by headline search"
    out = f'<a class="src" href="{esc(src.url, quote=True)}" target="_blank" rel="noopener">{esc(src.name)} ↗</a>'
    if note:
        out += f' <span class="note">({esc(note)})</span>'
    if src.archive:
        out += f' <a class="note" href="{esc(src.archive, quote=True)}" target="_blank" rel="noopener">archive</a>'
    return out


def _sentence(c: str) -> str:
    c = c.strip()
    return c if c.endswith((".", "!", "?")) else c + "."


def paras_html(st: Story, per_para: int = 3, first_only: bool = False) -> str:
    """Paragraphs of verified claims, each ending with its source. Outlets that only corroborate are listed in one line."""
    out, corro, n = [], [], 0
    for p in st.paras:
        if not p.claims:
            corro.append(p.source)
            continue
        chunks = [p.claims[i: i + per_para] for i in range(0, len(p.claims), per_para)]
        for k, ch in enumerate(chunks):
            cite = f' <span class="cite">{src_html(p.source)}</span>' if k == len(chunks) - 1 else ""
            out.append(f"<p>{esc(' '.join(_sentence(c) for c in ch))}{cite}</p>")
            n += 1
            if first_only and n >= 1:
                break
        if first_only and n >= 1:
            break
    html_ = "".join(out)
    if corro and not first_only:
        html_ += '<p class="also">Also reported by ' + ", ".join(src_html(x) for x in corro) + ".</p>"
    return html_



def _when(st: Story) -> str:
    return st.published.astimezone(IST).strftime("%d %b, %H:%M IST") if st.published else ""


def _meta(st: Story, show_region: bool = True, extra: str = "") -> str:
    from news.cluster import region_of
    bits = []
    if show_region:
        bits.append(esc(region_of(st)))
    if st.sectors:
        bits.append(esc(st.sectors[0]))
    n = len(st.sources)
    if n > 1:
        bits.append(f"<b>{n} outlets</b>")
    t = _when(st)
    if t:
        bits.append(esc(t.split(", ")[-1].replace(" IST", "")))
    return " · ".join(bits) + extra


def _by(st: Story) -> str:
    names = ", ".join(esc(x.name) for x in st.sources[:4])
    return names + (f" and {len(st.sources) - 4} more" if len(st.sources) > 4 else "")


# ---------------------------------------------------------------- foldable units
def _terms(st: Story) -> str:
    """A short 'Terms' note: plain-English meanings of jargon this story uses (fixed dictionary, no model)."""
    text = st.headline + " " + " ".join(cl for p in st.paras for cl in p.claims)
    found = terms_in(text)
    if not found:
        return ""
    return '<p class="terms"><b>Terms:</b> ' + " ".join(f"<span><i>{esc(t)}</i>: {esc(d)}</span>" for t, d in found) + "</p>"


def story_details(st: Story, sid: str = "", show_region: bool = True, open_: bool = False, related: Optional[list] = None, why: str = "", flag: str = "") -> str:
    """One story: the headline is the whole closed state. Opened, it shows the verified paragraphs and, for a storyline, the related stories (each foldable)."""
    badge = f' <span class="badge">+{len(related)} related</span>' if related else ""
    rel = ""
    if related:
        rel = '<div class="related"><div class="related-h">Related reporting</div>' + "".join(story_details(r, show_region=False) for r in related) + "</div>"
    id_attr = f' id="{esc(sid, quote=True)}"' if sid else ""
    return (f'<details class="st{" line" if related else ""}"{id_attr}{" open" if open_ else ""}>'
            f'<summary><span class="ht">{esc(st.headline)}</span><span class="meta">{_meta(st, show_region)}{badge}</span>{f"<span class=why>Why here: {esc(why)}</span>" if why else ""}</summary>'
            f'<div class="body"><div class="by">{_by(st)}</div>{flag}{paras_html(st)}{_terms(st)}{rel}</div></details>')


def figures_flag(c: Cluster) -> str:
    """A small box when two different outlets give different figures for what looks like the same measure. Never says which is right."""
    pairs = [(p.source.name, cl) for st in c.stories for p in st.paras for cl in p.claims]
    diffs = disagreements(pairs)
    if not diffs:
        return ""
    rows = "".join(f'<li><b>{esc(a.source)}</b>: {esc(a.shown)} <span class="vs">vs</span> <b>{esc(b.source)}</b>: {esc(b.shown)}</li>' for a, b in diffs)
    return f'<div class="figdiff"><b>Figures differ.</b> Outlets give different numbers for what looks like the same measure (it may be a different time or basis). Check before relying on either.<ul>{rows}</ul></div>'


def cluster_details(c: Cluster, uid: str = "", show_region: bool = True, why: str = "") -> str:
    return story_details(c.lead, uid, show_region, related=c.related or None, why=why, flag=figures_flag(c))


def fold(kind: str, fid: str, title: str, inner: str, count: str = "", accent: str = "", kicker: str = "", open_: bool = False, summary: str = "") -> str:
    """kind: 'block' (main section) or 'sub' (subsection). Closed it is a headline plus, when it holds several parts, one small line saying what is inside."""
    style = f' style="--accent:{accent}"' if accent else ""
    return (f'<details class="{kind}" id="{fid}"{style}{" open" if open_ else ""}><summary>'
            f'{f"<span class=kick>{esc(kicker)}</span>" if kicker else ""}<span class="ft">{esc(title)}</span>'
            f'{f"<span class=count>{esc(count)}</span>" if count else ""}{f"<span class=sum>{esc(summary)}</span>" if summary else ""}</summary><div class="inner">{inner}</div></details>')


def _n(clusters: list[Cluster]) -> int:
    return sum(len(c.stories) for c in clusters)


def _short(t: str, n: int = 58) -> str:
    t = re.sub(r"\s+", " ", t).strip()
    return t if len(t) <= n else t[: n - 1].rsplit(" ", 1)[0].rstrip(",;:-") + "…"


def names_summary(pairs: list[tuple[str, int]], limit: int = 7) -> str:
    """'Crude oil 7 · Gold 4 · ...' for a section made of named parts."""
    if len(pairs) < 2:
        return ""
    shown = [f"{n} {c}" for n, c in pairs[:limit]]
    return " · ".join(shown) + (f" · +{len(pairs) - limit} more" if len(pairs) > limit else "")


def leads_summary(clusters: list, limit: int = 3) -> str:
    """For a part made of several storylines: its leading headlines, shortened."""
    if len(clusters) < 2:
        return ""
    heads = [_short(c.lead.headline) for c in clusters[:limit]]
    return "Includes: " + " · ".join(heads) + (f" · +{len(clusters) - limit} more" if len(clusters) > limit else "")


def watch_groups(clusters: list[Cluster], imp: Importance) -> list[tuple[str, list[Cluster]]]:
    """Storylines whose headline mentions something on the reader's watchlist (config importance.watchlist), grouped by that word, in the config's order.
    A storyline goes under the first watchlist word it mentions."""
    groups: dict[str, list[Cluster]] = {w: [] for w, _ in imp.watch}
    for c in clusters:
        head = " . ".join([c.lead.headline] + list(c.lead.aka))
        for w, rx in imp.watch:
            if rx is not None and rx.search(head):
                groups[w].append(c)
                break
    return [(w.title() if w.islower() else w, cl) for w, cl in groups.items() if cl]


def groups_html(prefix: str, groups: list[tuple[str, list[Cluster]]], hue_by_index: bool = False) -> str:
    out = []
    for i, (name, cl) in enumerate(groups):
        inner = "".join(cluster_details(c, f"{prefix}-{_slug(name)}-{j}", show_region=(prefix != "r")) for j, c in enumerate(cl))
        out.append(fold("sub", f"{prefix}-{_slug(name)}", name, inner, f"{_n(cl)}", _accent_for(name, i if hue_by_index else 0), summary=leads_summary(cl)))
    return "".join(out)


# ---------------------------------------------------------------- markets, events, updates, missed, coverage
def load_glance(path: Path, now: datetime, max_age_days: int = 3) -> tuple[list[dict], str]:
    if not path.exists():
        return [], "Market figures are not shown: the action report has not written its dashboard yet."
    try:
        data = json.loads(path.read_text())
        gen = datetime.fromisoformat(data["generated"])
        rows = [r for r in data["rows"] if r.get("value") is not None]
    except Exception as e:  # noqa: BLE001
        return [], f"Market figures are not shown: dashboard file unreadable ({type(e).__name__})."
    age = (now - gen).total_seconds() / 86400
    if age > max_age_days:
        return [], f"Market figures are not shown: the dashboard is {age:.1f} days old (limit {max_age_days})."
    return rows, f"Dashboard generated {gen.astimezone(IST):%d %b %H:%M IST}; each tile shows its own as-of date."


def markets_html(rows: list[dict], note: str) -> str:
    if not rows:
        return ""                                       # no dashboard (it is written by the action report on your own computer): show nothing
    tiles = []
    for r in rows:
        chg = r.get("chg_text") or ""
        cls = "up" if chg.strip().startswith("+") else "down" if chg.strip().startswith(("-", "−")) else ""
        tiles.append(f'<div class="tile"><div class="tl">{esc(str(r["label"]))}</div><div class="tv">{esc(str(r["value_text"]))}</div>'
                     f'<div class="tc {cls}">{esc(chg) if chg else "&nbsp;"}</div><div class="ta">{esc(str(r["as_of"]))} · {esc(str(r["source"]))}</div></div>')
    return f'<section class="strip" id="markets"><div class="tiles">{"".join(tiles)}</div><p class="stripnote">{esc(note)}</p></section>'



def event_rows(cal: dict, now: datetime, horizon_days: int, max_age_days: int) -> tuple[str, int, str]:
    today = now.astimezone(IST).date()
    warn = ""
    try:
        reviewed = date.fromisoformat(str(cal.get("last_reviewed")))
        if (today - reviewed).days > max_age_days:
            warn = f"Calendar last reviewed {reviewed:%d %b %Y} ({(today - reviewed).days} days ago): events may be missing or changed."
    except Exception:  # noqa: BLE001
        warn = "Calendar has no review date: treat it as incomplete."
    days: dict[date, list[dict]] = {}
    for e in cal.get("events") or []:
        try:
            d = date.fromisoformat(str(e["date"]))
        except Exception:  # noqa: BLE001
            continue
        if today <= d <= today + timedelta(days=horizon_days):
            days.setdefault(d, []).append(e)
    if not days:
        return '<p class="intro">No events in the calendar for this period.</p>', 0, ""
    out, total, per_day = [], 0, []
    for d in sorted(days):
        rows = []
        for e in sorted(days[d], key=lambda x: str(x.get("time_ist", ""))):
            total += 1
            src = (f'<a class="src" href="{esc(e["source_url"], quote=True)}" target="_blank" rel="noopener">{esc(e["source_name"])} ↗</a>'
                   if e.get("source_url") and e.get("source_name") else "")
            aff = " · ".join(esc(a) for a in (e.get("affects") or []))
            unv = ' <span class="badge warn">unverified</span>' if not e.get("verified", True) else ""
            rows.append(f'<details class="st ev"><summary><span class="ht"><span class="et">{esc(str(e.get("time_ist") or "all day"))}</span> {esc(e["title"])}</span>'
                        f'<span class="meta">{aff}{unv}</span></summary><div class="body"><p>{esc(str(e.get("expectation") or "No expectation recorded."))}'
                        f'{f" <span class=cite>{src}</span>" if src else ""}</p></div></details>')
        tag = "Today" if d == today else "Tomorrow" if d == today + timedelta(days=1) else d.strftime("%A")
        per_day.append((tag, len(rows)))
        titles = " · ".join(_short(e["title"], 34) for e in sorted(days[d], key=lambda x: str(x.get("time_ist", "")))[:3])
        out.append(fold("sub", f"ev-{d:%m%d}", f"{tag} · {d:%d %b}", "".join(rows), str(len(rows)), "#495057", open_=(d == today),
                        summary=(f"Includes: {titles}" + (f" · +{len(rows) - 3} more" if len(rows) > 3 else "")) if len(rows) > 1 else ""))
    return (f'<p class="warnline">{esc(warn)}</p>' if warn else "") + "".join(out), total, names_summary(per_day, 8)


def _acro(title: str) -> set[str]:
    return {w.lower() for w in re.findall(r"\b[A-Z]{3,6}\b", title)}


def event_updates(stories: list[Story], cal: dict, now: datetime) -> tuple[list[tuple[str, list[Story]]], set]:
    """Official releases, plus stories that match a calendar event dated within the last 3 days or today. Matching needs a shared acronym
    (CPI, RBI, OPEC ...) or two shared distinctive words; nothing is inferred beyond that."""
    from news.cluster import _toks
    today = now.astimezone(IST).date()
    groups: list[tuple[str, list[Story]]] = []
    used: set[int] = set()
    off = [s for s in stories if s.section == "Official releases"]
    if off:
        groups.append(("Official releases", off))
        used |= {id(s) for s in off}
    for e in cal.get("events") or []:
        try:
            d = date.fromisoformat(str(e["date"]))
        except Exception:  # noqa: BLE001
            continue
        if not (today - timedelta(days=3) <= d <= today):
            continue
        ac, tk = _acro(e["title"]), _toks(e["title"])
        hit = []
        for s in stories:
            if id(s) in used:
                continue
            blob = s.headline + " " + " ".join(c for p in s.paras for c in p.claims[:2])
            if (ac & {w.lower() for w in re.findall(r"\b[A-Za-z]{3,6}\b", s.headline)}) or len(tk & _toks(blob)) >= 2:
                hit.append(s)
        if hit:
            groups.append((f"{e['title']} · {d:%d %b}", hit))
            used |= {id(s) for s in hit}
    return groups, used


def headlines_html(unread: list, archive_fn, limit: int) -> str:
    from news.cluster import REGION_ORDER
    from news.paper import REGION_SECTION, item_score
    groups: dict[str, list] = {r: [] for r in REGION_ORDER}
    shown = 0
    for it, _why in sorted(unread, key=lambda x: (-item_score(x[0]), -(x[0].published.timestamp() if x[0].published else 0)))[:limit]:
        r = REGION_SECTION.get(it.feed_region or (it.regions[0] if it.regions else ""), "Global markets and macro")
        groups[r].append(it)
        shown += 1
    subs = []
    for r, lst in groups.items():
        if not lst:
            continue
        li = []
        for it in lst:
            arch = f' · <a class="note" href="{esc(archive_fn(it), quote=True)}" target="_blank" rel="noopener">archive</a>' if archive_fn(it) else ""
            li.append(f'<li><a href="{esc(it.link, quote=True)}" target="_blank" rel="noopener">{esc(it.title)}</a> <span class="note">{esc(it.publisher or it.source)}</span>{arch}</li>')
        subs.append(fold("sub", f"h-{_slug(r)}", r, f'<ul class="hl">{"".join(li)}</ul>', str(len(lst)), "#868e96",
                         summary="Includes: " + " · ".join(_short(it.title, 50) for it in lst[:2]) + (f" · +{len(lst) - 2} more" if len(lst) > 2 else "") if len(lst) > 1 else ""))
    if not subs:
        return ""
    intro = '<p class="intro">For these the article text could not be read even after archives and a search, so nothing is summarised. Tap a region to open its list.</p>'
    return fold("block", "headlines", "What we missed: headlines we could not read", intro + "".join(subs), str(shown), "#868e96", "Not read",
                summary=names_summary([(r, len(l)) for r, l in groups.items() if l]))


def coverage_html(stats: dict, coverage: list, reasons: list, llm, llm_error: str, inr: float = 0.0) -> str:
    rows = "".join(f'<tr><td>{esc(c["feed"])}</td><td class="{"ok" if c["ok"] else "bad"}">{"ok" if c["ok"] else "FAILED: " + esc(c["reason"][:90])}{(" · " + esc(c["reason"][:110])) if c["ok"] and c["reason"] else ""}</td><td>{c["kept"]}</td></tr>' for c in coverage)
    why = "".join(f"<li>{n} × {esc(k)}</li>" for k, n in reasons)
    use = (f"<p>Model use this run: {llm.tokens_in:,} input and {llm.tokens_out:,} output tokens, {getattr(llm, 'search_count', 0)} searches "
           f"(estimated ${llm.est_cost_usd():.3f}{f', about ₹{llm.est_cost_usd() * inr:,.0f}' if inr else ''}, at the prices in config/news_sources.yaml).</p>") if llm is not None else ""
    err = f'<p class="warnline">Model check failed: {esc(llm_error)}</p>' if llm_error else ""
    inner = (f'<div class="quiet">{err}<p>Articles read: {stats["read_direct"]} directly, {stats["read_wayback"]} via Wayback, {stats["read_archive"]} via other archives, '
             f'{stats["read_search"]} by finding the same story elsewhere, {stats["read_partial"]} partially, {stats["read_summary"]} from feed summaries. '
             f'{stats["unreadable"]} could not be read, {stats["cost_stopped"]} left unread by the spending stop, {stats["extract_errors"]} model errors. '
             f'Claims kept {stats["claims_kept"]}, dropped by the guard {stats["claims_dropped"]}.</p>{use}'
             f'{"<h4>Most common reasons an article was not turned into a story</h4><ul>" + why + "</ul>" if why else ""}'
             f'<h4>Feeds</h4><div class="tw"><table><tr><th>Feed</th><th>Status</th><th>Items kept</th></tr>{rows}</table></div></div>')
    return fold("block", "coverage", "Coverage and quality", inner, "", "#adb5bd")


# ---------------------------------------------------------------- page
CSS = """
:root{--paper:#fbf8f3;--card:#ffffff;--ink:#1d1b18;--mute:#6d675e;--rule:#ddd6c9;--red:#b3201f;--chip:#efe9dc;--up:#1b7f3b;--down:#b3201f;
--serif:"Playfair Display","Iowan Old Style","Palatino Linotype",Georgia,serif;--text:"Source Serif 4","Charter","Iowan Old Style",Georgia,serif;
--sans:"Inter",-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;color-scheme:light dark}
@media (prefers-color-scheme:dark){:root{--paper:#14130f;--card:#1d1b17;--ink:#ece7dc;--mute:#a39c8e;--rule:#3a362e;--chip:#2a2721;--up:#5fd08a;--down:#ff8a80}}
*{box-sizing:border-box}html{scroll-behavior:smooth;scroll-padding-top:3.4rem}
body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--text);font-size:1.04rem;line-height:1.6;-webkit-text-size-adjust:100%}
a{color:inherit}.wrap{max-width:860px;margin:0 auto;padding:0 14px}
.mast{text-align:center;padding:1.2rem 14px .6rem}
.mast .top{font:600 .62rem var(--sans);letter-spacing:.14em;text-transform:uppercase;color:var(--mute);display:flex;justify-content:center;gap:.2rem 1rem;flex-wrap:wrap}
.mast h1{font:900 clamp(2.3rem,9vw,4.2rem)/1 var(--serif);margin:.4rem 0;letter-spacing:-.02em}
.mast .rule{border-top:3px double var(--ink);border-bottom:1px solid var(--ink);height:7px;margin:.4rem 0 .5rem}
.mast .sub{font:400 .78rem var(--sans);color:var(--mute)}
nav.bar{position:sticky;top:0;z-index:20;background:color-mix(in srgb,var(--paper) 93%,transparent);backdrop-filter:blur(8px);border-bottom:1px solid var(--rule)}
nav.bar .wrap{display:flex;gap:.4rem;overflow-x:auto;padding:.5rem 14px;scrollbar-width:none}nav.bar .wrap::-webkit-scrollbar{display:none}
nav.bar a,.ctl button{font:700 .72rem var(--sans);letter-spacing:.06em;text-transform:uppercase;text-decoration:none;white-space:nowrap;padding:.5rem .9rem;border-radius:99px;background:var(--chip);border:0;color:var(--ink);cursor:pointer}
.ctl{display:flex;gap:.5rem;margin:.9rem 0 .2rem}.ctl button{background:transparent;border:1px solid var(--rule)}
.alert{margin:1rem 0;padding:.8rem 1rem;border-left:5px solid var(--red);background:color-mix(in srgb,var(--red) 10%,var(--card));font:500 .92rem var(--sans);border-radius:6px}
.strip{margin:1rem 0}.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:.5rem}
.tile{background:var(--card);border:1px solid var(--rule);border-radius:8px;padding:.5rem .7rem;font-family:var(--sans)}
.tl{font-size:.66rem;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--mute)}.tv{font:700 1.1rem var(--serif)}
.tc{font-size:.78rem;font-weight:600}.tc.up{color:var(--up)}.tc.down{color:var(--down)}.ta{font-size:.62rem;color:var(--mute)}.stripnote{font:400 .72rem var(--sans);color:var(--mute);margin:.4rem 0 0}
/* every unit is a one-line summary that opens */
summary{list-style:none;cursor:pointer;-webkit-tap-highlight-color:transparent}summary::-webkit-details-marker{display:none}
summary:before{content:"›";font:700 1.3rem/1 var(--sans);float:left;margin:.05rem .55rem 0 0;color:var(--accent,var(--mute));transition:transform .15s}
details[open]>summary:before{transform:rotate(90deg)}
details.block{margin:.9rem 0;border-top:3px solid var(--accent,var(--ink))}
details.block>summary{display:flex;flex-wrap:wrap;align-items:baseline;gap:.15rem .6rem;padding:.7rem 0;min-height:48px}
.sum{flex:0 0 100%;order:9;font:400 .74rem/1.4 var(--sans);color:var(--mute);padding-left:1.15rem;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
details[open]>summary>.sum{display:none}
.ft{font:800 1.25rem/1.15 var(--serif);flex:1;min-width:0}.kick{font:800 .6rem var(--sans);letter-spacing:.16em;text-transform:uppercase;color:var(--accent,var(--mute));order:3}
.count{font:700 .72rem var(--sans);color:var(--mute);background:var(--chip);border-radius:99px;padding:.15rem .55rem;white-space:nowrap}
/* depth you can see: each level is indented and a shade deeper in the section's own colour; an open item's headline gets a tinted bar */
details.sub{margin:.35rem 0 .35rem .6rem;border-left:5px solid var(--accent,var(--rule));background:color-mix(in srgb,var(--accent,#888) 7%,var(--card));border-radius:0 8px 8px 0}
details.sub>summary{display:flex;flex-wrap:wrap;align-items:baseline;gap:.15rem .6rem;padding:.7rem .8rem;min-height:46px}details.sub>summary .ft{font-size:1.08rem}
details.sub>.inner{padding:.1rem .4rem .6rem .6rem}details.block>.inner{padding:0 0 .6rem}
details.block[open]>summary,details.sub[open]>summary{background:color-mix(in srgb,var(--accent,#888) 14%,transparent);border-radius:0 8px 0 0}
details.st{border-top:1px solid var(--rule);margin-left:.4rem;padding-left:.45rem;border-left:3px solid color-mix(in srgb,var(--accent,#888) 45%,transparent)}details.st:first-child{border-top:0}
details.st[open]{background:color-mix(in srgb,var(--accent,#888) 6%,var(--card));border-left-color:var(--accent,#888)}
details.st[open]>summary{font-weight:inherit;border-bottom:1px dotted var(--rule)}
details.st>summary{padding:.65rem .1rem;min-height:46px}
.ht{display:block;font:700 1.02rem/1.28 var(--serif);overflow:hidden}
.terms{margin:.5rem 0 0;font:400 .78rem/1.5 var(--sans);color:var(--mute);border-top:1px dotted var(--rule);padding-top:.4rem}.terms span{display:block;margin-top:.15rem}.terms i{font-style:normal;font-weight:700;color:var(--ink)}
.nums{list-style:none;margin:0;padding:0}.nums li{padding:.45rem 0;border-top:1px solid var(--rule);font:400 .85rem/1.4 var(--sans)}.nums li:first-child{border-top:0}.nums b{font:800 1rem var(--sans);color:var(--red);margin-right:.3rem}.nums em{color:var(--mute);font-size:.7rem;font-style:normal;margin-left:.3rem}
.tools{display:flex;flex-wrap:wrap;gap:.5rem;align-items:center;margin:.5rem 0}.tools input{flex:1 1 11rem;min-width:0;font:500 .9rem var(--sans);padding:.55rem .8rem;border-radius:99px;border:1px solid var(--rule);background:var(--card);color:var(--ink)}
.tools .note{font:500 .72rem var(--sans);color:var(--mute);flex:0 0 100%}details.st.isread>summary .ht{opacity:.55}details.st.isread>summary .ht:after{content:" ✓";color:#2b8a3e}
.figdiff{margin:.5rem 0;padding:.5rem .7rem;border-left:4px solid #f59f00;background:color-mix(in srgb,#f59f00 12%,var(--card));font:500 .8rem/1.45 var(--sans);border-radius:0 6px 6px 0}.figdiff ul{margin:.3rem 0 0 1rem;padding:0}.figdiff .vs{color:var(--mute);font-style:italic}
.why{display:block;font:500 .66rem var(--sans);letter-spacing:.02em;color:var(--mute);margin-top:.1rem}
.meta{display:block;font:500 .7rem var(--sans);color:var(--mute);margin-top:.15rem;overflow:hidden}
.meta b,.badge{font-weight:800;color:var(--red)}.badge{font:800 .62rem var(--sans);letter-spacing:.06em;text-transform:uppercase;background:var(--chip);padding:.1rem .4rem;border-radius:4px;color:var(--ink)}.badge.warn{background:#f59f00}
details.st>.body{padding:.1rem .2rem .9rem 1.15rem}.by{font:500 .72rem var(--sans);color:var(--mute);margin-bottom:.5rem}
.body p{margin:0 0 .75rem}.body p.also{font:400 .82rem var(--sans);color:var(--mute)}
.cite{font:500 .78rem var(--sans)}a.src{font-weight:700;color:var(--accent,var(--red));text-decoration:none;border-bottom:1px solid currentColor}.note{font:400 .7rem var(--sans);color:var(--mute);text-decoration:none}
.related{margin:.6rem 0 0 .3rem;border-top:1px dashed var(--rule);padding-top:.4rem}.related-h{font:800 .62rem var(--sans);letter-spacing:.16em;text-transform:uppercase;color:var(--mute)}
.related details.st{border-left:3px solid var(--accent,var(--rule));margin:.35rem 0 .35rem .4rem;background:color-mix(in srgb,var(--accent,#888) 13%,var(--card));padding-left:.5rem}
.related details.st[open]{background:color-mix(in srgb,var(--accent,#888) 19%,var(--card))}
details.st.ev .et{font:700 .74rem var(--sans);color:var(--mute);margin-right:.4rem}
.five{--accent:var(--red);margin:1rem 0;background:var(--card);border-top:4px solid var(--red);border-radius:0 0 8px 8px;padding:.2rem .9rem .3rem}
.five h2{font:800 .74rem var(--sans);letter-spacing:.18em;text-transform:uppercase;color:var(--red);margin:.7rem 0 .2rem}
.five details.st>summary .ht{font-size:1.1rem}
ul.hl{margin:.2rem 0 .6rem;padding-left:1rem;font:400 .9rem/1.45 var(--sans)}ul.hl li{margin:.4rem 0}
.intro{font:400 .86rem var(--sans);color:var(--mute);margin:.1rem 0 .6rem}.warnline{font:600 .82rem var(--sans);color:var(--down)}
.quiet{font:400 .88rem var(--sans)}.tw{overflow-x:auto}.quiet table{border-collapse:collapse;width:100%;font-size:.78rem}.quiet td,.quiet th{border-bottom:1px solid var(--rule);padding:.3rem .5rem;text-align:left}
.ok{color:var(--up)}.bad{color:var(--down)}
footer.foot{margin:2.5rem 0 2.5rem;padding-top:1rem;border-top:3px double var(--ink);font:400 .78rem/1.55 var(--sans);color:var(--mute)}
.totop{position:fixed;right:14px;bottom:16px;z-index:30;width:46px;height:46px;border-radius:50%;background:var(--ink);color:var(--paper);display:flex;align-items:center;justify-content:center;text-decoration:none;font:700 1.2rem var(--sans);box-shadow:0 2px 10px #0004;opacity:.88}
@media(max-width:560px){.kick{display:none}}
@media(min-width:900px){body{font-size:1.08rem}.wrap{max-width:900px}}
@media print{nav.bar,.ctl,.tools,.totop{display:none}details.st[open],.related details.st,.related details.st[open]{background:none}details.sub{background:color-mix(in srgb,var(--accent,#888) 4%,#fff)}details.block[open]>summary,details.sub[open]>summary{background:none}summary:before{display:none}details.block{margin:.4rem 0}details.sub{margin:.4rem 0 .4rem .3rem}details.st{margin-left:.3rem}.related{margin-left:.2rem}.body,.five,details{break-inside:auto}h2,.ft,.ht{break-after:avoid}body{font-size:.9rem}details.st>.body{padding-left:.2rem}}
"""

JS = ("function openTo(el){for(var n=el;n;n=n.parentElement){if(n.tagName==='DETAILS')n.open=true;}}"
      "function sk(){try{return JSON.parse(localStorage.getItem('me-read')||'{}')}catch(e){return {}}}"
      "function sv(o){try{localStorage.setItem('me-read',JSON.stringify(o))}catch(e){}}"
      "function hk(t){var h=0;for(var i=0;i<t.length;i++){h=(h*31+t.charCodeAt(i))|0}return String(h)}"
      "var R=sk(),D=new Date().toISOString().slice(0,10);"
      "document.querySelectorAll('details.st').forEach(function(d){var h=d.querySelector('.ht');if(h&&R[hk(h.textContent)])d.classList.add('isread');});"
      "document.addEventListener('toggle',function(e){var d=e.target;if(d.tagName==='DETAILS'&&d.classList.contains('st')&&d.open){var h=d.querySelector('.ht');if(h){R[hk(h.textContent)]=D;"
      "var c=Date.parse(D)-6048e5;for(var k in R){if(Date.parse(R[k])<c)delete R[k]}sv(R);d.classList.add('isread');}}},true);"
      "var fs=parseFloat((function(){try{return localStorage.getItem('me-font')}catch(e){return null}})())||1;document.documentElement.style.fontSize=(fs*100)+'%';"
      "document.addEventListener('click',function(e){var a=e.target.closest('a[href^=\"#\"]');if(a&&a.hash.length>1){var t=document.getElementById(a.hash.slice(1));"
      "if(t){openTo(t);setTimeout(function(){t.scrollIntoView({behavior:'smooth',block:'start'})},30);e.preventDefault();}return;}"
      "var f=e.target.closest('button[data-font]');if(f){fs=Math.min(1.5,Math.max(.8,fs+.1*parseInt(f.getAttribute('data-font'))));document.documentElement.style.fontSize=(fs*100)+'%';try{localStorage.setItem('me-font',String(fs))}catch(x){}return;}"
      "var b=e.target.closest('button[data-all]');if(b){var o=b.getAttribute('data-all')==='open';document.querySelectorAll('details').forEach(function(d){d.open=o;});}});"
      "var q=document.getElementById('q');if(q){q.addEventListener('input',function(){var v=q.value.trim().toLowerCase(),n=0,st=document.querySelectorAll('details.st');"
      "st.forEach(function(d){d.style.display='';});"
      "if(v.length<2){document.getElementById('qn').textContent='';return;}"
      "st.forEach(function(d){var own=(d.querySelector('summary')||{}).textContent+' '+((d.querySelector('.body')||{}).textContent||'');if(own.toLowerCase().indexOf(v)>-1){n++;openTo(d.parentElement);}else{d.style.display='none';}});"
      "document.getElementById('qn').textContent=n+' stories match';});}")


def outlets(c: Cluster) -> int:
    return len({x.name for st in c.stories for x in st.sources})


def numbers_of_the_day(top: list, limit: int = 6) -> str:
    """One verified figure per leading story, shown with the claim it came from and the outlet. Nothing is computed or rephrased."""
    chips, seen = [], set()
    for c, _ in top:
        for st in [c.lead]:
            for p in st.paras:
                for cl in p.claims[:2]:
                    fg = [f for f in figures(cl, p.source.name) if f.anchors]
                    if fg and (c.lead.headline not in seen):
                        seen.add(c.lead.headline)
                        chips.append(f'<li><b>{esc(fg[0].shown)}</b> <span>{esc(_short(cl, 110))}</span> <em>{esc(p.source.name)}</em></li>')
                        break
                if c.lead.headline in seen:
                    break
        if len(chips) >= limit:
            break
    return '<ul class="nums">' + "".join(chips) + "</ul>" if len(chips) >= 2 else ""


def seen_yesterday(headline: str, prev_heads: list[str]) -> bool:
    want = sig_words(headline)
    if len(want) < 3:
        return False
    return any(len(want & sig_words(h)) / min(len(want), max(1, len(sig_words(h)))) >= 0.6 for h in prev_heads)


def lead_clusters(all_clusters: list[Cluster], lim: dict, imp_cfg: Optional[dict] = None) -> list[tuple[Cluster, Rating]]:
    """Today in X: [(storyline, rating)]. What a storyline is ABOUT (central banks, crude, gold, data, your watchlist...) plus how many outlets and
    whether an official source confirms it decides; see news/importance.py. Never fewer than `top_floor`, never more than `top_max`."""
    return pick_top(all_clusters, Importance(imp_cfg), int(lim.get("top_floor", 3)), int(lim.get("top_max", 15)))


def _first_sentence(st: Story) -> str:
    for p in st.paras:
        if p.claims:
            return _sentence(p.claims[0])
    return ""


def render_magazine(now: datetime, stories: list[Story], unread: list, stats: dict, coverage: list, reasons: list, llm_note: str, llm_error: str,
                    llm, cal_note: str, cal: dict, cfg: dict, archive, prev_heads: Optional[list] = None, market_rows: Optional[list] = None, **_ignored) -> str:
    lim = cfg["limits"]
    sector_order = list((cfg.get("sectors") or {}).keys())
    upd_groups, used = event_updates(stories, cal, now)
    rest = [s for s in stories if id(s) not in used]
    commodity = [s for s in rest if is_commodity(s)]
    general = [s for s in rest if not is_commodity(s)]

    all_clusters = cluster(stories)
    top = lead_clusters(all_clusters, lim, cfg.get("importance"))
    words = sum(len(" ".join(c for p in s.paras for c in p.claims).split()) for s in stories)
    mins = max(1, round(words / 230))

    parts: list[str] = []
    if llm_note:
        parts.append(f'<div class="alert"><strong>Read this first.</strong> {esc(llm_note)}</div>')
    parts.append('<div class="ctl"><button data-all="open">Expand all</button><button data-all="close">Collapse all</button><button data-font="-1" aria-label="Smaller text">A−</button><button data-font="1" aria-label="Larger text">A+</button></div>'
                 '<div class="tools"><input id="q" type="search" placeholder="Search this edition (e.g. crude, RBI, Nvidia)" aria-label="Search"><div class="note" id="qn"></div></div>')

    rows, gnote = load_glance(ROOT_DASH(cfg), now)
    if not rows and market_rows:
        rows, gnote = market_rows, "Free public daily closes (Stooq), not live prices; each tile shows its own as-of date."
    parts.append(markets_html(rows, gnote))

    prev = list(prev_heads or [])
    fresh = [seen_yesterday(c.lead.headline, prev) is False for c, _ in top] if prev else []
    if top:
        trend = f" {sum(fresh)} new today, {len(fresh) - sum(fresh)} still running from yesterday." if prev else ""
        parts.append(f'<section class="five" id="top"><h2>Today in {len(top)}</h2><p class="intro">What matters most this morning, judged by subject (rates, oil, gold, data, your watchlist), how many outlets carry it and whether an official source confirms it. The rest is in the sections below. Tap a headline to read it.{trend}</p>'
                     + (f'<details class="sub numsbox" id="nums" style="--accent:#e67700"><summary><span class="ft">Numbers of the day</span><span class=count>{numbers_of_the_day(top).count("<li>")}</span></summary><div class="inner">{numbers_of_the_day(top)}</div></details>' if numbers_of_the_day(top) else "")
                     + "".join(cluster_details(c, f"top-{i}", why=" · ".join(([("new today" if fresh[i] else "still running from yesterday")] if prev else []) + r.why)) for i, (c, r) in enumerate(top)) + "</section>")
    elif not llm_note:
        parts.append('<div class="alert">No story could be built from the sources in this window.</div>')

    ev_html, n_ev, ev_sum = event_rows(cal, now, int(lim.get("calendar_horizon_days", 21)), int(lim.get("calendar_review_max_age_days", 7)))
    if cal_note:
        ev_html = f'<p class="intro">Calendar: {esc(cal_note)}</p>' + ev_html
    parts.append(fold("block", "events", "Upcoming events", ev_html, f"{n_ev} · times in IST", "#495057", "Looking ahead", summary=ev_sum))

    if upd_groups:
        inner = "".join(fold("sub", f"u-{_slug(lbl)}", lbl, "".join(cluster_details(c, f"u-{i}-{j}") for j, c in enumerate(cluster(sts))), str(len(sts)), "#2b8a3e", summary=leads_summary(cluster(sts)))
                        for i, (lbl, sts) in enumerate(upd_groups))
        parts.append(fold("block", "updates", "Event updates", inner, str(sum(len(s) for _, s in upd_groups)), "#2b8a3e", "What happened",
                          summary=names_summary([(lbl, len(sts)) for lbl, sts in upd_groups], 5)))

    mine = watch_groups(all_clusters, Importance(cfg.get("importance")))
    if mine:
        parts.append(fold("block", "mine", "On your list", groups_html("w", mine), str(sum(_n(cl) for _, cl in mine)), "#c2255c", "Your instruments",
                          summary=names_summary([(n, _n(cl)) for n, cl in mine])))

    if commodity:
        com = by_commodity(commodity)
        parts.append(fold("block", "commodities", "Energy and metals", groups_html("c", com), str(len(commodity)), "#b8860b", "The trading desk",
                          summary=names_summary([(n, _n(cl)) for n, cl in com])))

    reg, sec = by_region(general), by_sector(general, sector_order)
    parts.append(fold("block", "regions", "News by jurisdiction", groups_html("r", reg) or '<p class="intro">No stories in this window.</p>',
                      str(sum(_n(cl) for _, cl in reg)), "#d9480f", "Read by place", open_=True,
                      summary=names_summary([(n, _n(cl)) for n, cl in reg])))
    parts.append(fold("block", "sectors", "News by sector", groups_html("s", sec, True) or '<p class="intro">No sector stories in this window.</p>',
                      str(sum(_n(cl) for _, cl in sec)), "#5b3cc4", "Read by industry",
                      summary=names_summary([(n, _n(cl)) for n, cl in sec])))

    parts.append(headlines_html(unread, archive, int(lim.get("in_brief_max", 450))))
    parts.append(coverage_html(stats, coverage, reasons, llm, llm_error, float((cfg.get('llm') or {}).get('usd_inr', 0) or 0)))

    nav = ['<a href="#top">Top</a>', '<a href="#events">Events</a>']
    if upd_groups:
        nav.append('<a href="#updates">Updates</a>')
    if mine:
        nav.append('<a href="#mine">On your list</a>')
    if commodity:
        nav.append('<a href="#commodities">Energy &amp; metals</a>')
    nav += ['<a href="#regions">Jurisdictions</a>', '<a href="#sectors">Sectors</a>', '<a href="#headlines">Missed</a>', '<a href="#coverage">Coverage</a>']

    n_st = len(stories)
    n_src = len({p.source.url for s in stories for p in s.paras})
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>The Morning Edition · {now.astimezone(IST):%d %b %Y}</title>"
            "<link rel='preconnect' href='https://fonts.googleapis.com'><link rel='preconnect' href='https://fonts.gstatic.com' crossorigin>"
            "<link href='https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Playfair+Display:wght@700;800;900&family=Source+Serif+4:opsz,wght@8..60,400;8..60,600&display=swap' rel='stylesheet'>"
            f"<style>{CSS}</style></head><body>"
            f"<header class='wrap mast'><div class='top'><span>{now.astimezone(IST):%A %d %B %Y}</span><span>24 hours to {now.astimezone(IST):%H:%M} IST</span></div>"
            "<h1>The Morning Edition</h1><div class='rule'></div>"
            f"<div class='sub'>{n_st} {'story' if n_st == 1 else 'stories'} from {n_src} {'article' if n_src == 1 else 'articles'} · about {mins} min of reading · tap any headline to open it</div></header>"
            f"<nav class='bar'><div class='wrap'>{''.join(nav)}</div></nav>"
            f"<main class='wrap'>{''.join(parts)}"
            "<footer class='foot'>How this page is made: each sentence is a statement taken from the article linked after it. Code checked that the supporting words "
            "appear in that article and that every number in the sentence is present in it. Sentences are paraphrased, not copied. Nothing here is a recommendation to buy or sell.</footer>"
            f"</main><a class='totop' href='#top' aria-label='Back to top'>↑</a><script>{JS}</script></body></html>")


def ROOT_DASH(cfg: dict) -> Path:
    return Path(__file__).resolve().parent.parent / cfg.get("dashboard_file", "data/dashboard_latest.json")
