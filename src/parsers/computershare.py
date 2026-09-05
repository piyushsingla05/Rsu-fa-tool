"""Computershare 'Portfolio details' export.

Layout: three header lines (participant, user id, as-of date), a note, then a
table whose header row starts with 'Allocation date'. Columns to the right of
the export may contain a preparer's own working - those are ignored; only the
broker's own columns are read.

Two share types matter:
  Purchase Shares - ESPP purchase, becomes a lot at its cost basis.
  Dividend Shares - a reinvested dividend. It is BOTH dividend income (quantity
                    x price) and a new lot, so it is emitted twice.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from ..models import BUY, DIV, StatementData
from .base import BrokerParser, register

HEADER_KEY = "allocation date"


@register("computershare")
class ComputersharePlanParser(BrokerParser):
    display_name = "Computershare Plan Managers"
    extensions = (".xlsx", ".xls")

    @classmethod
    def sniff(cls, path: Path, text: str) -> float:
        t = text.lower()
        score = 0.0
        if "allocation date" in t:
            score += 0.5
        if "outstanding quantity" in t or "available quantity" in t:
            score += 0.3
        if "computershare" in t or "portfoliodetails" in path.name.lower():
            score += 0.2
        return min(score, 1.0)

    def parse(self, path: Path) -> StatementData:
        raw = pd.read_excel(path, header=None)
        hdr = self._header_row(raw)
        if hdr is None:
            raise ValueError(f"{path.name}: no 'Allocation date' header row found.")

        cols = {str(raw.iat[hdr, j]).strip().lower(): j
                for j in range(raw.shape[1])
                if isinstance(raw.iat[hdr, j], str)}

        def col(*names):
            for n in names:
                if n in cols:
                    return cols[n]
            return None

        c_date = col("allocation date")
        c_type = col("contribution type")
        c_instr = col("instrument")
        c_price = col("strike price / cost basis", "cost basis")
        c_qty = col("outstanding quantity", "allocated quantity")
        c_plan = col("plan")

        participant = self._cell_after(raw, "participant name")
        account = self._cell_after(raw, "user id")
        symbol = self._symbol(raw, c_plan, hdr)

        events, warnings = [], []
        for i in range(hdr + 1, raw.shape[0]):
            d = raw.iat[i, c_date]
            if pd.isna(d):
                continue
            qty = self._num(raw.iat[i, c_qty])
            price = self._num(raw.iat[i, c_price])
            if not qty:
                continue
            date = pd.Timestamp(d).date()
            ctype = str(raw.iat[i, c_type]).strip() if c_type is not None else ""
            instr = str(raw.iat[i, c_instr]).strip() if c_instr is not None else ""
            is_div = "dividend" in instr.lower()

            events.append({
                "date": date, "broker": "COMPUTERSHARE", "account_no": account,
                "symbol": symbol, "event": BUY, "quantity": qty,
                "price_fc": price, "amount_fc": "", "tax_fc": "",
                "currency": "USD",
                "notes": f"{instr or ctype}".strip(),
            })
            if is_div:
                # A reinvested dividend is income as well as an acquisition.
                events.append({
                    "date": date, "broker": "COMPUTERSHARE", "account_no": account,
                    "symbol": symbol, "event": DIV, "quantity": "",
                    "price_fc": "", "amount_fc": round(qty * price, 2), "tax_fc": "",
                    "currency": "USD",
                    "notes": "Dividend reinvested into plan shares",
                })

        if not events:
            warnings.append(f"{path.name}: header found but no allocation rows read.")
        warnings.append(
            "Computershare export shows outstanding quantity only - it carries no "
            "sale history, so any shares sold must be supplied separately."
        )
        return StatementData(
            events=pd.DataFrame(events), warnings=warnings,
            broker="COMPUTERSHARE", source_file=f"{participant} | {path.name}",
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _header_row(raw: pd.DataFrame) -> int | None:
        for i in range(min(30, raw.shape[0])):
            v = raw.iat[i, 0]
            if isinstance(v, str) and v.strip().lower() == HEADER_KEY:
                return i
        return None

    @staticmethod
    def _cell_after(raw: pd.DataFrame, label: str) -> str:
        for i in range(min(15, raw.shape[0])):
            v = raw.iat[i, 0]
            if isinstance(v, str) and v.strip().lower() == label:
                return str(raw.iat[i, 1]).strip()
        return ""

    @staticmethod
    def _symbol(raw: pd.DataFrame, c_plan: int | None, hdr: int) -> str:
        """Computershare names the plan, not the ticker, so the plan name is
        mapped to a symbol. Only rows BELOW the header are scanned - the
        participant name sits in column B above it and would otherwise win."""
        plan = ""
        if c_plan is not None:
            for i in range(hdr + 1, raw.shape[0]):
                v = raw.iat[i, c_plan]
                if isinstance(v, str) and v.strip():
                    plan = v.strip()
                    break
        table = {"ibm": "IBM", "microsoft": "MSFT", "broadcom": "AVGO",
                 "amazon": "AMZN", "qualcomm": "QCOM", "texas instruments": "TXN"}
        low = plan.lower()
        for k, v in table.items():
            if k in low:
                return v
        return plan.split()[0].upper() if plan else "UNKNOWN"

    @staticmethod
    def _num(v) -> float:
        try:
            f = float(v)
            return 0.0 if pd.isna(f) else f
        except (TypeError, ValueError):
            return 0.0
