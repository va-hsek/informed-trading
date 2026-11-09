# Source verification log

Checked on 2026-11-07 using the provider's own pages where reachable. This sandbox's
shell cannot reach any data host, so nothing below was fetched by the collectors
themselves. Every collector re-validates the response shape on each live run and
refuses (fail closed) if it differs.

## Verified (series exists, units and frequency confirmed from the provider page)

| Source | What | Confirmed facts | Caveat found |
|---|---|---|---|
| FRED `DTWEXBGS` | Nominal Broad U.S. Dollar Index | Daily, index Jan 2006=100, Federal Reserve H.10 | **Lags about a week**: page updated 2 Nov 2026 showed latest obs 30 Oct. Not the ICE DXY |
| FRED `DEXINUS` | INR per USD | Daily, Fed H.10, rupees per dollar | **Same weekly lag** (latest obs 30 Oct on 7 Nov) |
| FRED `DFII10` | 10Y TIPS (real) yield | Daily, percent | Page range showed through 5 Nov (about 1-2 day lag) |
| FRED `DGS2` | 2Y Treasury yield | Daily, percent, Fed H.15 | Latest obs 4 Nov |
| FRED `VIXCLS` | CBOE VIX | Daily close, index | FRED marks it copyrighted, citation required |
| EIA API v2, route `petroleum/stoc/wstk`, series `WCESTUS1` | Weekly US commercial crude stocks excl. SPR | Row fields `period, series, value, units`; units `MBBL`; value is a string | Documented by a third-party page, not eia.gov. Collector asserts series and units on every run |
| EIA `ir.eia.gov/ngs/wngsr.json` | Weekly natural gas storage report | Keyless; fields `release_date, current_week, 5yr_avg, series[...calculated{5yr-avg, net_change, pct-chg_5yr-avg}]`, Lower 48 in bcf | Fetched copy showed a 2024 report (cache), so freshness is enforced from `release_date` |
| CFTC Public Reporting, dataset `72hh-3qpy` (Disaggregated, futures only) | Managed-money positioning | Fields `report_date_as_yyyy_mm_dd, market_and_exchange_names, m_money_positions_long_all, m_money_positions_short_all, open_interest_all`. Codes: gold 088691, silver 084691, WTI 067651, nat gas 023651 | Gold's latest record was a week older than silver's at check time; staleness is enforced |
| RBI press-release RSS | Policy/auction notices | Valid RSS 2.0 | pubDate has no timezone, treated as IST |
| TechCrunch RSS | Tech headlines | Valid RSS 2.0, 20 items | none |
| pya3 (Alice Blue) | Python client | `Aliceblue(user_id, api_key)`, `get_session_id()`, `get_contract_master("MCX")`, `get_instrument_by_symbol`, `get_historical(instrument, from, to, interval, indices)` with interval `"D"` or `"1"`, `get_balance()` | PyPI docs only. Candle response format and any margin-per-lot endpoint are NOT documented, so the adapter validates the shape and margins come from `config/margins.yaml` |

## Contract specs (MCX)

| Contract | Lot | Quoted per | Tick | Status |
|---|---|---|---|---|
| Silver Micro | 1 kg | kg | Rs 1 | Two sources agree |
| Gold Ten | 10 g | 10 g | Rs 1 | Two sources agree on unit and tick; per-10g quotation from one |
| Crude Oil Mini | 10 barrels | barrel | Rs 1 | **One source, which says to confirm with MCX** |
| Natural Gas Mini | **250 or 2,500 mmBtu** | mmBtu | Rs 0.10 | **Sources conflict. Multiplier left empty** |

## NOT verified (kept `to_verify`, so excluded from scoring)
- Brent futures curve, copper, rig count, gold/silver ratio source, EIA natural-gas API v2 weekly route.
- News feeds other than RBI and TechCrunch (blocked or unreachable from the verification tool); `verify_feeds.py` checks them live.
- Axios: robots.txt disallows automated fetching, so it is used by headline link only.

## Addendum 2026-11-09: FRED series added for the market dashboard
Checked by opening each FRED series page; each exists with the stated units and frequency.

| Series | Title (as shown) | Units | Notes |
|---|---|---|---|
| DCOILBRENTEU | Crude Oil Prices: Brent - Europe (EIA) | $/bbl, daily | Lags about a week (latest 2026-11-03 when checked) |
| DCOILWTICO | WTI Cushing (EIA) | $/bbl, daily | Same lag |
| DHHNGSP | Henry Hub Natural Gas Spot Price (EIA) | $/MMBtu, daily | Same lag; spot, not the futures price |
| SP500 | S&P 500 (S&P DJI) | index, daily close | FRED keeps only 10 years of history |
| NASDAQCOM | NASDAQ Composite | index, daily close | Marked copyrighted by FRED; personal use only |
| DGS10 | 10-year Treasury yield (H.15) | %, daily | |
| T10Y2Y | 10y minus 2y yield | %, daily | |
| BAMLH0A0HYM2 | ICE BofA US High Yield OAS | %, daily | FRED keeps only 3 years since April 2026 |
| DFF | Effective federal funds rate | %, daily | 3.88 on 2026-11-05 |

Independent cross-checks against news pages on the same dates: Brent 107.03 on 3 Nov (matches a Super Macro Briefing figure);
S&P 500 7,353.73 on 5 Nov times +0.31% matches the 7,376.86 close on 6 Nov; NASDAQCOM 24,579.42 on 6 Nov matches The Closing Print exactly;
T10Y2Y 0.35 matches the 35 bp curve quoted by Investomine; DFF 3.88 sits inside the 3.75-4.00% target range.
Not checked in this pass: DJIA, DEXUSEU (not used).
