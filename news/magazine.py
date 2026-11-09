"""The Morning Edition as a magazine page: masthead, lead story, storylines grouped by jurisdiction and by sector, a commodities desk,
a week-ahead timeline, collapsed headlines and a collapsed 'Coverage and quality' panel. One self-contained HTML file, no scripts needed
(a few lines of script only make the navigation switch lenses). Pure rendering: it never fetches anything and never adds text of its own
to a story, so every sentence on the page is a verified claim followed by its source link."""
from __future__ import annotations

import html
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from news.cluster import Cluster, by_commodity, by_region, by_sector, cluster, is_commodity
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


def _chips(st: Story, show_region: bool = True) -> str:
    from news.cluster import region_of
    bits = []
    if show_region:
        bits.append(f'<span class="chip">{esc(region_of(st))}</span>')
    if st.sectors:
        bits.append(f'<span class="chip alt">{esc(st.sectors[0])}</span>')
    n = len(st.sources)
    if n > 1:
        bits.append(f'<span class="chip hot">{n} outlets</span>')
    return "".join(bits)


# ---------------------------------------------------------------- story and storyline cards
def story_html(st: Story, lead: bool = False, per_para: int = 3, sid: str = "") -> str:
    cls = "story lead" if lead else "story"
    names = ", ".join(esc(x.name) for x in st.sources[:4])
    more = f" and {len(st.sources) - 4} more" if len(st.sources) > 4 else ""
    return (f'<article class="{cls}" id="{sid}"><div class="chips">{_chips(st)}</div>'
            f'<h3>{esc(st.headline)}</h3>'
            f'<div class="by">{names}{more}{" · " + esc(_when(st)) if st.published else ""}</div>'
            f'<div class="body">{paras_html(st, per_para)}</div></article>')


def related_html(st: Story) -> str:
    first = paras_html(st, 3, first_only=True)
    full = paras_html(st, 3)
    rest = ""
    if len(full) > len(first) + 20:
        rest = f'<details class="more"><summary>Read the full report</summary>{full}</details>'
    return (f'<div class="rel"><div class="chips">{_chips(st, show_region=False)}</div><h4>{esc(st.headline)}</h4>'
            f'<div class="by">{esc(_when(st))}</div>{first}{rest}</div>')


def cluster_html(c: Cluster, feature: bool = False, uid: str = "") -> str:
    out = [f'<section class="storyline{" feature" if feature else ""}">', story_html(c.lead, lead=feature, sid=uid)]
    if c.related:
        out.append(f'<div class="related"><div class="related-h">Related reporting · {len(c.related)}</div>'
                   + "".join(related_html(r) for r in c.related) + "</div>")
    out.append("</section>")
    return "".join(out)


def section_html(sec_id: str, title: str, kicker: str, clusters: list[Cluster], accent: str, intro: str = "", feature_first: bool = True,
                 cap: Optional[int] = None) -> str:
    shown = clusters[:cap] if cap else clusters
    n_st = sum(len(c.stories) for c in clusters)
    body = "".join(cluster_html(c, feature=(feature_first and i == 0), uid=f"{sec_id}-{i}") for i, c in enumerate(shown))
    hidden = ""
    if cap and len(clusters) > cap:
        rest = "".join(cluster_html(c, uid=f"{sec_id}-x{i}") for i, c in enumerate(clusters[cap:]))
        hidden = f'<details class="moresec"><summary>{len(clusters) - cap} more storylines in {esc(title)}</summary><div class="grid">{rest}</div></details>'
    return (f'<section class="block" id="{sec_id}" style="--accent:{accent}">'
            f'<header class="bh"><span class="kick">{esc(kicker)}</span><h2>{esc(title)}</h2>'
            f'<span class="count">{n_st} stories</span></header>{f"<p class=intro>{esc(intro)}</p>" if intro else ""}'
            f'<div class="grid">{body}</div>{hidden}</section>')


