"""Document identifier and extraction orchestrator.

    file -> identify -> (profile | generic) -> normalised events + source record

An unknown broker never fails the run. It falls through to the generic pass, and
whatever cannot be mapped is raised for review with the confidence attached.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from . import generic as G
from . import tabular
from .profiles import (
    BROKER_STATEMENT, FORM_1042S, PORTFOLIO_HOLDINGS, Profile, TTBR_TABLE,
    TRANSACTION_SUMMARY, UNKNOWN, load_profiles,
)

MONTHS = ("january|february|march|april|may|june|july|august|september|"
          "october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec")

# "From Jan-01-2025 to Dec-31-2025", and the same without the leading "from",
# which is how the period usually appears in a filename.
RE_FROM_TO = re.compile(
    rf"(?:from\s+)?({MONTHS})[-\s](\d{{1,2}})[-,\s]+(\d{{4}})\s+to\s+({MONTHS})[-\s](\d{{1,2}})[-,\s]+(\d{{4}})")
# "January 1, 2026 - March 31, 2026"  /  "January 1- March 31, 2025"
RE_RANGE = re.compile(
    rf"({MONTHS})\s+(\d{{1,2}})(?:,\s*(\d{{4}}))?\s*[-–]\s*({MONTHS})\s+(\d{{1,2}}),?\s*(\d{{4}})")
# "December 1-31, 2025"
RE_SAME_MONTH = re.compile(rf"({MONTHS})\s+(\d{{1,2}})\s*[-–]\s*(\d{{1,2}}),?\s*(\d{{4}})")
RE_TAX_YEAR = re.compile(r"tax year\s+(\d{4})")

M2N = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _mon(s: str) -> int:
    return M2N.get(s[:3].lower(), 0)


@dataclass
class SourceRecord:
    """One uploaded file, what it was taken to be, and what it covers."""
    file: str
    kind: str
    document_type: str
    profile_id: str
    broker: str
    confidence: float
    period_start: dt.date | None = None
    period_end: dt.date | None = None
    securities: list[str] = field(default_factory=list)
    rows: int = 0
    notes: str = ""
    reports_dividends: bool = True   # does this document type carry dividend income?
    holdings: list = field(default_factory=list)   # broker's own lot snapshot
    cash: list = field(default_factory=list)      # custodial balances for FA-A2
    transfers: list = field(default_factory=list)  # shares moving between accounts
    form1042s: list = field(default_factory=list)  # 1042-S records read from a form
    content_hash: str = ""                        # for duplicate-upload detection
    closed_lots: list = field(default_factory=list)   # complete realised-gain records
    control_total: dict | None = None   # a stated summary line, for cross-checking
    recon_breaks: list = field(default_factory=list)  # rows failing the report's own
                                                      # arithmetic
    cg_authority: bool = False   # this document is the capital-gains source of
                                 # record for its broker over the period it covers
    google_fx: object = None     # a returned Google Finance FX fallback table
    exercises: list = field(default_factory=list)       # option exercises stated
    unvested_awards: list = field(default_factory=list)  # disclosed, never held
    invalid_quantities: list = field(default_factory=list)  # present-but-unparseable
                                                             # quantity/shares, excluded
                                                             # from every downstream figure

    @property
    def coverage(self) -> str:
        if self.period_start and self.period_end:
            return f"{self.period_start:%d-%m-%Y} to {self.period_end:%d-%m-%Y}"
        return "not determined"


@dataclass
class ExtractResult:
    events: pd.DataFrame
    sources: list[SourceRecord] = field(default_factory=list)
    ttbr: pd.DataFrame = None
    form1042s: pd.DataFrame = None
    unmapped: list[tuple[str, list[str]]] = field(default_factory=list)


# ----------------------------------------------------------------------
def detect_period(text: str) -> tuple[dt.date | None, dt.date | None]:
    """Best-effort reporting period from a statement's own wording."""
    t = text.lower()
    m = RE_FROM_TO.search(t)
    if m:
        return (dt.date(int(m.group(3)), _mon(m.group(1)), int(m.group(2))),
                dt.date(int(m.group(6)), _mon(m.group(4)), int(m.group(5))))
    m = RE_SAME_MONTH.search(t)
    if m:
        y, mo = int(m.group(4)), _mon(m.group(1))
        return dt.date(y, mo, int(m.group(2))), dt.date(y, mo, int(m.group(3)))
    m = RE_RANGE.search(t)
    if m:
        y2 = int(m.group(6))
        y1 = int(m.group(3)) if m.group(3) else y2
        try:
            return (dt.date(y1, _mon(m.group(1)), int(m.group(2))),
                    dt.date(y2, _mon(m.group(4)), int(m.group(5))))
        except ValueError:
            pass
    m = RE_TAX_YEAR.search(t)
    if m:
        y = int(m.group(1))
        return dt.date(y, 1, 1), dt.date(y, 12, 31)
    return None, None


RE_STMT_MONTH = re.compile(
    rf"^\s{{6,}}({MONTHS})\s+(20\d{{2}})\s*$", re.I | re.M)


def detect_period_span(doc) -> tuple[dt.date | None, dt.date | None]:
    """The whole span a COMBINED document covers, not just its first statement.

    A broker may return several monthly statements in one download. Taking the
    first month as the period would understate coverage badly - and coverage is
    what the dividend precedence rule turns on.
    """
    seen = []
    for line in (doc.lines or []):
        m = RE_STMT_MONTH.match(line)
        if m:
            mo, yr = _mon(m.group(1)), int(m.group(2))
            if mo:
                seen.append((yr, mo))
    if not seen:
        return None, None
    (y1, m1), (y2, m2) = min(seen), max(seen)
    start = dt.date(y1, m1, 1)
    nxt = dt.date(y2 + (m2 == 12), 1 if m2 == 12 else m2 + 1, 1)
    return start, nxt - dt.timedelta(days=1)


def identify(doc: tabular.Document, profiles: list[Profile]):
    """Pick the profile that claims this document, else classify it generically."""
    # Two profiles can both claim a document at full confidence - every 1042-S
    # says "Form 1042-S", but only one of them also says which institution
    # issued it. The MORE SPECIFIC profile wins a tie: the one demanding more
    # required markers has more evidence behind its claim.
    best, best_key = None, (0.0, -1)
    for p in profiles:
        s = p.score(doc)
        key = (round(s, 4), len(p.match.get("all_text", []) or []))
        if key > best_key:
            best, best_key = p, key
    best_score = best_key[0]
    if best and best_score >= best.min_score:
        return best, best.document_type, round(best_score, 2)

    t = doc.text or ""
    if "form 1042-s" in t or "1042-s" in t or "foreign person's u.s. source income" in t:
        return None, FORM_1042S, 0.9
    if "tt buying" in t or "ttbr" in t or ("sbi" in t and "rate" in t):
        return None, TTBR_TABLE, 0.7
    if any(k in t for k in ("outstanding quantity", "holding", "portfolio details",
                            "allocation date", "lot")):
        return None, PORTFOLIO_HOLDINGS, 0.4
    if any(k in t for k in ("statement", "account summary", "transaction")):
        return None, BROKER_STATEMENT, 0.4
    return None, UNKNOWN, 0.2


def _digest(path) -> str:
    import hashlib
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]
    except Exception:
        return ""


