"""The application's tool registry.

This is a MANIFEST, not a container. It describes which tools the application
offers, what each one is called, and what it puts in the sidebar. It holds no
tool logic and imports no tool code - so listing a tool here costs nothing and
couples nothing.

Adding a tool is three things and no more:

    1. a package under src/tools/<id>/ with its own api.py and routes.py
    2. an entry below with status AVAILABLE and its router's import path
    3. nothing else

Two tools never see each other. They share the application shell - one sign-in,
one sidebar, one set of static assets - and nothing underneath it. Each keeps
its own working directory, its own job store, its own API prefix and its own
dependencies, so either can be deployed, tested or removed on its own.

In particular: the tax engine must never acquire a dependency on any other
tool, and no other tool may introduce tax calculations into it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

AVAILABLE = "available"
RESERVED = "reserved"        # named in the navigation, not yet implemented


@dataclass(frozen=True)
class Section:
    """One entry in a tool's own navigation."""
    id: str
    label: str
    group: str = ""          # optional heading above it in the sidebar


@dataclass(frozen=True)
class Tool:
    id: str
    label: str
    blurb: str
    status: str
    api_prefix: str
    sections: tuple[Section, ...] = ()
    router: str = ""         # "package.module:attribute", imported only if AVAILABLE
    note: str = ""           # shown when the tool is RESERVED


TAX = Tool(
    id="tax",
    label="Tax / Foreign Assets Working Paper",
    blurb="Broker statements in, Schedule FA working paper out.",
    status=AVAILABLE,
    api_prefix="/api/tools/tax",
    router="src.tools.tax.routes:router",
    sections=(
        Section("documents", "Documents", "Prepare"),
        Section("processing", "Processing", "Prepare"),
        Section("summary", "Summary", "Working paper"),
        Section("fa_a2", "FA-A2", "Working paper"),
        Section("fa_a3", "FA-A3", "Working paper"),
        Section("capital_gains", "Capital Gains", "Working paper"),
        Section("dividends", "Dividends / 1042-S", "Working paper"),
        Section("fx_working", "FX Working", "Working paper"),
        Section("reconciliation", "Reconciliation", "Working paper"),
        Section("vesting", "Vesting & Sales", "Working paper"),
        Section("review", "Review / Blockers", "Sign-off"),
        Section("sources", "Source Traceability", "Sign-off"),
        Section("export", "Export", "Sign-off"),
    ),
)

# Reserved. Metadata only - there is deliberately no src/tools/bank code, no
# import, and no router. The navigation space exists so the tool has somewhere
# to land; nothing here anticipates how it will work.
BANK = Tool(
    id="bank",
    label="Bank Statement Analyzer",
    blurb="Bank statement analysis.",
    status=RESERVED,
    api_prefix="/api/tools/bank",
    note="Coming soon. This space is reserved - the tool is built separately "
         "and plugs in without touching the tax module.",
)

REGISTRY: tuple[Tool, ...] = (TAX, BANK)


def by_id(tool_id: str) -> Tool | None:
    return next((t for t in REGISTRY if t.id == tool_id), None)


def available() -> tuple[Tool, ...]:
    return tuple(t for t in REGISTRY if t.status == AVAILABLE)


def manifest() -> list[dict]:
    """What the shell sends the browser to build its navigation."""
    return [{
        "id": t.id, "label": t.label, "blurb": t.blurb, "status": t.status,
        "api_prefix": t.api_prefix, "note": t.note,
        "sections": [{"id": s.id, "label": s.label, "group": s.group}
                     for s in t.sections],
    } for t in REGISTRY]