# ---------------------------------------------------------------- markets, calendar, headlines
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
        return f'<section class="strip empty"><p>{esc(note)}</p></section>'
    tiles = []
    for r in rows:
        chg = r.get("chg_text") or ""
        cls = "up" if chg.strip().startswith("+") else "down" if chg.strip().startswith(("-", "−")) else ""
        tiles.append(f'<div class="tile"><div class="tl">{esc(str(r["label"]))}</div><div class="tv">{esc(str(r["value_text"]))}</div>'
                     f'<div class="tc {cls}">{esc(chg) if chg else "&nbsp;"}</div><div class="ta">{esc(str(r["as_of"]))} · {esc(str(r["source"]))}</div></div>')
    return f'<section class="strip" id="markets"><div class="tiles">{"".join(tiles)}</div><p class="stripnote">{esc(note)}</p></section>'


def calendar_html(cal: dict, now: datetime, horizon_days: int, max_age_days: int) -> str:
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
        return ('<section class="block" id="week" style="--accent:#495057"><header class="bh"><span class="kick">Looking ahead</span><h2>Week ahead</h2></header>'
                '<p class="intro">No events in the calendar for this period.</p></section>')
    cols = []
    for d in sorted(days):
        rows = []
        for e in sorted(days[d], key=lambda x: str(x.get("time_ist", ""))):
            src = (f'<a class="src" href="{esc(e["source_url"], quote=True)}" target="_blank" rel="noopener">{esc(e["source_name"])} ↗</a>'
                   if e.get("source_url") and e.get("source_name") else "")
            aff = "".join(f'<span class="chip alt">{esc(a)}</span>' for a in (e.get("affects") or []))
            unv = "" if e.get("verified", True) else ' <span class="chip warn">unverified</span>'
            rows.append(f'<li><div class="et">{esc(str(e.get("time_ist") or "all day"))}</div><div class="ec"><strong>{esc(e["title"])}</strong>{unv}'
                        f'<div class="ex">{esc(str(e.get("expectation") or ""))}</div><div class="chips">{aff}</div><div class="cite">{src}</div></div></li>')
        tag = "Today" if d == today else "Tomorrow" if d == today + timedelta(days=1) else d.strftime("%A")
        cols.append(f'<div class="day"><div class="dh"><span class="dn">{d:%d}</span><span class="dm">{d:%b}</span><span class="dw">{tag}</span></div><ul>{"".join(rows)}</ul></div>')
    return (f'<section class="block" id="week" style="--accent:#495057"><header class="bh"><span class="kick">Looking ahead</span><h2>Week ahead</h2>'
            f'<span class="count">times in IST</span></header>{f"<p class=warnline>{esc(warn)}</p>" if warn else ""}<div class="days">{"".join(cols)}</div></section>')


def headlines_html(unread: list, archive_fn, limit: int) -> str:
    from news.cluster import REGION_ORDER
    from news.paper import REGION_SECTION, item_score
    groups: dict[str, list] = {r: [] for r in REGION_ORDER}
    for it, _why in sorted(unread, key=lambda x: (-item_score(x[0]), -(x[0].published.timestamp() if x[0].published else 0)))[:limit]:
        r = REGION_SECTION.get(it.feed_region or (it.regions[0] if it.regions else ""), "Global markets and macro")
        groups[r].append(it)
    parts = []
    for r, lst in groups.items():
        if not lst:
            continue
        li = []
        for it in lst:
            arch = f' · <a class="note" href="{esc(archive_fn(it), quote=True)}" target="_blank" rel="noopener">archive</a>' if archive_fn(it) else ""
            li.append(f'<li><a href="{esc(it.link, quote=True)}" target="_blank" rel="noopener">{esc(it.title)}</a> <span class="note">{esc(it.publisher or it.source)}</span>{arch}</li>')
        parts.append(f'<details class="hl"><summary>{esc(r)} <span class="count">{len(lst)}</span></summary><ul>{"".join(li)}</ul></details>')
    if not parts:
        return ""
    return ('<section class="block" id="headlines" style="--accent:#868e96"><header class="bh"><span class="kick">The rest of the news</span><h2>Headlines we could not read in full</h2></header>'
            '<p class="intro">For these the article text was unreadable even after archives and a headline search, so nothing is summarised. Tap a region to open its list.</p>'
            + "".join(parts) + "</section>")


