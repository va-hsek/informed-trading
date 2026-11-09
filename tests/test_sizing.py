import pytest

from core.report import inr
from core.sizing import SizingRefused, build_scenario

SPEC = {"multiplier": 100}  # synthetic: Rs 100 per price point per lot


def long_case(**kw):
    args = dict(direction="long", entry=1000, stop=950, next_level=1100, spec=SPEC,
                margin_per_lot=15000, capital=20000, free_cash=None, margin_buffer=0.25)
    args.update(kw)
    return build_scenario(**args)


def test_long_hand_calculated_numbers():
    s = long_case()
    # qty = 1 lot * 100 = 100
    assert s.margin_required == 15000
    assert s.margin_with_buffer == 18750
    assert s.loss_at_stop == 5000          # 50 points * 100
    assert s.gain_at_next_level == 10000   # 100 points * 100
    # required cash = 18750 + 5000 = 23750; free 20000 -> need 3750
    assert s.need_additional_inr == 3750 and not s.affordable
    assert s.eod_cash_stop == 15000        # 20000 - 5000
    assert s.eod_cash_next == 30000        # 20000 + 10000
    assert s.margin_call_price == 950      # 1000 - (20000-15000)/100
    assert s.margin_call_price_buffered == 987.5   # 1000 - (20000-18750)/100
    assert s.stop_before_margin_call is False       # 950 is below 987.5: shortfall first
    # 10% adverse move: price 900, pnl -10000, eod 10000, margin 15000 breached
    assert s.ten_pct_move_price == 900 and s.ten_pct_move_pnl == -10000
    assert s.ten_pct_move_eod_cash == 10000 and s.ten_pct_move_breaches_margin
    # 10% of capital = 2000 -> 20 points -> 2% of entry, before the 50-point stop
    assert s.ten_pct_capital_loss_amount == 2000
    assert s.ten_pct_capital_loss_price_move == 20
    assert abs(s.ten_pct_capital_loss_price_move_pct - 2.0) < 1e-9
    assert s.ten_pct_capital_loss_vs_stop == "before stop"
    assert s.stop_distance == 50 and abs(s.stop_distance_pct - 5.0) < 1e-9


def test_short_mirror():
    s = build_scenario("short", 1000, 1050, 900, SPEC, 15000, 20000)
    assert s.loss_at_stop == 5000 and s.gain_at_next_level == 10000
    assert s.margin_call_price == 1050            # 1000 + 5000/100
    assert s.margin_call_price_buffered == 1012.5
    assert s.stop_before_margin_call is False     # 1050 above 1012.5
    assert s.ten_pct_move_price == 1100 and s.ten_pct_move_pnl == -10000


def test_enough_cash_and_stop_before_margin_call():
    s = long_case(capital=100000)
    assert s.need_additional_inr == 0 and s.affordable
    # buffered mc = 1000 - (100000-18750)/100 = 187.5 -> stop 950 triggers long before
    assert s.margin_call_price_buffered == 187.5 and s.stop_before_margin_call is True


def test_free_cash_less_than_capital():
    s = long_case(capital=100000, free_cash=20000)
    assert s.free_cash == 20000 and s.need_additional_inr == 3750
    # but 10% of CAPITAL (100000) = 10000 -> 100 points
    assert s.ten_pct_capital_loss_price_move == 100
    assert s.ten_pct_capital_loss_vs_stop == "after stop"


def test_free_cash_below_margin_has_no_margin_call_price():
    s = long_case(capital=10000)
    assert s.margin_call_price is None and s.margin_call_price_buffered is None
    assert s.stop_before_margin_call is False
    assert s.need_additional_inr == 23750 - 10000


def test_costs_included_change_numbers():
    s = long_case(costs_roundtrip_per_lot=200)
    assert s.costs_included
    assert s.need_additional_inr == 23950 - 20000
    assert s.eod_cash_stop == 20000 - 5000 - 200
    assert s.eod_cash_next == 20000 + 10000 - 200


def test_costs_missing_is_disclosed():
    s = long_case()
    assert not s.costs_included and any("NOT included" in n for n in s.notes)


def test_multiple_lots_scale_linearly():
    a, b = long_case(lots=1), long_case(lots=3)
    assert b.loss_at_stop == 3 * a.loss_at_stop
    assert b.margin_required == 3 * a.margin_required


def test_next_level_on_wrong_side_omits_gain():
    s = long_case(next_level=990)
    assert s.gain_at_next_level is None and s.eod_cash_next is None


@pytest.mark.parametrize("kw", [
    dict(spec={}), dict(spec={"multiplier": None}), dict(spec={"multiplier": 0}),
    dict(margin_per_lot=None), dict(stop=1000), dict(stop=1050), dict(direction="up"),
    dict(entry=0), dict(capital=0), dict(lots=0),
])
def test_refuses_bad_inputs(kw):
    with pytest.raises(SizingRefused):
        long_case(**kw)


def test_inr_indian_grouping():
    assert inr(1234567) == "Rs 12,34,567"
    assert inr(-3750) == "-Rs 3,750"
    assert inr(999) == "Rs 999"
    assert inr(100000) == "Rs 1,00,000"
    assert inr(None) == "n/a"
