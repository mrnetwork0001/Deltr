"""deltr/nl_intent.py — deterministic natural-language intent grammar (no LLM branch).

``deltr_prompt(text)`` and the UI console pass free text through :func:`parse_intent`; the
result is an :class:`~deltr.models.Intent` that the engine turns into a *proposal only*.
Everything here is regex + a verb table, so the same text always yields the same intent
(replay determinism) and nothing here can ever change the run mode.

Grammar
* amounts   — ``$5,000`` · ``5k`` · ``2.5k`` · ``1m`` · ``5000 USDT`` · ``5000 USDC`` (USDC is
  accepted and treated as USDT-equivalent for sizing; ``stablecoin_note`` records it, decision 25).
  A bare number is NOT an amount when it is a leverage (``3x``), a bps/percent figure, or a
  position id fragment.
* leverage  — ``2x`` · ``3 x`` · ``leverage 2`` · ``2x leverage`` · ``at 2.5x``.
* verbs     — see :data:`VERB_TABLE` (first match in table order wins).
* stress    — kind from keywords (basis / equity / dex leg / funding flip / feed stale / reset),
  magnitude from the first number carrying ``bps`` / ``%`` (funding-flip bps → rate).
* kill      — ``magnitude`` 1.0 for "on"/"engage" (default), 0.0 for "off"/"disarm".
"""

from __future__ import annotations

import re
from typing import Optional

from deltr.models import Intent, StressKind, TraceSource

STABLECOIN_NOTE = "USDC treated as USDT-equivalent; no conversion leg in v1"

# --------------------------------------------------------------------------- #
# Regexes                                                                      #
# --------------------------------------------------------------------------- #
_NUM = r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
AMOUNT_RE = re.compile(
    r"(?<![\w.$-])(?P<dollar>\$\s*)?" + _NUM + r"\s*(?P<mult>[kKmM]\b)?"
    r"(?!\s*(?:[x×]\b|bps?\b|%|percent\b|leverage\b|settlements?\b|min\b|h\b|hours?\b))"
    r"(?:\s*(?P<coin>usdc|usdt|busd|usd|dollars?|bucks))?",
    re.IGNORECASE,
)
LEVERAGE_RE = re.compile(
    r"(?:(?<![\w.])(?P<a>\d+(?:\.\d+)?)\s*[x×](?![\w]))"
    r"|(?:\bleverage\s*(?:of|=|:|at|to)?\s*(?P<b>\d+(?:\.\d+)?))"
    r"|(?:(?<![\w.])(?P<c>\d+(?:\.\d+)?)\s*(?:x\s*)?leverage\b)",
    re.IGNORECASE,
)
MIN_EDGE_RE = re.compile(r"min(?:imum)?[\s_-]*edge[^-\d]*(?P<v>-?\d+(?:\.\d+)?)", re.IGNORECASE)
BPS_RE = re.compile(r"(?P<v>-?\d+(?:\.\d+)?)\s*(?P<unit>bps?\b|%|percent\b)", re.IGNORECASE)
RATE_RE = re.compile(r"(?P<v>-?0?\.\d+)(?![\d%])")
HORIZON_RE = re.compile(r"(?P<v>\d+(?:\.\d+)?)\s*(?:h\b|hours?\b|hr\b)", re.IGNORECASE)
POSITION_ID_RE = re.compile(r"\b(pos_[A-Za-z0-9]+)\b")
SYMBOL_RE = re.compile(r"\b(?P<sym>BNB|ETH|BTC|SOL|XRP|DOGE)(?:USDT|/USDT|-USDT)?\b", re.IGNORECASE)

