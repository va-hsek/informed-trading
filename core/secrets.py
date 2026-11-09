"""Secrets loader. Reads config/secrets.env; real environment variables override it.

Values are never printed. Use redact() on any text that might contain one.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "secrets.env"


class MissingSecret(RuntimeError):
    pass


def load_secrets(path: Path | str | None = None, environ: dict | None = None) -> dict[str, str]:
    p = Path(path) if path else DEFAULT_PATH
    env = os.environ if environ is None else environ
    out: dict[str, str] = {}
    if p.exists():
        for raw in p.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and v:
                out[k] = v
    for k in list(out) + [k for k in env if k.isupper()]:
        if k in env and env[k]:
            out[k] = env[k]
    return out


def require(secrets: dict[str, str], names: Iterable[str]) -> None:
    missing = [n for n in names if not secrets.get(n)]
    if missing:
        raise MissingSecret(
            "Missing secret(s): " + ", ".join(missing) + ". Add them to config/secrets.env (names only shown here)."
        )


_PATTERN = re.compile(r"(?i)(api[_-]?key|token|secret|password|x-app-token|authorization)=?[:=]?\s*([^&\s\"']+)")


def redact(text: str, secrets: dict[str, str] | None = None) -> str:
    out = _PATTERN.sub(lambda m: f"{m.group(1)}=***", str(text))
    for v in (secrets or {}).values():
        if v and len(v) >= 6:
            out = out.replace(v, "***")
    return out
