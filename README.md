# RSU → Schedule FA / A2 / A3 / ITR engine

**Broker-agnostic. Document-agnostic. Period-agnostic. Extensible.**

Turns broker statements into a professional tax working paper: FA-A2, FA-A3,
capital gains, dividends, Schedule FSI, with an exception register in front.

**This is a working paper, not a statutory filing.** Final treatment requires
professional review. The workbook says so on its first tab.

## Run it

```bash
# Upload folder: statements, portfolio Excel, 1042-S, SBI TTBR - any mix
python3 -m src.run clients/<CLIENT> --docs uploads/<CLIENT> \
    --from 2025-01-01 --to 2025-12-31 --client "Client Name"

# Or canonical CSVs
python3 -m src.run clients/<CLIENT> --from 2025-01-01 --to 2025-12-31 \
    --client "Client Name" [--limited-statement] [--entity-wise]
```

## A broker the engine has never seen

`+ Upload Broker Statement` is the whole workflow. The identifier inspects the
file, picks a profile if one claims it, and otherwise runs the generic pass:
headers are matched against a synonym dictionary, then a token-overlap pass
catches invented names like `Trade Dt`, `No. of Shares`, `Unit Cost (USD)`. The
ticker is recovered from the row or from anywhere in the document.

An unknown broker is **never** a rejected file. It is extracted, flagged with its
confidence, and listed in REVIEW_REQUIRED with the action *"promote the mapping
to a profile so future statements process automatically."*

Adding a broker is a YAML file in `config/mappings/`, not Python — and nothing in
the FA, capital-gain, dividend, FX or Excel engines moves.

The period is an input on every run — calendar year, FY, or an older AY.

## Layered architecture

    broker ingestion → normalised model → FX / market data → tax calc → presentation

Each layer only knows the one below it. Adding a broker is a new ingestion
adapter and touches no calculation code. `prepare()` is pure — inputs in,
dataframes out, no disk writes — so the CLI and the future web endpoint call the
same function. Only `write_workbook()` touches the filesystem.

## Valuation

    Initial value = held qty x vested/cost price x TTBR on the VEST date
    Peak value    = held qty x annual high price x TTBR on the PERIOD-END date
    Closing value = held qty x period-end close  x TTBR on the PERIOD-END date

One row per lot still held at period end. Lots fully sold during the period are
not carried as holdings; they appear only through gross proceeds.

**Peak quantity is the period-end quantity** — a deliberate simplification. It
understates the peak whenever the holding shrank during the year, so any security
whose quantity fell is flagged automatically rather than passed silently.

## Limited-statement mode (`--limited-statement`)

A single quarterly statement is a **supported scenario, not an error**. In this
mode the initial value converts the cost *taken from the statement* at the
period-end TTBR. The cost is never invented — only the FX date is approximated,
and every affected row and the SUMMARY tab say so.

## Capital gains

    Sale consideration  = qty x sale price   x TTBR on the SALE date
    Cost of acquisition = qty x vested price x TTBR on the VEST date

Where the vest date cannot be traced, the sale-date TTBR is used for cost and the
row is raised as a **blocker**, not a silent substitution.

A rate that is missing is **not** a rate of zero. Where a conversion has no rate
from any source, the rupee columns are left blank and a blocker names the date.
Multiplying by zero would report a cost of nil and therefore a "gain" equal to
the entire sale consideration — a wrong figure that looks like a real one. The
foreign-currency columns still show the substance, so the row is reviewable.
See **FX sources** below.

### The already-taxed-income principle

**Where a broker's reported capital gain uses a basis that excludes
compensation, perquisite or ordinary income already recognised and taxed under
another head, that reported gain is not the Indian capital gain.**

This is a standing rule of the engine, not a broker adapter. Two brokers have
exposed it in two different shapes and a third will expose a third. Where the
source evidence supports it, the engine:

1. **identifies the already-taxed component** from figures the broker itself
   states — never inferred, never estimated;
2. **adds it to the acquisition cost** to give the adjusted basis;
3. **computes the Indian gain or loss on the adjusted basis**;
4. **retains the broker's own figures** as supporting source evidence, carried
   through to the workbook in `broker_gain_fc`, `broker_adj_cost_fc` and
   `broker_adj_gain_fc`;
5. **shows the adjustment and names its source** on the row and in the exception
   register, so a reviewer sees what moved and why;
6. **never taxes the same income twice.**

Where the evidence does *not* state the already-taxed component, nothing is
adjusted and nothing is invented — the row is reported as the broker states it
and the uncertainty is raised.

Which figure is authoritative is **configuration**, because only the shape
differs:

| Broker | How the statement exposes it | Profile rule |
|---|---|---|
| E\*TRADE | an `Adjusted Cost Basis` column beside the ordinary one | `fields.cost_fc` names the adjusted column |
| UBS | a separate option-exercise table stating option cost *and* value at exercise | `rules.exercise_fmv_is_cost` |

`src/ingest/adjustments.py` remains the documented extension point for
behaviour configuration genuinely cannot express, and its registry is still
**empty**. Neither of these two cases needed it.

### E\*TRADE "G&L Expanded" — adjusted figures are authoritative

