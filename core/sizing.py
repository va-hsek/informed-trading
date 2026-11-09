"""Sizing, margin and scenario arithmetic. Pure functions, no LLM, no guessing.

Money model (documented so it can be audited):
  * 'multiplier' = rupees of P&L per 1.00 price unit per 1 lot.
  * Variation margin is settled in cash: equity = free_cash + unrealised P&L.
  * A margin shortfall begins when equity < margin held for the position.
      long : price_call = entry - (free_cash - margin_held) / (lots * multiplier)
      short: price_call = entry + (free_cash - margin_held) / (lots * multiplier)
  * We report two margin-call levels: with the broker-quoted margin, and with
    the margin plus a safety buffer (config margin_buffer). Exchanges raise
    margins around volatile events, so these are ESTIMATES.
  * If a spec (multiplier, margin) is missing, we refuse. We never default.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


class SizingRefused(ValueError):
    pass


@dataclass
class Scenario:
    direction: str
    lots: int
    entry: float
    stop: float
    next_level: Optional[float]           # resistance for long, support for short (scenario, NOT a target)
    margin_per_lot: float
    margin_required: float                # lots * margin_per_lot
    margin_with_buffer: float
    free_cash: float
    need_additional_inr: float            # extra cash needed (0 if enough)
    affordable: bool
    loss_at_stop: float                   # positive number = loss
    gain_at_next_level: Optional[float]
    costs_included: bool
    eod_cash_stop: float
    eod_cash_next: Optional[float]
    margin_call_price: Optional[float]    # with broker margin
    margin_call_price_buffered: Optional[float]
    stop_before_margin_call: Optional[bool]   # stop triggers before buffered margin-call level
    stop_distance: float
    stop_distance_pct: float
    ten_pct_move_pnl: float               # P&L if underlying moves 10% AGAINST the position (negative)
    ten_pct_move_price: float
    ten_pct_move_eod_cash: float
    ten_pct_move_breaches_margin: bool
    ten_pct_capital_loss_amount: float
    ten_pct_capital_loss_price_move: Optional[float]   # price points against position that cost 10% of capital
    ten_pct_capital_loss_price_move_pct: Optional[float]
    ten_pct_capital_loss_vs_stop: Optional[str]        # 'before stop' / 'after stop'
    notes: list[str]


def _need(spec: dict, key: str) -> float:
    v = spec.get(key)
    if v is None or isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
        raise SizingRefused(f"instrument spec '{key}' is missing or not positive; refusing to size (fetch it live first)")
    return float(v)


def build_scenario(
    direction: str,
    entry: float,
    stop: float,
    next_level: Optional[float],
    spec: dict,
    margin_per_lot: Optional[float],
    capital: float,
    free_cash: Optional[float] = None,
    margin_buffer: float = 0.25,
    lots: int = 1,
    costs_roundtrip_per_lot: Optional[float] = None,
    underlying_move_pct: float = 10.0,
    capital_loss_pct: float = 10.0,
) -> Scenario:
    if direction not in ("long", "short"):
        raise SizingRefused("direction must be 'long' or 'short'")
    mult = _need(spec, "multiplier")
    if margin_per_lot is None or margin_per_lot <= 0:
        raise SizingRefused("broker margin per lot is missing; refusing to size")
    if entry <= 0 or lots < 1 or capital <= 0:
        raise SizingRefused("entry, lots and capital must be positive")
    sgn = 1 if direction == "long" else -1
    if sgn * (entry - stop) <= 0:
        raise SizingRefused("stop must be on the losing side of entry")

    free = capital if free_cash is None else free_cash
    notes: list[str] = []
    qty = lots * mult

    margin_req = lots * margin_per_lot
    margin_buf = margin_req * (1 + margin_buffer)
    loss_at_stop = sgn * (entry - stop) * qty            # positive
    gain_next = None
    if next_level is not None:
        g = sgn * (next_level - entry) * qty
        gain_next = g if g > 0 else None
        if gain_next is None:
            notes.append("Next level is not on the profitable side of entry; gain scenario omitted.")

    costs = None
    if costs_roundtrip_per_lot is not None:
        costs = costs_roundtrip_per_lot * lots
    else:
        notes.append("Brokerage/exchange costs are NOT included in these figures.")
    c = costs or 0.0

    # cash required to hold the position AND absorb the stop-out loss without shortfall
    required_cash = margin_buf + loss_at_stop + c
    need_add = max(0.0, required_cash - free)

    mc = entry - sgn * (free - margin_req) / qty if free >= margin_req else None
    mc_buf = entry - sgn * (free - margin_buf) / qty if free >= margin_buf else None
    stop_before = None
    if mc_buf is not None:
        stop_before = (stop > mc_buf) if direction == "long" else (stop < mc_buf)
    elif free < margin_buf:
        stop_before = False
        notes.append("Free cash is below the buffered margin: a margin shortfall exists before any price move.")

    # 10% underlying move against the position
    move_price = entry * (1 - sgn * underlying_move_pct / 100)
    ten_pnl = -(underlying_move_pct / 100) * entry * qty
    ten_eod = free + ten_pnl - c
    breach = (free + ten_pnl) < margin_req

    # price move (points) that costs capital_loss_pct of capital
    cap_loss_amt = capital * capital_loss_pct / 100
    cap_move = cap_loss_amt / qty
    cap_move_pct = cap_move / entry * 100
    stop_dist = abs(entry - stop)
    cap_vs_stop = "before stop" if cap_move < stop_dist else "after stop"
    if cap_move >= entry:
        notes.append("A capital loss of that size would need the price to fall to zero or beyond.")

    return Scenario(
        direction=direction, lots=lots, entry=entry, stop=stop, next_level=next_level,
        margin_per_lot=margin_per_lot, margin_required=margin_req, margin_with_buffer=margin_buf,
        free_cash=free, need_additional_inr=need_add, affordable=need_add <= 0,
        loss_at_stop=loss_at_stop, gain_at_next_level=gain_next, costs_included=costs is not None,
        eod_cash_stop=free - loss_at_stop - c,
        eod_cash_next=(free + gain_next - c) if gain_next is not None else None,
        margin_call_price=mc, margin_call_price_buffered=mc_buf, stop_before_margin_call=stop_before,
        stop_distance=stop_dist, stop_distance_pct=stop_dist / entry * 100,
        ten_pct_move_pnl=ten_pnl, ten_pct_move_price=move_price, ten_pct_move_eod_cash=ten_eod,
        ten_pct_move_breaches_margin=breach,
        ten_pct_capital_loss_amount=cap_loss_amt,
        ten_pct_capital_loss_price_move=cap_move, ten_pct_capital_loss_price_move_pct=cap_move_pct,
        ten_pct_capital_loss_vs_stop=cap_vs_stop,
        notes=notes,
    )