# Verb table: (action, compiled pattern).  Order matters — the FIRST match wins, so the more
# specific phrasings ("reset halt", "set min edge") sit above the generic ones ("explain").
VERB_TABLE: list[tuple[str, re.Pattern[str]]] = [
    ("reset_halt", re.compile(r"\b(reset|clear|lift|release)\b.*\bhalt|\bresume\s+trading|\bun-?halt\b", re.I)),
    ("kill", re.compile(r"\bkill\b|\bemergency\s+stop\b|\bpanic\b|\bhalt\s+everything\b|\bstop\s+all\b", re.I)),
    ("set_min_edge", re.compile(r"\bmin(?:imum)?[\s_-]*edge\b.*\d|\bset\b.*\bedge\b.*\bto\b", re.I)),
    ("stress", re.compile(r"\bsimulat|\bstress|\bshock\b|\bwhat\s+if\b|\bscenario\b|\bdrill\b|\bflip\b.*\bfunding|\bfunding\b.*\bflip|\bstale\s+feed|\bfeed\s+stale|\bfail\b.*\bdex\b|\bdex\b.*\bfail", re.I)),
    ("unwind", re.compile(r"\bunwind|\bclose\b.*\b(all|every|position|everything|hedge|out)\b|\bflatten\b|\bexit\b.*\bposition|\bliquidate\b|\bclose\s+out\b", re.I)),
    ("rebalance", re.compile(r"\brebalanc|\ballocat|\bdeploy\b|\bput\b.*\binto\b|\bmove\b.*\binto\b|\binvest\b", re.I)),
    ("hedge", re.compile(r"\bhedge\b|\bopen\b.*\b(position|hedge|trade)\b|\benter\b|\bgo\s+delta[\s-]*neutral\b|\bshort\b.*\bperp|\bpropose\b|\bbuy\b.*\bshort\b", re.I)),
    ("scan", re.compile(r"\bscan\b|\blook\s+for\b|\bfind\b.*\bopportunit|\bsearch\b|\bany\s+opportunit|\bis\s+there\s+(an\s+)?(edge|opportunity)", re.I)),
    ("explain", re.compile(r"\bexplain\b|\bwhat(?:'s|\s+is)\s+the\s+edge\b|\bedge\b.*\bright\s+now\b|\bbreak\s*down\b|\bwhy\b|\bshow\b.*\bedge\b|\bcurrent\s+edge\b|\bhow\s+much\s+edge\b", re.I)),
    ("status", re.compile(r"\bstatus\b|\bhealth\b|\bhow\s+are\s+we\b|\breport\b|\bpositions?\b|\bpnl\b|\bequity\b|\bdrawdown\b|\bwhat(?:'s|\s+is)\s+(open|running)\b", re.I)),
]

_STRESS_KINDS: list[tuple[StressKind, re.Pattern[str]]] = [
    (StressKind.RESET, re.compile(r"\breset\b|\bclear\b|\bundo\b", re.I)),
    (StressKind.DEX_LEG_FAIL, re.compile(r"\bdex\b.*\bfail|\bfail.*\bdex\b|\bleg\s+fail|\bswap\s+fail|\brevert", re.I)),
    (StressKind.FUNDING_FLIP, re.compile(r"\bfunding\b.*\b(flip|negative|reverse|invert|turn)|\bnegative\s+funding\b|\bflip\b.*\bfunding\b", re.I)),
    (StressKind.FEED_STALE, re.compile(r"\bstale\b|\bfeed\b|\bfreeze\b|\boutage\b|\bdisconnect", re.I)),
    (StressKind.EQUITY_SHOCK, re.compile(r"\bequity\b|\bdrawdown\b|\bcapital\s+(loss|shock)\b|\blose\b", re.I)),
    (StressKind.BASIS_SHOCK, re.compile(r"\bbasis\b|\bspread\b|\bprice\b|\bmark\b", re.I)),
]


# --------------------------------------------------------------------------- #
# Component parsers                                                            #
# --------------------------------------------------------------------------- #
def _to_float(num: str) -> float:
    return float(num.replace(",", ""))


def parse_amount(text: str) -> tuple[Optional[float], Optional[str]]:
    """``(usd, stablecoin_note)`` for the first amount-like token; ``(None, None)`` if none."""
    for m in AMOUNT_RE.finditer(text):
        dollar, mult, coin = m.group("dollar"), m.group("mult"), m.group("coin")
        value = _to_float(m.group("num"))
        if mult:
            value *= 1_000.0 if mult.lower() == "k" else 1_000_000.0
        # A bare number (no $, no k/m, no coin) must be comma-grouped or ≥ 100 to count as money.
        if not dollar and not mult and not coin and "," not in m.group("num") and value < 100:
            continue
        note = STABLECOIN_NOTE if (coin and coin.lower() == "usdc") else None
        return value, note
    return None, None


def parse_leverage(text: str) -> Optional[float]:
    """``2x`` · ``3 x`` · ``leverage 2`` · ``2x leverage`` → float; None when absent."""
    m = LEVERAGE_RE.search(text)
    if not m:
        return None
    raw = m.group("a") or m.group("b") or m.group("c")
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def parse_symbol(text: str, default_symbol: str) -> str:
    m = SYMBOL_RE.search(text)
    return f"{m.group('sym').upper()}USDT" if m else default_symbol


