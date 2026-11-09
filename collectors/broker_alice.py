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

    def margin_one_lot(self, instrument, exchange: str, price: float, direction: str, lot_qty: float) -> float:
        """Margin Alice Blue asks for ONE lot, via its basket-margin call (needs a logged-in session)."""
        if self.client is None:
            raise BrokerError("not connected")
        order = {"exchange": exchange, "tradingSymbol": getattr(instrument, "symbol", ""), "price": str(price), "qty": str(int(lot_qty)),
                 "product": "NRML", "priceType": "L", "triggerPrice": "", "transType": "B" if direction == "long" else "S"}
        try:
            resp = self.client.basket_margin([order])
        except Exception as e:  # noqa: BLE001
            raise BrokerError(f"basket margin request failed: {type(e).__name__}") from e
        return parse_margin(resp)

    def index_history(self, search_text: str, start: datetime, end: datetime) -> pd.DataFrame:
        """Daily candles for an index (Nifty 50, Sensex, India VIX...). Exact match on the index name only."""
        if self.client is None:
            raise BrokerError("not connected")
        try:
            self.client.get_contract_master("INDICES")
            found = self.client.search_instruments("INDICES", search_text)
        except Exception as e:  # noqa: BLE001
            raise BrokerError(f"index lookup failed: {type(e).__name__}") from e
        want = search_text.strip().upper()
        exact = [i for i in (found or []) if str(getattr(i, "symbol", "")).strip().upper() == want or str(getattr(i, "name", "")).strip().upper() == want]
        if len(exact) != 1:
            near = sorted({str(getattr(i, "symbol", "")) for i in (found or [])})[:6]
            raise BrokerError(f"index '{search_text}': {len(exact)} exact matches in the INDICES master (need exactly 1); similar names: {near}")
        try:
            raw = self.client.get_historical(exact[0], start, end, "D", True)
        except Exception as e:  # noqa: BLE001
            raise BrokerError(f"index history request failed: {type(e).__name__}") from e
        return normalize_candles(raw)

    def resolve_symbol(self, exchange: str, symbol: str) -> Optional[dict]:
        """Does this exact trading symbol exist in the exchange's contract master? Returns {symbol, lot_size} or None."""
        if self.client is None:
            raise BrokerError("not connected")
        key = (exchange, symbol.upper())
        cache = self.__dict__.setdefault("_sym_cache", {})
        if key in cache:
            return cache[key]
        try:
            if exchange not in self.__dict__.setdefault("_masters", set()):
                self.client.get_contract_master(exchange)
                self._masters.add(exchange)
            found = self.client.search_instruments(exchange, symbol)
        except Exception as e:  # noqa: BLE001
            raise BrokerError(f"symbol lookup failed: {type(e).__name__}") from e
        hit = None
        for i in (found or []):
            names = {str(getattr(i, "symbol", "")).upper(), str(getattr(i, "name", "")).upper(), str(getattr(i, "symbol", "")).upper().removesuffix("-EQ")}
            if symbol.upper() in names:
                hit = {"symbol": str(getattr(i, "symbol", symbol)), "lot_size": self.lot_size(i)}
                break
        cache[key] = hit
        return hit

    def lot_size(self, instrument) -> Optional[float]:
        v = getattr(instrument, "lot_size", None)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None


def _num(x) -> Optional[float]:
    try:
        v = float(str(x).replace(",", "").strip())
        return v if v == v and abs(v) != float("inf") else None
    except (TypeError, ValueError):
        return None


def parse_margin(resp) -> float:
    """Margin needed for the order, from Alice Blue's basket-margin reply.

    Documented fields (v2api.aliceblueonline.com, orders management): preOrderMargin, postOrderMargin, currentOrderMargin,
    totalCashAvailable, rmsValidationCheck, fundShort, inside `result` (a dict or a one-item list). We use currentOrderMargin and
    cross-check it against postOrderMargin - preOrderMargin; if either is missing or they disagree by more than 2%, we refuse."""
    r = resp
    if isinstance(r, dict) and "result" in r:
        r = r["result"]
    if isinstance(r, list) and len(r) == 1:
        r = r[0]
    if not isinstance(r, dict):
        raise BrokerError("margin reply was not a dictionary")
    cur, pre, post = _num(r.get("currentOrderMargin")), _num(r.get("preOrderMargin")), _num(r.get("postOrderMargin"))
    if cur is None or cur <= 0:
        raise BrokerError(f"margin reply had no usable currentOrderMargin (fields seen: {sorted(r)[:12]})")
    if pre is not None and post is not None and abs((post - pre) - cur) > 0.02 * cur:
        raise BrokerError(f"margin reply is inconsistent: currentOrderMargin {cur} vs post-pre {post - pre}")
    return cur


MARGIN_MIN_PCT, MARGIN_MAX_PCT = 2.0, 50.0       # a futures margin outside 2-50% of contract value means the quantity unit is wrong


def plausible_margin(margin: float, price: float, multiplier: float) -> bool:
    value = price * multiplier
    return value > 0 and MARGIN_MIN_PCT <= margin / value * 100 <= MARGIN_MAX_PCT


def derive_spec(inst_cfg: dict, lot_size: Optional[float]) -> tuple[Optional[float], str]:
    """Contract multiplier from the broker's lot size. Accepted ONLY if lot_size is one of the values config/instruments.yaml
    lists in `lot_size_candidates` (the sizes the exchange has published); anything else is refused, never guessed."""
    cands = [float(x) for x in (inst_cfg.get("lot_size_candidates") or [])]
    if lot_size is None:
        return None, "Alice Blue gave no lot size for this contract"
    if not cands:
        return None, "no expected lot sizes listed in config/instruments.yaml, so the broker figure cannot be validated"
    if float(lot_size) not in cands:
        return None, f"Alice Blue lot size {lot_size:g} is not one of the expected {sorted(cands)}"
    upq = float(inst_cfg.get("units_per_quote", 1))
    return float(lot_size) / upq, f"lot size {lot_size:g} from the Alice Blue contract master (quoted per {upq:g} unit(s))"


def refresh_store(broker: AliceBroker, store, inst_key: str, exchange: str, symbol_hint: str,
                  today: date, days: int = 400, max_contracts: int = 3) -> dict:
    """Fetch history for the nearest live contracts and merge into the on-disk store."""
    picked = broker.contracts(exchange, symbol_hint, today)[:max_contracts]
    info = {"contracts": [], "lot_sizes": {}, "nearest": None}
    for sym, exp, ins in picked:
        df = broker.daily_history(ins, datetime.combine(today, datetime.min.time()) - timedelta(days=days),
                                  datetime.combine(today, datetime.min.time()) + timedelta(days=1))
        store.save_contract(inst_key, sym, exp, df)
        info["contracts"].append((sym, exp.isoformat(), len(df)))
        info["lot_sizes"][sym] = broker.lot_size(ins)
        if info["nearest"] is None and len(df):
            info["nearest"] = {"symbol": sym, "instrument": ins, "lot_size": broker.lot_size(ins), "last_close": float(df["close"].iloc[-1])}
    return info
