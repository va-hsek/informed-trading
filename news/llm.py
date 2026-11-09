"""Anthropic Messages API over plain HTTPS (no SDK). Logs token usage so real cost can be measured."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from collectors.http import FetchError
from core.secrets import redact

URL = "https://api.anthropic.com/v1/messages"


def _post(url: str, headers: dict, body: dict, timeout: int = 90) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    last = ""
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code < 500 and e.code != 429:
                break
        except Exception as e:  # noqa: BLE001
            last = type(e).__name__
        time.sleep(2 ** attempt)
    raise FetchError(f"Anthropic API call failed: {last}")


class LLM:
    def __init__(self, api_key: str, model: str, usage_log: Optional[Path] = None,
                 price_in: float = 0.0, price_out: float = 0.0, post: Callable = _post):
        self.api_key, self.model, self.post = api_key, model, post
        self.usage_log, self.price_in, self.price_out = usage_log, price_in, price_out
        self.tokens_in = self.tokens_out = 0

    def complete(self, system: str, user: str, max_tokens: int = 800) -> str:
        body = {"model": self.model, "max_tokens": max_tokens, "system": system,
                "messages": [{"role": "user", "content": user}]}
        headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        try:
            resp = self.post(URL, headers, body)
        except FetchError as e:
            raise FetchError(redact(str(e), {"k": self.api_key})) from e
        try:
            text = "".join(b.get("text", "") for b in resp["content"] if b.get("type") == "text")
            u = resp.get("usage", {})
            tin, tout = int(u.get("input_tokens", 0)), int(u.get("output_tokens", 0))
        except Exception as e:  # noqa: BLE001
            raise FetchError("Anthropic API returned an unexpected response shape") from e
        self.tokens_in += tin
        self.tokens_out += tout
        if self.usage_log:
            self.usage_log.parent.mkdir(parents=True, exist_ok=True)
            with open(self.usage_log, "a") as f:
                f.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "model": self.model,
                                    "in": tin, "out": tout}) + "\n")
        return text

    def complete_with_search(self, system: str, user: str, tool_type: str, max_uses: int = 12, max_tokens: int = 4000) -> str:
        """Same call with Anthropic's server-side web search tool. tool_type comes from config (check Anthropic's docs)."""
        body = {"model": self.model, "max_tokens": max_tokens, "system": system,
                "tools": [{"type": tool_type, "name": "web_search", "max_uses": max_uses}],
                "messages": [{"role": "user", "content": user}]}
        headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        try:
            resp = self.post(URL, headers, body, 180)
        except TypeError:
            resp = self.post(URL, headers, body)
        except FetchError as e:
            raise FetchError(redact(str(e), {"k": self.api_key})) from e
        text = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
        u = resp.get("usage", {})
        self.tokens_in += int(u.get("input_tokens", 0))
        self.tokens_out += int(u.get("output_tokens", 0))
        if self.usage_log:
            self.usage_log.parent.mkdir(parents=True, exist_ok=True)
            with open(self.usage_log, "a") as f:
                f.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "model": self.model,
                                    "in": int(u.get("input_tokens", 0)), "out": int(u.get("output_tokens", 0)), "search": True}) + "\n")
        return text

    def est_cost_usd(self) -> float:
        return self.tokens_in / 1e6 * self.price_in + self.tokens_out / 1e6 * self.price_out