def parse_min_edge(text: str) -> Optional[float]:
    m = MIN_EDGE_RE.search(text)
    return float(m.group("v")) if m else None


def parse_stress(text: str) -> tuple[Optional[StressKind], Optional[float]]:
    """(kind, magnitude).  bps for basis shock, percent for equity shock, rate for funding flip."""
    kind: Optional[StressKind] = None
    for k, pat in _STRESS_KINDS:
        if pat.search(text):
            kind = k
            break
    magnitude: Optional[float] = None
    m = BPS_RE.search(text)
    if m:
        magnitude = float(m.group("v"))
        if kind == StressKind.FUNDING_FLIP and m.group("unit").lower().startswith("bp"):
            magnitude = magnitude / 1e4
        elif kind == StressKind.FUNDING_FLIP:
            magnitude = magnitude / 100.0
    elif kind == StressKind.FUNDING_FLIP:
        r = RATE_RE.search(text)
        if r:
            magnitude = float(r.group("v"))
    if magnitude is None and kind not in (None, StressKind.RESET, StressKind.DEX_LEG_FAIL, StressKind.FEED_STALE):
        n = re.search(r"(?<![\w.])(-?\d+(?:\.\d+)?)(?![\w.])", text)
        if n:
            magnitude = float(n.group(1))
    return kind, magnitude


def _match_action(text: str) -> Optional[str]:
    for action, pat in VERB_TABLE:
        if pat.search(text):
            return action
    return None


# --------------------------------------------------------------------------- #
# Entry point                                                                  #
# --------------------------------------------------------------------------- #
def parse_intent(text: str, default_symbol: str = "BNBUSDT", source: TraceSource = TraceSource.UI) -> Intent:
    """Deterministic parse.  Unknown text → ``action="unknown"``, ``confidence=0``."""
    raw = text or ""
    t = raw.strip()
    symbol = parse_symbol(t, default_symbol)
    action = _match_action(t)
    capital, note = parse_amount(t)
    leverage = parse_leverage(t)
    confidence = 1.0

    if action is None:
        if capital is not None:
            action, confidence = "hedge", 0.7  # "$5,000 at 2x" — sizing words only
        else:
            return Intent(action="unknown", symbol=symbol, confidence=0.0, raw=raw, source=source)

    kw: dict = {"action": action, "symbol": symbol, "raw": raw, "source": source, "confidence": confidence}
    if action in ("rebalance", "hedge"):
        kw.update(capital_usd=capital, leverage=leverage, stablecoin_note=note)
        if capital is None:
            kw["confidence"] = 0.8  # engine falls back to Settings.capital_usd
    elif action == "unwind":
        pid = POSITION_ID_RE.search(t)
        kw["position_id"] = pid.group(1) if pid else "all"
    elif action == "stress":
        kind, magnitude = parse_stress(t)
        if kind is None:
            kind, kw["confidence"] = StressKind.BASIS_SHOCK, 0.6
        kw.update(stress_kind=kind, magnitude=magnitude)
    elif action == "set_min_edge":
        v = parse_min_edge(t)
        if v is None:
            m = BPS_RE.search(t) or re.search(r"(?<![\w.])(?P<v>-?\d+(?:\.\d+)?)(?![\w.])", t)
            v = float(m.group("v")) if m else None
        kw["min_edge_bps"] = v
        if v is None:
            kw["confidence"] = 0.5
    elif action == "kill":
        off = re.search(r"\b(off|disarm|disable|release|clear)\b", t, re.I) is not None
        kw["magnitude"] = 0.0 if off else 1.0
    elif action == "explain":
        kw.update(capital_usd=capital, leverage=leverage, stablecoin_note=note)
        h = HORIZON_RE.search(t)
        if h:
            kw["magnitude"] = float(h.group("v"))  # horizon hours for deltr_explain_edge
    return Intent(**kw)


__all__ = [
    "AMOUNT_RE", "LEVERAGE_RE", "MIN_EDGE_RE", "VERB_TABLE", "STABLECOIN_NOTE",
    "parse_amount", "parse_leverage", "parse_symbol", "parse_min_edge", "parse_stress", "parse_intent",
]