An E\*TRADE Gains & Losses Expanded report states **two different gains for the
same sale**, and only one of them is the Indian capital gain:

| Column | What it is |
|---|---|
| `Gain/Loss` | proceeds − **acquisition** cost. For an equity plan this still contains the ordinary income recognised on vest or purchase. **Supporting information only.** |
| `Adjusted Cost Basis` | acquisition cost **+ ordinary income recognised** — the cost actually borne. **Authoritative.** |
| `Adjusted Gain/Loss` | proceeds − adjusted cost basis. **Authoritative.** |

The ordinary income has already been taxed as a **perquisite**. Taking the
ordinary column would tax it a second time. On the INTC sample report the two
columns differ by **$15,021.67** on $39,526.32 of proceeds — the ordinary column
says $29,357.68 and the adjusted column says $14,336.01.

So: **capital gains for an E\*TRADE G&L Expanded statement are built from the
statement's `Adjusted Cost Basis` and `Adjusted Capital Gain/Loss`, with the SBI
TTBR applied on the actual transaction/sale date.** The ordinary `Gain/Loss`
column is carried through to the workbook as supporting reconciliation data and
is never the Indian capital-gains input while the adjusted fields are present.

This is a **document-format rule, not a security rule**. Detection is on the
report's own column headings, so any future E\*TRADE G&L Expanded statement — any
client, any ticker — is treated the same way with no new configuration. No
security is named anywhere in the profile.

