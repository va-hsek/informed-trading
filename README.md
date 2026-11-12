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

### Page layout and diagnostics
The edition is a magazine page (`news/magazine.py`): lead story, "Today in X", market tiles, an Energy and metals desk, storylines grouped by jurisdiction or by sector
(switch at the top), a week-ahead timeline, collapsed headlines and a collapsed Coverage and quality panel.
Each run also writes `data/news/diagnostics.json` (model errors, top failure reasons, sample unreadable articles, failed feeds) and the pre-flight
`data/news/selftest.json`; both are committed, so a failed run can be diagnosed from the repo.

### Doctor (deep check)
Actions tab, **Doctor**, Run workflow. It fetches every feed, pushes the newest items of each through the full reading chain, runs the model on a sample, and saves `reports/doctor.html` and `data/news/doctor.json`.

### Model provider
Either a Google Gemini key or an Anthropic key works. The key goes in the GitHub secret `GEMINI_API_KEY` (or the older name `ANTHROPIC_API_KEY`); only keys starting `sk-ant-` are treated as Anthropic; any other key is Gemini.
Gemini uses `gemini-3.5-flash-lite` for reading and headline search (Google Search grounding), and `gemini-3.6-flash` for the weekly calendar. Models and prices are in `config/news_sources.yaml` under `llm.gemini`.

### Cost per run (Anthropic list prices, Nov 2026)
Haiku 5.5 extraction about $0.0006 per article (max about $0.45), headline search about $0.011 each (150 searches about $1.65), weekly calendar refresh about $0.05/day.
Typical $1-1.5/day, ceiling about $2.1/day. Actual spend is logged in `data/news_usage.jsonl` and printed in the run stats.

### Action report and Alice Blue
Run on your own machine (Alice Blue needs a daily login). Put `ALICEBLUE_USER_ID` and `ALICEBLUE_API_KEY` in `config/secrets.env`, `pip install pya3`.
It then fetches lot sizes (contract master), per-lot margin (basket margin), index history (INDICES master) and validates every symbol each run.
Anything unconfirmed is flagged and the report fails closed. No index CSVs are needed.
India 10-year yield: Alice Blue has no bond yield, so it comes from the ABSL AMC Daily Fixed Income Tracker PDF (single source, kept in `data/history/india_10y_benchmark.csv`).

Suggested schedule: GitHub Actions `.github/workflows/morning-edition.yml` (05:30 IST) and `calendar-refresh.yml` (weekly).

### Page structure
Every unit is a one-line headline that opens: sections, subsections, storylines and stories. Order: Today in X (as many stories as qualify: carried by 3+ outlets, never fewer than 3 or more than 15), Upcoming events, Event updates (official
releases and stories matching a recent calendar event), Energy and metals, News by jurisdiction, News by sector, What we missed, Coverage.

### Phone PDF and delivery
Email (any number of recipients): secrets `GMAIL_USER`, `GMAIL_APP_PASSWORD`, `EMAIL_TO` (comma-separated). The PDF is attached; recipients are blind-copied.
Each morning run makes an A5 PDF (`news/pdf.py`) and sends it with `news/deliver.py` to Telegram and/or WhatsApp, whichever secrets exist.
Secrets: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`; `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_TO`, optional `WHATSAPP_TEMPLATE`, `WHATSAPP_TEMPLATE_LANG`.
Nothing is sent when no stories were built.

### Newsletters
Add your newsletter under `newsletters:` in `config/news_sources.yaml` and set secrets `GMAIL_USER` and `GMAIL_APP_PASSWORD` (a Google app password). Its linked
headlines are read as extra items on top of the normal quotas (`limits.newsletter_reads`).


## Cost per run and the daily schedule
The Morning Edition runs every day at 05:30 IST (GitHub cron `0 0 * * *`; GitHub can start it up to ~15 minutes late).
Cost is almost all the model reading articles. With gemini-3.5-flash-lite (about $0.30 per million input tokens, $2.50 per million output) a normal run of roughly 450-780 readable articles
is estimated at **$0.8-1.8, about Rs 70-160** (at `llm.usd_inr`), so about Rs 2,000-5,000 a month. This is an estimate: each run now writes its real token use and cost
(in dollars and rupees) to `data/news/diagnostics.json` (`llm_use`) and to the Coverage panel; compare after the first healthy run. `limits.max_cost_usd_per_run` (2.5, about Rs 225) stops reading if a run goes over.

## What arrives in your email
Both the PDF (for the phone) and the HTML page (open it in a browser to expand and fold). Put several addresses in `EMAIL_TO`, separated by commas; they are sent as Bcc.
