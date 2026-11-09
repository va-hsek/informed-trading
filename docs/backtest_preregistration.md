# Backtest pre-registration

Written BEFORE any backtest result exists. Once the first run happens, this file
is frozen; any later change must be added as a dated amendment at the bottom,
never edited in place.

## 1. What is and is not backtested

| Layer | Backtestable? | How it is validated |
|---|---|---|
| Numeric drivers (`config/drivers.yaml`) | Yes | Historical series, Cycle A / B below |
| Technical layer (levels, ATR, stop zones, volume profile) | Yes | Historical OHLCV |
| News-derived inputs (LLM extraction, news report) | **No.** Historical news retrieval is not free, and a model that "knows" past events leaks hindsight | Forward paper-trading only |

The report may only describe a confidence band as "calibrated" if the backtest
supports it (section 5). Otherwise it must say "uncalibrated".

## 2. Window and cycles

- Window: the last 5 full calendar years before the test date (data-availability permitting).
- **Cycle A:** January, March, May, July, September, November of each year.
- **Cycle B:** February, April, June, August, October, December of each year.
- Step 1: tune driver weights and thresholds on Cycle A only.
- Step 2: freeze every parameter. Run Cycle B once. Record results unedited.
- Step 3 (cross-check): repeat with roles swapped (tune on B, test on A).
- Known weakness: adjacent months share market regimes, so B is a soft out-of-sample test. Forward paper-trading is the real validation.
- Pre-COVID years are excluded by decision (not relevant to the present regime).

## 3. Rules that prevent fooling ourselves

1. Parameters tuned on one cycle are never changed after seeing the other cycle's results.
2. Only the parameters already listed in `drivers.yaml` and `user.yaml` may be tuned. No new rules are added after seeing results.
3. Every driver observation uses only data that was **published before** the decision timestamp (release-time aware). No look-ahead.
4. Futures history uses explicit roll rules (roll N days before expiry; N fixed in advance as 5 trading days). Back-adjustment method is recorded.
5. Costs are included: brokerage, exchange charges, STT/CTT, GST, slippage (fixed in advance as 1 tick each side, for commodities with a known tick size).
6. All trade counts are reported, including cases where the system said "no view".
7. Sparse-data months are reported as sparse, not dropped.

## 4. Metrics (fixed in advance)

For every call the system makes (direction, band, asset, horizon):

- Direction hit-rate: did price move in the called direction over the driver horizon?
- Adverse excursion (MAE): worst move against the call before the horizon ends.
- Favourable excursion (MFE): best move in favour before the horizon ends.
- Stop-out rate: how often the suggested ATR stop zone was touched before the move played out.
- No-view rate: share of days with "Fundamental neutral" / "Data degraded".
- For a hypothetical entry at the next open with the suggested stop and a trailing exit (rules fixed in section 6): net P&L after costs, expectancy per trade, profit factor, max drawdown, payoff ratio, trade count.

## 5. Calibration bands

Agreement scores are grouped into bands: 0.70-0.80, 0.80-0.90, 0.90-1.00.

A band is labelled **"calibrated >= 70%"** only if, in BOTH Cycle A and Cycle B:
- at least 30 independent calls fall in the band, and
- the direction hit-rate is at least 70%, and
- the lower end of a 95% Wilson interval for the hit-rate is reported next to it.

Otherwise the band is **uncalibrated**, and the report says so in plain words.

## 6. Hypothetical exit rules used for P&L simulation (fixed now)

- Entry: next session open after the call.
- Initial stop: entry -/+ 1.5 x ATR(14).
- Move stop to break-even after a favourable move of 1 x the initial stop distance.
- Trailing stop: 2 x ATR(14) from the best price after break-even.
- Time stop: close at the horizon end (intraday: session close; swing: 10 sessions).
- These are for measuring the signal's behaviour, not advice on how you must trade.

## 7. Data caveats to resolve before the first run

- How much intraday history Alice Blue provides free for MCX (to be checked). If shallow, intraday tests are limited and the backtest uses daily bars.
- Source of Brent curve, copper and rig count history (see `to_decide` entries in `drivers.yaml`).
- Release-time handling for each driver (publication timestamps).

## Amendments
(none yet)

### Amendment 1 (2026-11-07, before any run on real data)
- Daily bars only (section 7 allowed this). "Intraday" calls are measured as entry at the next open and exit at that session's close.
- "Positional" horizon had no length in section 6: fixed at 20 sessions (swing stays 10).
- Costs: not verified per instrument, so a single assumed round-trip cost in percent of price is used (`backtest_cost_roundtrip_pct` in user.yaml, default 0.10) and reported as an assumption. Gross expectancy is reported alongside.
- Exit simulation uses ATR(14) as of the decision day for both the initial stop and the trail (not recomputed). Stops are checked before being updated within a bar; a bar that could have hit both the stop and a favourable level counts the stop first; gaps fill at the open.
- Independent calls: after a call on an asset, later calls on that asset are ignored until its horizon has ended.
- Tuning: coordinate search over each verified driver's weight multiplier {0.5, 1, 1.5, 2} and threshold multiplier {0.75, 1, 1.25}; objective = Wilson lower bound of the independent hit-rate scaled by min(1, n/30); only strict improvements are accepted. Flag-only drivers and the confidence bar are not tuned.
- R11 (gas storage) is backtestable only if the user supplies eia_ngs.csv; R15 (manual duty input) is not backtestable.