Each row's own arithmetic is checked (`proceeds − adjusted cost = adjusted
gain`) and the file's `Summary` line is read as a **control total**, never as a
ninth sale. A break is reported as *"G&L adjusted figures do not reconcile"* and
the broker's figures are left exactly as stated — never rewritten to force
agreement.

Acquisition dates in the report are used for the Indian 24-month test. The
report's own `Capital Gains Status` column is deliberately **not** mapped: it is
the US 12-month classification. Where a report carries no acquisition date the
holding period stays `Undeterminable` and the existing blocker is raised.

### UBS — the perquisite inside an option exercise

A stock option exercised and sold states two costs, and only one is the Indian
cost of acquisition:

| Figure | What it is |
|---|---|
| Option cost | what the employee paid to exercise. **Not** the cost of acquisition. |
| Value at the time of exercise | market value on the exercise date. **Authoritative.** |

The spread between them is a **perquisite**, taxed as salary when the option is
exercised. UBS's own "short-term capital gains" table uses the option cost, so
the gain it prints still contains that salary income. On this statement UBS
shows a capital gain of **$3,133.85**; the real Indian capital result is a
**$10.19 loss** — the brokerage — and **$3,144.04** is salary.

So the cost of acquisition is the **value at the time of exercise**, and the
perquisite is raised as a review item naming the amount that belongs in salary
income. This is the same principle as the E\*TRADE `Adjusted Cost Basis` rule in
a different broker's layout: **a broker's stated cost may exclude income already
taxed under another head, and its stated "gain" may therefore contain it.**

Unvested awards and unexercised options are **disclosed but never reported** in
Schedule FA — they are not owned. The statement itself excludes them from the
account value, and the working paper says so rather than leaving a reviewer to
wonder where $99,995.53 of plan value went.

## Dividends — precedence, so nothing is double counted

Decided on **actual reporting-period coverage**, not on amounts. Each ingested
document reports the period it covers; those intervals are merged and checked
against the reporting period. Full coverage means transaction-level data is
authoritative and the 1042-S is evidence only. Any gap and the 1042-S is
authoritative, the transaction rows are excluded from FSI, and the tab names the
uncovered dates. Never both.

1042-S totals convert **in aggregate at the period-end TTBR** — but only because
the form usually carries no payment dates. Where it prints a dated detail
schedule, as UBS's does, **each payment converts at its own date**; the aggregate
basis is a fallback for missing dates, not a preference. No dividend date or rate
is ever invented, and the form's own subtotal serves as a control total (UBS's
detail sums to $144.12 against a form face rounded to $144.00).

Foreign tax **withheld** and foreign tax credit **claimed** are separate columns.
They are not the same figure, and Form 67 must be filed before the return.

## Capital-gain source precedence — so no sale is counted twice

Same shape as the dividend rule: decided on **actual coverage**, not on amounts.

A broker's realised gain/loss report is a complete closed-lot record — quantity,
acquisition date, sale date, proceeds and the cost the broker actually removed.
Where one is supplied it becomes the **capital-gains source of record** for that
broker over the dates it covers, and a disposal of the same security on the same
date from another document of that broker is the same sale seen twice. The
statement's rows are held as evidence, excluded from the computation, and the
supersession is disclosed on REVIEW_REQUIRED.

Precedence extends **only to the dates the report actually covers**. A sale
outside them is still taken from the statement, so a quarterly statement keeps
working unchanged alongside a part-year report — and where no such report is
supplied, nothing changes at all.

## FX sources — ranked, universal, never silent

No INR figure depends on one provider. Every conversion in the engine — vest and
acquisition dates, sale and transaction dates, dividends, foreign tax, period-end
valuation, limited-data valuation — resolves through the same ranked chain:

    1. SBI_TTBR        the statutory house rate: the shipped table, or a
                       user-uploaded SBI TTBR workbook (which outranks it)
    2. GOOGLE_FINANCE  historical FX, used ONLY where SBI has no rate for the date
    3. ECB             euro foreign exchange reference rate - EUR/INR direct,
                       every other pair a transparently derived cross-rate
    4. FBIL            Financial Benchmarks India reference rate - PREPARER-
                       SUPPLIED, never fetched. USD/INR direct; EUR, GBP and JPY
                       are FBIL's own crosses through USD
    5. MANUAL          a rate the preparer entered and signed off
    6. unresolved      the INR figure is left BLANK and raised as FX_UNAVAILABLE

**Never zero. Never an unrelated period-end rate. Never an estimate.**

The first source that answers wins outright. A lower-ranked source is never
preferred merely because its date happens to be closer — rank decides, then, and
only then, does the date search run inside that source.

**No source is ever labelled as an SBI TTBR.** Each quote carries the real
provider name, that provider's actual methodology, and its fallback level (1-6),
and every one of them states *"SBI TTBR unavailable — &lt;provider&gt; used"* in
its basis text.

### The third and fourth sources

**ECB** — the euro foreign exchange reference rates, fixed around 14:15 CET and
published about 16:00 CET on TARGET working days. The ECB quotes everything
**against the euro**, so only EUR/INR is a direct INR rate. Anything else is
derived:

    base/INR = (EUR/INR) / (EUR/base)

Both legs must come from the **same published day** — a cross spliced from two
dates is not a rate for a date, and the engine returns nothing rather than build
one. The component rates and the arithmetic are carried on the quote and printed
in FX_WORKING:

    Rate is              : derived cross-rate
    Cross-rate components: EUR/INR on 03-04-2023 = 89.434000 |
                           EUR/USD on 03-04-2023 = 1.090600
    Derivation           : Cross-rate derived from ECB euro reference rates on
                           03-04-2023: USD/INR = EUR/INR 89.4340 / EUR/USD
                           1.090600 = 82.0044. The ECB does not publish USD/INR
                           directly.

The ECB publishes these rates **"for information purposes only"** and states
they are "not intended to be used in any market transactions", so it never
outranks the SBI TT buying rate. It sits above FBIL for a practical reason
rather than a methodological one: it is openly published and freely
retrievable, whereas FBIL is licensed data the engine cannot fetch.

**FBIL** — Financial Benchmarks India Pvt Ltd, which took the rupee reference
rate over from the RBI in July 2018. Two things about it are easy to get wrong,
and both were got wrong here before being corrected:

**Only USD/INR is a direct rate.** From FBIL's own methodology document, the
USD/INR reference rate is "computed based on the data in respect of the actual
spot US dollar/Indian rupee transactions… during the one-hour time window from
11.30 Hours to 12.30 Hours", a randomly selected 15 minutes inside it, a minimum
of ten transactions aggregating USD 25 million, ±3SD outlier removal, published
around 13:30 IST on Mumbai business days. But for the other three, "cross-currency
rates (EURO/USD, GBP/USD, USD/JPY) are obtained from electronic platforms during
the same 15-minute window and crossed with the USD/INR rate". **EUR/INR, GBP/INR
and JPY/INR are FBIL's own crosses**, and the workbook says so on every such row.

**FBIL data is licensed, and the engine does not fetch it.** FBIL's FAQ states
that an End-Users Licence is required and that use is fee liable, that "all the
entities in India intending to use FBIL Benchmarks… are fee liable", and that
"any use of its Benchmarks including commercial use and distribution/ display
will be only with the express authorization of FBIL". Unauthorised use is "dealt
with legally". FBIL's *methodology* document is silent on usage — and silence is
not permission; an earlier version of this README wrongly inferred that it was.

So an FBIL rate reaches the engine only as a table the **preparer** supplies,
transcribed from FBIL's own publication under whatever licence the firm holds.
`fxfetch.fbil_template()` gives the sheet; `fxfetch.load_preparer_fbil()`
validates it and stamps the provenance. The loader keeps only FBIL's four rupee
pairs, requires every row to name the FBIL publication it came from, drops zero
and non-numeric rates, and marks every row `PREPARER_SUPPLIED` — never
`FETCHED`, because there is no fetch to claim.

**No provider is ever written under another provider's name.** This is not
hypothetical: an earlier version of `fxfetch.py` pulled `api.frankfurter.dev/v1`
and labelled the result "FBIL reference rate, direct quote". That endpoint has
no provider selection and returns the default ECB dataset cross-computed —
`89.471 / 1.087 = 82.31`, exactly the "USD/INR" it serves. Roughly-right numbers
under a false label are worse than missing ones, because nothing flags them. The
acceptance harness checks for it by name.

### `Rate is` — three states, not two

| Value | Meaning |
|---|---|
| `directly quoted` | the provider quotes this pair itself (SBI, Google, ECB's EUR/INR, FBIL's USD/INR) |
| `provider's own cross-rate` | the **provider** published it as a cross (FBIL's EUR/INR, GBP/INR, JPY/INR). Taken as published, not recomputed — but not a direct rupee rate either |
| `derived cross-rate` | **this engine** derived it, and shows the components and the arithmetic |

Collapsing the middle state into either of the others would misdescribe it.

### Retrieval — deliberate, never during a run

