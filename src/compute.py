"""Computation engine.

Schedule FA - Table A3, one row per lot still held at period end:
    Initial value = held qty x vested price      x TTBR on the VEST date
    Peak value    = held qty x annual high price x TTBR on the PERIOD-END date
    Closing value = held qty x period-end close  x TTBR on the PERIOD-END date

Limited-statement mode: where the statement set cannot establish historical
acquisition FX, the initial value converts the SAME cost at the PERIOD-END TTBR.
The cost is never invented - only the FX date is approximated, and every row says so.

Peak quantity is the period-end quantity. That is a deliberate simplification, and
it understates the peak whenever the holding shrank during the year, so any
security whose quantity fell is flagged rather than passed silently.

Disposals are not all sales. Shares surrendered to fund tax (TAX_WITHHOLD) reduce
the holding but produce no proceeds and no capital gain; a sell-to-cover, where
shares are genuinely sold on market, is a SELL and does both.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import pandas as pd

from .fx import FXTable
from .marketdata import MarketData
from .models import (
    ACQUIRING_EVENTS, DISPOSING_EVENTS, PROCEEDS_EVENTS, BUY, DIV, DIV_TAX,
    SELL, SPLIT, TAX_WITHHOLD, TRANSFER_IN, TRANSFER_OUT, VEST, FX_SAME_DAY,
    Lot, Period, SaleMatch,
)
from .fxsources import FALLBACK_BANNER
from .review import (
    AGG_1042S, DIV_SOURCE_CONFLICT, FA_FX_GAP, FX_UNAVAILABLE, LIMITED_STATEMENT,
    MISSING_DIV_DATES, MISSING_VEST_DATE, PEAK_QTY_FELL, PEAK_QTY_ROSE,
    PERIOD_END_FX_BASIS, RECON_BREAK, Register, SALE_TTBR_FALLBACK,
    TRANSFER_COST_MISSING, UNVERIFIED_FX,
)

LTCG_DAYS = 730


def _already_netted(lot, disposal_date) -> bool:
    """True when a disposal is already reflected in a broker-stated position.

    A stated position is the holding AFTER everything up to its date, so a sale
    on or before that date must not reduce it a second time. E*TRADE prints its
    holdings on a trade-date basis with a sale traded the same day - without
    this, the shares come off twice.
    """
    return (lot.event == "POSITION"
            and disposal_date <= (lot.stated_at or lot.acquired))


def _rate_or_flag(fx: FXTable, register: Register, date: dt.date, cur: str,
                   purpose: str, subject: str, source: str) -> float:
    """fx.rate(), but an unresolved date is flagged rather than passed on mute.

    Returns 0.0 on an unresolved date, exactly as fx.rate() itself would -
    the arithmetic and the resulting total are unchanged - but the gap is now
    raised on REVIEW_REQUIRED instead of disappearing into the total with no
    trace. Callers that can instead leave the whole figure blank on an
    unresolved date should keep using rate_quote() directly and gate the
    cell on the quote, the way build_a3's own initial/peak/closing values do.

    Design note (why a partial, flagged total and not a blanked one): this is
    deliberately NOT the same choice build_a3 makes for Initial Value, where
    ONE unresolvable vest date blanks the WHOLE weighted average - that is a
    genuine average, and averaging over only the resolvable lots would silently
    change what the figure MEANS. A2 balance/credited and A3 dividend/proceeds
    are plain sums of independent, dated amounts: each resolvable date's
    contribution is a real, correct figure on its own, so summing the ones that
    ARE resolvable and flagging the one that is not (understated, never
    fabricated) is the more informative choice, and is the same "known
    limitation, disclosed rather than blanked" pattern the engine already uses
    for PEAK_QTY_FELL (peak value understates on disposals by convention,
    flagged, not blanked). Blanking the entire total on one bad date among many
    would also risk changing a real acceptance-run figure with no clear
    requirement forcing that; this fix only had to make the existing gap
    visible, not change what is computed.
    """
    q = fx.rate_quote(date, cur, FX_SAME_DAY, purpose=purpose)
    if q is None:
        register.review(
            FA_FX_GAP, subject,
            f"No FX source can price {cur} for {date:%d-%m-%Y}. This date's "
            f"contribution to \"{purpose}\" has been left at nil rather than "
            "converted at an unavailable rate, so the total is understated "
            "to that extent.",
            source,
            "Supply the missing FX rate for this date, or accept the total "
            "as understated to that extent.")
    return q.rate if q else 0.0


@dataclass
class ComputeOptions:
    ltcg_days: int = LTCG_DAYS
    a3_granularity: str = "lot"          # "lot" or "entity"
    limited_statement: bool = False      # period-end FX basis for initial value


@dataclass
class ComputeResult:
    a3: pd.DataFrame
    a2: pd.DataFrame
    vesting: pd.DataFrame
    sales: pd.DataFrame
    dividends: pd.DataFrame
    form1042s: pd.DataFrame
    fsi: pd.DataFrame
    reconciliation: pd.DataFrame
    summary: dict = field(default_factory=dict)
    dividend_basis: str = ""


# ======================================================================
# Lot tracking and FIFO matching
# ======================================================================
def build_lots(events: pd.DataFrame, register: Register, as_at=None):
    """Replay events to get lots and FIFO-matched disposals.

    `as_at` matters whenever a statement extends beyond the reporting period -
    Morgan Stanley's runs to Jul-2026 for a CY2025 return. Without it, sales made
    after the period end would be deducted from the period-end holding and
    Schedule FA would be understated.
    """
    ev = events.sort_values(["date", "symbol"]).copy()
    if as_at is not None:
        ev = ev[ev["date"] <= as_at]
    open_lots: dict[str, list[Lot]] = {}
    matches: list[SaleMatch] = []
    all_lots: list[Lot] = []

    for _, r in ev.iterrows():
        sym, kind = r["symbol"], r["event"]
        qty = float(r["quantity"] or 0)

        if kind in ACQUIRING_EVENTS:
            # A stated position that names its lot's own trade date carries the
            # REAL acquisition date, so Schedule FA converts the initial value
            # at the vest date rather than at the statement date. The statement
            # date is kept separately - it is what says which disposals the
            # position already reflects.
            acq = r["date"]
            stated_lot = r.get("acquired_on")
            if isinstance(stated_lot, str):
                stated_lot = stated_lot.strip() or None
            if stated_lot is not None and not isinstance(stated_lot, dt.date):
                try:
                    stated_lot = pd.to_datetime(stated_lot).date()
                except Exception:
                    stated_lot = None
            if stated_lot is not None and kind == "POSITION":
                acq = stated_lot
            if kind == TRANSFER_IN:
                # A transfer-in with no cost basis would otherwise silently
                # become a zero-cost lot, overstating any later capital gain
                # by the full sale proceeds. Never invent the cost - flag it
                # instead, so the lot is not relied on until the cost is
                # supplied. (Other acquiring events - VEST/BUY/POSITION -
                # carry a broker-stated price and are unaffected.)
                try:
                    _transfer_cost = float(r["price_fc"] or 0)
                except (TypeError, ValueError):
                    _transfer_cost = 0.0
                if not (_transfer_cost == _transfer_cost) or _transfer_cost <= 0:
                    register.blocker(
                        TRANSFER_COST_MISSING, sym,
                        f"TRANSFER_IN of {qty:g} shares on {acq:%d-%m-%Y} carries "
                        "no cost basis. Recording it at zero cost would overstate "
                        "any later capital gain by the full sale proceeds, so the "
                        "lot is flagged rather than costed at zero.",
                        str(r.get("broker", "")),
                        "Obtain the transferring broker's cost basis for this "
                        "lot, or confirm the cost manually, before relying on "
                        "any gain computed from it.")
            lot = Lot(symbol=sym, acquired=acq, quantity=qty,
                      price_fc=float(r["price_fc"] or 0), currency=r["currency"],
                      broker=r["broker"], account_no=str(r["account_no"]), event=kind,
                      stated_at=r["date"])
            open_lots.setdefault(sym, []).append(lot)
            all_lots.append(lot)

        elif kind in DISPOSING_EVENTS:
            # A closed-lot record carries its own acquisition date, so the match
            # is a fact, not an inference. Still consume open lots where they
            # exist so the holding stays right, but never raise a shortfall.
            stated_acq = r.get("acquired_on")
            if isinstance(stated_acq, str):
                stated_acq = stated_acq.strip() or None
            if stated_acq is not None and not isinstance(stated_acq, dt.date):
                try:
                    stated_acq = pd.to_datetime(stated_acq).date()
                except Exception:
                    stated_acq = None

            stated = r.get("cost_fc")
            try:
                stated = float(stated) if str(stated).strip() not in ("", "nan") else None
            except (TypeError, ValueError):
                stated = None
            first_match = len(matches)
            remaining = qty
            if stated_acq is not None and kind in PROCEEDS_EVENTS:
                matches.append(SaleMatch(
                    symbol=sym, sold_on=r["date"], acquired_on=stated_acq,
                    quantity=qty, sale_price_fc=float(r["price_fc"] or 0),
                    cost_price_fc=0.0, currency=r["currency"],
                    holding_days=(r["date"] - stated_acq).days))
                for lot in open_lots.get(sym, []):        # keep holdings correct
                    if remaining <= 1e-9:
                        break
                    if _already_netted(lot, r["date"]):
                        continue
                    take = min(lot.remaining, remaining)
                    lot.remaining -= take
                    remaining -= take
                remaining = 0.0                            # record is complete
            for lot in open_lots.get(sym, []):
                if remaining <= 1e-9:
                    break
                if lot.remaining <= 1e-9 or _already_netted(lot, r["date"]):
                    continue
                take = min(lot.remaining, remaining)
                lot.remaining -= take
                remaining -= take
                if kind in PROCEEDS_EVENTS:
                    matches.append(SaleMatch(
                        symbol=sym, sold_on=r["date"], acquired_on=lot.acquired,
                        quantity=take, sale_price_fc=float(r["price_fc"] or 0),
                        cost_price_fc=lot.price_fc, currency=r["currency"],
                        holding_days=(r["date"] - lot.acquired).days))
            # A broker using specific-lot identification states the cost it
            # actually removed. Spread it across the FIFO slices pro rata so the
            # total gain is the broker's, while each slice keeps its own dates.
            if stated is not None and kind in PROCEEDS_EVENTS:
                slices = matches[first_match:]
                filled = sum(m.quantity for m in slices)
                if filled > 1e-9:
                    for m in slices:
                        m.stated_cost_fc = stated * (m.quantity / filled)

            if remaining > 1e-6 and stated is not None and kind in PROCEEDS_EVENTS:
                # Cost is stated by the broker, so nothing needs inferring from
                # lots. Only the holding period is unknown, and build_cg flags it.
                # The cost is attached here because the pro-rata allocation above
                # has already run against the (empty) FIFO slice list.
                unmatched = SaleMatch(
                    symbol=sym, sold_on=r["date"], acquired_on=None,
                    quantity=remaining, sale_price_fc=float(r["price_fc"] or 0),
                    cost_price_fc=0.0, currency=r["currency"], holding_days=0)
                unmatched.stated_cost_fc = stated * (remaining / qty) if qty else stated
                matches.append(unmatched)
                remaining = 0.0

            if remaining > 1e-6:
                if kind in PROCEEDS_EVENTS:
                    matches.append(SaleMatch(
                        symbol=sym, sold_on=r["date"], acquired_on=None,
                        quantity=remaining, sale_price_fc=float(r["price_fc"] or 0),
                        cost_price_fc=0.0, currency=r["currency"], holding_days=0))
                register.blocker(
                    MISSING_VEST_DATE, sym,
                    f"Disposal of {qty:g} on {r['date']:%d-%m-%Y} exceeds tracked "
                    f"holdings by {remaining:g} shares - no acquisition lot to match.",
                    str(r.get("notes", "")),
                    "Supply the earlier statement or an opening holding, or confirm "
                    "the cost of acquisition manually.")

            # Whatever the broker's own realised gain/loss report stated travels
            # with the disposal, so the working paper can show the broker's
            # figures beside the engine's own arithmetic.
            _attach_broker_figures(r, matches[first_match:], qty)

        elif kind == SPLIT:
            ratio = float(r["quantity"] or 1)
            for lot in open_lots.get(sym, []):
                lot.quantity *= ratio
                lot.remaining *= ratio
                lot.price_fc /= ratio if ratio else 1

    return all_lots, matches


# ======================================================================
# Schedule FA - Table A3
# ======================================================================
def build_a3(events, lots, entities, md: MarketData, fx: FXTable,
             period: Period, opts: ComputeOptions, register: Register):
    ent = entities.set_index("symbol").to_dict("index") if not entities.empty else {}
    rows = []

    div_by_sym, proceeds_by_sym = {}, {}
    for sym in sorted(set(events["symbol"].dropna())):
        div_by_sym[sym] = _sum_dividends(events, sym, fx, period, register)
        proceeds_by_sym[sym] = _sum_proceeds(events, sym, fx, period, register)

    held = [l for l in lots if l.remaining > 1e-9 and l.acquired <= period.end]
    by_symbol: dict[str, list[Lot]] = {}
    for l in held:
        by_symbol.setdefault(l.symbol, []).append(l)

    rate_end = None
    for sym in sorted(by_symbol):
        meta = ent.get(sym, {})
        sym_lots = sorted(by_symbol[sym], key=lambda l: l.acquired)
        cur = sym_lots[0].currency
        q_end = fx.rate_quote(period.end, cur, FX_SAME_DAY, purpose="A3 peak & closing")
        rate_end = q_end.rate if q_end else 0.0
        end_ok = q_end is not None
        high, close, high_ok, close_ok, _ = md.resolve(sym, period.end)

        _flag_peak_quantity(events, sym, period, register)

        groups = ([(sym_lots, sym_lots[0].acquired)] if opts.a3_granularity == "entity"
                  else [([l], l.acquired) for l in sym_lots])

        first = True
        for grp, acq in groups:
            qty = sum(l.remaining for l in grp)
            wavg_price = (sum(l.remaining * l.price_fc for l in grp) / qty) if qty else 0.0

            if opts.limited_statement:
                rate_init, init_ok = rate_end, end_ok
                basis = "Limited statement data - period-end TTBR basis"
            else:
                rate_init, init_ok = 0.0, True
                fallback_dates = []
                for l in grp:
                    q = fx.rate_quote(l.acquired, cur, FX_SAME_DAY,
                                      purpose=f"A3 initial value {sym}")
                    if q is None:
                        # One unresolvable vest date makes the whole weighted
                        # average unusable. It is left blank, not part-converted.
                        init_ok = False
                        continue
                    if q.is_fallback:
                        fallback_dates.append((q.fallback_banner, l.acquired))
                    rate_init += l.remaining * l.price_fc * q.rate
                denom = qty * wavg_price
                rate_init = (rate_init / denom) if denom else 0.0
                basis = "Vest-date TTBR"
                if fallback_dates:
                    basis = ("Vest-date FX - "
                             + "; ".join(sorted({b for b, _ in fallback_dates}))
                             + " for "
                             + ", ".join(f"{d:%d-%m-%Y}"
                                         for _, d in fallback_dates))
            if q_end is not None and q_end.is_fallback:
                basis += f" | period-end FX: {q_end.fallback_banner}"

            rows.append({
                "Sr. No": len(rows) + 1,
                "Country Name": meta.get("country", "United States of America"),
                "Name of Entity": meta.get("entity_name", sym),
                "Address of Entity": meta.get("address", ""),
                "ZIP Code": meta.get("zip", ""),
                "Nature of Entity": meta.get("nature_of_entity",
                                             "Listed company - equity shares"),
                "Date of Acquiring the Interest": acq,
                "Initial Value of the Investment (Rs.)": (
                    round(qty * wavg_price * rate_init, 0) if init_ok else ""),
                "Peak Value of Investment During the Period (Rs.)": (
                    round(qty * high * rate_end, 0) if (end_ok and high_ok) else ""),
                "Closing Value (Rs.)": (
                    round(qty * close * rate_end, 0) if (end_ok and close_ok) else ""),
                "Total Gross Amount Paid/Credited w.r.t. the Holding (Rs.)":
                    round(div_by_sym.get(sym, 0.0), 0) if first else 0,
                "Total Gross Proceeds from Sale/Redemption (Rs.)":
                    round(proceeds_by_sym.get(sym, 0.0), 0) if first else 0,
                "_symbol": sym, "_qty": round(qty, 4),
                "_vest_price_fc": round(wavg_price, 4),
                "_rate_vest": round(rate_init, 4),
                "_high_fc": high, "_close_fc": close, "_rate_end": rate_end,
                "_basis": basis, "_init_ok": init_ok,
                "_peak_ok": end_ok and high_ok, "_close_val_ok": end_ok and close_ok,
                "_broker": ", ".join(sorted({l.broker for l in grp})),
            })
            first = False

    if opts.limited_statement and rows:
        register.review(
            PERIOD_END_FX_BASIS, "All securities",
            "Initial value converted at the period-end TTBR because the statement "
            "set does not establish historical acquisition FX. The cost itself is "
            "taken from the statement and has not been estimated.",
            "Limited statement mode",
            "Obtain the full statement set to restore vest-date conversion, or "
            "accept and disclose this basis.")

    reported = {r["_symbol"] for r in rows}
    for sym, amt in proceeds_by_sym.items():
        if sym not in reported and amt:
            meta = ent.get(sym, {})
            rows.append({
                "Sr. No": len(rows) + 1,
                "Country Name": meta.get("country", "United States of America"),
                "Name of Entity": meta.get("entity_name", sym),
                "Address of Entity": meta.get("address", ""), "ZIP Code": meta.get("zip", ""),
                "Nature of Entity": meta.get("nature_of_entity",
                                             "Listed company - equity shares"),
                "Date of Acquiring the Interest": "",
                "Initial Value of the Investment (Rs.)": 0,
                "Peak Value of Investment During the Period (Rs.)": 0,
                "Closing Value (Rs.)": 0,
                "Total Gross Amount Paid/Credited w.r.t. the Holding (Rs.)":
                    round(div_by_sym.get(sym, 0.0), 0),
                "Total Gross Proceeds from Sale/Redemption (Rs.)": round(amt, 0),
                "_symbol": sym, "_qty": 0, "_vest_price_fc": 0, "_rate_vest": 0,
                "_high_fc": 0, "_close_fc": 0, "_rate_end": rate_end or 0,
                "_basis": "Fully sold during the period", "_broker": "",
            })
    return pd.DataFrame(rows)


def _flag_peak_quantity(events, sym, period, register: Register) -> None:
    """The period-end-quantity convention can misstate peak in EITHER direction.

    Understatement: shares disposed of during the period were held earlier
    but are excluded from the period-end quantity the peak is based on.
    Overstatement: shares acquired (vested/bought/transferred in) during the
    period are INCLUDED in that same period-end quantity, even though they
    were not actually held on the date the annual high occurred - if that
    high fell before the acquisition, the peak is computed on shares that,
    on that date, did not yet exist in the holding.

    The convention itself (period-end quantity x annual high) is unchanged
    and stays exactly as agreed; both flags are disclosure only.
    """
    ev = events[(events["symbol"] == sym)
                & (events["date"] >= period.start) & (events["date"] <= period.end)]
    disposed = sum(float(r["quantity"] or 0) for _, r in ev.iterrows()
                   if r["event"] in DISPOSING_EVENTS)
    if disposed > 1e-6:
        register.review(
            PEAK_QTY_FELL, sym,
            f"{disposed:,.4f} shares left the holding during the period. Peak value "
            "uses the PERIOD-END quantity by agreed convention, so the peak is "
            "understated to the extent shares were held earlier and disposed of.",
            "Agreed A3 convention",
            "Confirm the convention is acceptable for this client, or compute the "
            "peak on the quantity actually held on the high-price date.")

    acquired = sum(float(r["quantity"] or 0) for _, r in ev.iterrows()
                   if r["event"] in (VEST, BUY, TRANSFER_IN))
    if acquired > 1e-6:
        register.review(
            PEAK_QTY_ROSE, sym,
            f"{acquired:,.4f} shares were acquired during the period. Peak value "
            "uses the PERIOD-END quantity by agreed convention, so to the extent "
            "the annual high fell BEFORE these shares were acquired, the peak may "
            "be overstated relative to what was actually held on the high-price "
            "date.",
            "Agreed A3 convention",
            "Confirm the convention is acceptable for this client, or compute the "
            "peak on the quantity actually held on the high-price date.")


# ======================================================================
# Schedule FA - Table A2
# ======================================================================
def build_a2(cash, accounts, events, fx, period, register: Register):
    rows = []
    if accounts.empty:
        register.review("Table A2 not produced", "", "No custodial account master data.",
                        "accounts.csv", "Add the custodian details to produce A2.")
        return pd.DataFrame()

    for _, acct in accounts.iterrows():
        broker = acct["broker"]
        # An account master with no number is normal for an equity-plan account
        # the statement never prints. Never let a missing value reach the working
        # paper as the text "nan".
        acno = str(acct["account_no"])
        acno = "" if acno.strip().lower() in ("nan", "none") else acno
        label = f"{broker} {acno}".strip() or str(broker)
        cur, peak_inr, closing_inr = "USD", 0.0, 0.0
        sub = pd.DataFrame()
        if not cash.empty:
            sub = cash[(cash["broker"] == broker)
                       & (cash["account_no"].astype(str) == acno)].copy()
        if sub.empty:
            register.review(
                LIMITED_STATEMENT, label,
                "No cash balance data, so A2 peak and closing show as nil.",
                "cash.csv", "Confirm the account held no un-swept cash, or supply "
                "the balance data.")
        else:
            sub["date"] = pd.to_datetime(sub["date"]).dt.date
            sub = sub[(sub["date"] >= period.start) & (sub["date"] <= period.end)]
            if "currency" in sub and len(sub):
                cur = sub["currency"].iloc[0]
            vals = [(c["date"], float(c["balance_fc"])
                     * _rate_or_flag(fx, register, c["date"], cur,
                                     f"A2 balance {acno}", label, "cash.csv"))
                    for _, c in sub.iterrows()]
            peak_inr = max((v for _, v in vals), default=0.0)
            end = [v for d, v in vals if d == period.end]
            closing_inr = end[0] if end else (vals[-1][1] if vals else 0.0)

        credited = 0.0
        if not events.empty:
            ev = events[(events["broker"] == broker)
                        & (events["account_no"].astype(str) == acno)]
            for _, r in ev.iterrows():
                if not (period.start <= r["date"] <= period.end):
                    continue
                if r["event"] in (DIV, SELL):
                    amt = float(r["amount_fc"] or 0)
                    if not amt and r["event"] == SELL:
                        amt = float(r["quantity"] or 0) * float(r["price_fc"] or 0)
                    credited += amt * _rate_or_flag(
                        fx, register, r["date"], cur, f"A2 credit {acno}",
                        label, str(r.get("notes", "") or "events"))
        rows.append({
            "Sr. No": len(rows) + 1,
            "Country Name": acct.get("country", "United States of America"),
            "Name of Financial Institution": acct.get("institution_name", ""),
            "Address of Financial Institution": acct.get("address", ""),
            "ZIP Code": acct.get("zip", ""),
            "Account Number": acct.get("account_no", ""),
            "Status": acct.get("status", "Owner"),
            "Account Opening Date": acct.get("opening_date", ""),
            "Peak Balance During the Period (Rs.)": round(peak_inr, 0),
            "Closing Balance (Rs.)": round(closing_inr, 0),
            "Gross Amount Paid/Credited During the Period (Rs.)": round(credited, 0),
        })
    return pd.DataFrame(rows)


# ======================================================================
# Capital gains
# ======================================================================
def _attach_broker_figures(row, slices, qty: float) -> None:
    """Carry a broker's own stated gain figures onto the matched slices.

    Supporting data only - the engine still computes the rupee gain itself from
    proceeds and cost. Split pro rata where one disposal matched several lots, so
    the parts still add back to what the broker stated.
    """
    def num(key):
        v = row.get(key)
        try:
            return float(v) if str(v).strip() not in ("", "nan", "None") else None
        except (TypeError, ValueError):
            return None

    gain, adj_cost, adj_gain = (num("broker_gain_fc"), num("broker_adj_cost_fc"),
                                num("broker_adj_gain_fc"))
    ref = str(row.get("source_ref") or "").strip()
    if gain is adj_cost is adj_gain is None and not ref:
        return
    filled = sum(m.quantity for m in slices) or qty
    for m in slices:
        share = (m.quantity / filled) if filled else 1.0
        m.source_ref = ref
        if gain is not None:
            m.broker_gain_fc = gain * share
        if adj_cost is not None:
            m.broker_adj_cost_fc = adj_cost * share
        if adj_gain is not None:
            m.broker_adj_gain_fc = adj_gain * share


def build_cg(matches, fx: FXTable, opts: ComputeOptions, period: Period,
             register: Register) -> pd.DataFrame:
    rows = []
    for m in matches:
        if not (period.start <= m.sold_on <= period.end):
            continue
        # The SALE-date TTBR, always. Never a period-end rate: the sale has a
        # date and the rate for that date is the one the statute wants.
        q_sale = fx.rate_quote(m.sold_on, m.currency, FX_SAME_DAY,
                               purpose="CG sale consideration")
        rate_sale = q_sale.rate if q_sale else 0.0
        sale_rate_date = q_sale.used_date if q_sale else m.sold_on
        sale_src = q_sale.source_type if q_sale else "NONE"
        cost_rate_date, q_cost = None, None
        if m.acquired_on:
            q_cost = fx.rate_quote(m.acquired_on, m.currency, FX_SAME_DAY,
                                   purpose="CG cost of acquisition")
            rate_cost = q_cost.rate if q_cost else 0.0
            cost_rate_date = q_cost.used_date if q_cost else None
            cost_basis = ("Vest-date TTBR" if (q_cost and not q_cost.is_fallback)
                          else "Vest-date FX - "
                               + (q_cost.fallback_banner if q_cost
                                  else FALLBACK_BANNER))
            term = ("Long Term" if m.holding_days > opts.ltcg_days else "Short Term")
        else:
            rate_cost, cost_rate_date, q_cost = rate_sale, sale_rate_date, q_sale
            cost_basis = ("Sale-date TTBR (acquisition date unavailable)"
                          if (q_sale and not q_sale.is_fallback)
                          else "Sale-date FX (acquisition date unavailable) - "
                               + (q_sale.fallback_banner if q_sale
                                  else FALLBACK_BANNER))
            term = "Undeterminable"
            stated_note = (" The cost itself is broker-stated, so only the holding "
                           "period is missing." if m.stated_cost_fc is not None else "")
            register.blocker(
                SALE_TTBR_FALLBACK, m.symbol,
                f"Sale on {m.sold_on:%d-%m-%Y} carries no acquisition date, so the "
                "Indian short/long-term classification cannot be determined and the "
                "cost is converted at the sale-date TTBR." + stated_note,
                "Lot matching", "Supply the vesting record, or confirm the "
                "acquisition date and holding period manually.")
        proceeds = m.quantity * m.sale_price_fc * rate_sale
        if m.stated_cost_fc is not None:
            cost_fc_per_share = m.stated_cost_fc / m.quantity if m.quantity else 0.0
            cost = m.stated_cost_fc * rate_cost
            cost_basis += " - broker-stated cost"
        else:
            cost_fc_per_share = m.cost_price_fc
            cost = m.quantity * m.cost_price_fc * rate_cost
        if m.broker_adj_cost_fc is not None:
            cost_basis += (" (Adjusted Cost Basis - includes the ordinary income "
                           "already taxed as a perquisite)")
        # A missing rate is not a rate of zero. Where either conversion has no
        # rate on file the rupee figures are NOT computed, because a cost of nil
        # would report the entire sale proceeds as gain - an error far larger and
        # far less visible than a blank cell beside a blocker.
        computable = bool(rate_sale) and bool(rate_cost)
        # Each rupee column is blanked against the rate IT actually needs, and
        # every rendering of this row - screen, API and workbook - applies the
        # same test:
        #   consideration  needs the SALE-date rate
        #   cost           needs the COST-date rate
        #   gain           needs BOTH, because it is their difference
        # A figure that IS determinable is not thrown away, and a figure that is
        # not is BLANK, never a rupee nil. `proceeds` is qty x price x rate, so
        # with no sale rate it computes to zero - and a consideration of nil is a
        # wrong figure that looks like a real one, which is worse than a blank
        # cell beside a blocker.
        rows.append({
            "Symbol": m.symbol, "Date of Acquisition": m.acquired_on or "",
            "Date of Sale": m.sold_on, "Quantity": m.quantity,
            "Holding Period (days)": m.holding_days or "",
            "Nature of Gain": term,
            "Sale Price per Share (FC)": m.sale_price_fc,
            "FX Rate - Sale Date": rate_sale or "",
            "Full Value of Consideration (Rs.)": (round(proceeds, 0) if rate_sale
                                                  else ""),
            "Vested Price per Share (FC)": round(cost_fc_per_share, 6),
            "FX Rate - Vest Date": rate_cost or "",
            "Cost of Acquisition (Rs.)": round(cost, 0) if rate_cost else "",
            "Capital Gain (Rs.)": round(proceeds - cost, 0) if computable else "",
            "Cost Conversion Basis": cost_basis,
            # ---- backend provenance: what the broker said and which rate was used
            "Currency": m.currency,
            "Proceeds (FC)": round(m.quantity * m.sale_price_fc, 2),
            "Cost of Acquisition (FC)": round(
                m.stated_cost_fc if m.stated_cost_fc is not None
                else m.quantity * m.cost_price_fc, 2),
            "Broker Adjusted Gain/Loss (FC)": (
                "" if m.broker_adj_gain_fc is None else round(m.broker_adj_gain_fc, 2)),
            "Broker Ordinary Gain/Loss (FC)": (
                "" if m.broker_gain_fc is None else round(m.broker_gain_fc, 2)),
            "FX Date Used - Sale": sale_rate_date or "",
            "FX Date Used - Cost": cost_rate_date or "",
            "FX Source - Sale": sale_src,
            "FX Source - Cost": (q_cost.source_type if q_cost else "NONE"),
            "Computable": "Yes" if computable else "No - FX unavailable",
            "Source Document": m.source_ref,
        })
    return pd.DataFrame(rows)


# ======================================================================
# Dividends: transaction-level vs Form 1042-S
# ======================================================================
def coverage_gaps(intervals, period: Period):
    """Parts of the reporting period no source document covers."""
    spans = sorted((s, e) for s, e in intervals if s and e)
    gaps, cursor = [], period.start
    for s, e in spans:
        if e < period.start or s > period.end:
            continue
        s, e = max(s, period.start), min(e, period.end)
        if s > cursor:
            gaps.append((cursor, s - dt.timedelta(days=1)))
        cursor = max(cursor, e + dt.timedelta(days=1))
        if cursor > period.end:
            break
    if cursor <= period.end:
        gaps.append((cursor, period.end))
    return gaps


def build_dividends(events, form1042s, fx: FXTable, period: Period,
                    register: Register, txn_intervals=None):
    """Decide which dividend source feeds Schedule FSI, then build both views.

    Precedence is decided on ACTUAL reporting-period coverage, not on amounts.
    If the documents that supplied transaction-level dividends span the whole
    reporting period, transaction-level data is authoritative and the 1042-S is
    evidence only. If they leave any gap, the 1042-S is authoritative and the
    transaction rows are excluded from FSI. Never both.
    """
    rows = []
    ev = events[events["event"].isin([DIV, DIV_TAX])]
    for _, r in ev.iterrows():
        if not (period.start <= r["date"] <= period.end):
            continue
        cur = r["currency"]
        gross, tax = float(r["amount_fc"] or 0), float(r.get("tax_fc") or 0)
        if r["event"] == DIV_TAX:
            gross, tax = 0.0, float(r["amount_fc"] or 0)
        # A dividend has a payment date, so it converts at THAT date's rate -
        # both the income and the foreign tax withheld on it.
        q = fx.rate_quote(r["date"], cur, FX_SAME_DAY, purpose="Dividend conversion")
        rate = q.rate if q else 0.0
        rows.append({
            "Date": r["date"], "Symbol": r["symbol"], "Currency": cur,
            "Gross Dividend (FC)": gross, "Foreign Tax Withheld (FC)": tax,
            "FX Rate": rate if q else "",
            "Gross Dividend (Rs.)": round(gross * rate, 0) if q else "",
            "Foreign Tax Withheld (Rs.)": round(tax * rate, 0) if q else "",
            "Net Received (Rs.)": round((gross - tax) * rate, 0) if q else "",
            "FX Source": q.source_type if q else "NONE",
            "FX Basis": (q.basis if q else
                         "FX_UNAVAILABLE - not converted, see REVIEW_REQUIRED"),
            "Source": r.get("notes", ""),
        })
    div = pd.DataFrame(rows)

    # ---- Form 1042-S, converted in aggregate at the period-end TTBR ----
    f_rows = []
    if not form1042s.empty:
        for _, r in form1042s.iterrows():
            cur = r.get("currency", "USD")
            # A 1042-S carries no payment dates, so the agreed aggregate
            # period-end basis stands. That is a stated methodology, not a
            # silent substitution, and the Basis column says so on every row.
            q = fx.rate_quote(period.end, cur, FX_SAME_DAY,
                              purpose="1042-S aggregate conversion")
            rate = q.rate if q else 0.0
            gross, tax = float(r["gross_income_fc"] or 0), float(r["tax_withheld_fc"] or 0)
            f_rows.append({
                "Tax Year": r.get("tax_year", ""), "Broker": r.get("broker", ""),
                "Payer": r.get("payer", ""), "Income Code": r.get("income_code", ""),
                "Gross Dividend Income (FC)": gross,
                "Foreign Tax Withheld (FC)": tax, "Currency": cur,
                # No period-end rate means no conversion, not a conversion at
                # nil - the rate cell and both rupee cells stay blank.
                "Conversion Rate (period-end TTBR)": rate or "",
                "Gross Dividend Income (Rs.)": (round(gross * rate, 0) if rate
                                                else ""),
                "Foreign Tax Withheld (Rs.)": (round(tax * rate, 0) if rate
                                               else ""),
                "Source Document": r.get("source_file", ""),
                "Basis": "1042-S aggregate conversion - period-end TTBR used because "
                         "transaction-level dividend dates were unavailable",
                "Status": "For review",
            })
        register.review(
            AGG_1042S, "Dividends",
            "1042-S totals converted in aggregate at the period-end TTBR. No "
            "individual dividend dates or rates have been invented.",
            ", ".join(str(r.get("source_file", "")) for _, r in form1042s.iterrows()),
            "Confirm the aggregate basis is acceptable for this client.")
    f1042 = pd.DataFrame(f_rows)

    # ---- Precedence, decided on actual reporting-period coverage ----
    gaps = coverage_gaps(txn_intervals or [], period)
    covers = not gaps
    gap_text = "; ".join(f"{a:%d-%m-%Y} to {b:%d-%m-%Y}" for a, b in gaps)

    if not f1042.empty and not div.empty:
        if covers:
            basis = "Transaction-level"
            register.note(
                DIV_SOURCE_CONFLICT, "Dividends",
                "Transaction-level dividend documents span the whole reporting "
                "period, so they are authoritative and feed Schedule FSI. The 1042-S "
                "is retained as evidence only - it is NOT added.",
                "Both sources present", "None - no double count.")
        else:
            basis = "Form 1042-S"
            register.review(
                DIV_SOURCE_CONFLICT, "Dividends",
                "Transaction-level dividend documents do not cover the whole "
                f"reporting period (uncovered: {gap_text}). The 1042-S is "
                "authoritative and feeds Schedule FSI; the transaction rows are "
                "EXCLUDED from FSI to avoid a double count.",
                "Both sources present",
                "Supply statements covering the gap if date-wise conversion is wanted.")
    elif not div.empty and not covers:
        basis = "Transaction-level"
        register.review(
            LIMITED_STATEMENT, "Dividends",
            f"Dividend documents do not cover the whole reporting period "
            f"(uncovered: {gap_text}) and there is no 1042-S to fall back on. "
            "Dividend income may be understated.",
            "Coverage check", "Supply the missing statements or a 1042-S.")
    elif not f1042.empty:
        basis = "Form 1042-S"
        register.review(
            MISSING_DIV_DATES, "Dividends",
            "Dividend information is available only from the 1042-S; no payment "
            "dates exist, so aggregate period-end conversion is used.",
            "1042-S only", "Accept the aggregate basis or obtain transaction history.")
    else:
        basis = "Transaction-level"

    # ---- Schedule FSI, built from whichever source won ----
    if basis == "Form 1042-S" and not f1042.empty:
        fsi = f1042.groupby("Payer", as_index=False).agg(**{
            "Gross Income (Rs.)": ("Gross Dividend Income (Rs.)", "sum"),
            "Foreign Tax Withheld (Rs.)": ("Foreign Tax Withheld (Rs.)", "sum")})
        fsi = fsi.rename(columns={"Payer": "Source"})
    elif not div.empty:
        fsi = div.groupby("Symbol", as_index=False).agg(**{
            "Gross Income (Rs.)": ("Gross Dividend (Rs.)", "sum"),
            "Foreign Tax Withheld (Rs.)": ("Foreign Tax Withheld (Rs.)", "sum")})
        fsi = fsi.rename(columns={"Symbol": "Source"})
    else:
        fsi = pd.DataFrame()

    if not fsi.empty:
        fsi.insert(0, "Country", "United States of America")
        fsi["Head of Income"] = "Income from Other Sources"
        fsi["Basis"] = basis
        # Withheld and claimed are deliberately separate columns.
        fsi["Foreign Tax Credit Claimed (Rs.)"] = ""
        fsi["Relief Section"] = "Section 90 - India-USA DTAA"
        fsi = fsi[["Country", "Source", "Head of Income", "Gross Income (Rs.)",
                   "Foreign Tax Withheld (Rs.)", "Foreign Tax Credit Claimed (Rs.)",
                   "Relief Section", "Basis"]]
        register.review(
            "Foreign tax credit not yet determined", "Schedule FSI",
            "Foreign tax WITHHELD is stated. The credit CLAIMABLE is a separate "
            "question - relief under section 90 is capped at the lower of foreign "
            "tax paid and Indian tax on the doubly-taxed income.",
            "FSI working",
            "Compute the admissible credit and file Form 67 BEFORE the return.")
    return div, f1042, fsi, basis


# ======================================================================
# Reconciliation
# ======================================================================
POSITION_MARKER = "position stated by the broker"


def build_reconciliation(events, lots, period: Period, register: Register):
    rows = []
    for sym in sorted(set(events["symbol"].dropna())):
        ev = events[events["symbol"] == sym]
        # Where the holding is the broker's own stated position, the quantity
        # walk is informational: the closing figure does not depend on it, and a
        # break means the earlier statements are missing, not that A3 is wrong.
        broker_stated = any(POSITION_MARKER in str(n) for n in ev.get("notes", []))
        opening = _qty(ev[ev["date"] < period.start])
        inside = ev[(ev["date"] >= period.start) & (ev["date"] <= period.end)]

        def total(kinds):
            return sum(float(r["quantity"] or 0) for _, r in inside.iterrows()
                       if r["event"] in kinds)

        vested = total({"VEST"})
        bought = total({"BUY", "POSITION"})
        tin = total({TRANSFER_IN})
        sold = total({SELL})
        withheld = total({TAX_WITHHOLD})
        tout = total({TRANSFER_OUT})

        # A split multiplies the holding, so the walk needs an explicit term for
        # the shares it creates. Without one, every split security breaks.
        split_adj, running = 0.0, opening
        for _, r in inside.sort_values("date").iterrows():
            q = float(r["quantity"] or 0)
            if r["event"] in ACQUIRING_EVENTS:
                running += q
            elif r["event"] in DISPOSING_EVENTS:
                running -= q
            elif r["event"] == SPLIT and q:
                added = running * (q - 1)
                split_adj += added
                running += added

        computed = opening + vested + bought + tin + split_adj - sold - withheld - tout
        actual = sum(l.remaining for l in lots
                     if l.symbol == sym and l.acquired <= period.end)
        diff = actual - computed
        ok = abs(diff) < 1e-4
        rows.append({
            "Symbol": sym, "Opening Quantity": round(opening, 4),
            "Vested": round(vested, 4), "Purchased": round(bought, 4),
            "Transferred In": round(tin, 4),
            "Corporate Action Adjustment": round(split_adj, 4),
            "Sold": round(sold, 4),
            "Withheld for Tax": round(withheld, 4), "Transferred Out": round(tout, 4),
            "Expected Closing": round(computed, 4),
            "Closing per Lots": round(actual, 4),
            "Difference": round(diff, 4),
            "Status": ("Reconciled" if ok else
                       ("Broker-stated holding" if broker_stated else "BREAK")),
        })
        if not ok and broker_stated:
            register.review(
                RECON_BREAK, sym,
                f"Quantity walk gives {computed:,.4f} against the broker-stated "
                f"holding of {actual:,.4f} (difference {diff:,.4f}). The closing "
                "figure is the broker's own stated position and does not depend on "
                "the walk; the gap is missing earlier statements.",
                "Reconciliation",
                "Unable to fully reconcile - source statement coverage is "
                "incomplete. Confirm the stated position is the one to report.")
        elif not ok:
            register.blocker(
                RECON_BREAK, sym,
                f"Expected closing {computed:,.4f} vs {actual:,.4f} from lots "
                f"(difference {diff:,.4f}).",
                "Reconciliation",
                "Unable to fully reconcile - source statement coverage is likely "
                "incomplete. Do not file until resolved.")
    return pd.DataFrame(rows)


def build_cross_broker(events, transfers, lots, period: Period, register: Register):
    """Reconcile a security held across more than one broker.

        acquired - sales - transfers out + transfers in - sales = period-end

    The linkage is only asserted where the documents establish it. Where a
    broker reports no acquisitions and no transfers out, the chain cannot be
    closed and that is reported, not guessed around.
    """
    if events.empty:
        return pd.DataFrame()
    brokers = sorted({str(b) for b in events["broker"].dropna().unique()})
    if len(brokers) < 2:
        return pd.DataFrame()

    tf = pd.DataFrame(transfers) if transfers else pd.DataFrame()
    rows = []
    for sym in sorted(set(events["symbol"].dropna())):
        ev = events[(events["symbol"] == sym) & (events["date"] <= period.end)]
        if ev["broker"].nunique() < 2:
            continue
        holding = sum(l.remaining for l in lots
                      if l.symbol == sym and l.acquired <= period.end)
        detail, unresolved = [], []
        for b in brokers:
            bev = ev[ev["broker"] == b]
            if bev.empty:
                continue
            acq = sum(float(r["quantity"] or 0) for _, r in bev.iterrows()
                      if r["event"] in ("VEST", "BUY"))
            sold = sum(float(r["quantity"] or 0) for _, r in bev.iterrows()
                       if r["event"] == SELL)
            t_in = t_out = 0.0
            if not tf.empty:
                sub = tf[(tf["symbol"] == sym) & (tf["broker"] == b)
                         & (tf["date"] <= period.end)]
                t_in = float(sub[sub["direction"] == "IN"]["quantity"].sum())
                t_out = float(sub[sub["direction"] == "OUT"]["quantity"].sum())
            if sold and not acq and not t_out:
                unresolved.append(
                    f"{b} reports {sold:,.0f} shares sold but no acquisitions and "
                    "no transfer out")
            detail.append({
                "Security": sym, "Broker": b,
                "Acquired / Vested": round(acq, 4),
                "Sold": round(sold, 4),
                "Transferred In": round(t_in, 4),
                "Transferred Out": round(t_out, 4),
            })
        net = sum(d["Acquired / Vested"] + d["Transferred In"]
                  - d["Sold"] - d["Transferred Out"] for d in detail)
        linked = abs(net - holding) < 1e-4 and not unresolved
        for d in detail:
            d["Combined Period-End Holding"] = round(holding, 4)
            d["Chain Total"] = round(net, 4)
            d["Status"] = ("Linked" if linked else "Transfer linkage incomplete")
            rows.append(d)
        if not linked:
            register.review(
                "Transfer linkage incomplete", sym,
                (f"Chain across {', '.join(brokers)} gives {net:,.0f} against a "
                 f"period-end holding of {holding:,.0f}. "
                 + ("; ".join(unresolved) if unresolved else
                    "The documents do not establish which lots moved between "
                    "accounts.")),
                "Cross-broker reconciliation",
                "Obtain the transferring broker's vesting and transfer records. "
                "No linkage has been assumed.")
    return pd.DataFrame(rows)


def _qty(ev: pd.DataFrame) -> float:
    """Quantity walk including corporate actions, in date order."""
    t = 0.0
    for _, r in ev.sort_values("date").iterrows():
        q = float(r["quantity"] or 0)
        if r["event"] in ACQUIRING_EVENTS:
            t += q
        elif r["event"] in DISPOSING_EVENTS:
            t -= q
        elif r["event"] == SPLIT and q:
            t *= q
    return t


# ======================================================================
def _sum_dividends(events, sym, fx, period, register: Register) -> float:
    t = 0.0
    ev = events[(events["symbol"] == sym) & (events["event"] == DIV)]
    for _, r in ev.iterrows():
        if period.start <= r["date"] <= period.end:
            t += float(r["amount_fc"] or 0) * _rate_or_flag(
                fx, register, r["date"], r["currency"], f"A3 dividend {sym}",
                sym, str(r.get("notes", "") or "events"))
    return t


def _sum_proceeds(events, sym, fx, period, register: Register) -> float:
    """Only genuine sales. Share withholding produces no proceeds."""
    t = 0.0
    ev = events[(events["symbol"] == sym) & (events["event"].isin(PROCEEDS_EVENTS))]
    for _, r in ev.iterrows():
        if period.start <= r["date"] <= period.end:
            amt = float(r["amount_fc"] or 0) or float(r["quantity"] or 0) * float(r["price_fc"] or 0)
            t += amt * _rate_or_flag(
                fx, register, r["date"], r["currency"], f"A3 sale proceeds {sym}",
                sym, str(r.get("notes", "") or "events"))
    return t
