"""Turn any spreadsheet, CSV or PDF into candidate tables plus searchable text.

Nothing here knows about a broker. It finds where a table starts, what its
headers are, and hands back a frame with the source coordinates attached, so a
figure can always be traced to a file, sheet and row.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

MAX_HEADER_SCAN = 40      # rows to search for a header line
MIN_HEADER_CELLS = 3      # a header needs at least this many labelled columns


@dataclass
class Table:
    df: pd.DataFrame                 # data rows, columns named by the header
    header_row: int                  # 0-based row index of the header in the sheet
    sheet: str
    source_file: str
    raw: pd.DataFrame = field(repr=False, default=None)   # the untouched grid

    def coords(self, i: int) -> str:
        """Human-readable source coordinate for data row i (0-based)."""
        if "_line" in self.df.columns and i < len(self.df):
            return f"{Path(self.source_file).name} | {self.sheet} | line {self.df['_line'].iloc[i]}"
        return f"{Path(self.source_file).name} | {self.sheet} | row {self.header_row + 2 + i}"


@dataclass
class Document:
    path: Path
    kind: str                        # excel | csv | pdf | unknown
    text: str = ""                   # searchable text, lowercased
    lines: list[str] = field(default_factory=list)   # PDF only, original case
    tables: list[Table] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.path.name


# ----------------------------------------------------------------------
def load(path: str | Path) -> Document:
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix in (".xlsx", ".xls", ".xlsm"):
        return _load_excel(p)
    if suffix in (".csv", ".tsv", ".txt"):
        return _load_csv(p)
    if suffix == ".pdf":
        return _load_pdf(p)
    return Document(path=p, kind="unknown")


def _load_excel(p: Path) -> Document:
    doc = Document(path=p, kind="excel")
    try:
        xl = pd.ExcelFile(p)
    except Exception:
        return doc
    chunks = []
    for sheet in xl.sheet_names:
        raw = pd.read_excel(p, sheet_name=sheet, header=None)
        chunks.append(_grid_text(raw))
        for t in find_tables(raw, sheet, str(p)):
            doc.tables.append(t)
    doc.text = "\n".join(chunks).lower()
    return doc


def _load_csv(p: Path) -> Document:
    doc = Document(path=p, kind="csv")
    try:
        raw = pd.read_csv(p, header=None, dtype=str, sep=None, engine="python")
    except Exception:
        return doc
    doc.text = _grid_text(raw).lower()
    doc.tables = find_tables(raw, "csv", str(p))
    return doc


def _load_pdf(p: Path) -> Document:
    """Text plus the raw lines, so a profile can carve sections out of them."""
    doc = Document(path=p, kind="pdf")
    try:
        out = subprocess.run(["pdftotext", "-layout", str(p), "-"],
                             capture_output=True, timeout=120)
        raw = out.stdout.decode("utf-8", "ignore")
    except Exception:
        return doc
    # Statement PDFs are littered with zero-width and soft-hyphen characters that
    # break every regex written against what the page looks like.
    for ch in ("\u200b", "\u00ad", "\ufeff", "\u200e", "\u200f"):
        raw = raw.replace(ch, "")
    doc.lines = [l.rstrip() for l in raw.splitlines()]
    doc.text = raw.lower()
    return doc


def _compile(pattern):
    import re as _re
    if not pattern:
        return None
    try:
        return _re.compile(pattern)
    except Exception:
        return None


def pdf_sections(doc: "Document", sections: list[dict],
                 context: dict | None = None) -> list[Table]:
    """Carve configured sections out of a PDF's lines into tables.

    Each section declares where it starts, optionally where it ends, and one
    regex whose NAMED GROUPS become the table's columns. That keeps a layout as
    tricky as a broker statement in configuration rather than in code.
    """
    import re as _re
    out: list[Table] = []
    lines = doc.lines or []
    context = context or {}
    for cfg in sections or []:
        name = cfg.get("name", "section")
        start_marker = (cfg.get("start_after") or "").lower()
        end_marker = (cfg.get("end_before") or "").lower()
        try:
            row_re = _re.compile(cfg["row_regex"])
        except Exception:
            continue

        # Statements often print a date once and leave continuation rows blank.
        # Schwab does this for both transactions and stock-plan activity.
        carry = cfg.get("carry_forward") or []
        # A broker may deliver SEVERAL statements in one PDF - UBS returns five
        # months per download. A boundary regex marks where each one begins, and
        # an as-at regex says what date that statement's balances are struck at,
        # so a row is tagged with the statement it belongs to rather than with
        # whatever period the file as a whole was taken to cover.
        bound_re = _compile(context.get("statement_boundary"))
        asat_re = _compile(context.get("as_at_regex"))
        repeat = bool(cfg.get("repeat_sections"))

        started = not start_marker
        rows, coords, last = [], [], {}
        ctx = {}
        for idx, line in enumerate(lines):
            low = line.lower()
            if bound_re is not None:
                bm = bound_re.search(line)
                if bm:
                    ctx = {f"_{k}": v for k, v in bm.groupdict().items() if v}
                    # A new statement begins: re-arm the section marker so the
                    # same section is picked up again in the next statement.
                    if repeat:
                        started = not start_marker
            if asat_re is not None:
                # A header line often carries the opening AND closing date:
                # "Opening balance on Dec 1 ($)   Closing balance on Dec 31 ($)".
                # The balances beneath it are the CLOSING ones, so the last
                # match on the line is the date that applies.
                ams = list(asat_re.finditer(line))
                if ams:
                    ctx.update({f"_{k}": v for k, v in ams[-1].groupdict().items() if v})
            if not started:
                if start_marker in low:
                    started = True
                continue
            if end_marker and end_marker in low:
                if rows and not repeat:
                    break
                started = not start_marker    # end of this statement's block
                continue
            m = row_re.search(line)
            if m:
                g = m.groupdict()
                for f in carry:
                    if not (g.get(f) or "").strip():
                        g[f] = last.get(f, "")
                    else:
                        last[f] = g[f]
                g.update(ctx)                  # which statement this row is from
                rows.append(g)
                coords.append(idx + 1)
        if not rows:
            continue
        df = pd.DataFrame(rows)
        df["_line"] = coords
        out.append(Table(df=df, header_row=0, sheet=name,
                         source_file=str(doc.path), raw=None))
    return out


# ----------------------------------------------------------------------
def find_tables(raw: pd.DataFrame, sheet: str, source_file: str) -> list[Table]:
    """Locate every plausible header row and build a table beneath it.

    A header row is one with several distinct non-numeric labels followed by at
    least one row carrying data. Preambles above it (participant name, as-of
    date) are ignored, which is what makes this work on real broker exports.
    """
    tables: list[Table] = []
    limit = min(MAX_HEADER_SCAN, len(raw))
    for i in range(limit):
        labels = [(j, str(v).strip()) for j, v in enumerate(raw.iloc[i])
                  if isinstance(v, str) and str(v).strip()]
        if len(labels) < MIN_HEADER_CELLS:
            continue
        if len({l.lower() for _, l in labels}) < MIN_HEADER_CELLS:
            continue
        body = raw.iloc[i + 1:]
        if body.empty or body.dropna(how="all").empty:
            continue
        cols = {}
        for j, lab in labels:
            key = lab.lower()
            cols[key] = cols.get(key, j)
        df = body.iloc[:, [j for j in cols.values()]].copy()
        df.columns = list(cols.keys())
        df = df.dropna(how="all")
        if df.empty:
            continue
        tables.append(Table(df=df.reset_index(drop=True), header_row=i,
                            sheet=sheet, source_file=source_file, raw=raw))
        break   # first real header per sheet is enough for our layouts
    return tables


def _grid_text(raw: pd.DataFrame) -> str:
    vals = []
    for row in raw.itertuples(index=False):
        for v in row:
            if isinstance(v, str) and v.strip():
                vals.append(v.strip())
    return " \n".join(vals)


def cell_after_label(raw: pd.DataFrame, label: str, max_rows: int = 20) -> str:
    """Read the value beside a label in a statement preamble."""
    lab = label.strip().lower()
    for i in range(min(max_rows, len(raw))):
        for j in range(min(4, raw.shape[1])):
            v = raw.iat[i, j]
            if isinstance(v, str) and v.strip().lower() == lab:
                for k in range(j + 1, min(j + 4, raw.shape[1])):
                    nxt = raw.iat[i, k]
                    if pd.notna(nxt) and str(nxt).strip():
                        return str(nxt).strip()
    return ""