def coverage_html(stats: dict, coverage: list, reasons: list, llm, llm_error: str) -> str:
    rows = "".join(f'<tr><td>{esc(c["feed"])}</td><td class="{"ok" if c["ok"] else "bad"}">{"ok" if c["ok"] else "FAILED: " + esc(c["reason"][:90])}</td><td>{c["kept"]}</td></tr>' for c in coverage)
    why = "".join(f"<li>{n} × {esc(k)}</li>" for k, n in reasons)
    use = (f"<p>Model use this run: {llm.tokens_in:,} input and {llm.tokens_out:,} output tokens, {getattr(llm, 'search_count', 0)} searches "
           f"(estimated ${llm.est_cost_usd():.3f} at the prices in config/news_sources.yaml).</p>") if llm is not None else ""
    err = f'<p class="warnline">Model check failed: {esc(llm_error)}</p>' if llm_error else ""
    return ('<section class="block quiet" id="coverage" style="--accent:#adb5bd"><details><summary><h2>Coverage and quality</h2></summary>'
            f'{err}<p>Articles read: {stats["read_direct"]} directly, {stats["read_wayback"]} via Wayback, {stats["read_archive"]} via other archives, '
            f'{stats["read_search"]} by finding the same story elsewhere, {stats["read_partial"]} partially, {stats["read_summary"]} from feed summaries. '
            f'{stats["unreadable"]} could not be read, {stats["cost_stopped"]} left unread by the spending stop, {stats["extract_errors"]} model errors. '
            f'Claims kept {stats["claims_kept"]}, dropped by the guard {stats["claims_dropped"]}.</p>{use}'
            f'{"<h4>Most common reasons an article was not turned into a story</h4><ul>" + why + "</ul>" if why else ""}'
            f'<h4>Feeds</h4><div class="tw"><table><tr><th>Feed</th><th>Status</th><th>Items kept</th></tr>{rows}</table></div></details></section>')


