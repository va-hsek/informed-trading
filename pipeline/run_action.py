"""Action report: collect -> validate -> fundamentals -> technicals -> agreement -> sizing -> report.

    python3 -m pipeline.run_action                 # uses stored prices
    python3 -m pipeline.run_action --refresh-prices   # first pulls fresh candles from Alice Blue

Report-only. Nothing here places an order.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

import yaml

from collectors import registry
from collectors.broker_alice import AliceBroker, BrokerError, derive_spec, plausible_margin, refresh_store
from core import technical
from core.agreement import compare
from core import dashboard as dash
from core import newslink
from core.hedge import hedge_table, render_hedge
from core.prices import write_csv, PriceDataError, PriceStore, read_csv
from core.render_html import md_to_html
from core.report import inr, px, render_asset, render_report
from core.secrets import load_secrets
from core.signals import evaluate_asset
from core.sizing import SizingRefused, build_scenario
from core.validation import check_observation

IST = timezone(timedelta(hours=5, minutes=30))
ASSET_ORDER = ["crude", "natgas", "gold", "silver"]
SPEC_OK = {"confirmed", "cross_checked", "broker_confirmed"}
ROOT = Path(__file__).resolve().parent.parent


def _yaml(p: Path) -> dict:
    return yaml.safe_load(p.read_text()) or {}


def _as_date(x) -> Optional[date]:
    if x is None:
        return None
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, date):
        return x
    return date.fromisoformat(str(x))


def spec_problem(key: str, inst: dict) -> Optional[str]:
    m = inst.get("multiplier")
    if inst.get("spec_status") not in SPEC_OK:
        return (f"Contract spec for {key} is '{inst.get('spec_status')}' (lot: {inst.get('lot_text')}). Confirm the lot size and "
                f"quotation unit in your Alice Blue contract details, then set spec_status: confirmed in config/instruments.yaml.")
    if m is None or isinstance(m, bool) or not isinstance(m, (int, float)) or m <= 0:
        return f"Multiplier for {key} is not set in config/instruments.yaml."
    return None


def broker_margin(key: str, inst: dict, direction: str, state: dict, price: float) -> tuple[Optional[float], Optional[str], str]:
    """Margin for one lot straight from Alice Blue. Returns (margin, problem, note). Fails closed on anything implausible."""
    info = (state.get("nearest") or {}).get(key)
    br = state.get("broker")
    if br is None or not info:
        return None, "Alice Blue is not connected, so the live margin could not be read.", ""
    mult = inst.get("multiplier")
    lot = info.get("lot_size")
    if not mult or not lot:
        return None, "lot size or multiplier unknown, so the margin request cannot be built.", ""
    ck = (key, direction)
    if ck in state["cache"]:
        return state["cache"][ck]
    tried = []
    res = (None, "margin request did not return a plausible figure", "")
    for qty in (lot, 1):                                             # the API's quantity unit for MCX is not documented: try both, accept only a plausible one
        try:
            m = br.margin_one_lot(info["instrument"], inst["exchange"], price, direction, qty)
        except BrokerError as e:
            tried.append(str(e))
            continue
        if plausible_margin(m, price, float(mult)):
            note = (f"Margin per lot INR {m:,.0f} read from Alice Blue (basket-margin for {info['symbol']}, {direction}, quantity {qty:g}, "
                    f"price {price:g}); lot size {lot:g} from the contract master.")
            res = (m, None, note)
            break
        tried.append(f"quantity {qty:g} gave {m:,.0f}, outside 2-50% of contract value {price * float(mult):,.0f}")
    else:
        res = (None, "Alice Blue margin could not be used (" + "; ".join(tried)[:300] + "). Enter it in config/margins.yaml if you want sizing.", "")
    state["cache"][ck] = res
    return res


def margin_for(key: str, margins: dict, today: date, max_age: int) -> tuple[Optional[float], Optional[str]]:
    as_of = _as_date(margins.get("as_of"))
    v = (margins.get("margins_per_lot_inr") or {}).get(key)
    if v is None or as_of is None:
        return None, f"Margin per lot for {key} is not entered (config/margins.yaml: copy it from your Alice Blue margin calculator)."
    age = (today - as_of).days
    if age < 0:
        return None, "The margin date in config/margins.yaml is in the future."
    if age > max_age:
        return None, f"The margin figure for {key} is {age} days old (limit {max_age}). Update config/margins.yaml."
    return float(v), None


def pick_stop(direction: str, tech: technical.TechView, atr_mult: float) -> tuple[float, str]:
    """Use the farther of the ATR stop and the structure stop, so the stop is not inside normal noise."""
    if direction == "long":
        cands = [(tech.stop_zone_long, f"{atr_mult:g} ATR below price"), (tech.stop_structure_long, "just below the nearest support")]
        price, how = min((c for c in cands if c[0] is not None), key=lambda c: c[0])
    else:
        cands = [(tech.stop_zone_short, f"{atr_mult:g} ATR above price"), (tech.stop_structure_short, "just above the nearest resistance")]
        price, how = max((c for c in cands if c[0] is not None), key=lambda c: c[0])
    return price, how


def calendar_events(cal: dict) -> list[dict]:
    """Calendar entries that carry an exact time (`at`) and `affects_assets`, in the events.yaml shape."""
    out = []
    for e in (cal.get("events") or []):
        if e.get("at") and e.get("affects_assets") and e.get("verified") is not False:
            out.append({"name": e["title"], "at": e["at"], "affects": list(e["affects_assets"])})
    return out


def upcoming_events(events_cfg: dict, now: datetime, horizon_hours: float = 72) -> list[dict]:
    out = []
    for e in (events_cfg.get("events") or []):
        try:
            at = datetime.fromisoformat(str(e["at"]))
            if at.tzinfo is None:
                continue
        except Exception:  # noqa: BLE001
            continue
        hrs = (at - now).total_seconds() / 3600
        if -1 <= hrs <= horizon_hours:
            out.append({**e, "hours": hrs, "at_dt": at})
    out.sort(key=lambda e: e["hours"])
    return out


def _fmt_ist(dt: datetime) -> str:
    return dt.astimezone(IST).strftime("%d %b %Y, %H:%M IST")


def build_action_report(
    root: Path, now: datetime, secrets: dict,
    http_json: Optional[Callable] = None, http_raw: Optional[Callable] = None,
    refresh_prices: bool = False, broker_factory: Optional[Callable] = None,
) -> tuple[str, dict]:
    cfg = _yaml(root / "config" / "user.yaml")
    drivers = _yaml(root / "config" / "drivers.yaml")["drivers"]
    insts = _yaml(root / "config" / "instruments.yaml")
    margins = _yaml(root / "config" / "margins.yaml")
    events = _yaml(root / "config" / "events.yaml")
    cal_path = root / "config" / "calendar.yaml"
    if cal_path.exists():
        seen = {(str(e.get("name")), str(e.get("at"))) for e in (events.get("events") or [])}
        extra = [e for e in calendar_events(_yaml(cal_path)) if (e["name"], str(e["at"])) not in seen]
        events = {**events, "events": list(events.get("events") or []) + extra}
    store = PriceStore(root / cfg.get("data_dir", "data") / "prices")
    dq: list[str] = []

    capital = float(cfg["capital_inr"])
    free_cash = float(cfg["free_cash_inr"]) if cfg.get("free_cash_inr") is not None else capital

    # 1. fundamentals inputs
    obs, errors = registry.collect(drivers, secrets, now, root / "config" / "manual_inputs.yaml", http_json, http_raw)
    checked = {d["id"]: check_observation(d, obs.get(d["metric"]), now, fetch_error=errors.get(d["metric"])) for d in drivers}

    # 1b. India 10-year yield (stored daily so changes build up)
    try:
        from collectors import india_yield
        rec = india_yield.fetch_latest(now.date(), http_raw or __import__("collectors.http", fromlist=["get"]).get)
        india_yield.append_history(root / "data" / "history" / "india_10y_benchmark.csv", rec)
    except Exception as e:  # noqa: BLE001
        dq.append(f"India 10-year yield not refreshed ({type(e).__name__}: {str(e)[:160]}); a stored reading is shown only if recent.")

    # 2. prices
    bstate: dict = {"broker": None, "nearest": {}, "cache": {}}
    if refresh_prices:
        try:
            if not (secrets.get("ALICEBLUE_USER_ID") and secrets.get("ALICEBLUE_API_KEY")):
                raise BrokerError("ALICEBLUE_USER_ID / ALICEBLUE_API_KEY are not set in config/secrets.env")
            broker = AliceBroker(secrets["ALICEBLUE_USER_ID"], secrets["ALICEBLUE_API_KEY"], broker_factory)
            broker.connect()
            bstate["broker"] = broker
            for key, inst in insts["instruments"].items():
                try:
                    info = refresh_store(broker, store, key, inst["exchange"], inst["symbol_hint"], now.date(), cfg.get("price_history_days", 400))
                    if info.get("nearest"):
                        bstate["nearest"][key] = info["nearest"]
                        mult, why = derive_spec(inst, info["nearest"]["lot_size"])
                        old = inst.get("multiplier")
                        if mult is None:
                            dq.append(f"Contract spec for {key}: broker value not used ({why}).")
                        elif old is not None and abs(float(old) - mult) > 1e-9:
                            insts["instruments"][key] = {**inst, "spec_status": "conflicting", "multiplier": None,
                                                         "lot_text": f"CONFLICT: config says multiplier {old}, Alice Blue implies {mult:g}"}
                            dq.append(f"Contract spec for {key}: config multiplier {old} disagrees with Alice Blue ({mult:g}). Sizing refused until you resolve it.")
                        else:
                            insts["instruments"][key] = {**inst, "multiplier": mult, "spec_status": "broker_confirmed",
                                                         "lot_text": f"{info['nearest']['lot_size']:g} (Alice Blue contract master, {info['nearest']['symbol']})"}
                except Exception as e:  # noqa: BLE001
                    dq.append(f"Price refresh failed for {key}: {type(e).__name__}: {e}")
            for slug, text in (cfg.get("index_fetch") or {}).items():
                try:
                    df = broker.index_history(text, datetime.combine(now.date(), datetime.min.time()) - timedelta(days=int(cfg.get("price_history_days", 400))),
                                              datetime.combine(now.date(), datetime.min.time()) + timedelta(days=1))
                    write_csv(df, store.root / "index" / f"{slug}.csv")
                except Exception as e:  # noqa: BLE001
                    dq.append(f"Index {text}: not refreshed ({type(e).__name__}: {e}); stored data used if fresh enough.")
        except BrokerError as e:
            dq.append(f"Broker unavailable, stored prices used: {e}")

    blocks, rows = [], []
    all_excluded: dict[str, str] = {}
    meta = {"assets": {}}
    for asset in ASSET_ORDER:
        key, inst = next(((k, i) for k, i in insts["instruments"].items() if i["asset"] == asset), (None, None))
        if inst is None:
            continue
        fund = evaluate_asset(asset, drivers, checked, cfg)
        for c in fund.excluded:
            all_excluded.setdefault(c.driver_id, f"{c.driver_id} ({c.metric}): {c.reason}")

        # technicals
        tech, price_df = None, None
        try:
            df = store.continuous(key).tail(int(cfg.get("price_history_days", 400)))
            price_df = df
            age = (now.date() - df.index[-1].date()).days
            if age > int(cfg.get("max_price_age_days", 5)):
                raise PriceDataError(f"newest price bar is {age} days old ({df.index[-1].date()}); limit {cfg.get('max_price_age_days', 5)}")
            tech = technical.analyse(df, int(cfg["atr_period"]), float(cfg["stop_atr_multiple"]), float(cfg["caution_distance_atr"]))
        except (PriceDataError, technical.TechnicalDataError, ValueError) as e:
            dq.append(f"{asset}: technicals withheld: {e}")

        agree = compare(fund, tech, float(cfg.get("min_room_atr", 1.0)))

        # view expiry (earliest stale time among supporting drivers)
        if fund.state in ("UP", "DOWN"):
            sign = 1 if fund.state == "UP" else -1
            stale = {d["id"]: d["stale_after_hours"] for d in drivers}
            exp = []
            for f in fund.firings:
                if f.contribution * sign > 0:
                    exp.append((datetime.fromisoformat(f.as_of) + timedelta(hours=stale[f.driver_id]), f.driver_id))
            if exp:
                when, did = min(exp)
                agree.watch.append(f"This view rests on data that goes stale first at {_fmt_ist(when)} ({did}); "
                                   "re-run before then, or earlier if any supporting driver refreshes.")
        for e in upcoming_events(events, now):
            if asset in (e.get("affects") or []):
                agree.watch.append(f"EVENT: {e['name']} at {_fmt_ist(e['at_dt'])} ({e['hours']:.1f} hours away). Expect larger moves around it.")

        # sizing
        scenario, why_none, direction = None, "", None
        if fund.state in ("UP", "DOWN"):
            direction = "long" if fund.state == "UP" else "short"
            if tech is None:
                why_none = "Sizing withheld: technicals are unavailable, so there is no stop or entry reference."
            else:
                problem = spec_problem(key, inst)
                margin, mprob = margin_for(key, margins, now.date(), int(cfg.get("margin_max_age_days", 3)))
                bnote = ""
                if bstate["broker"] is not None:                       # the live broker figure wins over the hand-entered file
                    bm, bprob, bnote = broker_margin(key, inst, direction, bstate, tech.last_close)
                    if bm is not None:
                        margin, mprob = bm, None
                    elif margin is None and bprob:
                        mprob = bprob
                if problem:
                    why_none = "Sizing withheld: " + problem
                elif mprob:
                    why_none = "Sizing withheld: " + mprob
                else:
                    stop, how = pick_stop(direction, tech, float(cfg["stop_atr_multiple"]))
                    nxt = (tech.resistance.price if tech.resistance else None) if direction == "long" else (tech.support.price if tech.support else None)
                    try:
                        scenario = build_scenario(
                            direction, tech.last_close, stop, nxt, {"multiplier": inst["multiplier"]}, margin, capital, free_cash,
                            float(cfg["margin_buffer"]), 1, cfg.get("costs_roundtrip_per_lot"),
                            float(cfg["scenario_underlying_move_pct"]), float(cfg["scenario_capital_loss_pct"]))
                        scenario.notes.append(f"Stop used: {how} (the farther of the ATR and structure stops).")
                        if bnote:
                            scenario.notes.append(bnote)
                    except SizingRefused as e:
                        why_none = f"Sizing withheld: {e}"
        else:
            why_none = "No directional view, so no scenario is shown (the system does not offer the opposite side)."

        label = f"{inst['symbol_hint']}, {inst['quote_unit']}, lot {inst['lot_text']}"
        block = render_asset(fund, tech, agree, scenario, label, why_none)

        # hedge
        if fund.state in ("UP", "DOWN") and price_df is not None:
            eq = {}
            for sym in (insts.get("hedge_watchlist") or {}).get(asset, []):
                p = store.root / "equity" / f"{sym}.csv"
                if p.exists():
                    try:
                        eq[sym] = read_csv(p)
                    except PriceDataError as e:
                        dq.append(f"{sym}: equity history unreadable: {e}")
            block += "\n\n" + "\n".join(render_hedge(asset, direction, hedge_table(price_df, eq), 60))
        blocks.append(block)

        sizing_cell = "scenario shown" if scenario else ("withheld" if fund.state in ("UP", "DOWN") else "n/a")
        if scenario is not None and not scenario.affordable:
            sizing_cell = f"need {inr(scenario.need_additional_inr)} more"
        rows.append(f"| {asset} | {inst['symbol_hint']} | {fund.state} | {tech.location if tech else 'n/a'} | {agree.label} | {sizing_cell} |")
        meta["assets"][asset] = {"state": fund.state, "agreement": agree.label, "location": tech.location if tech else None,
                                 "scenario": scenario is not None}

    for c in checked.values():
        if c.ok and c.age_hours is not None and c.age_hours > 72:
            dq.append(f"{c.driver_id}: newest data point is {c.age_hours / 24:.1f} days old (published with a lag).")
    dq = sorted(all_excluded.values()) + dq

    ev = [f"{e['name']}: {_fmt_ist(e['at_dt'])} ({e['hours']:.1f} h) affects {', '.join(e.get('affects') or [])}" for e in upcoming_events(events, now)]
    summary = ["| Asset | Contract | Fundamental | Technical location | Agreement | Sizing |", "|---|---|---|---|---|---|"] + rows
    as_of_m = margins.get("as_of")
    cash = f"Capital {inr(capital)}, free cash {inr(free_cash)} (from config/user.yaml). Margins entered as of {as_of_m or 'NOT ENTERED'}."
    footer = [
        "## Parking idle cash (information, not advice)",
        "When no trade is open, idle money can sit in capital-stability instruments: overnight or liquid mutual funds, money-market funds, "
        "or an exchange-traded liquid ETF (for example LIQUIDBEES). What differs: how fast you can redeem, whether your broker accepts it "
        "as margin collateral and at what haircut (ask Alice Blue), and exit loads. The interest-rate trend is not automated yet, so this "
        "report gives no duration view.",
    ]
    extra: list[str] = []
    rows_d: list[dict] = []
    if (root / "config" / "dashboard.yaml").exists():
        try:
            rows_d = dash.build_rows(root, now, secrets, http_json, http_raw)
            extra.append("\n".join(["## Markets at a glance (energy, metals, FX, debt, equity)", ""] + dash.render_dashboard(rows_d)))
            dash.write_latest(rows_d, now, root / cfg.get("data_dir", "data") / "dashboard_latest.json")
            meta["dashboard_rows"] = sum(1 for r in rows_d if not r["problem"])
            for r in rows_d:
                if r["problem"]:
                    dq.append(f"Dashboard: {r['label']}: {r['problem']}")
        except Exception as e:  # noqa: BLE001
            extra.append(f"## Markets at a glance\n\nNot available: dashboard failed ({type(e).__name__}: {e}).")
    extra.append("\n".join(["## Where the news meets tradable instruments", ""] + newslink.load_and_render(root, now, rows_d, bstate['broker'].resolve_symbol if bstate['broker'] is not None else None)))
    md = render_report(now.astimezone(IST), blocks, dq, ev, summary, footer, cash, extra)
    return md, meta


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--refresh-prices", action="store_true", help="pull fresh candles from Alice Blue first")
    a = ap.parse_args(argv)
    now = datetime.now(IST)
    sec = load_secrets()
    md, meta = build_action_report(ROOT, now, sec, refresh_prices=a.refresh_prices or bool(sec.get('ALICEBLUE_USER_ID') and sec.get('ALICEBLUE_API_KEY')))
    out = ROOT / "reports"
    out.mkdir(exist_ok=True)
    stem = f"action_{now:%Y-%m-%d_%H%M}"
    (out / f"{stem}.html").write_text(md_to_html(md, f"Action report {now:%d %b %Y %H:%M}"))
    print(f"wrote reports/{stem}.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
