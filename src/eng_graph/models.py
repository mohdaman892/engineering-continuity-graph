"""Constants and request models.

Vocabulary is guidance for the client. Entity types, fact statuses, edge
types, and placement actions outside these lists are stored, not rejected.
Consent decisions outside the list are recorded and never authorize storage.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_HINT = 1

ENTITY_TYPES = (
    "file",
    "symbol",
    "concept",
    "decision",
    "architecture",
    "failure",
    "requirement",
    "discovery",
    "env_constraint",
)

FACT_STATUSES = ("draft", "proposed", "approved", "rejected", "superseded")

EDGE_WEIGHTS: dict[str, float] = {
    "depends_on": 1.0,
    "contradicts": 0.75,
    "supersedes": 0.5,
    "caused_by": 0.8,
    "attempted_for": 0.9,
    "verifies": 0.75,
    "mentions": 0.5,
    "relates_to": 0.25,
}

CONFLICT_TYPES = ("contradiction", "supersession")
CONFLICT_RESOLUTIONS = ("new", "old", "existing", "both")
CONSENT_DECISIONS = ("approve_run", "approve_session", "reject", "reject_session")
PLACEMENT_ACTIONS = ("new", "update", "create", "merge", "replace", "drop", "supersede")

WEIGHT_COSINE = 0.50
WEIGHT_LEXICAL = 0.20
WEIGHT_ENTITY = 0.30

DUPLICATE_SIM = 0.85
SUPERSEDE_SIM = 0.75
RELATED_SIM = 0.45
SCORE_FLOOR = 0.20

SUPERSEDED_DEMOTION = 0.20
UNPROMOTED_DEMOTION = 0.25
DISPUTED_DEMOTION = 0.50

AUTO_SUPERSEDE_CONFIDENCE = 0.75
MAX_DEPTH = 2
MIN_EDGE_WEIGHT = 0.30
COCOMMIT_WEIGHT = 0.80
SHARED_ENTITY_WEIGHT = 0.50
DEFAULT_TTL_SECONDS = 30 * 60
LOCK_TIMEOUT_SECONDS = 2.0
SQLITE_TIMEOUT_SECONDS = 2.0
DEFAULT_MAX_TOKENS = 2500
EMBED_DIM = 256

UNPROMOTED_STATUSES = frozenset({"draft", "proposed"})


def vocabulary() -> dict:
    return {
        "entity_type": list(ENTITY_TYPES),
        "fact_status": list(FACT_STATUSES),
        "edge_type": dict(EDGE_WEIGHTS),
        "conflict_type": list(CONFLICT_TYPES),
        "conflict_resolution": list(CONFLICT_RESOLUTIONS),
        "consent_decision": list(CONSENT_DECISIONS),
        "placement_action": list(PLACEMENT_ACTIONS),
        "note": (
            "Values outside these lists are accepted for entities, facts, and "
            "placement actions. Only approve_run and approve_session authorize storage."
        ),
    }


def auto_inject_mode() -> str:
    mode = os.environ.get("ECG_AUTO_INJECT", "chat").strip().lower()
    if mode not in ("chat", "preview"):
        return "preview"
    return mode


def auto_inject_max_tokens() -> int:
    raw = os.environ.get("ECG_AUTO_INJECT_MAX_TOKENS", str(DEFAULT_MAX_TOKENS))
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_MAX_TOKENS


def debug_enabled() -> bool:
    return os.environ.get("ECG_DEBUG", "0").strip() not in ("", "0", "false", "False")


class EntityIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    entity_type: str = "concept"


class CandidateIn(BaseModel):
    """A fact the agent wants considered. Nothing here is stored yet."""

    model_config = ConfigDict(extra="ignore")

    title: str
    body: str = ""
    fact_type: str = "concept"
    entities: list[EntityIn] = Field(default_factory=list)
    confidence: float = 1.0
    action: str = "new"
    supersede_existing_fact_id: str | None = None
    target_fact_id: str | None = None


class MessageIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: str
    content: str


class PlacementIn(CandidateIn):
    """The agent's decision for one candidate. This object is what the user sees."""


@dataclass
class ScoredFact:
    fact_id: str
    title: str
    body: str
    fact_type: str
    status: str
    confidence: float
    disputed: bool
    session_id: str
    score: float
    entities: list[str] = field(default_factory=list)


@dataclass
class Cluster:
    label: str
    facts: list[ScoredFact]


@dataclass
class FactRecord:
    id: str
    repo_id: str
    session_id: str
    exchange_id: str | None
    title: str
    body: str
    fact_type: str
    status: str
    confidence: float
    disputed: bool
    promoted: bool
    superseded_by: str | None
    entities: list[tuple[str, str]]
    embedding: list[float] | None
