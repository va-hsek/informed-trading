"""Action report renderer (markdown). Deterministic: every sentence is built from
computed facts. No LLM. No buy/sell call; the decision is the user's.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from .agreement import Agreement
from .sizing import Scenario
from .signals import AssetView, rule_text
from .technical import TechView

DISCLAIMER = (
    "This report states facts and rule outputs. It is decision support, not a recommendation. "
    "Any call to trade is yours. Figures are estimates where marked; exchanges and brokers can "
    "change margins without notice."
)


def inr(x: Optional[float]) -> str:
    if x is None:
        return "n/a"
    sign = "-" if x < 0 else ""
    x = abs(x)
    s = f"{x:,.0f}"
    # Indian digit grouping
    digits = s.replace(",", "")
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts + [tail])
    return f"{sign}Rs {s}"


def px(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{x:,.2f}"


_SIGNED_UNITS = {"%", "pp", "mb", "bcf"}


def _today(value: float, unit: str) -> str:
    return f"{value:+.2f}{unit}" if unit.strip() in _SIGNED_UNITS else f"{value:.2f}{unit}"


def _age(hours: float) -> str:
    return f"{hours / 24:.1f} days old" if hours >= 48 else f"{hours:.0f} hours old"


def _trace_block(e, asset: str) -> list[str]:
    status = "TRIGGERED" if e.fired else "not triggered"
    head = f"**{e.driver_id} · {e.label}**" + (" (flag only)" if e.flag_only else f" (weight {e.weight:g}, horizon {e.horizon})")
    out = [head]
    out.append(f"- **Data point:** {e.data_point}. Data is {_age(e.age_hours)} [{e.source}].")
    if e.source_note:
        out.append(f"  - Note: {e.source_note}")
    for r in e.rules:
        eff = r["effect"]
        if r.get("flag"):
            then = f"flag: {r['flag']}"
        else:
            then = f"{asset} {'bullish' if eff > 0 else 'bearish'} (x{abs(eff):g})"
        mark = "  <- today" if r["fired"] else ""
        out.append(f"- **Rule:** IF {e.metric_phrase} {r['op']} {r['threshold']:g}{e.unit} THEN {then}{mark}")
    out.append(f"- **Today:** {e.metric_phrase} = {_today(e.value, e.unit)} -> {status}"
               + (f" (contribution {e.contribution:+.2f})" if e.fired and not e.flag_only else ""))
    for r in e.rules:
        if r["fired"] and r.get("why"):
            out.append(f"- **Rationale:** {r['why']}")
    return out


def _fund_section(v: AssetView) -> list[str]:
    out = [f"**Fundamental state: {v.state}**. {v.reason}"]
    if v.horizon:
        out.append(f"Shortest horizon among supporting drivers: **{v.horizon}**.")
    if v.evaluations:
        out.append("")
        out.append("Driver by driver (data point, then rule, then today's reading, then why it matters):")
        for e in v.evaluations:
            out.append("")
            out += _trace_block(e, v.asset)
    elif v.firings:
        out.append("")
        for f in v.firings:
            out.append(f"- {rule_text(f)}")
    if v.flags:
        out.append("")
        out.append("Flags (do not change direction):")
        out += [f"- {t}" for t in v.flags]
    if v.excluded:
        out.append("")
        out.append("Excluded from scoring (never guessed):")
        for c in v.excluded:
            out.append(f"- {c.driver_id} ({c.metric}): {c.reason}")
    return out


def _tech_section(t: Optional[TechView]) -> list[str]:
    if t is None:
        return ["Technical data unavailable or failed validation."]
    out = [
        f"Last close **{px(t.last_close)}**, ATR(14) **{px(t.atr)}**. Location: **{t.location}**"
        + (f" ({t.range_pos:.0%} of the way from support to resistance)." if t.range_pos is not None else ".")
    ]
    if t.support:
        out.append(f"- Nearest support: {px(t.support.price)} ({', '.join(t.support.tags)}); {px(t.support_dist_price)} away = {t.support_dist_atr:.2f} ATR.")
    if t.resistance:
        out.append(f"- Nearest resistance: {px(t.resistance.price)} ({', '.join(t.resistance.tags)}); {px(t.resistance_dist_price)} away = {t.resistance_dist_atr:.2f} ATR.")
    if t.profile:
        out.append(f"- Volume profile: POC {px(t.profile['poc'])}, value area {px(t.profile['val'])} to {px(t.profile['vah'])}.")
    mas = ", ".join(f"{k} {px(v)}" for k, v in t.ma.items() if v is not None)
    if mas:
        out.append(f"- Moving averages: {mas}.")
    out.append(f"- Volume: {t.volume_note}")
    out.append(
        f"- Stop zones (information, not an instruction): ATR-based long {px(t.stop_zone_long)} / short {px(t.stop_zone_short)}; "
        f"structure-based long {px(t.stop_structure_long)} / short {px(t.stop_structure_short)}."
    )
    for c in t.caution:
        out.append(f"- CAUTION: {c}")
    out += [f"- Note: {n}" for n in t.notes]
    return out


def _scenario_section(s: Optional[Scenario], reason_none: str) -> list[str]:
    if s is None:
        return [reason_none]
    L = []
    L.append(f"Scenario for a **{s.direction}** of {s.lots} lot(s) at {px(s.entry)} with stop {px(s.stop)} "
             f"({px(s.stop_distance)} points, {s.stop_distance_pct:.2f}% of entry). This is a scenario, not an instruction.")
    L.append("")
    L.append("| Item | Value |")
    L.append("|---|---|")
    L.append(f"| Margin per lot (broker figure) | {inr(s.margin_per_lot)} |")
    L.append(f"| Margin required | {inr(s.margin_required)} |")
    L.append(f"| Margin with safety buffer | {inr(s.margin_with_buffer)} |")
    L.append(f"| Free cash | {inr(s.free_cash)} |")
    if s.affordable:
        L.append("| Cash check | Enough for buffered margin plus the stop-out loss |")
    else:
        L.append(f"| Cash check | **Need {inr(s.need_additional_inr)} additional** (buffered margin + stop-out loss{'' if s.costs_included else ', before costs'}) |")
    L.append(f"| Loss if stop hits | {inr(-s.loss_at_stop)} |")
    L.append(f"| EOD cash if stop hits | {inr(s.eod_cash_stop)} |")
    if s.gain_at_next_level is not None:
        L.append(f"| Gain if price reaches {px(s.next_level)} (next level, a scenario not a target) | {inr(s.gain_at_next_level)} |")
        L.append(f"| EOD cash in that case | {inr(s.eod_cash_next)} |")
    L.append(f"| Margin-call price (broker margin) | {px(s.margin_call_price)} |")
    L.append(f"| Margin-call price (with buffer) | {px(s.margin_call_price_buffered)} |")
    if s.stop_before_margin_call is True:
        L.append("| Stop vs margin call | Stop triggers before the buffered margin-call level |")
    elif s.stop_before_margin_call is False:
        L.append("| Stop vs margin call | **Margin shortfall comes BEFORE the stop** |")
    L.append(f"| 10% move in underlying against you (to {px(s.ten_pct_move_price)}) | P&L {inr(s.ten_pct_move_pnl)}; EOD cash {inr(s.ten_pct_move_eod_cash)}{'; **margin breached**' if s.ten_pct_move_breaches_margin else ''} |")
    L.append(
        f"| 10% of capital lost ({inr(s.ten_pct_capital_loss_amount)}) | needs a {px(s.ten_pct_capital_loss_price_move)}-point move "
        f"({s.ten_pct_capital_loss_price_move_pct:.2f}% of entry), which is {s.ten_pct_capital_loss_vs_stop} |"
    )
    for n in s.notes:
        L.append(f"\nNote: {n}")
    L.append("\nMargin and costs are estimates; exchanges can raise margins around volatile events.")
    return L


def _can_go_wrong(f: AssetView, t: Optional[TechView], a: Agreement, s: Optional[Scenario]) -> list[str]:
    out = []
    out += [f"Conflict: {c}" for c in a.conflicts]
    out += [f"Flag: {x}" for x in f.flags]
    if f.down_weight and f.up_weight and f.state in ("UP", "DOWN"):
        opp = f.down_weight if f.state == "UP" else f.up_weight
        out.append(f"Opposing drivers carry weight {opp:.2f} against {max(f.up_weight, f.down_weight):.2f} supporting.")
    if f.excluded:
        out.append(f"{len(f.excluded)} driver(s) were excluded for stale/missing/unverified data, so the view rests on fewer inputs.")
    if s is not None and t is not None and s.stop_distance < t.atr:
        out.append(
            f"The scenario stop is only {s.stop_distance / t.atr:.2f} ATR from entry "
            f"({px(s.stop_distance)} points vs ATR {px(t.atr)}): ordinary daily volatility could reach it."
        )
    if s is not None and s.stop_before_margin_call is False:
        out.append("Margin shortfall begins before the stop level at current free cash.")
    if s is not None and s.ten_pct_move_breaches_margin:
        out.append("A 10% adverse move would breach margin at current free cash.")
    if not out:
        out.append("No specific conflicts flagged by the rules; markets can still move against any view.")
    return out


def render_asset(
    f: AssetView, t: Optional[TechView], a: Agreement, s: Optional[Scenario],
    instrument_label: str, scenario_note: str,
) -> str:
    L = [f"## {f.asset.upper()} ({instrument_label})", ""]
    L.append(f"**Agreement panel: {a.label}**")
    L.append("")
    L.append("### Fundamental")
    L += _fund_section(f)
    L.append("")
    L.append("### Technical")
    L += _tech_section(t)
    L.append("")
    L.append("### Where they agree and disagree")
    if a.supports:
        L += [f"- Agrees: {x}" for x in a.supports]
    if a.conflicts:
        L += [f"- DISAGREES: {x}" for x in a.conflicts]
    if a.resolution:
        L += [f"- Would help resolve it: {x}" for x in a.resolution]
    if not (a.supports or a.conflicts):
        L.append("- No directional comparison possible (see label above).")
    L.append("")
    L.append("### Size and margin scenario")
    L += _scenario_section(s, scenario_note)
    L.append("")
    L.append("### What could go wrong")
    L += [f"- {x}" for x in _can_go_wrong(f, t, a, s)]
    L.append("")
    L.append("### What to watch")
    L += [f"- {x}" for x in a.watch] or ["- Nothing specific."]
    ages = [fi.as_of for fi in f.firings]
    if f.firings and f.horizon:
        L.append(f"- Horizon of the view: {f.horizon}. Re-run when any supporting driver refreshes or goes stale.")
    return "\n".join(L)


def render_report(
    generated_at: datetime,
    asset_blocks: list[str],
    data_quality: list[str],
    events_warnings: list[str],
    summary_table: Optional[list[str]] = None,
    footer: Optional[list[str]] = None,
    cash_line: Optional[str] = None,
    extra_sections: Optional[list[str]] = None,
) -> str:
    L = [
        "# Action report",
        f"Generated: {generated_at.isoformat(timespec='minutes')}",
        "",
        f"> {DISCLAIMER}",
        "",
    ]
    if cash_line:
        L += [cash_line, ""]
    if summary_table:
        L += ["## At a glance", ""] + summary_table + [""]
    for sec in (extra_sections or []):
        L += [sec, ""]
    L.append("## Data quality")
    L += [f"- {x}" for x in data_quality] if data_quality else ["- All inputs used were fresh and verified."]
    L.append("")
    L.append("## Upcoming events")
    L += [f"- {x}" for x in events_warnings] if events_warnings else ["- No scheduled events loaded in the calendar (check config/events.yaml)."]
    L.append("")
    L.append("\n\n".join(asset_blocks))
    if footer:
        L += [""] + footer
    return "\n".join(L) + "\n"