def guess_broker(doc: tabular.Document) -> str:
    # Order matters: the MOST SPECIFIC brand wins. An E*TRADE statement also
    # names "Morgan Stanley Smith Barney LLC" because E*TRADE is a Morgan Stanley
    # business; the brand on the account is the better identification.
    known = {
        "computershare": "COMPUTERSHARE",
        "e*trade": "ETRADE", "etrade": "ETRADE",
        "shareworks": "SHAREWORKS",
        "charles schwab": "SCHWAB", "schwab": "SCHWAB",
        "fidelity": "FIDELITY",
        "morgan stanley": "MORGAN_STANLEY",
        "ubs": "UBS",
    }
    t = doc.text or ""
    for k, v in known.items():
        if k in t:
            return v
    # Unknown institution. Take a title-ish line, but never a form label such as
    # "Participant name" - that is the caption, not the institution.
    labels = ("participant", "user id", "as of", "note", "account", "date",
              "name", "client", "statement", "page", "total", "summary")
    for line in (doc.text or "").splitlines():
        cand = line.strip()
        low = cand.lower()
        if not (4 < len(cand) < 60) or any(c.isdigit() for c in cand):
            continue
        if any(low.startswith(l) or low == l for l in labels):
            continue
        # A candidate line is a guess, not a fact. Name it UNKNOWN_BROKER and let
        # the reviewer confirm, rather than stamping a participant's name into
        # the institution field.
        return "UNKNOWN_BROKER"
    return "UNKNOWN_BROKER"


# ----------------------------------------------------------------------
def extract_file(path: str | Path, profiles: list[Profile] | None = None):
    """Normalise one uploaded file. Never raises on an unknown layout."""
    import hashlib
    profiles = profiles if profiles is not None else load_profiles()
    doc = tabular.load(path)
    profile, doc_type, conf = identify(doc, profiles)
    broker = profile.broker if (profile and profile.broker) else guess_broker(doc)
    # Statements often carry the period only in the filename.
    start, end = detect_period(doc.text or "")
    if not start:
        start, end = detect_period(Path(path).stem.replace("_", " ").lower())
    # A combined statement PDF covers every month inside it.
    if profile is not None and (profile.layout or {}).get("statement_boundary"):
        s2, e2 = detect_period_span(doc)
        if s2 and e2:
            start, end = s2, e2

    rec = SourceRecord(file=Path(path).name, kind=doc.kind, document_type=doc_type,
                       profile_id=(profile.id if profile else "generic"),
                       broker=broker, confidence=conf,
                       period_start=start, period_end=end,
                       content_hash=_digest(path))
    if profile is not None:
        rec.reports_dividends = bool(
            (profile.rules or {}).get("reports_dividends", True))

    # A returned Google Finance FX table is checked FIRST: it mentions rates and
    # dates, so a TTBR classifier would otherwise claim it and its Google rates
    # would be merged in as SBI TT buying rates. They are not the same thing.
    if is_google_fx_table(doc):
        gfx = extract_google_fx(doc)
        rec.document_type = "GOOGLE_FX_TABLE"
        rec.profile_id = "google_fx_table"
        rec.reports_dividends = False
        rec.rows = len(gfx)
        rec.notes = "Google Finance FX fallback table"
        rec.google_fx = gfx
        return pd.DataFrame(), rec, [], pd.DataFrame()

    if doc_type == TTBR_TABLE:
        rates = extract_ttbr(doc)
        rec.rows = len(rates)
        rec.notes = "SBI TTBR table"
        return pd.DataFrame(), rec, [], rates

    holdings = []
    layout = (profile.layout or {}) if profile else {}
    if profile and layout.get("kind") == "pdf_sections":
        events, unmapped, holdings = _extract_pdf_sections(doc, profile, broker, rec)
    elif profile and layout.get("table_role") == "closed_lot_gains":
        events, unmapped = _extract_closed_lot_table(doc, profile, broker, rec)
    else:
        events, unmapped = _extract_events(doc, profile, broker, rec)
    rec.holdings = holdings
    rec.rows = len(events)
    # An annual tax form covers its whole calendar year, whatever dates happen
    # to appear on it. Coverage is what the dividend precedence rule turns on,
    # so it must not be narrowed to the first and last payment.
    if (profile is not None and (profile.rules or {}).get("annual_form")
            and not events.empty and "date" in events):
        years = {d.year for d in events["date"] if d}
        if len(years) == 1:
            y = years.pop()
            rec.period_start, rec.period_end = dt.date(y, 1, 1), dt.date(y, 12, 31)
    if not events.empty:
        rec.securities = sorted({s for s in events["symbol"].dropna().unique()})
    return events, rec, unmapped, pd.DataFrame()


MONEY_RE = re.compile(r"\$(-?[\d,]+\.?\d*)\s*USD")
BARE_NUM_RE = re.compile(r"(?<![\d.])(-?\d[\d,]*)(?![\d,]*\.\d)")


