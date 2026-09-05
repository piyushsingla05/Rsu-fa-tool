"""Third and fourth FX source regression - the ECB and FBIL.

What this proves:

    1. SBI TT buying rate              (unchanged - still wins outright)
    2. Google Finance historical FX    (unchanged - still second)
    3. ECB euro reference rate         EUR/INR direct, everything else DERIVED
    4. FBIL reference rate             preparer-supplied; USD/INR direct, and
                                       EUR, GBP, JPY are FBIL's own USD crosses
    5. preparer-entered rate
    6. no rate -> BLANK + FX_UNAVAILABLE

and, across all of them, that:

    * the first source that answers wins - a lower-ranked source is never
      preferred merely because its date is closer;
    * within a source, the exact date wins, else the nearest available date
      within 30 days either way, a tie going to the PRIOR date;
    * the date actually used is always recorded and never presented as the date
      required;
    * no source is ever labelled as an SBI TT buying rate;
    * a derived cross-rate carries its component rates and its arithmetic.

The FBIL and ECB rates in this file are TEST VALUES defined here. They are not
written to any shipped table and are not represented as published rates. This
sandbox has no outbound network, so nothing here was fetched; the ECB parser in
src/fxfetch.py is exercised against a local fixture for the same reason.

FBIL is never fetched at all - it is licensed benchmark data - so what is tested
is the preparer-supplied path: the loader that validates a transcribed table and
the per-pair methodology the engine then states.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.fx import FXTable                                    # noqa: E402
from src.fxfetch import (                                     # noqa: E402
    fbil_template, load_preparer_fbil, parse_ecb_hist, write_table,
)
from src.fxsources import (                                   # noqa: E402
    ECB, FBIL, GOOGLE_FINANCE, MANUAL, MAX_NEAREST_DAYS, PROVIDER_NAME,
    RANK, SBI_NOT_AVAILABLE, SBI_TTBR, pair_symbol,
)
from src.fx import RANKED                                     # noqa: E402
from src.review import FX_FALLBACK, FX_UNAVAILABLE, Register, Severity  # noqa: E402

FXC = ROOT / "config" / "fx_rates.csv"

SBI_DATE = dt.date(2025, 12, 31)      # on file in the shipped SBI table
SBI_RATE = 89.9619

# The three UBS dates accepted as input-data blockers.
UBS_DATES = [dt.date(2023, 4, 3), dt.date(2023, 7, 3), dt.date(2024, 7, 1)]


def ok(flag, label, detail=""):
    print(f"  {'PASS' if flag else 'FAIL'}  {label}{('  ' + detail) if detail else ''}")
    return 0 if flag else 1


def head(n, title):
    print()
    print("=" * 98)
    print(f"{n}. {title}")
    print("=" * 98)


def table(rows, source):
    """rows: (date, base, quote, rate)"""
    return pd.DataFrame([
        {"date": d, "base_currency": b, "quote_currency": q, "rate": r,
         "source": source, "retrieved_on": "2026-09-05", "status": "FIXTURE",
         "notes": "test value"}
        for d, b, q, r in rows])


def fbil(rows):
    return table(rows, PROVIDER_NAME[FBIL])


def ecb(rows):
    return table(rows, PROVIDER_NAME[ECB])


# FBIL fixture. USD/INR is the ONLY direct rupee rate; the published EUR, GBP
# and JPY rupee rates are FBIL's own crosses through USD.
FBIL_ROWS = [
    (dt.date(2023, 4, 3), "USD", "INR", 82.0500),
    (dt.date(2023, 7, 3), "USD", "INR", 81.9200),
    (dt.date(2024, 7, 1), "USD", "INR", 83.4500),
    (dt.date(2023, 4, 3), "GBP", "INR", 101.4200),
    (dt.date(2023, 4, 3), "JPY", "INR", 0.6190),
    (dt.date(2023, 4, 3), "EUR", "INR", 89.4000),
]

# The same day with only USD/INR and a cross-currency leg published, so the
# engine has to reproduce FBIL's own construction rather than read the figure.
FBIL_LEGS = [
    (dt.date(2023, 4, 3), "USD", "INR", 82.0500),
    (dt.date(2023, 4, 3), "EUR", "USD", 1.0906),
    (dt.date(2023, 4, 3), "USD", "JPY", 132.6400),
]

# ECB fixture - everything against the EURO. EUR/INR is the only direct INR pair.
ECB_ROWS = [
    (dt.date(2023, 4, 3), "EUR", "INR", 89.4340),
    (dt.date(2023, 4, 3), "EUR", "USD", 1.0906),
    (dt.date(2023, 4, 3), "EUR", "GBP", 0.8783),
    (dt.date(2023, 7, 3), "EUR", "INR", 89.1500),
    (dt.date(2023, 7, 3), "EUR", "USD", 1.0885),
    (dt.date(2024, 7, 1), "EUR", "INR", 89.4600),
    (dt.date(2024, 7, 1), "EUR", "USD", 1.0721),
]


def main() -> int:  # noqa: C901 - a checklist, deliberately linear
    fails = 0

    # ------------------------------------------------------------------
    head(1, "RANK ORDER - EACH SOURCE ONLY WHERE EVERY BETTER ONE IS SILENT")
    reg = Register()
    fx = FXTable(FXC, register=reg, fbil_frame=fbil(FBIL_ROWS),
                 ecb_frame=ecb(ECB_ROWS))
    fails += ok(RANKED == (SBI_TTBR, GOOGLE_FINANCE, ECB, FBIL, MANUAL),
                "the frozen chain is SBI > Google > ECB > FBIL > manual",
                " > ".join(RANKED))
    fails += ok([RANK[s_] for s_ in RANKED] == [1, 2, 3, 4, 5],
                "and the fallback levels follow it", str([RANK[s_] for s_ in RANKED]))

    q = fx.rate_quote(SBI_DATE, "USD")
    fails += ok(q.source_type == SBI_TTBR and abs(q.rate - SBI_RATE) < 1e-6,
                "SBI still wins outright where it has the date",
                f"{q.rate} - no other source is consulted at all")

    # Google present for the date -> Google, ahead of both ECB and FBIL.
    gfx = FXTable(FXC, register=Register(),
                  google_frame=table([(dt.date(2023, 4, 3), "USD", "INR", 82.1111)],
                                     "Google Finance historical FX"),
                  fbil_frame=fbil(FBIL_ROWS), ecb_frame=ecb(ECB_ROWS))
    qg = gfx.rate_quote(dt.date(2023, 4, 3), "USD")
    fails += ok(qg.source_type == GOOGLE_FINANCE and qg.fallback_level == 2,
                "Google Finance keeps rank 2 - neither ECB nor FBIL displaces it",
                f"{qg.source_type}, level {qg.fallback_level}")

    q3 = fx.rate_quote(dt.date(2023, 4, 3), "USD")
    fails += ok(q3.source_type == ECB and q3.fallback_level == RANK[ECB] == 3,
                "with no SBI and no Google rate, the ECB supplies it - ahead of "
                "FBIL", f"{q3.rate:.4f} at level {q3.fallback_level}")

    # FBIL only where the ECB is silent too.
    bfx = FXTable(FXC, register=Register(), fbil_frame=fbil(FBIL_ROWS),
                  ecb_frame=ecb([(dt.date(2023, 4, 3), "EUR", "GBP", 0.8783)]))
    qb = bfx.rate_quote(dt.date(2023, 4, 3), "USD")
    fails += ok(qb is not None and qb.source_type == FBIL
                and qb.fallback_level == RANK[FBIL] == 4,
                "where the ECB cannot form a rate, FBIL supplies it",
                f"USD/INR {qb.rate:.4f} at level {qb.fallback_level}")

    qe = q3   # the ECB quote, used again below

    mfx = FXTable(FXC, register=Register())
    mfx.manual.merge(table([(dt.date(2023, 4, 3), "SGD", "INR", 61.7000)],
                           "Preparer-entered rate"))
    qm = mfx.rate_quote(dt.date(2023, 4, 3), "SGD")
    fails += ok(qm is not None and qm.source_type == MANUAL
                and qm.fallback_level == 5,
                "a preparer-entered rate remains the lowest ranked source",
                f"level {qm.fallback_level} of {RANK['NONE']}")

    # ------------------------------------------------------------------
    head(2, "NO SOURCE IS EVER LABELLED AS AN SBI TT BUYING RATE")
    for q_, name in ((qb, "FBIL"), (qe, "ECB"), (qm, "manual")):
        b = q_.basis
        fails += ok("SBI TTBR unavailable" in b
                    and "TT buying rate" not in b.replace("SBI TTBR unavailable", ""),
                    f"the {name} basis says the TTBR was unavailable, and is not "
                    "called one", f'"{b[:78]}"')
        fails += ok(q_.sbi_availability == SBI_NOT_AVAILABLE,
                    f"and records SBI availability for the {name} rate",
                    q_.sbi_availability)
    fails += ok(PROVIDER_NAME[FBIL] in qb.basis and "FBIL" in qb.provider,
                "the FBIL basis names FBIL as the provider")
    fails += ok(PROVIDER_NAME[ECB] in qe.basis,
                "and the ECB basis names the ECB")
    fails += ok("volume-weighted" in qb.methodology.lower()
                and "11:30" in qb.methodology and "DIRECT" in qb.methodology,
                "FBIL's actual USD/INR methodology travels with the rate",
                qb.methodology[:64] + "...")
    fails += ok("information only" in qe.methodology.lower(),
                "and the ECB's information-only caveat travels with its rate")

    # ------------------------------------------------------------------
    head(3, "FBIL - ONLY USD/INR IS DIRECT; THE OTHER THREE ARE ITS OWN CROSSES")
    d = dt.date(2023, 4, 3)
    # FBIL alone, so nothing better answers first.
    only_fbil = FXTable(FXC, register=Register(), fbil_frame=fbil(FBIL_ROWS))

    qu = only_fbil.rate_quote(d, "USD")
    fails += ok(qu is not None and qu.source_type == FBIL
                and abs(qu.rate - 82.0500) < 1e-6
                and qu.rate_kind == "directly quoted" and not qu.is_derived,
                "USD/INR is FBIL's one DIRECT rupee rate", f"{qu.rate}")
    fails += ok("DIRECT" in qu.methodology and "11:30-12:30" in qu.methodology
                and "USD 25 million" in qu.methodology,
                "and carries FBIL's actual direct-rate methodology",
                "volume-weighted, 15-min window, +/-3SD")

    for cur, expect, leg in (("EUR", 89.4000, "EUR/USD"),
                             ("GBP", 101.4200, "GBP/USD"),
                             ("JPY", 0.6190, "USD/JPY")):
        qq = only_fbil.rate_quote(d, cur)
        fails += ok(qq is not None and qq.source_type == FBIL
                    and abs(qq.rate - expect) < 1e-6,
                    f"{cur}/INR is taken from FBIL as published", f"{qq.rate}")
        fails += ok(qq.rate_kind == "provider's own cross-rate",
                    f"but {cur}/INR is marked as FBIL's OWN CROSS, not a direct "
                    "quote", qq.rate_kind)
        fails += ok("NOT a direct rupee rate" in qq.methodology
                    and leg in qq.methodology,
                    f"and its methodology names the {leg} leg FBIL crossed "
                    "through", qq.methodology[:60] + "...")
        fails += ok("not a measured" in qq.derivation
                    and "taken as published" in qq.derivation,
                    "and says the figure was taken as published, not recomputed")

    fails += ok(qu.methodology != only_fbil.rate_quote(d, "EUR").methodology,
                "the direct and cross methodologies are different text",
                "- one provider, two different things")

    # Where the table carries the legs but not the published cross, the engine
    # reproduces FBIL's own construction and shows the arithmetic.
    legs = FXTable(FXC, register=Register(), fbil_frame=fbil(FBIL_LEGS))
    qe2 = legs.rate_quote(d, "EUR")
    want = 1.0906 * 82.0500
    fails += ok(qe2 is not None and abs(qe2.rate - want) < 1e-9
                and qe2.rate_kind == "derived cross-rate",
                "with only USD/INR and EUR/USD on file, FBIL's cross is "
                "reproduced", f"{qe2.rate:.4f}")
    fails += ok("EUR/USD 1.090600 x USD/INR 82.0500" in qe2.derivation,
                "the arithmetic is stated - the leg MULTIPLIES for EUR",
                qe2.derivation[:70] + "...")
    qj = legs.rate_quote(d, "JPY")
    fails += ok(qj is not None and abs(qj.rate - 82.0500 / 132.64) < 1e-9,
                "and DIVIDES for JPY, which is quoted per USD",
                f"{qj.rate:.6f}")
    fails += ok(len(qe2.components) == 2
                and {c[0] for c in qe2.components} == {"EUR/USD", "USD/INR"},
                "both legs are retained on the quote",
                " + ".join(f"{c[0]} {c[3]}" for c in qe2.components))
    fails += ok(all(c[2] == d for c in qe2.components),
                "from the SAME published day - never two dates spliced together")

    # A currency FBIL does not publish is not an FBIL rate at all.
    fails += ok(only_fbil.rate_quote(d, "CHF") is None,
                "a currency outside FBIL's four is not supplied as FBIL",
                "- CHF/INR is not an FBIL reference rate")

    head(4, "ECB - EUR/INR DIRECT, EVERY OTHER PAIR A TRANSPARENT CROSS-RATE")
    only_ecb = FXTable(FXC, register=Register(), ecb_frame=ecb(ECB_ROWS))
    qeur = only_ecb.rate_quote(d, "EUR")
    fails += ok(qeur.source_type == ECB and abs(qeur.rate - 89.4340) < 1e-6
                and not qeur.is_derived,
                "EUR/INR is taken directly - the ECB publishes it", f"{qeur.rate}")

    qusd = only_ecb.rate_quote(d, "USD")
    want = 89.4340 / 1.0906
    fails += ok(qusd.is_derived and abs(qusd.rate - want) < 1e-9,
                "USD/INR is DERIVED through the euro", f"{qusd.rate:.6f}")
    fails += ok(len(qusd.components) == 2
                and {c[0] for c in qusd.components} == {"EUR/INR", "EUR/USD"},
                "both component rates are retained on the quote",
                " + ".join(f"{c[0]} {c[3]}" for c in qusd.components))
    fails += ok(all(c[2] == d for c in qusd.components),
                "and both come from the SAME published day - never two dates "
                "spliced together")
    fails += ok("EUR/INR 89.4340 / EUR/USD 1.090600" in qusd.derivation
                and "does not publish USD/INR directly" in qusd.derivation,
                "the arithmetic is stated in words", qusd.derivation[:70] + "...")

    # A cross needs BOTH legs from one day; a missing leg yields no rate at all.
    half = FXTable(FXC, register=Register(),
                   ecb_frame=ecb([(d, "EUR", "INR", 89.4340)]))
    fails += ok(half.rate_quote(d, "USD") is None,
                "with EUR/INR but no EUR/USD, no USD rate is invented",
                "- an incomplete cross is not a rate")

    # ------------------------------------------------------------------
    head(5, "THE 30-DAY NEAREST-DATE RULE APPLIES TO EACH EXTERNAL SOURCE")
    # NOTE on strict vs non-strict, following the strict-mode fix: FXTable.
    # resolve() now asks every external source for the EXACT date only in its
    # one ranked pass (see fx_strict_mode_regression.py) - a lower-ranked
    # source's own nearest-date match must never cut in front of a
    # higher-ranked source's later carry/stale answer. resolve() never calls
    # an external source with strict=False; only SBI gets that, twice, after
    # the loop. So a source's OWN 30-day nearest-date mechanism - which is
    # still fully intact and still governed by the same MAX_NEAREST_DAYS
    # window - is exercised here by calling .lookup(strict=False) on the
    # source directly, exactly as a caller who explicitly wants a nearest
    # match (rather than the ranked comparison in resolve()) would. This is
    # the same property this suite has always tested; only the call site
    # changed to match the corrected architecture - the assertions and
    # expected values below are unchanged.
    near = FXTable(FXC, register=Register(),
                   fbil_frame=fbil([(dt.date(2023, 4, 6), "USD", "INR", 82.0800)]))
    qn = near.fbil.lookup(dt.date(2023, 4, 3), "USD", strict=False)
    fails += ok(qn is not None and qn.source_type == FBIL
                and qn.used_date == dt.date(2023, 4, 6) and qn.gap_days == 3
                and qn.status == "NEAREST_AVAILABLE_DATE",
                "FBIL: no exact date, so the nearest available one within 30 days",
                f"03-04 -> {qn.used_date:%d-%m-%Y}, 3d later")
    fails += ok(str(dt.date(2023, 4, 6).strftime("%d-%m-%Y")) in qn.basis
                and "nearest available" in qn.basis,
                "and the substituted date is stated, not passed off as the date "
                "required")

    far = FXTable(FXC, register=Register(),
                  fbil_frame=fbil([(dt.date(2023, 5, 15), "USD", "INR", 82.0800)]))
    fails += ok(far.fbil.lookup(dt.date(2023, 4, 3), "USD", strict=False) is None,
                f"beyond {MAX_NEAREST_DAYS} days FBIL supplies nothing",
                "- 42 days away is not a rate for the date")

    tie = FXTable(FXC, register=Register(),
                  fbil_frame=fbil([(dt.date(2023, 3, 29), "USD", "INR", 82.1000),
                                   (dt.date(2023, 4, 8), "USD", "INR", 82.3000)]))
    qt = tie.fbil.lookup(dt.date(2023, 4, 3), "USD", strict=False)
    fails += ok(qt.used_date == dt.date(2023, 3, 29) and qt.gap_days == 5,
                "equidistant either way, the PRIOR date wins",
                f"29-03 and 08-04 both 5d away -> {qt.used_date:%d-%m-%Y}")

    ecbn = FXTable(FXC, register=Register(),
                   ecb_frame=ecb([(dt.date(2023, 3, 31), "EUR", "INR", 89.2000),
                                  (dt.date(2023, 3, 31), "EUR", "USD", 1.0875)]))
    qen = ecbn.ecb.lookup(dt.date(2023, 4, 3), "USD", strict=False)
    fails += ok(qen is not None and qen.used_date == dt.date(2023, 3, 31)
                and all(c[2] == dt.date(2023, 3, 31) for c in qen.components),
                "the ECB obeys the same rule, and the cross still uses one day",
                f"-> {qen.used_date:%d-%m-%Y}")

    # Never reach into another year to find a rate.
    yr = FXTable(FXC, register=Register(),
                 fbil_frame=fbil([(dt.date(2019, 4, 3), "USD", "INR", 69.2000)]))
    fails += ok(yr.fbil.lookup(dt.date(2023, 4, 3), "USD", strict=False) is None,
                "a rate from a different year is never used to fill a date",
                "- 2019 does not answer for 2023")

    # ------------------------------------------------------------------
    head(6, "PROVENANCE RETAINED FOR EVERY RATE")
    aud = only_fbil.audit_frame()
    need = ["Date required", "Rate date used", "Gap (days)", "Currency",
            "Converted to", "Currency pair", "Rate", "FX source", "Provider",
            "Fallback level", "SBI TTBR availability", "Retrieval status",
            "Rate is", "Cross-rate components", "Derivation",
            "Provider methodology", "Basis stated in the working paper",
            "Review flag"]
    missing = [c for c in need if c not in aud.columns]
    fails += ok(not missing, "FX_WORKING carries every required provenance column",
                f"{len(need)} columns" if not missing else str(missing))

    ecb_aud = only_ecb.audit_frame()
    row = ecb_aud[(ecb_aud["Currency"] == "USD")].iloc[0]
    fails += ok(row["FX source"] == ECB and row["Fallback level"] == 3,
                "the source and its fallback level are both recorded",
                f'{row["FX source"]}, level {row["Fallback level"]}')
    fails += ok(row["Rate is"] == "derived cross-rate",
                "a derived rate is marked as derived, not as a quoted rate")
    fails += ok("EUR/INR on 03-04-2023" in row["Cross-rate components"]
                and "EUR/USD on 03-04-2023" in row["Cross-rate components"],
                "the component rates appear in the working paper",
                row["Cross-rate components"])
    fails += ok(row["Currency pair"] == pair_symbol("USD")
                and row["Converted to"] == "INR",
                "source currency, target currency and pair are all stated",
                row["Currency pair"])
    fails += ok(PROVIDER_NAME[ECB] in str(row["Provider"])
                and "DERIVED CROSS-RATE" in str(row["Review flag"]),
                "and the review flag names the provider and the derivation",
                str(row["Review flag"])[:70])

    fb_aud = only_fbil.audit_frame()
    fu = fb_aud[fb_aud["Currency"] == "USD"].iloc[0]
    fails += ok(fu["Rate is"] == "directly quoted"
                and fu["Cross-rate components"] == "",
                "FBIL's USD/INR is marked directly quoted, with no components",
                f'{fu["Rate is"]}')
    fg = fb_aud[fb_aud["Currency"] == "GBP"].iloc[0]
    fails += ok(fg["Rate is"] == "provider's own cross-rate",
                "but FBIL's GBP/INR is marked as the provider's own cross",
                f'{fg["Rate is"]}')
    fails += ok("GBP/USD" in str(fg["Provider methodology"])
                and "NOT a direct rupee rate" in str(fg["Provider methodology"]),
                "and its row carries the cross methodology, not the direct one")
    fails += ok("PROVIDER'S OWN CROSS-RATE" in str(fg["Review flag"]),
                "the review flag says so too", str(fg["Review flag"])[:70])
    fails += ok(fu["Provider methodology"] != fg["Provider methodology"],
                "two rows, one provider, two methodologies - as published")

    # ------------------------------------------------------------------
    head(7, "EVERY FALLBACK IS RAISED FOR REVIEW, NEVER APPLIED SILENTLY")
    reg7 = Register()
    # The ECB can form CHF/INR but not USD/INR here, so one rate comes from each
    # source and both must be flagged to the same standard.
    fx7 = FXTable(FXC, register=reg7, fbil_frame=fbil(FBIL_ROWS),
                  ecb_frame=ecb([(d, "EUR", "INR", 89.4340),
                                 (d, "EUR", "CHF", 0.9932)]))
    fx7.rate_quote(d, "USD")            # FBIL - the ECB has no EUR/USD leg
    fx7.rate_quote(d, "CHF")            # ECB, derived
    flags = [f for f in reg7.flags if f.reason == FX_FALLBACK]
    fails += ok(len(flags) == 2 and all(f.severity == Severity.REVIEW for f in flags),
                "one REVIEW entry per fallback rate", f"{len(flags)} entries")
    fails += ok(any(PROVIDER_NAME[FBIL] in f.detail for f in flags)
                and any(PROVIDER_NAME[ECB] in f.detail for f in flags),
                "each names the provider that actually supplied the rate")
    fails += ok(all("NOT an SBI TT buying rate" in f.detail for f in flags),
                "and each says plainly that it is not an SBI TT buying rate")
    chf_flag = [f for f in flags if "CHF" in f.subject][0]
    fails += ok("DERIVED" in chf_flag.detail and "Component rates" in chf_flag.detail,
                "the derived rate's flag carries the arithmetic and components")
    fails += ok("fallback level" in flags[0].detail,
                "and how far down the chain the rate came from")

    status = fx7.fx_status
    fails += ok(PROVIDER_NAME[FBIL] in status and PROVIDER_NAME[ECB] in status,
                "the SUMMARY FX status names every provider used", status[:96])
    fails += ok("derived cross-rate" in status,
                "and says a derived cross-rate is in the workbook")

    # ------------------------------------------------------------------
    head(8, "NOTHING IS INVENTED WHERE NO SOURCE HAS THE DATE")
    reg8 = Register()
    fx8 = FXTable(FXC, register=reg8, fbil_frame=fbil(FBIL_ROWS),
                  ecb_frame=ecb(ECB_ROWS))
    q8 = fx8.rate_quote(dt.date(2016, 2, 10), "SEK")
    fails += ok(q8 is None, "no quote at all", "10-02-2016 SEK")
    b = [f for f in reg8.flags if f.reason == FX_UNAVAILABLE]
    fails += ok(len(b) == 1 and b[0].severity == Severity.BLOCKER,
                "raised as an FX_UNAVAILABLE blocker")
    fails += ok(all(PROVIDER_NAME[s] in b[0].detail
                    for s in (SBI_TTBR, GOOGLE_FINANCE, FBIL, ECB, MANUAL)),
                "the blocker lists every source that was searched")
    aud8 = fx8.audit_frame()
    r8 = aud8[aud8["Currency"] == "SEK"].iloc[0]
    fails += ok(r8["Rate"] == "" and r8["Rate date used"] == "",
                "the rate and its date are BLANK - never zero, never a "
                "substitute", "both blank")

    # ------------------------------------------------------------------
    head(9, "THE THREE UBS DATES")
    print("  The FBIL/ECB fixtures below are TEST VALUES, not fetched rates -")
    print("  this sandbox has no outbound network. They prove the MECHANISM.")
    print()
    reg9 = Register()
    fx9 = FXTable(FXC, register=reg9, fbil_frame=fbil(FBIL_ROWS),
                  ecb_frame=ecb(ECB_ROWS))
    for u in UBS_DATES:
        qu = fx9.rate_quote(u, "USD")
        fails += ok(qu is not None and qu.source_type == ECB
                    and qu.status == "EXACT_DATE" and qu.is_derived,
                    f"{u:%d-%m-%Y} resolves from the ECB - rank 3, ahead of FBIL",
                    f"{qu.rate:.4f}, derived cross")
    only_b = FXTable(FXC, register=Register(), fbil_frame=fbil(FBIL_ROWS))
    for u in UBS_DATES:
        qu = only_b.rate_quote(u, "USD")
        fails += ok(qu is not None and qu.source_type == FBIL
                    and qu.status == "EXACT_DATE"
                    and qu.rate_kind == "directly quoted",
                    f"{u:%d-%m-%Y} would also resolve from FBIL, directly",
                    f"{qu.rate} - a Mumbai business day")

    # With the SHIPPED configuration - no fetched tables in this sandbox - the
    # three dates must still be blockers. The rule was not widened to clear them.
    reg9b = Register()
    fx9b = FXTable(FXC, register=reg9b)
    unresolved = 0
    for u in UBS_DATES:
        if fx9b.rate_quote(u, "USD") is None:
            unresolved += 1
    fails += ok(unresolved == 3,
                "with no fetched table present they REMAIN blockers",
                "- the 7-day and 30-day windows were not widened to reach them")
    fails += ok(not (ROOT / "config" / "fx_fbil.csv").exists()
                and not (ROOT / "config" / "fx_ecb.csv").exists(),
                "and no invented rate table has been shipped in config/",
                "- the tables are produced by src/fxfetch.py, deliberately")

    # ------------------------------------------------------------------
    head(10, "RETRIEVAL - PARSERS WORK ON DOWNLOADED DATA, OFFLINE")
    xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01"
 xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">
 <Cube>
  <Cube time="2023-04-03">
   <Cube currency="USD" rate="1.0906"/>
   <Cube currency="INR" rate="89.4340"/>
   <Cube currency="ZAR" rate="19.4"/>
  </Cube>
  <Cube time="2023-03-31">
   <Cube currency="USD" rate="1.0875"/>
   <Cube currency="INR" rate="89.2000"/>
  </Cube>
 </Cube>
</gesmes:Envelope>"""
    got = parse_ecb_hist(xml, currencies=("USD",))
    fails += ok(len(got) == 4,
                "the ECB history parser reads the published XML shape",
                f"{len(got)} rows, ZAR dropped by the currency filter")
    fails += ok(set(got["base_currency"]) == {"EUR"},
                "every ECB row is written base EUR - units per 1 EUR, as "
                "published")
    fx10 = FXTable(FXC, register=Register(), ecb_frame=got)
    q10 = fx10.rate_quote(dt.date(2023, 4, 3), "USD")
    fails += ok(q10 is not None and abs(q10.rate - 89.4340 / 1.0906) < 1e-9,
                "and the engine derives USD/INR straight from the parsed file",
                f"{q10.rate:.6f}")

    fails += ok(not hasattr(sys.modules["src.fxfetch"], "fetch_fbil"),
                "there is NO FBIL fetcher - it is licensed data the engine "
                "does not retrieve", "no endpoint, no scraping")
    fails += ok(list(fbil_template().columns)[:4]
                == ["date", "base_currency", "quote_currency", "rate"]
                and "fbil_publication" in fbil_template().columns,
                "the preparer template demands the FBIL publication per row",
                ", ".join(fbil_template().columns))

    supplied = pd.DataFrame([
        {"date": "2023-04-03", "base_currency": "USD", "quote_currency": "INR",
         "rate": 82.05, "fbil_publication": "FBIL Reference Rate 03-04-2023",
         "transcribed_by": "SP", "transcribed_on": "2026-09-05"},
        {"date": "2023-04-03", "base_currency": "EUR", "quote_currency": "INR",
         "rate": 89.40, "fbil_publication": "FBIL Reference Rate 03-04-2023",
         "transcribed_by": "SP", "transcribed_on": "2026-09-05"},
        # rejected: outside FBIL's four pairs
        {"date": "2023-04-03", "base_currency": "CHF", "quote_currency": "INR",
         "rate": 90.10, "fbil_publication": "FBIL Reference Rate 03-04-2023",
         "transcribed_by": "SP", "transcribed_on": "2026-09-05"},
        # rejected: no publication named
        {"date": "2023-04-04", "base_currency": "USD", "quote_currency": "INR",
         "rate": 82.12, "fbil_publication": "", "transcribed_by": "SP",
         "transcribed_on": "2026-09-05"},
        # rejected: zero
        {"date": "2023-04-05", "base_currency": "USD", "quote_currency": "INR",
         "rate": 0, "fbil_publication": "FBIL Reference Rate 05-04-2023",
         "transcribed_by": "SP", "transcribed_on": "2026-09-05"},
    ])
    loaded = load_preparer_fbil(supplied)
    fails += ok(len(loaded) == 2,
                "the loader keeps only attributed, in-scope, positive rows",
                f"{len(loaded)} of {len(supplied)} accepted")
    fails += ok(set(loaded["base_currency"]) == {"USD", "EUR"},
                "CHF is dropped - it is not one of FBIL's four rupee pairs")
    fails += ok(all(str(v) == "PREPARER_SUPPLIED" for v in loaded["status"]),
                "every row is marked PREPARER_SUPPLIED, never FETCHED",
                "- there is no fetch to claim")
    usd_note = loaded[loaded["base_currency"] == "USD"].iloc[0]["notes"]
    eur_note = loaded[loaded["base_currency"] == "EUR"].iloc[0]["notes"]
    fails += ok("DIRECT rate" in usd_note and "FBIL Reference Rate 03-04-2023"
                in usd_note,
                "the USD row names its publication and says it is direct")
    fails += ok("OWN CROSS" in eur_note and "not a measured rupee rate" in eur_note,
                "the EUR row says it is FBIL's own cross, not a rupee rate")
    fails += ok("End-Users Licence" in usd_note and "fee liable" in usd_note,
                "and every row carries FBIL's licence position",
                "- so no one downstream assumes it is free to use")

    round_trip = FXTable(FXC, register=Register(), fbil_frame=loaded)
    qrt = round_trip.rate_quote(dt.date(2023, 4, 3), "USD")
    fails += ok(qrt is not None and abs(qrt.rate - 82.05) < 1e-9
                and qrt.source_type == FBIL,
                "and the engine reads the loaded table straight back",
                f"{qrt.rate}")

    tmp = ROOT / "work" / "_fxtest" / "fx_ecb.csv"
    write_table(got, tmp)
    again = write_table(parse_ecb_hist(xml, currencies=("USD",)), tmp)
    reread = pd.read_csv(again)
    fails += ok(len(reread) == 4,
                "re-running the fetcher does not duplicate rows", f"{len(reread)}")
    tmp.unlink()
    tmp.parent.rmdir()

    print()
    print(f"RESULT: {'ALL CHECKS PASSED' if not fails else str(fails) + ' CHECK(S) FAILED'}")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