# ---------------------------------------------------------------- page
CSS = """
:root{--paper:#fbf8f3;--card:#ffffff;--ink:#1d1b18;--mute:#6d675e;--rule:#d9d2c5;--red:#b3201f;--chip:#efe9dc;--up:#1b7f3b;--down:#b3201f;
--serif:"Playfair Display","Iowan Old Style","Palatino Linotype",Georgia,serif;--text:"Source Serif 4","Charter","Iowan Old Style",Georgia,serif;
--sans:"Inter",-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;color-scheme:light dark}
@media (prefers-color-scheme:dark){:root{--paper:#14130f;--card:#1d1b17;--ink:#ece7dc;--mute:#a39c8e;--rule:#3a362e;--chip:#2a2721;--up:#5fd08a;--down:#ff8a80}}
*{box-sizing:border-box}html{scroll-behavior:smooth;scroll-padding-top:3.6rem}
body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--text);font-size:1.06rem;line-height:1.62;-webkit-text-size-adjust:100%}
a{color:inherit}.wrap{max-width:1120px;margin:0 auto;padding:0 18px}
.mast{text-align:center;padding:2.2rem 18px 1rem}
.mast .top{font:600 .72rem var(--sans);letter-spacing:.22em;text-transform:uppercase;color:var(--mute);display:flex;justify-content:space-between;gap:1rem;flex-wrap:wrap}
.mast h1{font:900 clamp(2.4rem,7.5vw,5rem)/1 var(--serif);margin:.55rem 0 .4rem;letter-spacing:-.02em}
.mast .rule{border-top:3px double var(--ink);border-bottom:1px solid var(--ink);height:7px;margin:.5rem 0 .6rem}
.mast .sub{font:400 .95rem var(--sans);color:var(--mute)}
nav.bar{position:sticky;top:0;z-index:20;background:color-mix(in srgb,var(--paper) 92%,transparent);backdrop-filter:blur(8px);border-bottom:1px solid var(--rule)}
nav.bar .wrap{display:flex;gap:.4rem;overflow-x:auto;padding:.55rem 18px;scrollbar-width:none}nav.bar .wrap::-webkit-scrollbar{display:none}
nav.bar a{font:600 .78rem var(--sans);letter-spacing:.06em;text-transform:uppercase;text-decoration:none;white-space:nowrap;padding:.38rem .8rem;border-radius:99px;background:var(--chip)}
nav.bar a:hover{background:var(--ink);color:var(--paper)}
.alert{margin:1rem 0;padding:.9rem 1.1rem;border-left:5px solid var(--red);background:color-mix(in srgb,var(--red) 10%,var(--card));font:500 .95rem var(--sans);border-radius:6px}
.front{display:grid;grid-template-columns:minmax(0,1.9fr) minmax(0,1fr);gap:2rem;margin:1.6rem 0 1rem}
@media(max-width:860px){.front{grid-template-columns:1fr}}
.five{background:var(--card);border-top:4px solid var(--red);padding:1.1rem 1.2rem;align-self:start;border-radius:0 0 8px 8px;box-shadow:0 1px 0 var(--rule)}
.five h2{font:800 .8rem var(--sans);letter-spacing:.18em;text-transform:uppercase;margin:0 0 .6rem;color:var(--red)}
.five ol{margin:0;padding:0;list-style:none;counter-reset:n}
.five li{counter-increment:n;padding:.7rem 0 .7rem 2.2rem;position:relative;border-top:1px solid var(--rule)}.five li:first-child{border-top:0}
.five li:before{content:counter(n);position:absolute;left:0;top:.55rem;font:900 1.6rem/1 var(--serif);color:var(--red)}
.five a.t{font:700 1.02rem/1.3 var(--serif);text-decoration:none;display:block}.five a.t:hover{text-decoration:underline}
.five .s{font:400 .86rem/1.45 var(--sans);color:var(--mute);margin-top:.15rem}
.strip{margin:1.2rem 0 .4rem}.strip.empty p{font:400 .85rem var(--sans);color:var(--mute);margin:0}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:.6rem}
.tile{background:var(--card);border:1px solid var(--rule);border-radius:8px;padding:.6rem .75rem;font-family:var(--sans)}
.tl{font-size:.7rem;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--mute)}.tv{font:700 1.2rem var(--serif);margin:.1rem 0}
.tc{font-size:.8rem;font-weight:600}.tc.up{color:var(--up)}.tc.down{color:var(--down)}.ta{font-size:.65rem;color:var(--mute);margin-top:.15rem}
.stripnote{font:400 .75rem var(--sans);color:var(--mute);margin:.5rem 0 0}
.lenses{margin:2.2rem 0 0}.lenses>input{position:absolute;opacity:0;pointer-events:none}
.switch{display:inline-flex;background:var(--chip);border-radius:99px;padding:4px;margin-bottom:.4rem}
.switch label{font:700 .78rem var(--sans);letter-spacing:.08em;text-transform:uppercase;padding:.5rem 1.2rem;border-radius:99px;cursor:pointer}
#lens-r:checked~.switch label[for=lens-r],#lens-s:checked~.switch label[for=lens-s]{background:var(--ink);color:var(--paper)}
.lens{display:none}#lens-r:checked~.lens-r,#lens-s:checked~.lens-s{display:block}
.block{margin:2.4rem 0}
.bh{display:flex;align-items:baseline;gap:.9rem;flex-wrap:wrap;border-bottom:3px solid var(--accent,var(--ink));padding-bottom:.45rem;margin-bottom:1.1rem}
.bh h2{font:800 clamp(1.5rem,4vw,2.1rem)/1.1 var(--serif);margin:0}
.kick{font:800 .72rem var(--sans);letter-spacing:.2em;text-transform:uppercase;color:var(--accent)}
.count{margin-left:auto;font:600 .75rem var(--sans);color:var(--mute)}
.intro{font:400 .92rem var(--sans);color:var(--mute);margin:-.3rem 0 1rem}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,340px),1fr));gap:1.3rem;align-items:start}
.storyline{background:var(--card);border:1px solid var(--rule);border-top:4px solid var(--accent,var(--ink));border-radius:0 0 10px 10px;padding:1.1rem 1.2rem 1rem;min-width:0}
.storyline.feature{grid-column:1/-1}
.storyline.feature .lead .body{column-width:22rem;column-gap:2.2rem}
.storyline.feature .lead .body p:first-child:first-letter{font:900 3.6rem/.85 var(--serif);float:left;padding:.28rem .5rem 0 0;color:var(--accent)}
.chips{display:flex;flex-wrap:wrap;gap:.35rem;margin-bottom:.45rem}
.chip{font:700 .62rem var(--sans);letter-spacing:.09em;text-transform:uppercase;background:var(--chip);color:var(--mute);padding:.18rem .5rem;border-radius:4px}
.chip.alt{background:color-mix(in srgb,var(--accent,#888) 14%,var(--card));color:var(--accent,var(--mute))}
.chip.hot{background:var(--red);color:#fff}.chip.warn{background:#f59f00;color:#1d1b18}
.story h3{font:800 1.4rem/1.2 var(--serif);margin:.1rem 0 .3rem;letter-spacing:-.005em}
.story.lead h3{font-size:clamp(1.6rem,3.2vw,2.3rem);line-height:1.12}
.by{font:500 .76rem var(--sans);color:var(--mute);margin-bottom:.7rem}
.body p{margin:.0 0 .8rem}.body p.also{font:400 .85rem var(--sans);color:var(--mute)}
.cite{font:500 .8rem var(--sans);white-space:normal}a.src{font-weight:700;color:var(--accent,var(--red));text-decoration:none;border-bottom:1px solid currentColor}
.note{font:400 .72rem var(--sans);color:var(--mute);text-decoration:none}
.related{margin-top:.9rem;border-top:1px dashed var(--rule);padding-top:.6rem}
.related-h{font:800 .66rem var(--sans);letter-spacing:.16em;text-transform:uppercase;color:var(--mute);margin-bottom:.5rem}
.rel{padding:.6rem 0 .3rem .85rem;border-left:3px solid var(--accent,var(--rule));margin-bottom:.7rem}
.rel h4{font:700 1.04rem/1.25 var(--serif);margin:.05rem 0 .15rem}.rel p{font-size:.97rem;margin:.1rem 0 .55rem}
details.more summary,details.moresec summary,details.hl summary,.quiet summary{cursor:pointer;font:700 .8rem var(--sans);letter-spacing:.06em;text-transform:uppercase;color:var(--accent,var(--mute));margin:.2rem 0 .6rem}
details.moresec{margin-top:1.2rem}details.moresec[open] summary{margin-bottom:1rem}
details.hl{background:var(--card);border:1px solid var(--rule);border-radius:8px;padding:.3rem 1rem;margin-bottom:.6rem}
details.hl summary{display:flex;justify-content:space-between;padding:.5rem 0;color:var(--ink)}details.hl ul{margin:.2rem 0 .8rem;padding-left:1.1rem;font:400 .92rem/1.45 var(--sans)}details.hl li{margin:.35rem 0}
.quiet summary h2{display:inline;font:800 1.2rem var(--serif);text-transform:none;letter-spacing:0;color:var(--ink)}
.quiet{font:400 .9rem var(--sans)}.tw{overflow-x:auto}.quiet table{border-collapse:collapse;width:100%;font-size:.8rem}.quiet td,.quiet th{border-bottom:1px solid var(--rule);padding:.3rem .5rem;text-align:left}
.ok{color:var(--up)}.bad{color:var(--down)}.warnline{font:600 .85rem var(--sans);color:var(--down)}
.days{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,300px),1fr));gap:1rem;align-items:start}
.day{background:var(--card);border:1px solid var(--rule);border-radius:10px;overflow:hidden}
.dh{display:flex;align-items:baseline;gap:.5rem;padding:.6rem 1rem;background:var(--ink);color:var(--paper)}
.dn{font:900 1.9rem/1 var(--serif)}.dm{font:700 .8rem var(--sans);text-transform:uppercase;letter-spacing:.1em}.dw{margin-left:auto;font:600 .72rem var(--sans);letter-spacing:.1em;text-transform:uppercase;opacity:.8}
.day ul{list-style:none;margin:0;padding:.3rem 1rem .6rem}.day li{display:grid;grid-template-columns:4.2rem 1fr;gap:.6rem;padding:.65rem 0;border-top:1px solid var(--rule)}.day li:first-child{border-top:0}
.et{font:700 .74rem var(--sans);color:var(--mute);padding-top:.2rem}.ec{font:400 .92rem/1.45 var(--sans)}.ec strong{font:700 1rem/1.3 var(--serif)}.ex{color:var(--mute);margin:.15rem 0 .3rem}
footer.foot{margin:3rem 0 2.5rem;padding-top:1rem;border-top:3px double var(--ink);font:400 .8rem/1.55 var(--sans);color:var(--mute)}
@media print{nav.bar,.switch{display:none}.lens{display:block!important}.storyline{break-inside:avoid}}
"""