def _extract_pdf_sections(doc, profile, broker, rec):
    """Build events from a PDF whose sections a profile describes.

    Two jobs. Transaction sections replay into normalised events. Holdings
    sections are kept as a snapshot so the replay can be reconciled against what
    the broker itself says is held.
    """
    rules = profile.rules or {}
    sections = (profile.layout or {}).get("sections", [])
    tables = tabular.pdf_sections(doc, sections, context=(profile.layout or {}))
    by_name = {t.sheet: t for t in tables}
    cfg_by_name = {c.get("name"): c for c in sections}

    ident = profile.identity or {}
    account = ""
    if ident.get("account_regex"):
        # A label and its value are often on different lines, so a broker
        # declares HOW to find its account number rather than assuming layout.
        rx = re.compile(ident["account_regex"])
        for line in (doc.lines or []):
            m = rx.search(line)
            if m:
                account = (m.groupdict().get("account") or m.group(0)).strip()
                break
    if not account:
        account = _label_value(doc, ident.get("account", "Account Number"))
    symbol = _symbol_from_doc(profile, doc.text or "")

    money_semantics = {int(k): v for k, v in (rules.get("money_semantics") or {}).items()}
    event_map = rules.get("activity_event_map") or {}
    additive_split = str(rules.get("split_style", "")).lower() == "additive"

    def _year(cfg):
        anchor = cfg.get("date_year_from", "period_end")
        d = rec.period_end if anchor == "period_end" else rec.period_start
        return d.year if d else None

    def _row_date(value, cfg):
        """Parse a section's date, supplying the year when the page omits it."""
        v = str(value or "").strip()
        if not v:
            return None
        if cfg.get("date_format") == "%m/%d":
            y = _year(cfg)
            if not y:
                return None
            try:
                m, dd = v.split("/")
                return dt.date(y, int(m), int(dd))
            except Exception:
                return None
        return G.to_date(v)

    def _stmt_as_at(r):
        """The date THIS row's statement was struck at.

        A combined PDF holds several statements. Where the row carries its own
        statement context, that wins over the file-level period - otherwise five
        months of balances would all be dated the same day.
        """
        mon = str(r.get("_asat_month") or "").strip()
        day = str(r.get("_asat_day") or "").strip()
        year = str(r.get("_stmt_year") or "").strip()
        if mon and day and year:
            m = _mon(mon)
            try:
                d = dt.date(int(year), m, int(day))
            except (ValueError, TypeError):
                return rec.period_end
            # A statement dated "January 2026" showing balances "on December 31"
            # is stating the PRIOR year end, not a date eleven months ahead.
            smon = _mon(str(r.get("_stmt_month") or "")) or m
            if m > smon + 6:
                try:
                    d = d.replace(year=int(year) - 1)
                except ValueError:
                    pass
            return d
        return rec.period_end

    rows, holdings, cash, transfers, f1042 = [], [], [], [], []
    exercises, unvested = [], []
    realised_gain = None
    for name, t in by_name.items():
        cfg = cfg_by_name.get(name) or {}
        role = cfg.get("role", "transactions")

        # ---- period-end position: a broker-stated holding is source-priority 1 ----
        if role == "holdings_as_acquisition":
            carry_lot = bool(rules.get("position_carries_lot_date"))
            for i, r in t.df.reset_index(drop=True).iterrows():
                as_at = _stmt_as_at(r)
                # NaN, not a silent zero, when present-but-unparseable - a
                # malformed shares cell must never be dropped exactly like a
                # genuinely blank one (see the invalid_quantities diagnostic
                # below), nor silently treated as a real zero-share position.
                shares = G.to_number(r.get("shares"), on_invalid="nan")
                if shares != shares:
                    rec.invalid_quantities.append({
                        "role": "holdings_as_acquisition", "event": "POSITION",
                        "symbol": str(r.get("ticker") or symbol).strip().upper(),
                        "broker": broker, "source": t.coords(i),
                        "raw": repr(r.get("shares")),
                    })
                    continue
                # NaN, not a silent zero, when present-but-unparseable - a
                # false Rs.0 acquisition cost would otherwise reach
                # build_lots() as a real POSITION price. "not cost_total"
                # is deliberately also true for NaN below (an explicit
                # extra check, not relying on truthiness) so a malformed
                # cost_total still falls through to the cost_per_share
                # fallback exactly as a genuinely blank/zero one already
                # did - the fallback is preserved, only defeated when BOTH
                # figures are unparseable, at which point cost_total stays
                # NaN and is picked up by the existing 1412859 price-NaN
                # diagnostic once it reaches build_lots().
                cost_total = G.to_number(r.get("cost_total"), on_invalid="nan")
                if not shares or as_at is None:
                    continue
                if not cost_total or cost_total != cost_total:
                    cps = G.to_number(r.get("cost_per_share"), on_invalid="nan")
                    cost_total = cps * shares
                # Some brokers state each lot's own trade date beside the
                # position. That is better evidence than the statement date, so
                # the A3 initial value can convert at the real acquisition date.
                lot_date = (_row_date(r.get("acquired"), cfg) if carry_lot else None)
                sym = str(r.get("ticker") or symbol).strip().upper()
                holdings.append({"symbol": sym, "acquired": lot_date or as_at,
                                 "shares": shares,
                                 "cost_per_share": round(cost_total / shares, 6),
                                 "cost_total": cost_total, "source": t.coords(i)})
                note = (f"{rules.get('plan_type', 'RSU')} | position stated by "
                        f"the broker at {as_at:%d-%m-%Y}; cost basis "
                        f"${cost_total:,.2f}")
                if lot_date:
                    note += f"; lot acquired {lot_date:%d-%m-%Y} as stated"
                rows.append({
                    "date": as_at, "broker": broker, "account_no": account,
                    "symbol": sym, "event": "POSITION", "quantity": shares,
                    "price_fc": round(cost_total / shares, 6), "amount_fc": "",
                    "tax_fc": "",
                    "acquired_on": lot_date.isoformat() if lot_date else "",
                    "cost_fc": "", "currency": "USD",
                    "notes": f"{note} | {t.coords(i)}",
                })
            continue

        # ---- custodial cash balance for FA-A2 ----
        if role == "cash":
            for i, r in t.df.reset_index(drop=True).iterrows():
                as_at = _stmt_as_at(r)
                if as_at is None:
                    continue
                bal = (r.get("balance") if r.get("balance") is not None
                       else r.get("ending"))
                cash.append({"date": as_at, "broker": broker,
                             "account_no": account,
                             # NaN, not a silent zero, when the balance was
                             # present but unparseable - see build_a2()'s
                             # invalid-balance handling.
                             "balance_fc": G.to_number(bal, on_invalid="nan"),
                             "currency": "USD", "source": t.coords(i)})
            continue

        # ---- a 1042-S that states the DATE of every payment ----
        # A 1042-S normally carries annual totals only, which is why the engine
        # converts it in aggregate at the period-end rate. When the form prints
        # a dated detail schedule, each payment converts at its own date - the
        # aggregate basis exists because dates are missing, not by preference.
        if role == "dividends_dated":
            for i, r in t.df.reset_index(drop=True).iterrows():
                d = _row_date(r.get("date"), {**cfg, "date_format":
                                              rules.get("date_format")})
                gross = G.to_number(r.get("gross"))
                if d is None or not gross:
                    continue
                rows.append({
                    "date": d, "broker": broker, "account_no": account,
                    "symbol": symbol, "event": "DIV", "quantity": "",
                    "price_fc": "", "amount_fc": round(gross, 2),
                    "tax_fc": round(G.to_number(r.get("tax")), 2),
                    "acquired_on": "", "cost_fc": "", "currency": "USD",
                    "notes": ("Form 1042-S dated payment detail - converted at "
                              f"this payment's own date | {t.coords(i)}"),
                })
            continue

        if role == "dividend_control_total":
            for i, r in t.df.reset_index(drop=True).iterrows():
                rec.control_total = {"gross": G.to_number(r.get("gross")),
                                     "tax": G.to_number(r.get("tax")),
                                     "source": t.coords(i)}
                break
            continue

        # ---- stock option exercises: the perquisite, stated by the broker ----
        if role == "option_exercises":
            for i, r in t.df.reset_index(drop=True).iterrows():
                # NaN, not a silent zero, when present-but-unparseable - a
                # malformed exercise quantity must be flagged rather than
                # dropped, since it defeats the exercise-FMV-as-cost
                # perquisite-double-tax fix that matches on this quantity.
                qty = G.to_number(r.get("qty"), on_invalid="nan")
                if qty != qty:
                    rec.invalid_quantities.append({
                        "role": "option_exercises", "event": "OPTION_EXERCISE",
                        "symbol": symbol, "broker": broker, "source": t.coords(i),
                        "raw": repr(r.get("qty")),
                    })
                    continue
                on = _row_date(r.get("exercise_date"), cfg)
                if not qty or on is None:
                    continue
                exercises.append({
                    "grant_no": str(r.get("grant_no") or "").strip(),
                    "grant_date": _row_date(r.get("grant_date"), cfg),
                    "exercise_date": on, "quantity": qty,
                    "option_cost_fc": G.to_number(r.get("option_cost")),
                    "fmv_at_exercise_fc": G.to_number(r.get("fmv_total")),
                    "stated_gain_fc": G.to_number(r.get("realized")),
                    "symbol": symbol, "source": t.coords(i)})
            continue

        # ---- unvested awards: disclosed, never a holding ----
        if role == "unvested_awards":
            for i, r in t.df.reset_index(drop=True).iterrows():
                n = G.to_number(r.get("unvested"), on_invalid="nan")
                if n != n:
                    rec.invalid_quantities.append({
                        "role": "unvested_awards", "event": "UNVESTED_AWARD",
                        "symbol": symbol, "broker": broker, "source": t.coords(i),
                        "raw": repr(r.get("unvested")),
                    })
                    continue
                if not n:
                    continue
                unvested.append({
                    "award_no": str(r.get("award_no") or "").strip(),
                    "award_date": _row_date(r.get("award_date"), cfg),
                    "plan": str(r.get("plan") or "").strip(),
                    "unvested": n, "vested_pending": G.to_number(r.get("pending")),
                    "unvested_value_fc": G.to_number(r.get("unvested_value")),
                    "as_at": _stmt_as_at(r), "symbol": symbol,
                    "source": t.coords(i)})
            continue

        # ---- dividends: gross and withholding printed as separate lines ----
        if role == "dividends":
            action_map = {k.lower(): v for k, v in (cfg.get("action_map") or {}).items()}
            bucket = {}
            for i, r in t.df.reset_index(drop=True).iterrows():
                d = _row_date(r.get("date"), cfg)
                kind = action_map.get(str(r.get("action", "")).strip().lower())
                if d is None or kind not in ("gross", "tax"):
                    continue
                sym = str(r.get("ticker") or symbol).strip().upper()
                b = bucket.setdefault((d, sym), {"gross": 0.0, "tax": 0.0, "src": []})
                b[kind] += G.to_number(r.get("amount"))
                b["src"].append(t.coords(i))
            for (d, sym), b in sorted(bucket.items()):
                rows.append({
                    "date": d, "broker": broker, "account_no": account,
                    "symbol": sym, "event": "DIV", "quantity": "", "price_fc": "",
                    "amount_fc": round(b["gross"], 2), "tax_fc": round(b["tax"], 2),
                    "acquired_on": "", "cost_fc": "", "currency": "USD",
                    "notes": ("Gross dividend and withholding paired from separate "
                              f"statement lines | {'; '.join(b['src'][:2])}"),
                })
            continue

        # ---- standalone withholding lines (no matching gross on the row) ----
        if role == "withholding":
            invert = str(cfg.get("sign", "")).lower() == "invert"
            for i, r in t.df.reset_index(drop=True).iterrows():
                d = _row_date(r.get("date"), cfg)
                amt = G.to_number(r.get("amount"))
                if d is None or not amt:
                    continue
                rows.append({
                    "date": d, "broker": broker, "account_no": account,
                    "symbol": str(r.get("ticker") or symbol).strip().upper(),
                    "event": "DIV_TAX", "quantity": "", "price_fc": "",
                    "amount_fc": round(-amt if invert else amt, 2), "tax_fc": "",
                    "acquired_on": "", "cost_fc": "", "currency": "USD",
                    "notes": ("Withholding reported on its own line, separate from "
                              f"the gross dividend | {t.coords(i)}"),
                })
            continue

        # ---- shares arriving from or leaving for another account ----
        if role == "transfers":
            direction = cfg.get("direction", "IN")
            for i, r in t.df.reset_index(drop=True).iterrows():
                # NaN, not a silent zero, when present-but-unparseable - a
                # dropped transfer quantity would silently break the FA-A2/A3
                # cross-broker quantity chain build_cross_broker() walks.
                qty = G.to_number(r.get("qty"), on_invalid="nan")
                d = _row_date(r.get("date"), cfg)
                if qty != qty:
                    rec.invalid_quantities.append({
                        "role": "transfers", "event": f"TRANSFER_{direction}",
                        "symbol": str(r.get("ticker") or symbol).strip().upper(),
                        "broker": broker, "source": t.coords(i),
                        "raw": repr(r.get("qty")),
                    })
                    continue
                if not qty or d is None:
                    continue
                transfers.append({
                    "date": d, "broker": broker, "account_no": account,
                    "symbol": str(r.get("ticker") or symbol).strip().upper(),
                    "direction": direction, "quantity": qty,
                    "value_fc": G.to_number(r.get("amount")),
                    "source": t.coords(i),
                })
            continue

        # ---- Form 1042-S: fields scattered across the form's boxes ----
        if role == "form_1042s":
            # A 1042-S PDF holds several IDENTICAL copies of the same form -
            # Copy B, C and D for the recipient. Summing them would treble the
            # income. Each gross line starts a form instance, the tax line that
            # follows belongs to it, and identical instances collapse to one.
            instances, current = [], None
            for i, r in t.df.reset_index(drop=True).iterrows():
                gross = G.to_number(r.get("gross"))
                tax = G.to_number(r.get("tax"))
                code = str(r.get("code") or "").strip()
                if gross:
                    if current:
                        instances.append(current)
                    current = {"gross": gross, "tax": 0.0, "code": code,
                               "src": t.coords(i)}
                elif tax and current is not None:
                    current["tax"] = tax
            if current:
                instances.append(current)

            unique, seen_forms = [], set()
            for inst in instances:
                key = (inst["code"], round(inst["gross"], 2), round(inst["tax"], 2))
                if key in seen_forms:
                    continue
                seen_forms.add(key)
                unique.append(inst)
            if len(instances) > len(unique):
                rec.notes = (f"{len(instances)} form copies collapsed to "
                             f"{len(unique)} distinct 1042-S record(s)")

            for inst in unique:
                f1042.append({
                    "tax_year": rec.period_end.year if rec.period_end else "",
                    "broker": broker, "payer": cfg.get("payer", broker),
                    "income_code": inst["code"],
                    "gross_income_fc": round(inst["gross"], 2),
                    "tax_withheld_fc": round(inst["tax"], 2),
                    "currency": "USD", "source_file": Path(doc.path).name,
                    "notes": inst["src"],
                })
            continue

        # ---- an aggregate realised gain, stated without lot detail ----
        if role == "realized_gain_summary":
            for i, r in t.df.reset_index(drop=True).iterrows():
                v = G.to_number(r.get("period"))
                if v:
                    realised_gain = {"amount": v, "label": str(r.get("label", "")),
                                     "source": t.coords(i)}
            continue

        # ---- disposals with proceeds but no lot detail ----
        if role == "sales":
            for i, r in t.df.reset_index(drop=True).iterrows():
                # NaN, not a silent zero, when present-but-unparseable - this
                # is a SELL, and a dropped/zeroed disposal quantity would
                # understate the holding without any trace.
                qty = G.to_number(r.get("qty"), on_invalid="nan")
                d = _row_date(r.get("date"), cfg)
                if qty != qty:
                    rec.invalid_quantities.append({
                        "role": "sales", "event": "SELL",
                        "symbol": str(r.get("ticker") or symbol).strip().upper(),
                        "broker": broker, "source": t.coords(i),
                        "raw": repr(r.get("qty")),
                    })
                    continue
                if not qty or d is None:
                    continue
                # NaN, not a silent zero, when present-but-unparseable - a
                # false Rs.0 price/proceeds would understate or fabricate a
                # loss once it reaches build_lots()/build_cg(). NaN is
                # explicitly excluded (never merely falsy) from the two
                # checks below so a malformed price still falls through to
                # the credited-amount fallback exactly as a genuinely
                # blank/zero price already did - only when BOTH are
                # unparseable does gross itself end up unresolved (NaN).
                price = G.to_number(r.get("price"), on_invalid="nan")
                credited = G.to_number(r.get("amount"), on_invalid="nan")
                # Schedule FA and capital gains both want the GROSS consideration.
                # A statement's cash credit is net of transaction fees, so the
                # gross is quantity x price and the difference is disclosed.
                price_ok = price == price
                credited_ok = credited == credited
                gross = qty * price if (price and price_ok) else credited
                fee = (round(gross - credited, 2)
                       if (price and price_ok and credited and credited_ok)
                       else 0.0)
                note = f"Sale reported without lot detail | {t.coords(i)}"
                if abs(fee) >= 0.01:
                    note += (f" | gross {gross:,.2f} less fees {fee:,.2f} = credited "
                             f"{credited:,.2f}; gross is reported")
                rows.append({
                    "date": d, "broker": broker, "account_no": account,
                    "symbol": str(r.get("ticker") or symbol).strip().upper(),
                    "event": "SELL", "quantity": qty,
                    "price_fc": round(price, 6), "amount_fc": round(gross, 2),
                    "tax_fc": "", "acquired_on": "", "cost_fc": "",
                    "currency": "USD", "notes": note,
                })
            continue

        # ---- realised gain/loss: a complete closed-lot record ----
        if role == "closed_lot_gains":
            for i, r in t.df.reset_index(drop=True).iterrows():
                # A realised-gain table may print the quantity negative because
                # it is a disposal. The SELL event already says that.
                # NaN, not a silent zero, when present-but-unparseable - abs()
                # of NaN is still NaN, so this is still an explicit check, not
                # truthiness.
                qty = abs(G.to_number(r.get("qty"), on_invalid="nan"))
                acq = _row_date(r.get("acquired"), cfg)
                sold = _row_date(r.get("sold"), cfg)
                if qty != qty:
                    rec.invalid_quantities.append({
                        "role": "closed_lot_gains", "event": "SELL",
                        "symbol": str(r.get("ticker") or symbol).strip().upper(),
                        "broker": broker, "source": t.coords(i),
                        "raw": repr(r.get("qty")),
                    })
                    continue
                if not qty or sold is None:
                    continue
                # NaN, not a silent zero, for either when present-but-
                # unparseable - a false Rs.0 cost or sale price would
                # otherwise reach build_lots()/build_cg() as a real figure.
                # Cost is caught there via the existing "stated" blank/NaN
                # exclusion (a877a1b); sale price/proceeds via build_cg()'s
                # sale_ok gate (this change).
                proceeds = G.to_number(r.get("proceeds"), on_invalid="nan")
                cost = G.to_number(r.get("cost"), on_invalid="nan")
                sym = str(r.get("ticker") or symbol).strip().upper()
                rows.append({
                    "date": sold, "broker": broker, "account_no": account,
                    "symbol": sym, "event": "SELL", "quantity": qty,
                    "price_fc": round(proceeds / qty, 6),
                    "amount_fc": round(proceeds, 2), "tax_fc": "",
                    "acquired_on": acq.isoformat() if acq else "",
                    "cost_fc": round(cost, 2), "currency": "USD",
                    "notes": (f"Closed-lot record: acquired {acq:%d-%m-%Y}, broker "
                              f"cost ${cost:,.2f} | {t.coords(i)}" if acq else
                              f"Closed-lot record | {t.coords(i)}"),
                })
            continue

        if role == "holdings":
            for i, r in t.df.reset_index(drop=True).iterrows():
                holdings.append({
                    "symbol": r.get("ticker") or symbol,
                    "acquired": G.to_date(r.get("acquired")),
                    "shares": G.to_number(r.get("shares")),
                    # NaN, not a silent zero, for the cost fields when
                    # present-but-unparseable - this role does not yet feed
                    # any event/computation (rec.holdings has no downstream
                    # reader today), but keeping the same convention here
                    # avoids a future consumer inheriting a false zero.
                    "cost_per_share": G.to_number(r.get("cost_per_share"),
                                                  on_invalid="nan"),
                    "cost_total": G.to_number(r.get("cost_total"), on_invalid="nan"),
                    "source": t.coords(i),
                })
            continue

        # ---- transaction rows ----
        staged = []
        for i, r in t.df.reset_index(drop=True).iterrows():
            date = G.to_date(r.get("date"))
            if date is None:
                continue
            activity = str(r.get("activity", "")).strip()
            rest = str(r.get("rest", ""))
            key = re.sub(r"\(.*\)", "", activity).strip()
            event = event_map.get(key, G.classify_event(activity, default="OTHER"))
            if event == "SKIP":
                continue

            money = [G.to_number(x) for x in MONEY_RE.findall(rest)]
            bare = MONEY_RE.sub(" ", rest)
            nums = BARE_NUM_RE.findall(bare)
            qty = G.to_number(nums[0]) if nums else 0.0

            named = dict(zip(money_semantics.get(len(money), []), money))
            staged.append({"date": date, "event": event, "qty": qty,
                           "price": named.get("price", 0.0),
                           "book_value": named.get("book_value", 0.0),
                           "source": t.coords(i)})

        rows += _stage_to_events(staged, symbol, broker, account, additive_split, rules)

    # Where the broker states proceeds and a realised gain but no lot cost, the
    # cost is their difference - arithmetic on two stated figures, not a guess.
    # It is allocated across the period's sales in proportion to proceeds.
    if rules.get("derive_cost_from_realized_gain") and realised_gain:
        sales = [r for r in rows if r["event"] == "SELL" and not r["cost_fc"]]
        total_proceeds = sum(float(r["amount_fc"] or 0) for r in sales)
        total_cost = total_proceeds - realised_gain["amount"]
        if sales and total_proceeds > 0 and total_cost >= 0:
            for r in sales:
                share = float(r["amount_fc"]) / total_proceeds
                r["cost_fc"] = round(total_cost * share, 2)
                r["notes"] += (f" | cost derived: proceeds {total_proceeds:,.2f} less "
                               f"broker-stated realised gain "
                               f"{realised_gain['amount']:,.2f}, allocated by "
                               f"proceeds ({realised_gain['source']})")

    # ---- a stock option exercise: perquisite first, capital gain second ----
    # Where shares come from exercising an option, the broker's stated cost is
    # the OPTION COST - what the employee paid to exercise. But the spread
    # between that and the market value at exercise has already been taxed as a
    # perquisite. For Indian capital gains the cost of acquisition is therefore
    # the value at the time of exercise, not the option cost. Using the option
    # cost taxes the same income twice.
    #
    # This is the same principle the E*TRADE G&L Expanded rule applies through
    # its "Adjusted Cost Basis" column; UBS states the two figures as separate
    # columns of an exercise table instead. Which column is authoritative stays
    # a configuration decision.
    if rules.get("exercise_fmv_is_cost") and exercises:
        for ex in exercises:
            for r in rows:
                if (r["event"] != "SELL" or r["symbol"] != ex["symbol"]
                        or abs(float(r["quantity"]) - ex["quantity"]) > 1e-6):
                    continue
                acq = str(r.get("acquired_on") or "")
                if acq and acq != ex["exercise_date"].isoformat():
                    continue
                option_cost = float(r["cost_fc"] or 0)
                fmv = ex["fmv_at_exercise_fc"]
                if not fmv or abs(fmv - option_cost) < 0.01:
                    continue
                perquisite = fmv - option_cost
                r["cost_fc"] = round(fmv, 2)
                r["broker_gain_fc"] = round(float(r["amount_fc"] or 0) - option_cost, 2)
                r["broker_adj_cost_fc"] = round(fmv, 2)
                r["broker_adj_gain_fc"] = round(float(r["amount_fc"] or 0) - fmv, 2)
                r["notes"] += (
                    f" | OPTION EXERCISE {ex['grant_no']} on "
                    f"{ex['exercise_date']:%d-%m-%Y}: option cost "
                    f"${option_cost:,.2f}, value at exercise ${fmv:,.2f}. The "
                    f"${perquisite:,.2f} spread is a PERQUISITE already taxed as "
                    "salary, so the cost of acquisition for capital gains is the "
                    "value at exercise, not the option cost | " + ex["source"])
                ex["applied_to"] = r["notes"]
                break

    rec.exercises = exercises
    rec.unvested_awards = unvested
    rec.cash = cash
    rec.transfers = transfers
    rec.form1042s = f1042
    rec.notes = (f"{len(holdings)} holding lots; {len(rows)} transactions"
                 if holdings else f"{len(rows)} transactions")
    if rules.get("adjustment"):
        from . import adjustments
        rows = adjustments.apply(rules["adjustment"], rows, rec, profile)
    return pd.DataFrame(rows), [], holdings


