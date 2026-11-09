"""CFTC Disaggregated Futures-Only: managed-money net position as % of open interest."""
from __future__ import annotations

from datetime import date, datetime
from typing import Callable, Optional

from .http import FetchError, get_json

URL = "https://publicreporting.cftc.gov/resource/72hh-3qpy.json"
SELECT = ("report_date_as_yyyy_mm_dd,market_and_exchange_names,m_money_positions_long_all,"
          "m_money_positions_short_all,open_interest_all")


def parse_cot(rows: list, expect_name: str, code: str) -> list[tuple[date, float]]:
    """Returns (report date, managed-money net long as % of open interest)."""
    if not isinstance(rows, list) or not rows:
        raise FetchError(f"CFTC {code}: no rows returned")
    out = []
    for r in rows:
        try:
            name = str(r["market_and_exchange_names"]).upper()
            if expect_name.upper() not in name:
                raise FetchError(f"CFTC {code}: market name {name!r} does not contain {expect_name!r}")
            d = datetime.strptime(str(r["report_date_as_yyyy_mm_dd"])[:10], "%Y-%m-%d").date()
            lng = float(r["m_money_positions_long_all"])
            sht = float(r["m_money_positions_short_all"])
            oi = float(r["open_interest_all"])
        except FetchError:
            raise
        except Exception as e:  # noqa: BLE001
            raise FetchError(f"CFTC {code}: unexpected row schema ({type(e).__name__}: {e})") from e
        if oi <= 0 or lng < 0 or sht < 0 or lng > oi or sht > oi:
            raise FetchError(f"CFTC {code}: implausible positions on {d} (long {lng}, short {sht}, OI {oi})")
        out.append((d, (lng - sht) / oi * 100.0))
    return out


def fetch_cot(code: str, expect_name: str, app_token: Optional[str] = None, weeks: int = 200,
              http_json: Callable = get_json) -> list[tuple[date, float]]:
    params = [("cftc_contract_market_code", code), ("$select", SELECT),
              ("$order", "report_date_as_yyyy_mm_dd DESC"), ("$limit", str(weeks))]
    headers = {"X-App-Token": app_token} if app_token else None
    return parse_cot(http_json(URL, params, headers), expect_name, code)
