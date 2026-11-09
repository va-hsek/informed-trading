"""Minimal HTTP layer (stdlib only). Secrets are redacted from every error message."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from core.secrets import redact

UA = "informed-trading/0.1 (personal research tool)"


class FetchError(RuntimeError):
    """Raised when a source cannot be fetched or its response is not what we expect."""


def get(url: str, params: Optional[list | dict] = None, headers: Optional[dict] = None,
        timeout: int = 30, retries: int = 2, backoff: float = 1.5, max_bytes: int = 20_000_000) -> bytes:
    if params:
        q = urllib.parse.urlencode(params, doseq=True, safe="$,")
        url = f"{url}{'&' if '?' in url else '?'}{q}"
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    last: Optional[str] = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise FetchError("response larger than the allowed size")
                return data
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code} for {redact(url)}"
            if e.code < 500 and e.code != 429:
                break                      # client errors are not retried
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = f"{type(e).__name__}: {redact(str(e))} for {redact(url)}"
        if attempt < retries:
            time.sleep(backoff ** (attempt + 1))
    raise FetchError(last or "unknown fetch failure")


def get_json(url: str, params=None, headers=None, **kw):
    raw = get(url, params, headers, **kw)
    try:
        return json.loads(raw)
    except Exception as e:  # noqa: BLE001
        raise FetchError(f"response was not JSON ({type(e).__name__}) for {redact(url)}") from e
