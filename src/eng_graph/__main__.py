"""MCP server for the Engineering Continuity Graph.

Tool docstrings are the contract a coding agent reads. Names are prefixed with
``ecg_`` so they do not collide with tools from other servers in the same client.

Nothing is written to SQLite unless the user grants consent, and fact bodies
enter the transcript only through ``ecg_search_context``. ``ecg_commit_step``
and ``ecg_approve_consent`` are the approval checkpoints: their arguments are
what the user reads in the client dialog, so callers must pass the real titles,
bodies, and grant decision rather than a placeholder.
"""

from __future__ import annotations

from functools import wraps

from mcp.server.fastmcp import FastMCP

from eng_graph.db import DatabaseBusy, DatabaseCorrupt, DatabaseMissing
from eng_graph.state import (
    SessionNotFound,
    approve_consent,
    cancel_step,
    commit_placements,
    commit_step,
    discard_proposal,
    get_session,
    list_conflicts,
    prepare_placements,
    record_provenance,
    resolve_conflict,
    search_context,
    stats,
)

mcp = FastMCP("Engineering Continuity Graph")


def _guard(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except SessionNotFound as exc:
            return {"ok": False, "error": "session_not_found", "message": str(exc)}
        except (DatabaseBusy, DatabaseCorrupt, DatabaseMissing) as exc:
            return {"ok": False, "error": type(exc).__name__, "message": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": "internal", "message": f"{type(exc).__name__}: {exc}"}

    return wrapper


@mcp.tool(name="ecg_get_session")
@_guard
def ecg_get_session(path: str) -> dict:
    """Start or resume the engineering-memory session for a project path.

    Returns session id, repository id, stats, a short repo map, consent state,
    and separate ``database`` and ``embeddings`` status objects. If one of those
    is degraded the other is still reported. This call does not store facts.
    Pass the project root (or any file inside it), not a login secret.
    """
    return get_session(path)


@mcp.tool(name="ecg_prepare_placements")
@_guard
def ecg_prepare_placements(session_id: str, candidates: list[dict]) -> dict:
    """Score candidate facts against the repo and stage a proposal. Does not store.

    Each candidate needs ``title`` and may include ``body``, ``fact_type``,
    ``entities`` (``name``, ``entity_type``), ``confidence`` (0..1), and
    ``action`` (new, update, create, merge, replace, drop, supersede).
    Unknown fact and entity types are accepted.

    The result is a ``prep_id`` (also returned as ``exchange_id``) plus a
    classification for each candidate: ``likely_duplicate``, ``possible_supersede``,
    ``related``, or ``new``, with the matched fact titles and scores.
    The proposal lives only in process memory. Abandoning it leaves no database row.
    """
    return prepare_placements(session_id, candidates)


@mcp.tool(name="ecg_commit_placements")
@_guard
def ecg_commit_placements(session_id: str, exchange_id: str, placements: list[dict]) -> dict:
    """Stage the agent's chosen placement actions. Does not write durable memory.

    ``exchange_id`` is the ``prep_id`` from ``ecg_prepare_placements``.
    Use this to record merge, replace, or drop decisions before the user
    approves ``ecg_commit_step``. Dropped placements must not be sent on later
    as if they were approved.
    """
    return commit_placements(session_id, exchange_id, placements)


@mcp.tool(name="ecg_record_provenance")
@_guard
def ecg_record_provenance(session_id: str, exchange_id: str, messages: list[dict]) -> dict:
    """Stage conversation turns that explain a proposal. Does not write durable memory.

    Each message is ``{"role": "user"|"assistant", "content": "..."}``.
    Full text is stored only if a later ``ecg_commit_step`` succeeds under consent.
    """
    return record_provenance(session_id, exchange_id, messages)


@mcp.tool(name="ecg_approve_consent")
@_guard
def ecg_approve_consent(session_id: str, grant: str) -> dict:
    """Record the user's consent decision. This call is the consent checkpoint.

    ``grant`` is one of ``approve_run``, ``approve_session``, ``reject``,
    or ``reject_session``. The value must be the user's choice from the approval
    dialog. Do not send ``approve_run`` or ``approve_session`` on your own.

    ``approve_run`` authorizes exactly one successful commit.
    ``approve_session`` authorizes storage for the rest of this session.
    ``reject`` is an audit entry and never authorizes storage.
    ``reject_session`` turns storage off and stops further consent prompts.
    Any other string is recorded and does not authorize storage.
    """
    return approve_consent(session_id, grant)


@mcp.tool(name="ecg_commit_step")
@_guard
def ecg_commit_step(
    session_id: str,
    exchange_id: str,
    messages: list[dict],
    placements: list[dict],
) -> dict:
    """Store approved facts for one exchange. This call is the storage checkpoint.

    The arguments are shown to the user in the native approval dialog. Pass the
    real ``title`` and ``body`` of every fact. Do not substitute "see above",
    a hash, or an empty placements list to hide what will be saved.

    ``exchange_id`` is the ``prep_id`` from prepare, or a new id when committing
    placements directly. ``messages`` are provenance turns. Each placement uses
    the same fields as a candidate, plus ``action`` and optional
    ``supersede_existing_fact_id`` / ``target_fact_id``.

    Storage is refused unless the session has ``approve_session`` or an
    unconsumed ``approve_run`` grant. One successful commit consumes one
    ``approve_run`` grant. A failed commit does not consume it.
    An explicit supersede at confidence >= 0.75 marks the old fact superseded
    and links ``supersedes``. Lower confidence, or an ambiguous target, keeps
    both facts and opens a conflict instead of overwriting anything.
    """
    return commit_step(session_id, exchange_id, messages, placements)


@mcp.tool(name="ecg_cancel_step")
@_guard
def ecg_cancel_step(session_id: str, exchange_id: str) -> dict:
    """Drop a staged proposal. No-op if it is already gone. Does not delete stored facts."""
    return cancel_step(session_id, exchange_id)


@mcp.tool(name="ecg_search_context")
def ecg_search_context(
    session_id: str,
    query: str,
    entity_names: list[str] | None = None,
    limit: int = 8,
) -> str:
    """Return the smallest fact neighborhood for ``query``. This is the only body export.

    The server ranks facts itself from ``query`` and ``entity_names``. It does
    not accept a fact-id list, so a caller cannot widen an approval by naming
    extra facts. Results are connected clusters labeled by their shared entity
    (or the top fact title). When the rendered block would exceed
    ``ECG_AUTO_INJECT_MAX_TOKENS``, or ``ECG_AUTO_INJECT=preview``, the block
    contains titles only and says why.
    """
    try:
        return search_context(session_id, query, entity_names, limit)
    except Exception as exc:
        return f"engineering memory unavailable: {type(exc).__name__}: {exc}\n"


@mcp.tool(name="ecg_discard_proposal")
@_guard
def ecg_discard_proposal(session_id: str) -> dict:
    """The user declined the staged proposal. Drop it. Do not store it later."""
    return discard_proposal(session_id)


@mcp.tool(name="ecg_list_conflicts")
@_guard
def ecg_list_conflicts(session_id: str) -> dict:
    """List open supersession or contradiction conflicts. Titles and ids only, no bodies."""
    return list_conflicts(session_id)


@mcp.tool(name="ecg_resolve_conflict")
@_guard
def ecg_resolve_conflict(session_id: str, resolution: str, conflicts: list[str]) -> dict:
    """Resolve open conflicts. This call is the user's choice, not a silent overwrite.

    ``resolution`` is ``new`` (keep the new fact, supersede the old), ``old`` or
    ``existing`` (keep the old fact, supersede the new), or ``both`` (keep both
    active). History is kept: the loser is marked superseded and linked, never
    deleted. ``conflicts`` is a list of conflict ids from ``ecg_list_conflicts``.
    """
    return resolve_conflict(session_id, resolution, conflicts)


@mcp.tool(name="ecg_stats")
@_guard
def ecg_stats(session_id: str) -> dict:
    """Read-only counts for the session's repository.

    Reports exchanges, active facts, superseded facts, total facts, edges,
    entities, and open conflicts. Fact bodies are not included.
    """
    return stats(session_id)


def main() -> None:
    """Run the MCP server on stdio. Used by the ``eng-server`` console script."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    from eng_graph.cli import main as cli_main

    cli_main()
