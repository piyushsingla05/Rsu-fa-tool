"""Generate a synthetic client + placeholder FX table so the engine can be
exercised end to end before any real broker statement arrives.

Every rate written here is marked verified=N on purpose: the engine must shout
about unverified rates, and this is the test that proves it does.
"""
import csv
import datetime as dt
import math
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
random.seed(7)


def next_month_end(d: dt.date) -> dt.date:
    """Given any month-end, return the next one. Stepping from the 28th of the
    SAME month lands back on the same month-end, so step from the 1st of the
    following month instead."""
    first_next = (d + dt.timedelta(days=1)).replace(day=1)
    first_after = (first_next + dt.timedelta(days=32)).replace(day=1)
    return first_after - dt.timedelta(days=1)

# ---------------------------------------------------------------- FX table
# Month-end USD placeholders. Real SBI TTBR values must replace these.
anchors = {
    2023: 83.10, 2024: 84.30, 2025: 87.90,
}
fx_rows = []
d = dt.date(2023, 1, 31)
while d <= dt.date(2025, 12, 31):
    base = anchors.get(d.year, 85.0)
    rate = round(base + math.sin(d.month / 2.0) * 0.9 + d.month * 0.10, 4)
    fx_rows.append([d.isoformat(), "USD", rate, "PLACEHOLDER - replace with SBI TTBR", "N"])
    d = next_month_end(d)

(ROOT / "config").mkdir(exist_ok=True)
with open(ROOT / "config" / "fx_rates.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["date", "currency", "ttbr", "source", "verified"])
    w.writerows(fx_rows)

# ------------------------------------------------------------ sample client
cdir = ROOT / "samples" / "SAMPLE_CLIENT"
cdir.mkdir(parents=True, exist_ok=True)

# ACME has full price data; ZED has none, to exercise the fallback path.
events = [["date", "broker", "account_no", "symbol", "event", "quantity",
           "price_fc", "amount_fc", "currency", "notes"]]
vest_dates = [dt.date(2023, 11, 15), dt.date(2024, 2, 15), dt.date(2024, 8, 15),
              dt.date(2025, 2, 17), dt.date(2025, 5, 15), dt.date(2025, 8, 15)]
for i, vd in enumerate(vest_dates):
    events.append([vd, "ETRADE", "X-88231", "ACME", "VEST", 25,
                   round(140 + i * 9.5, 2), "", "USD", f"RSU tranche {i+1}"])
events.append([dt.date(2025, 6, 12), "ETRADE", "X-88231", "ACME", "SELL", 40,
               196.40, 7856.00, "USD", "part sale"])
for q, (dd, amt) in enumerate([(dt.date(2025, 3, 14), 41.25),
                               (dt.date(2025, 6, 13), 44.10),
                               (dt.date(2025, 9, 12), 46.80),
                               (dt.date(2025, 12, 12), 49.20)]):
    events.append([dd, "ETRADE", "X-88231", "ACME", "DIV", "", "", amt, "USD",
                   f"Q{q+1} dividend, gross"])
# ZED: no price feed at all
events.append([dt.date(2024, 4, 10), "SHAREWORKS", "SW-4417", "ZED", "VEST", 60,
               72.30, "", "USD", "no price data - fallback path"])

with open(cdir / "events.csv", "w", newline="") as f:
    csv.writer(f).writerows(events)

prices = [["date", "symbol", "close_fc", "currency"]]
d = dt.date(2024, 12, 31)
px = 188.0
while d <= dt.date(2025, 12, 31):
    px = max(60.0, px * (1 + random.uniform(-0.035, 0.038)))
    prices.append([d.isoformat(), "ACME", round(px, 2), "USD"])
    d += dt.timedelta(days=7)
prices.append([dt.date(2025, 12, 31).isoformat(), "ACME", 231.60, "USD"])
with open(cdir / "prices.csv", "w", newline="") as f:
    csv.writer(f).writerows(prices)

cash = [["date", "broker", "account_no", "balance_fc", "currency"]]
d = dt.date(2025, 1, 31)
bal = 120.0
while d <= dt.date(2025, 12, 31):
    bal += random.uniform(-40, 900)
    cash.append([d.isoformat(), "ETRADE", "X-88231", round(max(bal, 0), 2), "USD"])
    d = next_month_end(d)
cash.append([dt.date(2025, 12, 31).isoformat(), "ETRADE", "X-88231", 1284.55, "USD"])
with open(cdir / "cash.csv", "w", newline="") as f:
    csv.writer(f).writerows(cash)

with open(cdir / "entities.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["symbol", "entity_name", "address", "zip", "country",
                "country_code", "nature_of_entity"])
    w.writerow(["ACME", "Acme Technologies Inc.", "1600 Amphi Way, Mountain View, CA",
                "94043", "United States of America", "2",
                "Listed company - equity shares"])
    w.writerow(["ZED", "Zed Systems Corp.", "500 Market St, San Francisco, CA",
                "94105", "United States of America", "2",
                "Listed company - equity shares"])

with open(cdir / "accounts.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["broker", "account_no", "institution_name", "address", "zip",
                "country", "country_code", "status", "opening_date"])
    w.writerow(["ETRADE", "X-88231", "Morgan Stanley Smith Barney LLC (E*TRADE)",
                "1585 Broadway, New York, NY", "10036",
                "United States of America", "2", "Owner", "2023-11-01"])

print("Sample client and placeholder FX table written.")
