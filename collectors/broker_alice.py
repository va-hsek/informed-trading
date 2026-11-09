"""Alice Blue adapter, written against the documented pya3 client:

    Aliceblue(user_id, api_key); get_session_id(); get_contract_master(exch);
    search_instruments(exch, text); get_historical(instrument, from, to, interval, indices);
    get_balance()

UNTESTED AGAINST THE LIVE BROKER (no network here). Everything it returns is validated; if
the response is not the shape we expect it raises BrokerError with the field names it saw,
and the report shows "price data unavailable" rather than guessing.

You must log in to Alice Blue (web/app) at least once a day before the API session works.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Callable, Optional

import pandas as pd

from core.prices import normalize_candles


class BrokerError(RuntimeError):
    pass


def _expiry(x) -> Optional[date]:
    if x is None or x == "":
        return None
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, date):
        return x
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d%b%Y", "%d-%b-%Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(str(x).strip(), fmt).date()
        except ValueError:
            continue
    return None


def pick_contracts(instruments: list, symbol_hint: str, today: date) -> list[tuple[str, date, Any]]:
    """Future contracts of `symbol_hint` that have not expired, soonest first. Excludes options."""
    out = []
    for ins in instruments:
        sym = str(getattr(ins, "symbol", "") or "")
        name = str(getattr(ins, "name", "") or "")
        exp = _expiry(getattr(ins, "expiry", None))
        if exp is None or exp < today:
            continue
        if not (name.upper() == symbol_hint.upper() or sym.upper().startswith(symbol_hint.upper())):
            continue
        up = sym.upper()
        if up.endswith(("CE", "PE")) or " CE" in up or " PE" in up:
            continue
        out.append((sym, exp, ins))
    out.sort(key=lambda t: t[1])
    seen, uniq = set(), []
    for t in out:
        if t[1] not in seen:
            seen.add(t[1])
            uniq.append(t)
    return uniq


class AliceBroker:
    def __init__(self, user_id: str, api_key: str, client_factory: Optional[Callable] = None):
        self.user_id, self.api_key = user_id, api_key
        self._factory = client_factory
        self.client = None

    def connect(self) -> None:
        try:
            if self._factory:
                self.client = self._factory(self.user_id, self.api_key)
            else:
                from pya3 import Aliceblue          # imported lazily: only needed on your machine
                self.client = Aliceblue(user_id=self.user_id, api_key=self.api_key)
            resp = self.client.get_session_id()
        except ImportError as e:
            raise BrokerError("pya3 is not installed (pip install pya3 on your machine)") from e
        except Exception as e:  # noqa: BLE001
            raise BrokerError(f"Alice Blue login failed: {type(e).__name__}") from e
        if isinstance(resp, dict) and (resp.get("stat") == "Not_ok" or "emsg" in resp):
            raise BrokerError(f"Alice Blue refused the session: {resp.get('emsg', 'unknown reason')}. "
                              "Log in once via web/app today, then retry.")

    def contracts(self, exchange: str, symbol_hint: str, today: date) -> list[tuple[str, date, Any]]:
        if self.client is None:
            raise BrokerError("not connected")
        try:
            self.client.get_contract_master(exchange)
            found = self.client.search_instruments(exchange, symbol_hint)
        except Exception as e:  # noqa: BLE001
            raise BrokerError(f"contract lookup failed: {type(e).__name__}") from e
        if not isinstance(found, list):
            raise BrokerError("contract lookup returned an unexpected type")
        picked = pick_contracts(found, symbol_hint, today)
        if not picked:
            raise BrokerError(f"no live futures contract found for {symbol_hint} on {exchange}")
        return picked

    def daily_history(self, instrument, start: datetime, end: datetime) -> pd.DataFrame:
        if self.client is None:
            raise BrokerError("not connected")
        try:
            raw = self.client.get_historical(instrument, start, end, "D", False)
        except Exception as e:  # noqa: BLE001
            raise BrokerError(f"history request failed: {type(e).__name__}") from e
        return normalize_candles(raw)

    def balance(self):
        if self.client is None:
            raise BrokerError("not connected")
        return self.client.get_balance()

    def lot_size(self, instrument) -> Optional[float]:
        v = getattr(instrument, "lot_size", None)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None


def refresh_store(broker: AliceBroker, store, inst_key: str, exchange: str, symbol_hint: str,
                  today: date, days: int = 400, max_contracts: int = 3) -> dict:
    """Fetch history for the nearest live contracts and merge into the on-disk store."""
    picked = broker.contracts(exchange, symbol_hint, today)[:max_contracts]
    info = {"contracts": [], "lot_sizes": {}}
    for sym, exp, ins in picked:
        df = broker.daily_history(ins, datetime.combine(today, datetime.min.time()) - timedelta(days=days),
                                  datetime.combine(today, datetime.min.time()) + timedelta(days=1))
        store.save_contract(inst_key, sym, exp, df)
        info["contracts"].append((sym, exp.isoformat(), len(df)))
        info["lot_sizes"][sym] = broker.lot_size(ins)
    return info
