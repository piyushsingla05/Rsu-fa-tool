"""Strict-mode regression for TableFXSource.lookup() (ECB / FBIL / Manual).

The defect this proves fixed: lookup(strict=True) used to ignore `strict`
entirely and always ran the 30-day nearest-date search - the same search
strict=False is supposed to reserve. That let a lower-ranked external source
(most visibly the ECB, once real data existed) answer with a NEARBY date
inside FXTable.resolve()'s first, strict pass, cutting in front of a
higher-ranked source's own legitimate carry-forward or nearest-date answer -
in direct contradiction of the frozen precedence and of resolve()'s own
documented contract ("each external source in turn gets its chance at the
EXACT date").

The fix: for ECB, FBIL and Manual, strict=True now returns an exact-date
match only. strict=False is completely unchanged for all of them - exact,
else nearest within MAX_NEAREST_DAYS, a tie going to the prior date, nothing
beyond the window.

Google Finance is deliberately EXEMPT from this. It is SBI's own designated
immediate backup - the "universal FX fallback" - and has always answered with
the nearest available date under strict=True too, so a weekend or holiday SBI
cannot quote is covered without waiting on SBI's own later carry/stale
fallback. GoogleFinanceSource.lookup() now overrides the base class
specifically to preserve that, unchanged. Section 7 below proves the
exemption; the pre-existing tests/fx_fallback_regression.py holiday/weekend
suite proves it end to end through the real resolver.

This file tests TableFXSource.lookup() directly, on ECB, FBIL and Google
instances, independent of FXTable.resolve()'s own call pattern - so what is
proved here does not depend on how resolve() happens to invoke a source.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.fx import FXTable, RANKED                             # noqa: E402
from src.fxsources import (                                    # noqa: E402
    ECB, ECBSource, FBIL, FBILSource, GOOGLE_FINANCE, GoogleFinanceSource,
    MANUAL, MAX_NEAREST_DAYS, SBI_TTBR,
)
from src.review import Register                                # noqa: E402

FXC = ROOT / "config" / "fx_rates.csv"

# The three UBS dates accepted as real input-data blockers - all TARGET
# working days, so the ECB carries each as a derived EUR-cross on the exact
# date. Same dates the freeze record and the real-data acceptance run use.
UBS_DATES = [dt.date(2023, 4, 3), dt.date(2023, 7, 3), dt.date(2024, 7, 1)]

ECB_ROWS = [
    (dt.date(2023, 4, 3), "EUR", "INR", 89.4340),
    (dt.date(2023, 4, 3), "EUR", "USD", 1.0906),
    (dt.date(2023, 7, 3), "EUR", "INR", 89.1500),
    (dt.date(2023, 7, 3), "EUR", "USD", 1.0885),
    (dt.date(2024, 7, 1), "EUR", "INR", 89.4600),
    (dt.date(2024, 7, 1), "EUR", "USD", 1.0721),
]


def ok(flag, label, detail=""):
    print(f"  {'PASS' if flag else 'FAIL'}  {label}{('  ' + detail) if detail else ''}")
    return 0 if flag else 1


def head(n, title):
    print()
    print("=" * 98)
    print(f"{n}. {title}")
    print("=" * 98)


def table(rows, source):
    return pd.DataFrame([
        {"date": d, "base_currency": b, "quote_currency": q, "rate": r,
         "source": source, "retrieved_on": "2026-09-05", "status": "FIXTURE",
         "notes": "test value"}
        for d, b, q, r in rows])


def main() -> int:
    fails = 0

    # ------------------------------------------------------------------
    head(1, "(a) STRICT=TRUE DOES NOT TAKE A NEAREST DATE")
    # ECB has a rate 3 days away from the requested date - nowhere near it.
    ecb_near = ECBSource(frame=table(
        [(dt.date(2023, 4, 6), "EUR", "INR", 89.5000),
         (dt.date(2023, 4, 6), "EUR", "USD", 1.0910)], "ECB"))
    q_strict = ecb_near.lookup(dt.date(2023, 4, 3), "USD", strict=True)
    fails += ok(q_strict is None,
                "ECB: a rate 3 days away is NOT returned under strict=True",
                f"got {q_strict.used_date if q_strict else None}")

    fbil_near = FBILSource(frame=table(
        [(dt.date(2023, 4, 6), "USD", "INR", 82.0800)], "FBIL"))
    q_strict_f = fbil_near.lookup(dt.date(2023, 4, 3), "USD", strict=True)
    fails += ok(q_strict_f is None,
                "FBIL: a rate 3 days away is NOT returned under strict=True",
                f"got {q_strict_f.used_date if q_strict_f else None}")

    # ------------------------------------------------------------------
    head(2, "(b) STRICT=FALSE STILL TAKES THE NEAREST DATE WITHIN 30 DAYS")
    q_loose = ecb_near.lookup(dt.date(2023, 4, 3), "USD", strict=False)
    fails += ok(q_loose is not None and q_loose.used_date == dt.date(2023, 4, 6)
                and q_loose.gap_days == 3,
                "ECB: strict=False still finds the 3-day-away rate",
                f"-> {q_loose.used_date:%d-%m-%Y}" if q_loose else "no quote")

    q_loose_f = fbil_near.lookup(dt.date(2023, 4, 3), "USD", strict=False)
    fails += ok(q_loose_f is not None and q_loose_f.used_date == dt.date(2023, 4, 6),
                "FBIL: strict=False still finds the 3-day-away rate",
                f"-> {q_loose_f.used_date:%d-%m-%Y}" if q_loose_f else "no quote")

    # ------------------------------------------------------------------
    head(3, "(c) AN EXACT DATE STILL WORKS UNDER STRICT=TRUE")
    ecb_exact = ECBSource(frame=table(
        [(dt.date(2023, 4, 3), "EUR", "INR", 89.4340),
         (dt.date(2023, 4, 3), "EUR", "USD", 1.0906)], "ECB"))
    q_exact = ecb_exact.lookup(dt.date(2023, 4, 3), "USD", strict=True)
    fails += ok(q_exact is not None and q_exact.used_date == dt.date(2023, 4, 3),
                "ECB: the exact published date resolves under strict=True",
                f"{q_exact.rate:.6f}" if q_exact else "no quote")
    fails += ok(q_exact is not None and q_exact.status == "EXACT_DATE",
                "ECB: and it is reported as an exact match, not a nearest one",
                str(q_exact.status) if q_exact else "no quote")

    # ------------------------------------------------------------------
    head(4, "(d) THE FROZEN SBI > GOOGLE > ECB PRECEDENCE IS PRESERVED")
    fails += ok(RANKED == (SBI_TTBR, GOOGLE_FINANCE, ECB, FBIL, MANUAL),
                "the chain is unchanged by this fix",
                " > ".join(RANKED))
    # SBI still wins outright wherever it has the exact date, even with a
    # fully populated ECB table sitting right behind it.
    fx = FXTable(FXC, register=Register(), ecb_frame=table(ECB_ROWS, "ECB"))
    sbi_dates = sorted(set(fx.df[fx.df["currency"] == "USD"]["date"]))
    d = sbi_dates[len(sbi_dates) // 2]
    q = fx.resolve(d, "USD")
    fails += ok(q is not None and q.source_type == SBI_TTBR,
                "SBI still wins outright wherever it has the exact date",
                f"{d:%d-%m-%Y} -> {q.source_type}" if q else "no quote")

    # ------------------------------------------------------------------
    head(5, "(e) THE THREE UBS BLOCKER DATES STILL RESOLVE THROUGH ECB")
    for d in UBS_DATES:
        fails += ok(fx.sbi.lookup(d, "USD", strict=True) is None,
                    f"{d:%d-%m-%Y}: confirmed SBI has no usable date here",
                    "(a precondition of this check, not the fix)")
        q = fx.resolve(d, "USD")
        fails += ok(q is not None and q.source_type == ECB,
                    f"{d:%d-%m-%Y}: resolves through the ECB",
                    f"{q.rate:.6f}" if q else "no quote")
        fails += ok(q is not None and q.used_date == d,
                    f"{d:%d-%m-%Y}: on the EXACT published date, not a nearby one",
                    f"{q.used_date:%d-%m-%Y}" if q else "no quote")

    # ------------------------------------------------------------------
    head(6, "(f) THE 30-DAY BOUNDARY IS UNCHANGED")
    on_edge = ECBSource(frame=table(
        [(dt.date(2023, 4, 3) + dt.timedelta(days=MAX_NEAREST_DAYS),
          "EUR", "INR", 89.6000),
         (dt.date(2023, 4, 3) + dt.timedelta(days=MAX_NEAREST_DAYS),
          "EUR", "USD", 1.0950)], "ECB"))
    q_edge = on_edge.lookup(dt.date(2023, 4, 3), "USD", strict=False)
    fails += ok(q_edge is not None and q_edge.gap_days == MAX_NEAREST_DAYS,
                f"exactly {MAX_NEAREST_DAYS} days away still resolves under strict=False",
                f"gap={q_edge.gap_days if q_edge else None}")

    beyond = ECBSource(frame=table(
        [(dt.date(2023, 4, 3) + dt.timedelta(days=MAX_NEAREST_DAYS + 1),
          "EUR", "INR", 89.7000),
         (dt.date(2023, 4, 3) + dt.timedelta(days=MAX_NEAREST_DAYS + 1),
          "EUR", "USD", 1.0960)], "ECB"))
    q_beyond = beyond.lookup(dt.date(2023, 4, 3), "USD", strict=False)
    fails += ok(q_beyond is None,
                f"beyond {MAX_NEAREST_DAYS} days it supplies nothing, even under "
                "strict=False",
                "unchanged from before this fix")

    # ------------------------------------------------------------------
    head(7, "GOOGLE IS EXEMPT - IT STILL TAKES THE NEAREST DATE UNDER STRICT=TRUE")
    google_near = GoogleFinanceSource(frame=table(
        [(dt.date(2023, 4, 6), "USD", "INR", 82.0900)], "Google Finance"))
    q_google_strict = google_near.lookup(dt.date(2023, 4, 3), "USD", strict=True)
    fails += ok(q_google_strict is not None
                and q_google_strict.used_date == dt.date(2023, 4, 6)
                and q_google_strict.source_type == GOOGLE_FINANCE,
                "Google: a rate 3 days away IS returned under strict=True "
                "(unlike ECB/FBIL)",
                f"-> {q_google_strict.used_date:%d-%m-%Y}"
                if q_google_strict else "no quote")

    print(f"\n{'ALL PASS' if fails == 0 else f'{fails} FAILURE(S)'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