def _stage_to_events(staged, symbol, broker, account, additive_split, rules):
    """Turn staged rows into events, converting additive splits into a ratio."""
    plan = rules.get("plan_type", "RSU")
    staged.sort(key=lambda x: x["date"])
    out, running = [], 0.0

    i = 0
    while i < len(staged):
        s = staged[i]
        if s["event"] == "SPLIT" and additive_split:
            # Every split row for the same date belongs to one corporate action.
            same = [x for x in staged if x["event"] == "SPLIT" and x["date"] == s["date"]]
            added = sum(x["qty"] for x in same)
            ratio = ((running + added) / running) if running else 1.0
            out.append({
                "date": s["date"], "broker": broker, "account_no": account,
                "symbol": symbol, "event": "SPLIT", "quantity": round(ratio, 6),
                "price_fc": "", "amount_fc": "", "tax_fc": "", "cost_fc": "",
                "currency": "USD",
                "notes": (f"{plan} | Stock split {ratio:.0f}:1 - broker reported "
                          f"{added:g} additional shares across {len(same)} lots "
                          f"against {running:g} held | {s['source']}"),
            })
            running += added
            i += len(same)
            continue

        qty = abs(s["qty"])
        if s["event"] == "SELL":
            price = s["price"] or (abs(s["book_value"]) / qty if qty else 0.0)
            out.append({
                "date": s["date"], "broker": broker, "account_no": account,
                "symbol": symbol, "event": "SELL", "quantity": qty,
                "price_fc": round(price, 6), "amount_fc": round(qty * price, 2),
                "tax_fc": "", "cost_fc": round(abs(s["book_value"]), 2),
                "currency": "USD",
                "notes": (f"{plan} | broker cost removed "
                          f"${abs(s['book_value']):,.2f} | {s['source']}"),
            })
            running -= qty
        elif s["event"] in ("VEST", "BUY", "TRANSFER_IN"):
            price = (abs(s["book_value"]) / qty) if qty else 0.0
            out.append({
                "date": s["date"], "broker": broker, "account_no": account,
                "symbol": symbol, "event": s["event"], "quantity": qty,
                "price_fc": round(price, 6), "amount_fc": "", "tax_fc": "",
                "cost_fc": "", "currency": "USD",
                "notes": f"{plan} | {s['source']}",
            })
            running += qty
        elif s["event"] == "TRANSFER_OUT":
            # Mirrors TRANSFER_IN above, in the opposite direction. TRANSFER_OUT
            # is a DISPOSING_EVENT (models.py) but is never a PROCEEDS_EVENT, so
            # build_lots() reduces the open lots by this quantity without ever
            # creating a SaleMatch/gain or inventing a cost - exactly like a
            # TRANSFER_IN never invents a gain on the receiving side. Preserving
            # this row (instead of the previous silent drop) is what lets the
            # source account's FA-A2/A3 quantities and the reconciliation walk
            # (build_reconciliation's `tout = total({TRANSFER_OUT})`) come out
            # right; no assumption is made about which destination broker
            # received the shares; a corresponding TRANSFER_IN at another
            # broker/account is that account's own independent event and is
            # tracked separately - there is nothing here to double count.
            price = (abs(s["book_value"]) / qty) if qty else 0.0
            out.append({
                "date": s["date"], "broker": broker, "account_no": account,
                "symbol": symbol, "event": "TRANSFER_OUT", "quantity": qty,
                "price_fc": round(price, 6), "amount_fc": "", "tax_fc": "",
                "cost_fc": "", "currency": "USD",
                "notes": f"{plan} | {s['source']}",
            })
            running -= qty
        elif s["event"] == "DIV":
            out.append({
                "date": s["date"], "broker": broker, "account_no": account,
                "symbol": symbol, "event": "DIV", "quantity": "", "price_fc": "",
                "amount_fc": abs(s["book_value"] or s["price"]), "tax_fc": "",
                "cost_fc": "", "currency": "USD",
                "notes": f"Dividend | {s['source']}",
            })
        i += 1
    return out


