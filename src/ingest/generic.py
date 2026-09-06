"""Generic extraction for a broker the engine has never seen.

An unknown broker is not a failure. Column headers are matched against a synonym
dictionary, a confidence is computed, and whatever maps is normalised. Whatever
does not map becomes a REVIEW_REQUIRED item - never a rejected file.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import pandas as pd

# Canonical field -> header phrases seen in the wild. Order matters: the most
# specific phrase should come first so "date acquired" beats a bare "date".
SYNONYMS: dict[str, list[str]] = {
    "acquired_date": ["date acquired", "acquisition date", "allocation date",
                      "award date", "vest date", "vesting date", "grant date",
                      "original acquisition date", "purchase date", "entry date"],
    "sale_date": ["date sold", "date sold or transferred", "sale date",
                  "disposal date", "trade date", "settlement date"],
    "date": ["date", "transaction date", "as of date", "process date"],
    "symbol": ["symbol", "ticker", "security symbol", "option symbol"],
    "security_name": ["security name", "description", "security", "instrument",
                      "fund", "name of security", "company"],
    "quantity": ["quantity/par", "quantity", "shares", "no. of shares",
                 "number of shares", "units", "outstanding quantity",
                 "allocated quantity", "available quantity", "total shares",
                 "qty", "share quantity"],
    "price_fc": ["price per share", "price/rate per share", "strike price / cost basis",
                 "adjusted cost basis per share", "cost basis per share",
                 "vested price", "fair market value", "fmv", "market price",
                 "price per unit", "share price", "price"],
    "cost_total_fc": ["total cost basis", "cost basis", "adjusted cost basis",
                      "total cost", "book value"],
    "amount_fc": ["total proceeds", "proceeds", "gross proceeds", "amount",
                  "market value", "current value", "gross amount"],
    "tax_fc": ["federal tax withheld", "tax withheld", "nonresident alien withholding",
               "nra tax", "foreign tax withheld", "taxes withheld", "withholding"],
    "currency": ["currency", "ccy"],
    "cusip": ["cusip", "cusip number", "isin"],
    "account_no": ["account number", "account #", "a/c no", "participant number",
                   "user id", "account"],
    "plan": ["plan", "plan name", "contribution type", "stock source", "type of money"],
}

# Words that identify what a row IS, when there is a type column
EVENT_WORDS = {
    "VEST": ["vest", "release", "rsu", "award", "lapse", "distribution"],
    "BUY": ["purchase", "buy", "espp", "acquired", "reinvest", "contribution"],
    "SELL": ["sale", "sell", "sold", "redemption", "disposal"],
    "DIV": ["dividend", "distribution income", "income"],
    "SPLIT": ["split", "stock split"],
    "TRANSFER_IN": ["transfer in", "deposit", "journal in", "received"],
    "TRANSFER_OUT": ["transfer out", "withdrawal", "journal out", "delivered out"],
}

PLAN_TYPES = {
    # Order matters. A dividend-reinvestment row inside an ESPP plan mentions
    # both, so the more specific classification must be tested first.
    "DIVIDEND_REINVESTMENT": ["dividend share", "dividend reinvest", "drip"],
    "OPTION": ["nqo", "iso", "sar", "stock option", "option grant"],
    "ESPP": ["espp", "employee stock purchase", "purchase shares", "stock purchase"],
    "STOCK_AWARD": ["stock award", "rsa", "restricted stock award",
                    "performance award", "psu"],
    "RSU": ["rsu", "restricted stock unit", "restricted share", "release"],
}


@dataclass
class Mapping:
    """What a generic pass made of one table."""
    columns: dict[str, str] = field(default_factory=dict)   # canonical -> header
    confidence: float = 0.0
    unmapped: list[str] = field(default_factory=list)
    detected: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        """Enough to say something about a holding."""
        return ("quantity" in self.columns
                and ("acquired_date" in self.columns or "date" in self.columns
                     or "sale_date" in self.columns))


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 /]+", " ", str(s).lower()).strip()


# Abbreviations real statements use. Expanded before matching so a header like
# "Trade Dt" or "No. of Shs" still lands on the right canonical field.
ABBREV = {
    "dt": "date", "qty": "quantity", "amt": "amount", "no": "number",
    "shs": "shares", "sh": "shares", "px": "price", "val": "value",
    "ccy": "currency", "cur": "currency", "desc": "description",
    "acct": "account", "sec": "security", "avg": "average", "tot": "total",
}
# Tokens too common to identify a field on their own.
STOPWORDS = {"of", "the", "per", "in", "and", "usd", "inr", "number", "total", "s"}


def _tokens(s: str) -> set[str]:
    out = set()
    for w in _norm(s).replace("/", " ").split():
        w = ABBREV.get(w, w)
        if w and w not in STOPWORDS:
            out.add(w)
    return out


def map_columns(headers) -> Mapping:
    """Match table headers to canonical fields.

    Two passes. First an exact/substring match on normalised synonym phrases,
    most specific first. Then a token-overlap pass for headers an unfamiliar
    broker invented - "Trade Dt", "No. of Shs", "Unit Cost (USD)" - so an
    unseen layout degrades gracefully instead of extracting nothing.
    """
    norm = {h: _norm(h) for h in headers}
    taken: set[str] = set()
    cols: dict[str, str] = {}

    # Longer synonym phrases are more specific, so try them first across all fields.
    candidates = []
    for canon, phrases in SYNONYMS.items():
        for rank, phrase in enumerate(phrases):
            candidates.append((len(phrase), -rank, canon, _norm(phrase)))
    candidates.sort(reverse=True)

    for _, _, canon, phrase in candidates:
        if canon in cols:
            continue
        for h, n in norm.items():
            if h in taken:
                continue
            if n == phrase or n.startswith(phrase) or phrase in n:
                cols[canon] = h
                taken.add(h)
                break

    # ---- fuzzy pass: token overlap against every synonym for the field ----
    header_tokens = {h: _tokens(h) for h in headers if h not in taken}
    for canon, phrases in SYNONYMS.items():
        if canon in cols:
            continue
        want = set()
        for ph in phrases:
            want |= _tokens(ph)
        best, best_score = None, 0.0
        for h, toks in header_tokens.items():
            if h in taken or not toks:
                continue
            score = len(toks & want) / len(toks)
            if score > best_score:
                best, best_score = h, score
        if best and best_score >= 0.5:
            cols[canon] = best
            taken.add(best)

    unmapped = [h for h in headers if h not in taken]
    core = {"quantity", "price_fc", "acquired_date", "sale_date", "date",
            "amount_fc", "symbol", "security_name"}
    hit = len(core & set(cols))
    confidence = min(hit / 5.0, 1.0) if hit else 0.0
    return Mapping(columns=cols, confidence=round(confidence, 2),
                   unmapped=unmapped, detected=sorted(cols))


def classify_event(text: str, default: str = "VEST") -> str:
    t = _norm(text)
    for event, words in EVENT_WORDS.items():
        if any(w in t for w in words):
            return event
    return default


def classify_plan(text: str) -> str:
    t = _norm(text)
    for plan, words in PLAN_TYPES.items():
        if any(w in t for w in words):
            return plan
    return "UNKNOWN"


def to_number(v, on_invalid: str = "zero") -> float:
    """Parse a broker's idea of a number: $1,234.56, (123), 1 234,56, '-'.

    `on_invalid` controls ONLY the case where a value was genuinely PRESENT
    but failed to parse as a number - garbled text ("garbled$$"), a stray
    footnote ("N/A - see attached"), or something that merely looks numeric
    but isn't ("12.34.56"). The default, "zero", is the ONLY behaviour any
    existing caller has ever seen - this parameter is purely additive, and
    a caller that does not pass it is completely unaffected. Pass
    on_invalid="nan" at a specific, tax-consequential call site to get NaN
    instead, so a genuine parse failure can never be mistaken for a real
    zero by the code that consumes it.

    This is UNRELATED to a genuinely blank/absent value - None, NaN, pd.NA,
    an empty or whitespace-only string, a bare "-"/"--", or "n/a"/"not
    applicable" used as an explicit no-data marker. Those mean "nothing was
    supplied here", a different and deliberate convention from a parse
    failure, and they ALWAYS return 0.0 - in both modes, exactly as before.
    """
    invalid = float("nan") if on_invalid == "nan" else 0.0
    if v is None or (isinstance(v, float) and pd.isna(v)) or v is pd.NA:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s or s.lower() in {"-", "--", "n/a", "not applicable"}:
        return 0.0
    neg = s.startswith("(") and s.endswith(")")
    s = re.sub(r"[^0-9.\-]", "", s.replace(",", ""))
    if not s or s in {"-", "."}:
        return invalid
    try:
        f = float(s)
    except ValueError:
        return invalid
    return -f if neg else f


def to_date(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        ts = pd.to_datetime(v, dayfirst=False, errors="coerce")
        return None if pd.isna(ts) else ts.date()
    except Exception:
        return None
