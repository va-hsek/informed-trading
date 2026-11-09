# Informed trading system (report-only)

Fundamentals say direction. Technicals say where. The report states both and where
they disagree. The decision is yours. No order is ever placed by this code.

## Status

| Piece | State |
|---|---|
| `config/drivers.yaml` | Written. 13 drivers verified against provider docs (2026-11-07); 5 (R07, R08, R10, R12, R13) still `to_verify` and excluded. Thresholds UNTUNED until the backtest |
| `config/instruments.yaml`, `margins.yaml` | Silver Micro and Gold Ten cross-checked; crude and natgas mini lot sizes NOT confirmed (sizing withheld until you confirm in Alice Blue). Margins: you fill from the Alice Blue calculator |
| Fundamental engine, technicals, sizing, agreement panel, action report | Built, 100+ tests incl. independent bisection check of margin-call prices |
| Collectors (FRED, EIA, CFTC) | Built, tested with fakes. NOT yet run live (the build sandbox cannot reach these hosts): run `python3 -m pipeline.verify_sources` |
| Alice Blue price adapter | Written against the documented pya3 API. NOT tested live. You can instead drop continuous daily CSVs in `data/prices/` |
| News report ("The Morning Edition") | Newspaper layout: multi-paragraph stories built only from verified claims, each paragraph ending in a hyperlinked source name; sections for India, Asia-Pacific, Europe, Americas, Middle East, energy/metals, tech, deals; week-ahead calendar from `config/calendar.yaml` (warns when not reviewed for 7 days); markets-at-a-glance. Tested with fixture feeds and a scripted model. Feeds NOT yet run live: `python3 -m news.verify_feeds` |
| Action report extras | Market dashboard (`config/dashboard.yaml`: energy, metals, FX, US/India debt, equity; stale or missing rows show n/a) and a news-to-instrument table (`config/news_links.yaml`, exposure only, symbols to be confirmed in Alice Blue). Calendar events with exact times also raise warnings in the asset sections. Tested; NOT yet run against live FRED |
| Backtest engine (Cycle A/B, reverse, calibration, exits, costs) | Built, tested on synthetic worlds (no look-ahead test, null-world test). Needs history: `python3 -m backtest.fetch_history` + price CSVs |
| Telegram delivery, scheduling/hosting | NOT built yet (after you review the reports) |

## Setup
1. `pip install -r requirements.txt`
2. Fill `config/secrets.env` (git-ignored, chmod 600). It is the only place keys live; edit it whenever your account access changes.
3. Fill `config/margins.yaml` (with `as_of`), confirm crude/natgas lot sizes in `config/instruments.yaml`, add events to `config/events.yaml`.
4. Check sources: `python3 -m pipeline.verify_sources` and `python3 -m news.verify_feeds`.
5. Reports: `python3 -m pipeline.run_action` and `python3 -m news.run_news`.
6. Backtest: `python3 -m backtest.fetch_history`, add price CSVs under `data/history/prices/`, then `python3 -m backtest.run_backtest`.

## Run the tests
    python3 -m pytest -q

## See the report format (SYNTHETIC numbers only)
    python3 demo_synthetic.py   ->  reports/DEMO_synthetic_action_report.md

## Fail-closed rules (summary)
- A driver whose source is not `verified`, whose data is stale, missing, unparseable,
  or whose two independent extractions disagree is EXCLUDED and listed. Never guessed.
- A missing multiplier or margin makes sizing refuse. Never a default.
- Weak agreement, low coverage, or fewer than 2 firing drivers => "no directional view".
- Confidence is an AGREEMENT score. It is called "calibrated" only if the backtest supports it.


## Running it (always on the current time)
All reports are HTML only (`reports/*.html`). Each run reads whatever sources are live at that moment.

    python3 -m news.run_news          # The Morning Edition (refreshes the calendar first if it is >7 days old)
    python3 -m pipeline.run_action    # action report + market dashboard + news-to-instrument table
    python3 -m news.calendar_refresh  # weekly calendar refresh, by date (also runs automatically from the news report)

### News edition
- Window: only items published from 05:30 IST the previous day (or the most recent 05:30 anchor minus 24 h) up to the run. Undated items are dropped and counted.
- Caps (tripled): 780 articles read per run (India 270, Americas 270, Europe 210, APAC 60, Middle East 60, other 120). Spending stop: `max_cost_usd_per_run` (8.0).
- Reading chain: direct, Wayback, any Memento archive, archive.today (robots honoured, stops after 3 failures), partial text (>=150 chars) or feed summary,
  then headline search (web search finds the same story at another outlet; the code fetches it and checks headline overlap before using it, and attributes it to that outlet).
  Short pieces are kept.
- Sectors section: 13 sector groups, tagged by keyword from the sector feeds.
- Every claim must appear verbatim in the article text, with its numbers; otherwise it is dropped.

### Cost per run (Anthropic list prices, Nov 2026)
Haiku 5.5 extraction about $0.0006 per article (max about $0.45), headline search about $0.011 each (150 searches about $1.65), weekly calendar refresh about $0.05/day.
Typical $1-1.5/day, ceiling about $2.1/day. Actual spend is logged in `data/news_usage.jsonl` and printed in the run stats.

### Action report and Alice Blue
Run on your own machine (Alice Blue needs a daily login). Put `ALICEBLUE_USER_ID` and `ALICEBLUE_API_KEY` in `config/secrets.env`, `pip install pya3`.
It then fetches lot sizes (contract master), per-lot margin (basket margin), index history (INDICES master) and validates every symbol each run.
Anything unconfirmed is flagged and the report fails closed. No index CSVs are needed.
India 10-year yield: Alice Blue has no bond yield, so it comes from the ABSL AMC Daily Fixed Income Tracker PDF (single source, kept in `data/history/india_10y_benchmark.csv`).

Suggested schedule: GitHub Actions `.github/workflows/morning-edition.yml` (05:30 IST) and `calendar-refresh.yml` (weekly).