def _label_value(doc, label: str) -> str:
    pat = re.compile(rf"{re.escape(label)}\s*:?\s*([A-Za-z0-9\-/]+)", re.I)
    for line in (doc.lines or []):
        m = pat.search(line)
        if m:
            return m.group(1).strip()
    return ""


def _symbol_from_doc(profile, text: str) -> str:
    table = {**{"amazon": "AMZN", "ibm": "IBM", "microsoft": "MSFT",
                "broadcom": "AVGO", "qualcomm": "QCOM",
                "texas instruments": "TXN"},
             **((profile.rules or {}).get("symbol_from_text", {}) if profile else {})}
    for k, v in table.items():
        if k in text:
            return v
    m = re.search(r"\b([A-Z]{2,6})\s*-\s*(?:NASDAQ|NYSE)", (text or "").upper())
    return m.group(1) if m else "UNKNOWN"


def _profile_columns(profile, table) -> dict:
    """Resolve a profile's canonical field names to this table's real headers.

    Header text is matched case-insensitively and whitespace-normalised, because
    a broker's export writes "Adjusted Gain/Loss" one year and "Adjusted Gain /
    Loss" the next and means the same column.
    """
    def norm(s):
        return re.sub(r"\s+", " ", str(s)).strip().lower()

    lower = {norm(h): h for h in table.df.columns}
    cols = {}
    for canon, names in (profile.fields or {}).items():
        for n in (names if isinstance(names, list) else [names]):
            h = lower.get(norm(n))
            if h is not None:
                cols[canon] = h
                break
    return cols