JS = ("document.addEventListener('click',function(e){var a=e.target.closest('a[data-lens]');if(!a)return;"
      "var r=document.getElementById(a.getAttribute('data-lens'));if(r)r.checked=true;});")


def _first_sentence(st: Story) -> str:
    for p in st.paras:
        if p.claims:
            return _sentence(p.claims[0])
    return ""


def render_magazine(now: datetime, stories: list[Story], unread: list, stats: dict, coverage: list, reasons: list, llm_note: str, llm_error: str,
                    llm, cal_note: str, cal: dict, cfg: dict, archive, **_ignored) -> str:
    lim = cfg["limits"]
    sector_order = list((cfg.get("sectors") or {}).keys())
    commodity = [s for s in stories if is_commodity(s) and s.section != "Official releases"]
    official = [s for s in stories if s.section == "Official releases"]
    general = [s for s in stories if s not in commodity and s not in official]

    all_clusters = cluster(stories)
    five = all_clusters[:5]
    lead_cluster = five[0] if five else None
    words = sum(len(" ".join(c for p in s.paras for c in p.claims).split()) for s in stories)
    mins = max(1, round(words / 230))

    parts: list[str] = []
    if llm_note:
        parts.append(f'<div class="alert"><strong>Read this first.</strong> {esc(llm_note)}</div>')

    if lead_cluster:
        five_li = "".join(f'<li><a class="t" href="#top-{i}">{esc(c.lead.headline)}</a><div class="s">{esc(_first_sentence(c.lead))}</div></li>'
                          for i, c in enumerate(five))
        hero = story_html(lead_cluster.lead, lead=True, sid="top-0")
        if lead_cluster.related:
            hero += '<div class="related"><div class="related-h">Related reporting</div>' + "".join(related_html(r) for r in lead_cluster.related) + "</div>"
        parts.append(f'<div class="front"><div class="storyline feature" style="--accent:var(--red);grid-column:auto">{hero}</div>'
                     f'<aside class="five"><h2>Today in five</h2><ol>{five_li}</ol></aside></div>')
    elif not llm_note:
        parts.append('<div class="alert">No story could be built from the sources in this window.</div>')

    rows, gnote = load_glance(ROOT_DASH(cfg), now)
    parts.append(markets_html(rows, gnote))

    if commodity:
        secs = by_commodity(commodity)
        inner = "".join(
            f'<h3 class="sub" style="--accent:{_accent_for(label)};font:800 1.15rem var(--serif);margin:1.4rem 0 .6rem;color:var(--accent)">{esc(label)}</h3>'
            f'<div class="grid">{"".join(cluster_html(c, feature=(i == 0 and len(cl) > 1), uid=f"cm-{_slug(label)}-{i}") for i, c in enumerate(cl))}</div>'
            for label, cl in secs)
        parts.append(f'<section class="block" id="commodities" style="--accent:#b8860b"><header class="bh"><span class="kick">The trading desk</span><h2>Energy and metals</h2>'
                     f'<span class="count">{len(commodity)} stories</span></header>{inner}</section>')

    reg = by_region(general)
    sec = by_sector(general, sector_order)
    r_html = "".join(section_html(f"r-{_slug(n)}", n, "Jurisdiction", cl, _accent_for(n), cap=int(lim.get("cluster_cap_per_section", 8))) for n, cl in reg)
    s_html = "".join(section_html(f"s-{_slug(n)}", n, "Sector", cl, _accent_for(n, i), cap=int(lim.get("cluster_cap_per_section", 8)), feature_first=False)
                     for i, (n, cl) in enumerate(sec))
    parts.append('<div class="lenses wrap-lens"><input type="radio" name="lens" id="lens-r" checked><input type="radio" name="lens" id="lens-s">'
                 '<div class="switch" role="tablist"><label for="lens-r">By jurisdiction</label><label for="lens-s">By sector</label></div>'
                 f'<div class="lens lens-r">{r_html or "<p class=intro>No stories in this window.</p>"}</div>'
                 f'<div class="lens lens-s">{s_html or "<p class=intro>No sector stories in this window.</p>"}</div></div>')

    if official:
        parts.append(section_html("official", "Official releases", "From the source", cluster(official), "#2b8a3e", feature_first=False))

    if cal_note:
        parts.append(f'<p class="intro">Calendar: {esc(cal_note)}</p>')
    parts.append(calendar_html(cal, now, int(lim.get("calendar_horizon_days", 21)), int(lim.get("calendar_review_max_age_days", 7))))
    parts.append(headlines_html(unread, archive, int(lim.get("in_brief_max", 450))))
    parts.append(coverage_html(stats, coverage, reasons, llm, llm_error))

    nav = ['<a href="#top-0">Front page</a>']
    if commodity:
        nav.append('<a href="#commodities">Energy &amp; metals</a>')
    nav.append('<a href="#lens-r" data-lens="lens-r">Jurisdictions</a>')
    for n, _ in reg[:6]:
        nav.append(f'<a href="#r-{_slug(n)}" data-lens="lens-r">{esc(n)}</a>')
    nav.append('<a href="#lens-s" data-lens="lens-s">Sectors</a>')
    nav += ['<a href="#week">Week ahead</a>', '<a href="#headlines">Headlines</a>', '<a href="#coverage">Coverage</a>']

    n_st = len(stories)
    n_src = len({p.source.url for s in stories for p in s.paras})
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>The Morning Edition · {now.astimezone(IST):%d %b %Y}</title>"
            "<link rel='preconnect' href='https://fonts.googleapis.com'><link rel='preconnect' href='https://fonts.gstatic.com' crossorigin>"
            "<link href='https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Playfair+Display:wght@700;800;900&family=Source+Serif+4:opsz,wght@8..60,400;8..60,600&display=swap' rel='stylesheet'>"
            f"<style>{CSS}</style></head><body>"
            f"<header class='wrap mast'><div class='top'><span>{now.astimezone(IST):%A %d %B %Y}</span><span>Edition for the 24 hours to {now.astimezone(IST):%H:%M} IST</span></div>"
            "<h1>The Morning Edition</h1><div class='rule'></div>"
            f"<div class='sub'>{n_st} {'story' if n_st == 1 else 'stories'} from {n_src} {'article' if n_src == 1 else 'articles'} · about {mins} min of reading · every sentence checked against its source article · information, not advice</div></header>"
            f"<nav class='bar'><div class='wrap'>{''.join(nav)}</div></nav>"
            f"<main class='wrap'>{''.join(parts)}"
            "<footer class='foot'>How this page is made: each sentence is a statement taken from the article linked after it. Code checked that the supporting words "
            "appear in that article and that every number in the sentence is present in it. Sentences are paraphrased, not copied. Nothing here is a recommendation to buy or sell.</footer>"
            f"</main><script>{JS}</script></body></html>")


def ROOT_DASH(cfg: dict) -> Path:
    return Path(__file__).resolve().parent.parent / cfg.get("dashboard_file", "data/dashboard_latest.json")