A tax computation must be reproducible: the same documents and the same rate
tables have to produce the same workbook next year, in front of an assessing
officer. So the engine **never fetches**. `src/fxfetch.py` is run deliberately,
writes `config/fx_ecb.csv`, and the engine only reads that table.

| Source | How it arrives | Coverage | Terms |
|---|---|---|---|
| ECB | `ecb.europa.eu/stats/eurofxref/eurofxref-hist.xml` — one public XML file, fetched by `python3 -m src.fxfetch` | all published currencies incl. INR, working days, 1999→ | information only, not for market transactions; no explicit licence or attribution requirement stated |
| FBIL | **not fetched.** A preparer-supplied CSV validated by `load_preparer_fbil()` | FBIL's four rupee pairs, Mumbai business days, 2018→ (RBI before that) | End-Users Licence required, fee liable, use and distribution only with FBIL's express authorisation |

The ECB source has a **parser** that reads bytes already on disk and a separate
thin fetcher, so a machine without network can download the XML by hand, drop it
in, and parse it with no loss of function — which is how the regression
exercises it. A zero or non-numeric rate is dropped at retrieval, so the
no-zero-rate rule is enforced before anything reaches a table.

Neither table is shipped. No rate in this repository was invented to fill a gap.

### Nearest available date on the primary table

Where SBI has no rate for the date and no secondary source can supply one, the
**nearest published SBI rate within 7 days** — either direction, prior winning a
tie — is used and flagged, before any older rate is carried forward. This is
universal: every currency, every broker, every conversion.

It matters most at the edge of the table. A date days *before* the earliest rate
on file has nothing to carry forward from, so it could not resolve at all; it now
takes the nearest later rate. Beyond 7 days the rate is not a rate for that date:
the pair stays `FX_UNAVAILABLE` and the INR figure stays blank.

SBI is consulted twice, deliberately. First strictly — does it hold this date, or
one a weekend away? If not, Google Finance gets its chance at the *exact* date,
which beats an SBI rate weeks stale. Only if Google has nothing either does the
carried-forward SBI rate come back, flagged, because a disclosed approximation
still beats reporting nothing at all.

A Google Finance rate is **never presented as an SBI TTBR**. FX_WORKING states
*"SBI TTBR unavailable — Google Finance historical FX used"* at the top of the
tab, on the SUMMARY status line, on every affected row's basis text, and as a
REVIEW item. Each rate carries its FX date, source and target currency, rate,
source type, SBI availability, retrieval status, any date adjustment actually
applied, and its review flag. The capital-gain rate columns are named `FX Rate —
Sale Date` and `FX Rate — Vest Date`, not "TTBR", precisely because they may not
be TTBR.

### The consolidated Google Finance table

`GOOGLEFINANCE()` is a **Google Sheets function**. Excel cannot evaluate it and
neither can the Python engine, so the engine works from a table rather than from
live calls:

1. A run reports which currency/date pairs SBI could not supply. FX_WORKING
   carries a **consolidated request block** — deduplicated, **one row per pair**,
   never one call per transaction. Fifty lookups of the same pair produce one row.
2. Each row carries the formula for **its own pair**, built from the source
   currency rather than hardcoded:

       =INDEX(GOOGLEFINANCE("CURRENCY:USDINR","price",A2),2,2)

   USD → `CURRENCY:USDINR`, EUR → `CURRENCY:EURINR`, GBP → `CURRENCY:GBPINR`,
   CHF → `CURRENCY:CHFINR`, and so on for any currency the documents contain.
   Opening the block in Google Sheets fills the rate column in place.
3. Save `date, base_currency, quote_currency, rate` as CSV and pass it with
   `--google-fx`, or drop it in the documents folder — the engine recognises a
   returned Google table and merges it into the **Google provider**, never into
   the SBI table.

A blank or non-numeric rate cell means Google returned nothing for that date. It
is skipped, never read as zero, and the pair stays unresolved.

### Nearest available Google date

Google publishes no quote at a weekend, on a holiday, or on any other
non-trading day, so the required date often simply is not there. The exact date
is tried first; failing that the search looks **both ways** and takes whichever
available date is closest. **A tie goes to the prior date** — a rate that already
existed on the date in question is a safer basis than one set afterwards.

The substituted date is never allowed to read as the date required. The quote
records the real rate date, the gap in days, which side it came from, and the
status `NEAREST_AVAILABLE_DATE` rather than `EXACT_DATE`, and the basis says so
in words:

    Required date : 25-12-2025
    Rate date     : 24-12-2025      gap 1 day, prior
    Pair          : CURRENCY:EURINR
    Source        : GOOGLE_FINANCE  SBI TTBR availability: NOT_AVAILABLE
    Status        : NEAREST_AVAILABLE_DATE
    Basis         : SBI TTBR unavailable; exact Google Finance date unavailable;
                    nearest available prior date 24-12-2025 used (1 day from
                    25-12-2025)

A gap inside a weekend or holiday cluster is routine. A wider one is marked
**MATERIALLY DISTANT** in the flag, the review action and the calculation basis,
so it cannot pass on the same footing as a weekend carry. Beyond 30 days no rate
is used at all — an unrelated rate is worse than no rate, so the pair goes to
`FX_UNAVAILABLE` and the INR figure stays blank.

