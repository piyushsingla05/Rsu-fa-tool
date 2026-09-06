"""Regression tests for the acceptance-harness compatibility fix.

This does NOT touch the tax engine, the FX hierarchy, or the frozen
acceptance baseline. It tests only the new column-reconciliation helpers in
tests/acceptance_realdata.py, added so Stage 7 can tell an intentional,
reviewed rename of an INTERNAL (underscore-prefixed) audit column - such as
build_a3()'s `_end_ok` -> `_peak_ok` + `_close_val_ok` split in f6a2329 -
apart from a real schema regression, while remaining strict about every
user-facing column and about any internal-column change that isn't on the
explicit compatibility list.

Run: python3 -m tests.acceptance_harness_regression
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.acceptance_realdata import (              # noqa: E402
    INTERNAL_COLUMN_COMPAT, _reconcile_columns, _reindex_rows,
)

PASS, FAIL = "PASS", "FAIL"
_fails = 0


def check(ok, label, detail=""):
    global _fails
    status = PASS if ok else FAIL
    print(f"  {status:<6} {label}{('  ' + detail) if detail else ''}")
    if not ok:
        _fails += 1


USER = ["Sr. No", "Symbol", "Amount (Rs.)"]


def section(title):
    print()
    print("=" * 90)
    print(title)
    print("=" * 90)


# ======================================================================
section("1. IDENTICAL COLUMNS - every frame, unconditionally, unchanged behaviour")

base_cols = USER + ["_a", "_b"]
cur_cols = USER + ["_a", "_b"]
ok, detail, common = _reconcile_columns("sales", base_cols, cur_cols)
check(ok, "identical column lists are accepted", detail)
check(common == base_cols, "and every column, including internal, is common",
      str(common))

# ======================================================================
section("2. THE RECORDED A3 MIGRATION - _end_ok -> _peak_ok + _close_val_ok")

a3_base = USER + ["_basis", "_init_ok", "_end_ok", "_broker"]
a3_cur = USER + ["_basis", "_init_ok", "_peak_ok", "_close_val_ok", "_broker"]
ok, detail, common = _reconcile_columns("a3", a3_base, a3_cur)
check(ok, "the exact f6a2329 migration is accepted for a3", detail)
check("_end_ok" not in common and "_peak_ok" not in common
      and "_close_val_ok" not in common,
      "the migrated columns are excluded from cell-level comparison, "
      "neither the old nor the new name", str(common))
check(common == USER + ["_basis", "_init_ok", "_broker"],
      "every user-facing column plus every UNCHANGED internal column "
      "remains comparable", str(common))

check(not _reconcile_columns("sales", a3_base, a3_cur)[0],
      "the SAME migration is NOT auto-accepted on a frame it was never "
      "recorded for (sales, not a3)")

# ======================================================================
section("3. STRICTNESS - a user-facing column change is NEVER tolerated")

check(not _reconcile_columns("a3", USER + ["_x"], USER[:-1] + ["_x"])[0],
      "a removed user-facing column still fails")
check(not _reconcile_columns(
    "a3", USER + ["_x"], USER + ["Extra Column (Rs.)", "_x"])[0],
    "an added user-facing column still fails")
reordered = [USER[1], USER[0], USER[2]] + ["_x"]
check(not _reconcile_columns("a3", USER + ["_x"], reordered)[0],
      "a reordered user-facing column still fails")
renamed = USER[:-1] + ["Amount Rs.", "_x"]
check(not _reconcile_columns("a3", USER + ["_x"], renamed)[0],
      "a renamed user-facing column still fails")

# ======================================================================
section("4. STRICTNESS - internal-column changes not on the list still fail")

check(not _reconcile_columns("a3", USER + ["_mystery"], USER)[0],
      "an unexplained internal column REMOVED (not the recorded rename) fails")
check(not _reconcile_columns("a3", USER, USER + ["_mystery"])[0],
      "an unexplained internal column ADDED still fails")

# A partial migration - only one of the two documented replacements shows up.
partial = USER + ["_basis", "_init_ok", "_peak_ok", "_broker"]  # missing _close_val_ok
ok, detail, _ = _reconcile_columns(
    "a3", USER + ["_basis", "_init_ok", "_end_ok", "_broker"], partial)
check(not ok, "a PARTIAL migration (only _peak_ok, not _close_val_ok) fails",
      detail)

# An internal column disappearing with nothing replacing it - not a
# migration, just a loss - still fails even on the frame with a compat entry.
dropped = USER + ["_basis", "_init_ok", "_broker"]  # _end_ok gone, nothing added
ok, detail, _ = _reconcile_columns(
    "a3", USER + ["_basis", "_init_ok", "_end_ok", "_broker"], dropped)
check(not ok, "_end_ok simply vanishing with no replacement still fails", detail)

# ======================================================================
section("5. _reindex_rows - name-based realignment, not positional")

orig_cols = USER + ["_basis", "_init_ok", "_end_ok", "_broker"]
rows = [["1", "AAPL", "100", "b1", True, False, "UBS"]]
got = _reindex_rows(rows, orig_cols, USER + ["_basis", "_init_ok", "_broker"])
check(got == [["1", "AAPL", "100", "b1", True, "UBS"]],
      "reindexing drops the excluded column and keeps the rest in the "
      "requested order, by name", str(got))

# The mirror case: current-side rows, aligned to the split columns, reindexed
# down to the same common set (the migrated columns dropped from THIS side).
cur_cols = USER + ["_basis", "_init_ok", "_peak_ok", "_close_val_ok", "_broker"]
cur_rows = [["1", "AAPL", "100", "b1", True, True, False, "UBS"]]
got2 = _reindex_rows(cur_rows, cur_cols, USER + ["_basis", "_init_ok", "_broker"])
check(got2 == [["1", "AAPL", "100", "b1", True, "UBS"]],
      "the current-side row reduces to the identical common shape as the "
      "baseline-side row", str(got2))
check(got == got2, "both sides land on the exact same shape for the "
      "cell-level comparison that follows", f"{got} == {got2}")

# ======================================================================
print()
print("=" * 90)
if _fails:
    print(f"RESULT: {_fails} CHECK(S) FAILED")
else:
    print("RESULT: ALL CHECKS PASSED")
print("=" * 90)

if __name__ == "__main__":
    sys.exit(1 if _fails else 0)