def _row_matches(row, spec, cols_lower) -> bool:
    """Does this row satisfy a profile's row_filter / control_row spec?"""
    if not spec:
        return True
    col = cols_lower.get(re.sub(r"\s+", " ", str(spec.get("column", "")).strip().lower()))
    if col is None:
        return False
    val = str(row.get(col, "") or "").strip().lower()
    return val in [str(v).strip().lower() for v in (spec.get("values") or [])]


def _extract_closed_lot_table(doc, profile, broker, rec):
    """A broker's realised gain/loss report, read as complete closed-lot records.

    This is the spreadsheet twin of the pdf_sections `closed_lot_gains` role. The
    report states, per lot: quantity, acquisition date, sale date, proceeds and
    cost. Nothing is inferred - no FIFO, no holding period guessed from the
    broker's own US short/long label.

    Which cost column is authoritative is a CONFIGURATION decision, because it
    differs by report. An equity-plan report states both an "Acquisition Cost"
    and an "Adjusted Cost Basis"; only the adjusted figure includes the ordinary
    income already taxed as a perquisite, so only the adjusted figure is the cost
    actually borne. The profile names the authoritative column; this code does
    not know or care which broker or security it is looking at.
    """
    rules = profile.rules or {}
    layout = profile.layout or {}
    tol = float(rules.get("reconcile_tolerance", 0.01))
    rows, notes = [], []
    row_filter = layout.get("row_filter")
    control_spec = layout.get("control_row")
    control = None

    for t in doc.tables:
        cols = _profile_columns(profile, t)
        need = ("quantity", "sale_date", "proceeds_fc", "cost_fc")
        if not all(k in cols for k in need):
            continue
        cols_lower = {re.sub(r"\s+", " ", str(h).strip().lower()): h
                      for h in t.df.columns}
        body = t.df.reset_index(drop=True)

        for i, r in body.iterrows():
            # A stated summary line is a CONTROL TOTAL, never a transaction.
            if control_spec and _row_matches(r, control_spec, cols_lower):
                control = {
                    "quantity": G.to_number(r.get(cols.get("quantity"))),
                    "gain_fc": G.to_number(r.get(cols.get("gain_fc"))),
                    "ordinary_gain_fc": G.to_number(r.get(cols.get("ordinary_gain_fc"))),
                    "source": t.coords(i),
                }
                continue
            if row_filter and not _row_matches(r, row_filter, cols_lower):
                continue

            sym_early = str(r.get(cols.get("symbol"), "") or "").strip().upper() or "UNKNOWN"
            # NaN, not a silent zero, when present-but-unparseable - a
            # dropped disposal quantity would understate the holding with no
            # trace, exactly like the pdf_sections closed_lot_gains role.
            qty = G.to_number(r.get(cols.get("quantity")), on_invalid="nan")
            sold = G.to_date(r.get(cols.get("sale_date")))
            if qty != qty:
                rec.invalid_quantities.append({
                    "role": "closed_lot_gains_table", "event": "SELL",
                    "symbol": sym_early, "broker": broker, "source": t.coords(i),
                    "raw": repr(r.get(cols.get("quantity"))),
                })
                continue
            if not qty or sold is None:
                continue
            acq = G.to_date(r.get(cols.get("acquired_date"))) if cols.get("acquired_date") else None
            # NaN, not a silent zero, for proceeds/cost/gain when present-
            # but-unparseable - cost is caught downstream via the existing
            # blank/NaN "stated" exclusion in build_lots() (a877a1b);
            # proceeds via build_cg()'s new sale_ok gate. gain only feeds
            # this table's own reconciliation note and broker_adj_gain_fc
            # provenance, not the computed capital gain itself.
            proceeds = G.to_number(r.get(cols.get("proceeds_fc")), on_invalid="nan")
            cost = G.to_number(r.get(cols.get("cost_fc")), on_invalid="nan")
            gain = (G.to_number(r.get(cols.get("gain_fc")), on_invalid="nan")
                    if cols.get("gain_fc") else proceeds - cost)
            ord_gain = (G.to_number(r.get(cols.get("ordinary_gain_fc")))
                        if cols.get("ordinary_gain_fc") else None)
            ord_income = (G.to_number(r.get(cols.get("ordinary_income_fc")))
                          if cols.get("ordinary_income_fc") else None)
            sym = sym_early

            # The report's own arithmetic must hold. It is never rewritten to
            # make it hold - a break is reported and the broker's figures stand.
            # A NaN in any of the three (present-but-unparseable) is ALSO
            # treated as a break - NaN comparisons are always False, so
            # without this explicit check a malformed figure would silently
            # pass this reconciliation test instead of failing it, which
            # would be strictly worse than not checking at all.
            broke = ((proceeds != proceeds) or (cost != cost) or (gain != gain)
                     or abs((proceeds - cost) - gain) > tol)
            if broke:
                notes.append({
                    "kind": "reconcile", "symbol": sym, "source": t.coords(i),
                    "detail": (f"proceeds {proceeds:,.2f} - adjusted cost "
                               f"{cost:,.2f} = {proceeds - cost:,.2f}, but the "
                               f"report states an adjusted gain of {gain:,.2f} "
                               f"(difference {(proceeds - cost) - gain:,.2f})."),
                })

            note = (f"G&L closed lot: acquired "
                    f"{acq:%d-%m-%Y}" if acq else "G&L closed lot: acquisition date "
                    "not stated")
            note += (f", proceeds ${proceeds:,.2f}, adjusted cost ${cost:,.2f}, "
                     f"adjusted gain ${gain:,.2f}")
            if ord_income:
                note += (f" (includes ${ord_income:,.2f} ordinary income already "
                         "taxed as a perquisite)")
            if ord_gain is not None and abs(ord_gain - gain) > tol:
                note += (f"; the report's ordinary Gain/Loss of ${ord_gain:,.2f} "
                         "excludes that ordinary income and is NOT the Indian gain")
            note += f" | {t.coords(i)}"

            rows.append({
                "date": sold, "broker": broker, "account_no": "",
                "symbol": sym, "event": "SELL", "quantity": qty,
                "price_fc": round(proceeds / qty, 6) if qty else 0.0,
                "amount_fc": round(proceeds, 2), "tax_fc": "",
                "acquired_on": acq.isoformat() if acq else "",
                "cost_fc": round(cost, 2), "currency": "USD", "notes": note,
                "broker_gain_fc": ("" if ord_gain is None else round(ord_gain, 2)),
                "broker_adj_cost_fc": round(cost, 2),
                "broker_adj_gain_fc": round(gain, 2),
                "source_ref": t.coords(i),
            })
        if rows or control:
            break

    df = pd.DataFrame(rows)
    if len(df):
        # The report's own coverage is the span of the disposals it states. That
        # is what it is authoritative FOR - no more.
        rec.period_start = rec.period_start or min(df["date"])
        rec.period_end = rec.period_end or max(df["date"])
    rec.closed_lots = rows
    rec.control_total = control
    rec.recon_breaks = notes
    rec.cg_authority = bool(rules.get("capital_gains_authority"))
    return df, []