The **same** rule governs every external source — Google Finance, FBIL and the
ECB alike — so no provider quietly behaves differently from another: exact date,
else nearest within 30 days either way with a tie to the prior date, and the
substituted date always disclosed. The search never reaches into a different year
to fill a date.

Adding a further provider is one entry in the `sources` list in `fxsources.py`.
Nothing downstream moves.

### Date integrity

Sales convert at the **actual sale date**. Vests and acquisitions convert at the
**actual acquisition date** — each lot at its own rate, never one blended rate.
Dividends convert at their **payment date**, and the foreign tax withheld on a
dividend at that same date. The one deliberate exception is a Form 1042-S, which
carries no payment dates: it converts in aggregate at the period-end rate, as
agreed, and every row says so in its Basis column.

## Market data — no single-website dependency

`marketdata.py` resolves prices by source rank:

    BROKER_STATEMENT > PRIMARY > SECONDARY > MANUAL

Every price carries ticker, security, exchange, price, date, basis, source type,
source name, retrieval date, confidence and verified status — all printed on the
MARKET_DATA tab. A high derived from month-end closes is flagged as approximate;
it is never silently called the annual high. Swapping providers changes nothing
in the tax engine.

## Exception register

Nothing uncertain is resolved silently. Every issue becomes a flag at one of
three severities, printed on REVIEW_REQUIRED and counted on SUMMARY:

- **Blocker** — a figure cannot be relied on until resolved
- **Review** — a documented approximation needing sign-off
- **Note** — disclosure only

SUMMARY states plainly whether the file is ready: *"NOT READY TO FILE — blockers
outstanding"* or *"No blockers. Review items still require sign-off."*

## Workbook — 11 tabs, empty ones suppressed

`SUMMARY` · `FA_A2` · `FA_A3` · `RSU_MASTER` · `VESTING_SALES` ·
`DIVIDENDS_1042` · `CAPITAL_GAINS` · `FX_WORKING` · `FSI_TR_WORKING` ·
`RECONCILIATION` · `REVIEW_REQUIRED`

SUMMARY is always first. `RSU_MASTER` is the normalised model itself, each row
naming the document, sheet and line it came from. Market-data provenance sits as
a second block on `FX_WORKING`, and the 1042-S as a second block on
`DIVIDENDS_1042`, rather than earning tabs of their own.

The filing tabs hold no hardcoded figures — every rupee is a formula over visible
grey backend columns.

## Reconciliation

    opening + vested + purchased + transferred in - sold - withheld - transferred out = closing

Compared against the closing position derived from lots. A break is a blocker and
says *"unable to fully reconcile — source statement coverage incomplete"* rather
than fabricating the missing activity.

## Configuration-driven ingestion

```yaml
id: computershare_portfolio
broker: COMPUTERSHARE
document_type: PORTFOLIO_HOLDINGS
match:
  all_text: ["allocation date"]
  any_text: ["outstanding quantity", "contribution type"]
fields:
  acquired_date: ["allocation date"]
  quantity: ["outstanding quantity", "allocated quantity"]
  price_fc: ["strike price / cost basis"]
rules:
  symbol_from_text: {ibm: IBM}
```

A portfolio-details Excel is a **primary source for FA-A3**, not a supporting
document. If it carries the holdings, no broker statement is required.

A profile also declares what its rows MEAN. `layout.kind: pdf_sections` carves a
laid-out statement into sections, each with a `role`; `layout.table_role` does
the same for a spreadsheet or CSV. `closed_lot_gains` says "these rows are
complete realised-gain records", and `row_filter` / `control_row` separate the
transaction lines from a stated summary line. Which cost column is authoritative
is configuration too, because it differs by report — that is how the E*TRADE
adjusted-cost rule is expressed without a line of broker-specific code.

## Inputs (one folder per client)

| File | Holds | Required |
|---|---|---|
| `events.csv` | vests, buys, sells, dividends, transfers, splits | **yes** |
| `form1042s.csv` | 1042-S gross income and tax withheld | if applicable |
| `cash.csv` | custodial balances, for A2 | no |
| `entities.csv` | issuer name, address, ZIP, country | no |
| `fx_google_finance.csv` | Google Finance FX fallback (`--google-fx`) | no |
| `fx_fbil.csv` | FBIL reference rates, written by `src/fxfetch.py` | no |
| `fx_ecb.csv` | ECB euro reference rates, written by `src/fxfetch.py` | no |
| `accounts.csv` | custodian master, for A2 | no |

Shared config: `config/fx_rates.csv`, `config/market_prices.csv`, and
optionally `config/fx_google_finance.csv`.

## Broker-specific adjustment principle

Configuration first. A broker's mappings, row shapes, section layout,
terminology, split style, cost-basis interpretation, dividend representation and
identity extraction all live in its YAML profile. `src/ingest/adjustments.py` is
the documented extension point for the residue - behaviour a real statement shows
that configuration cannot express. A profile names a hook with
`rules.adjustment: <name>`. **No hook is registered today**: every Schwab and
Morgan Stanley behaviour so far has been expressible as configuration. Nothing is
generalised until a second broker demonstrably needs it.

## Transfers between the client's own accounts

