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

Paywalled outlets (FT, WSJ, Bloomberg, NYT): direct page, then Wayback, then one polite archive.today request (robots.txt honoured,
CAPTCHA treated as a failure, stops after 3 failures in a row). Anything still unreadable is a headline with an archive link.
Suggested schedule (cron, IST machine): `30 5 * * * cd ~/informed-trading && python3 -m news.run_news`
