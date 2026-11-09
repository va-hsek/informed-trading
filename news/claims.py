"""Claim extraction with a verification guard.

The model may only extract. Code then checks every claim against the article text:
  1. the supporting excerpt must appear WORD FOR WORD in the article (normalised for case/space/quotes)
  2. every number in the claim must appear in the article text
  3. the claim must not copy 15+ consecutive words from the article (we paraphrase, not reproduce)
A claim that fails any check is dropped and counted. The excerpt itself is used only for this
check and is never printed in the report.
"""
from __future__ import annotations

import json
import re
from typing import Callable

SYSTEM = (
    "You turn ONE news article into a readable, fact-only briefing for an Indian retail trader who wants to read the news here and not "
    "click through. Reply with JSON only, no prose.\n"
    'Schema: {"claims":[{"claim":"...","evidence":"..."}]}\n'
    "Rules:\n"
    "- Use ONLY the article text. No outside knowledge, no inference, no combining separate facts, no advice.\n"
    "- Write 4 to 8 claims for a substantial article (1 to 3 for a short one). Together they must read as a short, complete account in the "
    "order a reader needs it: what happened, who is involved, the key numbers, the stated reasons, reactions, and what happens next.\n"
    "- Each claim stands alone and is understood without the headline: name the company, person, country, index or commodity every time "
    "(never 'the company', 'it', 'the bank'). Include dates and the key numbers exactly as the article writes them: prices, percentages, "
    "quantities, currencies, units, and the time period they refer to.\n"
    "- One distinct fact per claim, at most 45 words, plain neutral wording in your own words (never copied). Attribute opinions, forecasts "
    "and allegations ('Goldman Sachs expects ...', 'officials said ...').\n"
    '- "evidence" must be an EXACT excerpt of 6 to 25 consecutive words copied character for character from the article '
    "that supports that claim.\n"
    "- Prefer facts about prices, supply and demand, policy decisions, central banks, rates, currencies, earnings, deals, inventories, "
    "regulation and data releases. Skip adverts, subscription prompts, author notes and commentary with no checkable fact.\n"
    '- If there is nothing usable return {"claims":[]}.'
)


def build_user(title: str, text: str, max_claims: int) -> str:
    return f"Extract at most {max_claims} claims.\n\nTITLE: {title}\n\nARTICLE:\n{text}"


def parse_claims(raw: str) -> list[dict]:
    i, j = raw.find("{"), raw.rfind("}")
    if i < 0 or j <= i:
        raise ValueError("no JSON object in model output")
    data = json.loads(raw[i: j + 1])
    cl = data.get("claims")
    if not isinstance(cl, list):
        raise ValueError("'claims' is not a list")
    return [c for c in cl if isinstance(c, dict) and isinstance(c.get("claim"), str) and isinstance(c.get("evidence"), str)]


def norm(s: str) -> str:
    s = (s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
         .replace("–", "-").replace("—", "-").replace("\xa0", " "))
    return re.sub(r"\s+", " ", s.lower()).strip()


_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def numbers(s: str) -> set[str]:
    out = set()
    for m in _NUM.findall(s):
        out.add(m.replace(",", "").rstrip("."))
    return out


def _words(s: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", norm(s))


def copies_source(claim: str, article: str, n: int = 15) -> bool:
    cw, aw = _words(claim), _words(article)
    if len(cw) < n:
        return False
    grams = {" ".join(aw[i: i + n]) for i in range(len(aw) - n + 1)}
    return any(" ".join(cw[i: i + n]) in grams for i in range(len(cw) - n + 1))


def verify_claim(c: dict, article: str) -> tuple[bool, str]:
    claim, ev = c["claim"].strip(), c["evidence"].strip()
    if not claim or len(claim.split()) > 60:
        return False, "claim empty or too long"
    ew = len(ev.split())
    if not 5 <= ew <= 40:
        return False, f"evidence length {ew} words is outside 5-40"
    na = norm(article)
    if norm(ev) not in na:
        return False, "evidence excerpt not found word for word in the article"
    missing = numbers(claim) - numbers(article)
    if missing:
        return False, f"number(s) {sorted(missing)} not present in the article"
    if copies_source(claim, article):
        return False, "claim copies 15+ consecutive words from the article"
    return True, "ok"


def extract_claims(llm, title: str, text: str, max_claims: int, retries: int = 1) -> tuple[list[str], list[tuple[str, str]], str]:
    """Returns (verified claim texts, [(dropped claim, reason)], error). error is '' on success."""
    err = ""
    for _ in range(retries + 1):
        try:
            kw = {"json_mode": True} if getattr(llm, "supports_json", False) else {}
            raw = llm.complete(SYSTEM, build_user(title, text, max_claims), max_tokens=1600, **kw)
            cands = parse_claims(raw)
            break
        except (ValueError, json.JSONDecodeError) as e:
            err = f"model output unusable: {e}"
        except Exception as e:  # noqa: BLE001
            return [], [], f"{type(e).__name__}: {e}"
    else:
        return [], [], err
    ok, dropped = [], []
    for c in cands[:max_claims]:
        good, why = verify_claim(c, text)
        (ok.append(c["claim"].strip()) if good else dropped.append((c["claim"].strip(), why)))
    return ok, dropped, ""