def _extract_events(doc, profile, broker, rec):
    """Profile-driven where one matched, synonym-driven where none did."""
    rows, unmapped = [], []
    account = ""
    for t in doc.tables:
        if t.raw is not None:
            for lab in ("user id", "participant number", "account number", "account"):
                account = account or tabular.cell_after_label(t.raw, lab)

        mapping = (_mapping_from_profile(profile, t) if profile
                   else G.map_columns(list(t.df.columns)))
        if not mapping.usable:
            if mapping.unmapped:
                unmapped.append((t.coords(0), mapping.unmapped))
            continue
        if mapping.unmapped:
            unmapped.append((f"{Path(t.source_file).name} | {t.sheet}", mapping.unmapped))

        c = mapping.columns
        for i, r in t.df.reset_index(drop=True).iterrows():
            # Classified up front (before the quantity/date gates below) purely
            # so a malformed-quantity diagnostic can name the event/symbol it
            # concerns - classification itself does not depend on quantity.
            descriptor = " ".join(str(v) for v in r.values
                                  if isinstance(v, str) and v.strip())
            plan_type = G.classify_plan(descriptor)
            event = _event_for(profile, descriptor, plan_type)
            symbol = _symbol(profile, r, c, descriptor, doc.text or "")

            # NaN, not a silent zero, when present-but-unparseable - this is
            # the generic/synonym-driven fallback used for a broker with no
            # profile (and for a profile-driven table that is neither
            # pdf_sections nor closed_lot_gains), so a malformed quantity here
            # would otherwise disappear with no trace for any event type this
            # classifier can emit (VEST/BUY/SELL/TRANSFER_IN/TRANSFER_OUT/...).
            qty = G.to_number(r.get(c.get("quantity")), on_invalid="nan")
            if qty != qty:
                rec.invalid_quantities.append({
                    "role": "_extract_events", "event": event, "symbol": symbol,
                    "broker": broker, "source": t.coords(i),
                    "raw": repr(r.get(c.get("quantity"))),
                })
                continue
            if not qty:
                continue
            date = (G.to_date(r.get(c.get("acquired_date")))
                    or G.to_date(r.get(c.get("date")))
                    or G.to_date(r.get(c.get("sale_date"))))
            if date is None:
                continue

            # NaN, not a silent zero, when the price was present but
            # unparseable - this is the generic/synonym-driven fallback used
            # for a broker with no profile (and for a profile-driven table
            # that is neither pdf_sections nor closed_lot_gains), so a
            # malformed price here would otherwise reach build_lots() as a
            # real Rs.0 VEST/BUY price with no diagnostic (the existing
            # 1412859 NaN-price check never sees it, since it is not NaN).
            # The fallback to cost_total_fc is preserved for a malformed
            # price exactly as it already was for a blank/zero one - "not
            # price" alone is not enough, since NaN is truthy in Python and
            # would silently defeat the fallback.
            price = G.to_number(r.get(c.get("price_fc")), on_invalid="nan")
            price_ok = price == price
            if (not price or not price_ok) and c.get("cost_total_fc"):
                total = G.to_number(r.get(c.get("cost_total_fc")), on_invalid="nan")
                price = (total / qty) if qty else 0.0

            rows.append({
                "date": date, "broker": broker, "account_no": account or "",
                "symbol": symbol, "event": event, "quantity": qty,
                "price_fc": round(price, 6), "amount_fc": "", "tax_fc": "",
                "cost_fc": "",
                "currency": (str(r.get(c.get("currency"), "")) or "USD").upper()[:3],
                "notes": f"{plan_type} | {t.coords(i)}",
            })
            # A reinvested dividend is income as well as an acquisition.
            # `price == price` excludes NaN explicitly - NaN is truthy in
            # Python, so `and price` alone would fabricate a DIV row with an
            # unresolved (NaN) amount_fc for a malformed/unresolved price.
            if plan_type == "DIVIDEND_REINVESTMENT" and price and price == price:
                rows.append({
                    "date": date, "broker": broker, "account_no": account or "",
                    "symbol": symbol, "event": "DIV", "quantity": "",
                    "price_fc": "", "amount_fc": round(qty * price, 2), "tax_fc": "",
                    "cost_fc": "", "currency": "USD",
                    "notes": f"Dividend reinvested | {t.coords(i)}",
                })
    return pd.DataFrame(rows), unmapped