A transfer never creates an acquisition, a gain, proceeds, or a duplicated cost
basis. Where a broker states its own period-end position, that position already
contains every share transferred in, so those transfers are recorded as
**evidence** for the cross-broker reconciliation rather than as transactions.

The cross-broker chain — acquired + transferred in − sold − transferred out =
combined period-end holding — is reported on the RECONCILIATION tab. Where the
documents do not establish which lots moved, it says **"Transfer linkage
incomplete"** and nothing is assumed.

## Several statements in one file

A broker may return a whole run of monthly statements as a single download — UBS
sends five. Read as one statement, most of the year disappears, and coverage is
what the dividend precedence rule turns on.

A profile therefore declares `layout.statement_boundary` (where each statement
begins) and `layout.as_at_regex` (the date its balances are struck at). Every row
is tagged with the statement it came from, so five months of cash balances carry
five different dates instead of one, and the file reports the whole span it
covers. A header line naming both an opening and a closing date resolves to the
**closing** one, which is what the balances beneath it are.

## A position is a state, not a transaction

Six monthly statements state the same shares six times. `POSITION` events are
collapsed to the latest snapshot on or before the period end, per broker,
account and security; the rest are set aside and counted once. Byte-identical
uploads (the routine "(1)" copy) are detected by content hash and ignored.

Where a statement names each lot's own trade date beside the position, that date
is carried into the lot, so Schedule FA converts the initial value at the real
acquisition date instead of at the statement date. The statement date is kept
separately — it is what decides which disposals the position already reflects,
and confusing the two would undo the E\*TRADE fix below.

A stated position already nets **everything up to its date** — every acquisition
and every disposal. Neither is applied again. E*TRADE exposed the disposal half:
it prints holdings on a trade-date basis with a sale traded the same day, so
deducting that sale again reported 1,055 shares instead of 1,170.

## Broker-stated cost beats a FIFO guess

A broker using specific-lot identification states the cost it actually removed on
each sale. When a disposal carries `cost_fc`, the engine spreads that figure
across the FIFO-matched slices pro rata, so the total gain is the broker's while
each slice keeps its own acquisition date for the short/long split. Morgan
Stanley's statement does this; using FIFO instead would have misstated the cost
of the five evidenced sales by **$15,406.66**.

## Web UI — a modular multi-tool application

```bash
pip install fastapi uvicorn python-multipart
APP_PASSWORD='...' python3 -m src.web            # http://127.0.0.1:8000
```

The application is a neutral shell (`Workbench`, set `APP_NAME` to rename it)
hosting independent tools. It owns sign-in, the sidebar and the static assets,
and mounts whichever tool routers `src/modules.py` declares available.

    Workbench
      Tax / Foreign Assets Working Paper      (implemented)
        Prepare        Documents · Processing
        Working paper  Summary · FA-A2 · FA-A3 · Capital Gains ·
                       Dividends / 1042-S · FX Working · Reconciliation ·
                       Vesting & Sales
        Sign-off       Review / Blockers · Source Traceability · Export
      Bank Statement Analyzer                 (reserved — coming soon)

**The shell contains no tax vocabulary and imports no engine code**; it reaches
a tool only through the router path that tool's registry entry names. Adding a
tool is three things: a package under `src/tools/<id>/` with its own `api.py`
and `routes.py`, one registry entry, and nothing else.

`src/modules.py` is a manifest, not a container — its only imports are
`dataclasses` and `__future__`, asserted by test. Listing a reserved tool costs
nothing: no import is attempted, no routes exist behind it, and the navigation
space simply waits.

Each tool keeps its own API prefix (`/api/tools/<id>/…`), its own job store and
its own working directory (`work/<id>/`). Two tools share the sign-in and
nothing else, so either can be deployed, tested or removed on its own. The tax
engine must never acquire a dependency on another tool, and no other tool may
introduce tax calculations into it — both directions are asserted by test.

**The UI calculates nothing.** Every figure it shows is a value
`build_context()` already produced — the same dict `write_workbook()` renders —
so the screen and the downloaded workbook cannot disagree. `src/api.py` is a
thin service layer with no tax logic; it reaches the engine only through the
five existing integration points (`extract_file`, `ingest_documents`,
`prepare`, `build_context`, `write_workbook`).

Upload → identify → run → read the eleven sections → download the workbook.
Sections map one-to-one onto context keys: SUMMARY, FA-A2, FA-A3, capital
gains, dividends/1042-S, FX working, reconciliation, vesting, review, sources.

**FX working.** The screen shows every rate used, and beside it the **ranked
source chain** as the engine itself reports it — which sources had a table
loaded, how many currency/date pairs each actually supplied, how many of those
were derived cross-rates, each provider's own methodology, and the standing of
each ("NOT an SBI TT buying rate"). The hierarchy is not written into the
browser: it is a frame the engine builds, rendered by the screen and printed on
FX_WORKING, so the two cannot drift apart. Full-provenance columns —
methodology, derivation, component rates, basis text — are collapsed by default
and revealed with one click; they are hidden in CSS, never dropped from the
table, so the evidence is always there.

