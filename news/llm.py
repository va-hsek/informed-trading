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

import threading

URL = "https://api.anthropic.com/v1/messages"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_LOCK = threading.Lock()


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
                 price_in: float = 0.0, price_out: float = 0.0, post: Callable = _post, provider: Optional[str] = None):
        self.api_key, self.model, self.post = api_key, model, post
        self.provider = provider or detect_provider(api_key)
        self.usage_log, self.price_in, self.price_out = usage_log, price_in, price_out
        self.tokens_in = self.tokens_out = 0

    def complete(self, system: str, user: str, max_tokens: int = 800) -> str:
        if self.provider == "gemini":
            return self._gemini(system, user, max_tokens, search=False)
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
        with _LOCK:
            self.tokens_in += tin
            self.tokens_out += tout
        if self.usage_log:
            self.usage_log.parent.mkdir(parents=True, exist_ok=True)
            with open(self.usage_log, "a") as f:
                f.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "model": self.model,
                                    "in": tin, "out": tout}) + "\n")
        return text

    def complete_with_search(self, system: str, user: str, tool_type: str, max_uses: int = 12, max_tokens: int = 4000) -> str:
        """Same call with the provider's server-side web search (Anthropic web_search tool, or Gemini Google Search grounding)."""
        if self.provider == "gemini":
            return self._gemini(system, user, max_tokens, search=True)
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
        with _LOCK:
            self.search_count += int((u.get("server_tool_use") or {}).get("web_search_requests", 0))
        with _LOCK:
            self.tokens_in += int(u.get("input_tokens", 0))
            self.tokens_out += int(u.get("output_tokens", 0))
        if self.usage_log:
            self.usage_log.parent.mkdir(parents=True, exist_ok=True)
            with open(self.usage_log, "a") as f:
                f.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "model": self.model,
                                    "in": int(u.get("input_tokens", 0)), "out": int(u.get("output_tokens", 0)), "search": True}) + "\n")
        return text

    def _gemini(self, system: str, user: str, max_tokens: int, search: bool) -> str:
        """Gemini generateContent over plain HTTPS. Thinking tokens are billed as output, so they are counted and given headroom."""
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {"maxOutputTokens": int(max_tokens) + GEMINI_THINKING_HEADROOM}}
        if search:
            body["tools"] = [{"google_search": {}}]
        headers = {"x-goog-api-key": self.api_key, "content-type": "application/json"}
        url = GEMINI_URL.format(model=self.model)
        try:
            resp = self.post(url, headers, body, 180 if search else 90)
        except TypeError:
            resp = self.post(url, headers, body)
        except FetchError as e:
            raise FetchError(redact(str(e), {"k": self.api_key})) from e
        try:
            cands = resp.get("candidates") or []
            if not cands:
                why = (resp.get("promptFeedback") or {}).get("blockReason", "no candidates")
                raise FetchError(f"Gemini returned no answer ({why})")
            parts = (cands[0].get("content") or {}).get("parts") or []
            text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
            u = resp.get("usageMetadata") or {}
            tin = int(u.get("promptTokenCount", 0)) + int(u.get("toolUsePromptTokenCount", 0))
            tout = int(u.get("candidatesTokenCount", 0)) + int(u.get("thoughtsTokenCount", 0))
            nsearch = len((cands[0].get("groundingMetadata") or {}).get("webSearchQueries") or []) if search else 0
        except FetchError:
            raise
        except Exception as e:  # noqa: BLE001
            raise FetchError("Gemini API returned an unexpected response shape") from e
        with _LOCK:
            self.tokens_in += tin
            self.tokens_out += tout
            self.search_count += nsearch
        if self.usage_log:
            self.usage_log.parent.mkdir(parents=True, exist_ok=True)
            with open(self.usage_log, "a") as f:
                f.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "model": self.model, "in": tin, "out": tout,
                                    **({"search": nsearch} if search else {})}) + "\n")
        return text

    search_count = 0
    price_per_search = 0.01          # Anthropic: $10 per 1,000 searches. Gemini 3+: $14 per 1,000 after 5,000 free a month (set 0.014)

    def est_cost_usd(self) -> float:
        return (self.tokens_in / 1e6 * self.price_in + self.tokens_out / 1e6 * self.price_out
                + self.search_count * self.price_per_search)


GEMINI_THINKING_HEADROOM = 2000     # thinking tokens count inside the output limit; this keeps the visible answer from being cut off


def detect_provider(api_key: str) -> str:
    """Google API keys start with 'AIza'; anything else is treated as an Anthropic key."""
    return "gemini" if str(api_key).startswith("AIza") else "anthropic"


def pick_key(secrets: dict) -> str:
    """GEMINI_API_KEY wins; ANTHROPIC_API_KEY is also accepted (and may hold a Gemini key: the prefix decides the provider)."""
    return (secrets.get("GEMINI_API_KEY") or secrets.get("ANTHROPIC_API_KEY") or "").strip()


def model_settings(cfg: dict, provider: str, role: str) -> tuple:
    """(model, price_in, price_out) for role in 'extract' or 'calendar', from config/news_sources.yaml."""
    if provider == "gemini":
        g = (cfg.get("llm") or {}).get("gemini") or {}
        if role == "calendar":
            return g["calendar_model"], float(g["calendar_price_input_per_mtok"]), float(g["calendar_price_output_per_mtok"])
        return g["extract_model"], float(g["price_input_per_mtok"]), float(g["price_output_per_mtok"])
    if role == "calendar":
        c = cfg.get("calendar_refresh") or {}
        return c.get("model", "claude-sonnet-5-5"), float(c.get("price_input_per_mtok", 3.0)), float(c.get("price_output_per_mtok", 15.0))
    return cfg["llm"]["extract_model"], float(cfg["llm"]["price_input_per_mtok"]), float(cfg["llm"]["price_output_per_mtok"])


def make_llm(cfg: dict, secrets: dict, role: str, usage_log: Optional[Path]) -> Optional["LLM"]:
    key = pick_key(secrets)
    if not key:
        return None
    provider = detect_provider(key)
    model, pin, pout = model_settings(cfg, provider, role)
    llm = LLM(key, model, usage_log, pin, pout, provider=provider)
    if provider == "gemini":
        llm.price_per_search = float(((cfg.get("llm") or {}).get("gemini") or {}).get("price_per_search", 0.014))
    return llm