def _mapping_from_profile(profile: Profile, table) -> G.Mapping:
    cols, headers = {}, list(table.df.columns)
    lower = {h.lower(): h for h in headers}
    for canon, names in (profile.fields or {}).items():
        for n in (names if isinstance(names, list) else [names]):
            h = lower.get(str(n).lower())
            if h:
                cols[canon] = h
                break
    used = set(cols.values())
    m = G.Mapping(columns=cols, confidence=1.0,
                  unmapped=[h for h in headers if h not in used], detected=sorted(cols))
    if not m.usable:                       # profile did not fit this table
        return G.map_columns(headers)
    return m


def _event_for(profile: Profile, descriptor: str, plan_type: str) -> str:
    rules = (profile.rules or {}).get("event_from_text") if profile else None
    if rules:
        low = descriptor.lower()
        for key, ev in rules.items():
            if key != "default" and str(key).lower() in low:
                return ev
        if "default" in rules:
            return rules["default"]
    if plan_type in ("ESPP", "DIVIDEND_REINVESTMENT"):
        return "BUY"
    return G.classify_event(descriptor, default="VEST")


def _symbol(profile: Profile, row, cols, descriptor: str, doc_text: str = "") -> str:
    raw = str(row.get(cols.get("symbol"), "") or "").strip()
    if raw and len(raw) <= 6 and raw.isalpha():
        return raw.upper()
    text = f"{raw} {descriptor}".lower()
    table = (profile.rules or {}).get("symbol_from_text", {}) if profile else {}
    table = {**{"ibm": "IBM", "microsoft": "MSFT", "broadcom": "AVGO",
                "amazon": "AMZN", "qualcomm": "QCOM",
                "texas instruments": "TXN"}, **table}
    for k, v in table.items():
        if k in text:
            return v
    m = re.search(r"\(([A-Z]{1,6})\)", f"{raw} {descriptor}")
    if m:
        return m.group(1)
    # Last resort for an unfamiliar layout: the security is usually named
    # somewhere in the document even when it is not in this row.
    for k, v in table.items():
        if k in doc_text:
            return v
    m = re.search(r"\(([A-Z]{2,6})\)", doc_text.upper())
    if m:
        return m.group(1)
    return (raw.split()[0].upper() if raw else "UNKNOWN")


GOOGLE_FX_MARKERS = ("google_finance_formula", "googlefinance", "google finance",
                     "google_pair", "base_currency")


def is_google_fx_table(doc: tabular.Document) -> bool:
    """A populated Google Finance FX fallback table, returned by the preparer."""
    t = (doc.text or "").lower()
    return (("base_currency" in t or "google" in t)
            and ("googlefinance" in t or "google_pair" in t
                 or "google finance" in t))


def extract_google_fx(doc: tabular.Document) -> pd.DataFrame:
    """Read a populated Google Finance FX table back in.

    Accepts the table this engine emits, whether the rates were filled by Google
    Sheets or typed in. A row with no numeric rate is skipped, never read as a
    rate of zero - Google returning nothing for a date must stay unresolved.
    """
    out = []
    for t in doc.tables:
        low = {re.sub(r"\s+", " ", str(h).strip().lower()): h for h in t.df.columns}
        date_col = low.get("date") or next((low[h] for h in low if "date" in h), None)
        base_col = (low.get("base_currency") or low.get("currency")
                    or next((low[h] for h in low if "base" in h), None))
        rate_col = low.get("rate") or next((low[h] for h in low if "rate" in h), None)
        if not date_col or not rate_col:
            continue
        quote_col = low.get("quote_currency")
        for _, r in t.df.iterrows():
            d = G.to_date(r.get(date_col))
            v = G.to_number(r.get(rate_col))
            if not d or not v or v <= 0:
                continue
            base = str(r.get(base_col, "USD") or "USD").strip().upper()[:3]
            quote = str(r.get(quote_col, "INR") or "INR").strip().upper()[:3]
            out.append({
                "date": d, "base_currency": base, "quote_currency": quote,
                "rate": round(v, 6),
                "source": f"Google Finance historical FX (via {Path(t.source_file).name})",
                "retrieved_on": "", "status": "SUPPLIED", "notes": "",
            })
    if not out:
        return pd.DataFrame()
    return (pd.DataFrame(out)
            .drop_duplicates(subset=["date", "base_currency", "quote_currency"]))


# ----------------------------------------------------------------------
def extract_ttbr(doc: tabular.Document) -> pd.DataFrame:
    """Read a user-supplied SBI TTBR workbook whatever its column names."""
    out = []
    for t in doc.tables:
        headers = list(t.df.columns)
        low = {h.lower(): h for h in headers}
        date_col = next((low[h] for h in low if "date" in h), None)
        rate_col = next((low[h] for h in low
                         if any(k in h for k in ("ttbr", "tt buying", "buying rate",
                                                 "rate", "usd", "inr"))), None)
        cur_col = next((low[h] for h in low if "curren" in h or h.strip() == "ccy"), None)
        if not date_col or not rate_col:
            continue
        for _, r in t.df.iterrows():
            d = G.to_date(r.get(date_col))
            v = G.to_number(r.get(rate_col))
            if d and v:
                cur = str(r.get(cur_col, "USD") or "USD").upper()[:3]
                out.append({"date": d, "currency": cur, "ttbr": round(v, 4),
                            "source": f"User upload: {Path(t.source_file).name}",
                            "verified": "Y"})
    return pd.DataFrame(out).drop_duplicates(subset=["date", "currency"])
