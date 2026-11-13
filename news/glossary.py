"""Plain-English meanings for terms a student reader may not know. A fixed dictionary, no model: a term is explained under a story only when the story's
headline or text uses it. Wording is deliberately general and non-advisory."""
from __future__ import annotations

import re

TERMS = {
    "repo rate": "The rate at which the RBI lends short-term money to banks. It steers loan and deposit rates.",
    "reverse repo": "The rate at which banks park spare cash with the RBI.",
    "policy rate": "The central bank's main interest rate; it guides all other rates in the economy.",
    "basis points": "One hundredth of a percentage point. 25 basis points = 0.25%.",
    "bps": "Basis points: one hundredth of a percentage point.",
    "fomc": "The US Federal Reserve committee that sets American interest rates.",
    "hawkish": "Leaning towards higher interest rates to fight inflation.",
    "dovish": "Leaning towards lower interest rates to support growth.",
    "yield": "The return on a bond. When bond prices fall, yields rise.",
    "treasury yield": "The return on US government bonds, a benchmark for global borrowing costs.",
    "backwardation": "When near-term futures cost more than later ones, often a sign of tight supply.",
    "contango": "When later futures cost more than near-term ones, often a sign of ample supply.",
    "brent": "The global benchmark price for crude oil, from the North Sea.",
    "wti": "West Texas Intermediate: the US benchmark price for crude oil.",
    "opec": "The group of oil-producing countries that coordinates how much oil it supplies.",
    "opec+": "OPEC plus allied producers such as Russia, who agree output limits together.",
    "henry hub": "The US benchmark price point for natural gas.",
    "lng": "Liquefied natural gas: gas cooled into a liquid so it can be shipped by sea.",
    "fii": "Foreign institutional investors: overseas funds buying or selling Indian shares.",
    "fpi": "Foreign portfolio investors: overseas investors in Indian shares and bonds.",
    "dii": "Domestic institutional investors: Indian mutual funds and insurers.",
    "gdp": "Gross domestic product: the total value of goods and services an economy produces.",
    "cpi": "Consumer price index: the usual measure of inflation, what households pay.",
    "pmi": "Purchasing managers' index: a survey where above 50 means expansion and below 50 contraction.",
    "iip": "Index of Industrial Production: India's measure of factory, mining and power output.",
    "wpi": "Wholesale price index: inflation measured at the wholesale level.",
    "current account deficit": "When a country pays more to the world for goods, services and income than it earns.",
    "fiscal deficit": "How much more a government spends than it earns, before borrowing.",
    "quantitative tightening": "A central bank shrinking its balance sheet, which takes money out of the system.",
    "quantitative easing": "A central bank buying bonds to add money to the system.",
    "nifty": "India's NSE index of 50 large companies.",
    "sensex": "India's BSE index of 30 large companies.",
    "bank nifty": "NSE index of India's largest banks.",
    "mcx": "Multi Commodity Exchange: where Indian traders buy and sell commodity futures.",
    "sebi": "India's securities market regulator.",
    "futures": "Contracts to buy or sell something at a set price on a future date.",
    "open interest": "The number of futures or options contracts still open, a gauge of participation.",
    "safe haven": "An asset, such as gold, that investors buy when they fear trouble elsewhere.",
    "tariff": "A tax a government puts on imported goods.",
    "basel": "International banking rules, set by the Basel Committee, on how much capital banks must hold.",
    "stagflation": "Rising prices combined with weak growth and high unemployment.",
    "yield curve": "A chart of bond yields by length of loan; when short rates exceed long rates it is called inverted.",
    "dollar index": "A measure of the US dollar against a basket of major currencies.",
}
_RX = {t: re.compile(r"(?<![A-Za-z])" + re.escape(t).replace(r"\ ", r"\s+") + r"(?![A-Za-z])", re.I) for t in TERMS}


def terms_in(text: str, limit: int = 3) -> list[tuple[str, str]]:
    """Longer terms first so 'bank nifty' wins over 'nifty'; each position explained once."""
    found, taken = [], []
    for t in sorted(TERMS, key=len, reverse=True):
        m = _RX[t].search(text)
        if m and not any(a <= m.start() < b for a, b in taken):
            found.append((m.start(), t))
            taken.append((m.start(), m.end()))
    found.sort()
    return [(t, TERMS[t]) for _, t in found[:limit]]