**Google Finance round-trip.** Run → the consolidated FX request table appears
with one row per unresolved currency/date pair and a "Copy for Google Sheets"
button → paste the completed rates back or upload the CSV → re-run. Imported
rates merge into the **Google provider only**, never the SBI table; a row with
no usable positive rate is skipped, never read as zero.

**Access and retention.** Authentication is required on every route — there is
no anonymous access. Uploads live only in the job's own working directory, are
purged when the job is discarded or expires, and never leave the machine. v1 is
read-only: no browser overrides of FX, prices or tax figures. The workbook
remains authoritative.

Multi-user concurrency is not built, but nothing blocks it: jobs live behind
`api.STORE` and identity behind `require_session`, so both can be replaced
without the tax engine moving.

### One presentation rule, everywhere

**A rate of nil is not a rate, and neither is a figure converted at one.**

| Column | Needs | Shown when |
|---|---|---|
| `FX Rate - Sale Date` | the sale-date rate | it exists; otherwise **blank** |
| `FX Rate - Vest Date` | the cost-date rate | it exists; otherwise **blank** |
| `Full Value of Consideration (Rs.)` | the sale-date rate | it exists |
| `Cost of Acquisition (Rs.)` | the cost-date rate | it exists |
| `Capital Gain (Rs.)` | **both** — it is their difference | both exist |

Each column is blanked against the rate *it* needs, so a figure that is
determinable is never thrown away and a figure that is not is never printed as
zero. The engine frame, the API and the workbook apply the identical test, which
is asserted cell for cell by the API suite. Where a row has one rate and not the
other, CAPITAL_GAINS says so under the totals: the three column totals cover
different rows and do not tie to each other.

The same rule runs through the vesting register (`SBI TTBR - Acquisition Date`)
and the 1042-S aggregate conversion. The API suite sweeps **every frame the
engine builds** for a rate column holding a numeric zero, so a new column cannot
reintroduce one unnoticed.

## Real-data acceptance — the procedure

The FX layer is proved against fixtures. It is **not yet proved against real
fetched data**, because this environment has no outbound network:
`config/fx_fbil.csv` and `config/fx_ecb.csv` are empty and no rate was invented
to stand in. That last step is a runnable script, not a checklist someone has to
follow by hand.

```bash
python3 tests/acceptance_realdata.py --baseline   # ONCE, before fetching
python3 tests/acceptance_realdata.py --fetch      # populate the ECB table
python3 tests/acceptance_realdata.py              # the acceptance run
```

The baseline is captured **before** the fetch and refuses to run if either table
already exists — captured afterwards it would prove nothing. It records every
cell of eight frames, the headline figures and the unresolved pairs, and is
committed at `tests/fixtures/acceptance_baseline.json`.

Every stage reports **PASS**, **FAIL**, **BLOCKED** (needs data that is not
there — neither a defect nor a pass), **WARNING** (something to investigate that
is not on its own a defect — no verdict, but counted onto the overall line) or
**REPORT** (an observation for the preparer to sign off). Exit code is `0` only
when every stage passes; `2` while anything is blocked, so a dry run can never
be mistaken for an acceptance. The run writes
`work/acceptance/acceptance_report.md`.

| Stage | What it establishes |
|---|---|
| 1 | The ECB table populates from its documented endpoint with real coverage, EUR-quoted rows and a present EUR/INR leg. FBIL is **not fetched** — its table is present only if the preparer supplied one, and if present every row must name the FBIL publication it was transcribed from, be marked `PREPARER_SUPPLIED`, and carry no other provider's data |
| 2 | Each of the three UBS dates resolves — naming the provider, the fallback level, exact-vs-nearest, the basis text and, for a cross-rate, its components — or honestly reports that it still does not |
| 3 | The rates are what the providers publish. The **acceptance criterion is the established tolerance** — where both tables are present, FBIL's USD/INR and the ECB-derived cross must agree within 2%, and a material disagreement beyond it FAILS. Two sources landing on the same figure to the displayed precision is *not* a failure — a euro cross can round onto the FBIL figure — so it reports a WARNING with both sides' provenance retained. Plus the three manual spot-checks against fbil.org.in and the ECB's own page, to be recorded in the report |
| 4 | Exact-date and nearest-date behaviour on real published calendars: a published date returns its own rate; a non-publishing day takes the nearest and says so; beyond 30 days nothing is supplied |
| 5 | Precedence unchanged with real tables loaded: the chain is SBI > Google > ECB > FBIL > manual, SBI still wins outright, Google still outranks the ECB, the ECB is reached before FBIL, FBIL supplies only what the ECB could not — and FBIL's USD/INR reads `directly quoted` while its EUR, GBP and JPY read `provider's own cross-rate` |
| 6 | Provenance identical across context, API, UI and workbook; no fallback described as a TT buying rate; every derived rate carrying its components and arithmetic; no rate column anywhere reporting zero |
| 7 | **Nothing else moved.** Every cell of every compared frame against the pre-fetch baseline. The only permitted change is a cell that was blank because a rate was missing now holding a figure. Anything else is reported as ALTERED and fails |

Stage 7 is the one that matters. It is what makes "the fetch changed only what
it should have" a fact rather than an assurance.

### Why identical rates warn rather than fail

FBIL is a midday Indian interbank volume-weighted average; the ECB figure is a
cross of two rates from a 14:15 CET euro fixing. They measure the same market
hours apart, so they agree closely without agreeing exactly — but rounding to
the four decimals the workbook prints can put them on the same figure on a quiet
day. Failing on that would be failing on arithmetic, not on evidence.

So the tolerance is the criterion, and an identical displayed rate raises:

```
WARNING  independent sources returned identical displayed rate;
         investigate source independence
