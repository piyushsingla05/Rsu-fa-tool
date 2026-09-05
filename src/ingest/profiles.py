"""Configuration-driven ingestion.

A broker is a YAML file, not Python. Adding one that the engine has never seen
means dropping a profile into config/mappings/ - no code change, and nothing in
the tax engine moves. A profile that a reviewer has validated for an unknown
broker can be written straight back out as a new file, which is how a generic
mapping is promoted to a permanent adapter.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

PROFILE_DIR = Path(__file__).resolve().parent.parent.parent / "config" / "mappings"

# Document types the identifier can assign
BROKER_STATEMENT = "BROKER_STATEMENT"
PORTFOLIO_HOLDINGS = "PORTFOLIO_HOLDINGS"
FORM_1042S = "FORM_1042S"
TTBR_TABLE = "TTBR_TABLE"
TRANSACTION_SUMMARY = "TRANSACTION_SUMMARY"
UNKNOWN = "UNKNOWN"


@dataclass
class Profile:
    id: str
    label: str
    document_type: str
    broker: str = ""
    match: dict = field(default_factory=dict)
    layout: dict = field(default_factory=dict)
    fields: dict = field(default_factory=dict)
    identity: dict = field(default_factory=dict)
    rules: dict = field(default_factory=dict)
    source_path: str = ""

    def score(self, doc) -> float:
        """How strongly this profile claims a document, 0-1."""
        m = self.match
        text, name = doc.text or "", doc.path.name.lower()
        hits = weight = 0.0

        req = [s.lower() for s in m.get("all_text", [])]
        if req:
            weight += 0.5
            if all(s in text for s in req):
                hits += 0.5
            else:
                return 0.0            # a required marker is missing

        any_text = [s.lower() for s in m.get("any_text", [])]
        if any_text:
            weight += 0.35
            found = sum(1 for s in any_text if s in text)
            hits += 0.35 * min(found / max(len(any_text) * 0.5, 1), 1.0)

        hints = [s.lower() for s in m.get("filename_hint", [])]
        if hints:
            weight += 0.15
            if any(h in name for h in hints):
                hits += 0.15

        kinds = m.get("file_kind")
        if kinds and doc.kind not in kinds:
            return 0.0
        return (hits / weight) if weight else 0.0

    @property
    def min_score(self) -> float:
        return float(self.match.get("min_score", 0.6))


def load_profiles(directory: Path | None = None) -> list[Profile]:
    d = Path(directory or PROFILE_DIR)
    out: list[Profile] = []
    if not d.exists():
        return out
    for f in sorted(d.glob("*.y*ml")):
        try:
            data = yaml.safe_load(f.read_text()) or {}
        except Exception:
            continue
        out.append(Profile(
            id=data.get("id", f.stem), label=data.get("label", f.stem),
            document_type=data.get("document_type", UNKNOWN),
            broker=data.get("broker", ""), match=data.get("match", {}) or {},
            layout=data.get("layout", {}) or {}, fields=data.get("fields", {}) or {},
            identity=data.get("identity", {}) or {}, rules=data.get("rules", {}) or {},
            source_path=str(f)))
    return out


def write_profile(profile: Profile, directory: Path | None = None) -> Path:
    """Promote a validated mapping to a permanent profile. No code change."""
    d = Path(directory or PROFILE_DIR)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{profile.id}.yaml"
    path.write_text(yaml.safe_dump({
        "id": profile.id, "label": profile.label, "broker": profile.broker,
        "document_type": profile.document_type, "match": profile.match,
        "layout": profile.layout, "fields": profile.fields,
        "identity": profile.identity, "rules": profile.rules,
    }, sort_keys=False))
    return path
