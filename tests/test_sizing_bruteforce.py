"""Independent second check of the margin maths.

The production code uses a closed-form formula. Here we find the same price a
different way: equity = free_cash + P&L(price) is monotonic in price, so we
bisect for the price where equity equals the margin held. Different method,
same answer = verified.
"""
import random

from core.sizing import build_scenario


def bisect_margin_call(direction, entry, free, margin, qty):
    """Adverse price at which free + pnl == margin. None if never reached for price > 0."""
    sgn = 1 if direction == "long" else -1

    def equity(p):
        return free + sgn * (p - entry) * qty

    if direction == "long":
        lo, hi = 0.0, entry            # equity(lo) is the lowest equity on the adverse side
        if equity(lo) >= margin:
            return None
        for _ in range(200):
            mid = (lo + hi) / 2
            if equity(mid) < margin:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2
    lo, hi = entry, entry * 10_000
    if equity(hi) >= margin:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        if equity(mid) >= margin:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def test_closed_form_matches_bisection():
    random.seed(7)
    compared = 0
    for _ in range(200):
        direction = random.choice(["long", "short"])
        mult = random.choice([1, 5, 10, 100])
        entry = random.choice([500.0, 1000.0, 2500.0, 7000.0])
        lots = random.randint(1, 3)
        margin = random.uniform(2000, 40000)
        capital = random.uniform(margin * lots * 1.3, margin * lots * 6)
        stop = entry * (0.95 if direction == "long" else 1.05)
        s = build_scenario(direction, entry, stop, None, {"multiplier": mult}, margin, capital,
                           margin_buffer=0.25, lots=lots)
        qty = lots * mult
        ref = bisect_margin_call(direction, entry, capital, margin * lots, qty)
        ref_b = bisect_margin_call(direction, entry, capital, margin * lots * 1.25, qty)
        if ref is None:
            # long side: the price would have to go below zero -> formula must also be <= 0
            assert direction == "long" and s.margin_call_price <= 0
        else:
            assert abs(ref - s.margin_call_price) < 1e-6 * max(1.0, ref), (direction, entry, mult, lots, ref, s.margin_call_price)
            compared += 1
        if ref_b is None:
            assert direction == "long" and s.margin_call_price_buffered <= 0
        else:
            assert abs(ref_b - s.margin_call_price_buffered) < 1e-6 * max(1.0, ref_b)
    assert compared >= 80, "too few comparable cases; the check would be vacuous"


def test_loss_and_gain_signs_by_simulation():
    for direction, entry, stop, nxt in [("long", 1000, 960, 1080), ("short", 1000, 1040, 920)]:
        s = build_scenario(direction, entry, stop, nxt, {"multiplier": 7}, 10000, 100000)
        sgn = 1 if direction == "long" else -1
        assert s.loss_at_stop == -(sgn * (stop - entry) * 7) and s.loss_at_stop > 0
        assert s.gain_at_next_level == sgn * (nxt - entry) * 7 and s.gain_at_next_level > 0