```

with the provenance of both sides kept whole — each side's rate to six decimals,
its source string, retrieval date and status, the ECB derivation and its
component rates, and both basis texts — printed for the first ten and written in
full to `work/acceptance/stage3_identical_rates.json`. What actually settles
source independence is where each figure came from, and agreement to six
decimals between a derived cross and a direct quote, which is what that evidence
lets a reviewer check.

**What must not be edited to make a stage pass:** the 30-day nearest-date limit,
the SBI > Google > FBIL > ECB > manual precedence, and any tax calculation. If a
date does not resolve, the finding is the output.

## Validation

```bash
python3 tests/regression.py
```

| Case | Result |
|---|---|
| Computershare anchor vs the client's manual working file | 28 lots, 0 off by >0.1%, worst 0.02% |
| Computershare ESPP, calendar year | pass |
| Limited statement + 1042-S only (Fidelity/MSFT) | pass |
| Upload folder: unknown broker + portfolio Excel + user TTBR | pass |
| Morgan Stanley - profiled PDF, 20:1 split, specific-lot cost | pass |
| Schwab - positions, cash, dividends + NRA tax, closed-lot gains | pass |
| Fidelity - documents only (SPS reports + 1042-S), limited statement | pass |
| E*TRADE - single quarterly statement, LIMITED_DATA path | pass |
| Combined Fidelity + Schwab - transfer linkage | pass |
| Morgan Stanley evidence regression (3 independent anchors) | pass |
| Schwab evidence regression (6 sections, all to the cent) | pass |
| Fidelity + combined-client regression (6 sections) | pass |
| E*TRADE limited-data regression (7 sections) | pass |
| E*TRADE G&L Expanded - adjusted capital gains, FY period | pass |
| E*TRADE G&L Expanded regression (7 sections, 40 checks) | pass |
| E*TRADE G&L Expanded + Google Finance FX fallback | pass |
| UBS - 5 statements per PDF, option exercise, dated 1042-S | pass |
| UBS evidence regression (9 sections, 59 checks) | pass |
| Universal FX fallback - SBI > Google Finance > blank (90 checks) | pass |
| Third/fourth FX source - ECB and FBIL (10 sections, 93 checks) | pass |
| Dividend coverage (5 cases) + generic parity | pass |
| UI / API - screen == engine == workbook, modular shell (10 sections, 100 checks) | pass |

The anchor is the one that matters: the engine must keep reproducing a real
filing. Country codes are not reported — country name only.

## Open items

- Verify the SBI TTBR table against SBI. Seeded from a client working file; all
  rows currently `verified=N`.
- Replace month-end-close highs with true intraday annual highs.
- Adapters built: Computershare, Morgan Stanley, Schwab, Fidelity, E*TRADE
  (client statement + G&L Expanded), UBS (combined statements + dated 1042-S).
- A broker's stated cost may exclude income already taxed under another head.
  E*TRADE states it as an "Adjusted Cost Basis" column; UBS as a separate option
  exercise table. Both are expressed in configuration, not code.
- Historical SBI TTBR is needed back to 2019 for E*TRADE G&L acquisition dates.
  Without it those rows fall through to Google Finance, and without that too they
  report no rupee figure and raise a blocker.
- `GOOGLEFINANCE()` only evaluates inside Google Sheets, so the fallback works
  from the consolidated table above rather than from live calls. A live HTTP
  provider can be registered in `fxsources.py` without touching anything else.
- `config/fx_fbil.csv` and `config/fx_ecb.csv` are **not populated**. This
  environment has no outbound network, so nothing was fetched and nothing was
  invented to stand in. Run `python3 -m src.fxfetch` on a networked machine to
  write them; until then FBIL and the ECB are wired, tested and silent.
- The three UBS dates (03-04-2023, 03-07-2023, 01-07-2024) therefore remain
  input-data blockers. All three are Indian and TARGET business days, so both
  FBIL and the ECB should carry them on the first fetch. The 7-day and 30-day
  windows were **not** widened to reach them.
- Where a broker states proceeds and a realised gain but no lot cost, the cost is
  their difference - arithmetic on two stated figures, allocated by proceeds and
  disclosed on the row.
- A cash credit is net of transaction fees. Schedule FA and capital gains both
  want the GROSS consideration, so quantity x price is reported and the fee named.
- A broker's summary wording is not its arithmetic: Fidelity's "Stock sales
  total" is the GAIN, not proceeds. The detail rows are the source of truth.
- A 1042-S PDF holds several identical copies of the same form. They are
  collapsed, never summed.
- A broker's own short/long-term label is its US classification. The engine
  applies the Indian 24-month rule: Schwab's $8,475.76 "long term" is short term
  for Indian tax.
- Morgan Stanley needs historical SBI TTBR back to 2016; without it the engine
  reports nil and raises a blocker rather than inventing a rate.
