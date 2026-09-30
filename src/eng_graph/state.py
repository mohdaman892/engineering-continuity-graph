"""Durable graph: sessions, consent, facts, edges, conflicts, and stats.

Storage is default-deny. A commit writes only when the session is
``session_all`` or an unconsumed ``approve_run`` grant exists. The grant is
consumed in the same transaction as the write, so a failed commit leaves it
intact. Embeddings are computed before the write lock is taken.
"""

from __future__ import annotations

import hashlib
import itertools
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from eng_graph.core import (
    connected_components,
    cocommit_weight,
    expand_neighborhood,
    fts_query,
    index_status,
    infer_entities,
    normalize_bm25,
    normalize_name,
    placement_scores,
    rank_facts,
    recommend_action,
    render_memory,
    should_auto_supersede,
    clamp_confidence,
    estimate_tokens,
)
from eng_graph.db import (
    SCHEMA_VERSION,
    Database,
    DatabaseBusy,
    DatabaseCorrupt,
    DatabaseMissing,
    debug,
)
from eng_graph.embeddings import EmbeddingProvider, get_provider, provider_name
from eng_graph.models import (
    CONFLICT_RESOLUTIONS,
    EDGE_WEIGHTS,
    SHARED_ENTITY_WEIGHT,
    SUPERSEDE_SIM,
    CandidateIn,
    Cluster,
    FactRecord,
    MessageIn,
    PlacementIn,
    ScoredFact,
    auto_inject_max_tokens,
    auto_inject_mode,
    vocabulary,
)
from eng_graph.repo import RepoIdentity, resolve_repository
from eng_graph.staging import Preparation, ProviderApproval, TtlStaged

import struct


class ConsentDenied(Exception):
    pass


class SessionNotFound(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _pack(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *[float(v) for v in vector])


def _unpack(blob: bytes) -> list[float]:
    count = len(blob) // 4
    if count == 0:
        return []
    return list(struct.unpack(f"<{count}f", blob[: count * 4]))


def database_path(root: Path) -> Path:
    override = os.environ.get("ECG_DB_PATH")
    if override:
        return Path(override).expanduser().resolve()
    convo = root / ".convo"
    legacy = root / ".eng_graph"
    if legacy.exists() and not convo.exists():
        return legacy / "memory.db"
    return convo / "memory.db"


class Registry:
    def __init__(self) -> None:
        self.preps: TtlStaged[Preparation] = TtlStaged()
        self.provs: TtlStaged[ProviderApproval] = TtlStaged()
        self.dbs: dict[str, Database] = {}
        self.lock = threading.RLock()
        self.embedder: EmbeddingProvider | None = None
        self.embedder_info: dict | None = None


REGISTRY = Registry()


def reset_registry() -> None:
    for db in list(REGISTRY.dbs.values()):
        db.close()
    REGISTRY.dbs.clear()
    REGISTRY.preps.clear()
    REGISTRY.provs.clear()
    REGISTRY.embedder = None
    REGISTRY.embedder_info = None


def embedding_status() -> dict:
    if REGISTRY.embedder_info is not None:
        return dict(REGISTRY.embedder_info)
    name = provider_name()
    try:
        provider = get_provider(name)
        vector = provider.get_embedding("ping")
        REGISTRY.embedder = provider
        info = {"ok": True, "provider": getattr(provider, "name", name), "dimensions": len(vector)}
    except Exception as exc:
        REGISTRY.embedder = None
        info = {"ok": False, "provider": name, "error": f"{type(exc).__name__}: {exc}"}
    REGISTRY.embedder_info = info
    return dict(info)


def _database(path: Path) -> Database:
    key = str(path)
    with REGISTRY.lock:
        db = REGISTRY.dbs.get(key)
        if db is None:
            db = Database(path)
            REGISTRY.dbs[key] = db
        return db


def _ensure_env_db() -> None:
    override = os.environ.get("ECG_DB_PATH")
    if not override:
        return
    path = Path(override).expanduser()
    if path.exists():
        _database(path.resolve())


class Memory:
    def __init__(
        self,
        db: Database,
        embedder: EmbeddingProvider | None,
        preps: TtlStaged[Preparation],
        provs: TtlStaged[ProviderApproval],
    ) -> None:
        self.db = db
        self.embedder = embedder
        self.preps = preps
        self.provs = provs

    def open_session(self, identity: RepoIdentity) -> dict:
        stamp = _now()
        with self.db.write() as conn:
            row = conn.execute(
                "SELECT id FROM repositories WHERE id = ?",
                (identity.repo_id,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO repositories(id, path, remote_url, created_at) VALUES (?, ?, ?, ?)",
                    (identity.repo_id, str(identity.root), identity.remote, stamp),
                )
            else:
                conn.execute(
                    "UPDATE repositories SET path = ?, remote_url = ? WHERE id = ?",
                    (str(identity.root), identity.remote, identity.repo_id),
                )
            existing = conn.execute(
                """
                SELECT id, repo_id, mode, ask_prompt, may_store
                FROM sessions WHERE repo_id = ?
                ORDER BY updated_at DESC LIMIT 1
                """,
                (identity.repo_id,),
            ).fetchone()
            if existing is not None:
                conn.execute(
                    "UPDATE sessions SET updated_at = ? WHERE id = ?",
                    (stamp, existing["id"]),
                )
                return {
                    "id": existing["id"],
                    "repo_id": existing["repo_id"],
                    "mode": existing["mode"],
                    "resumed": True,
                }
            session_id = _new_id("sess")
            conn.execute(
                """
                INSERT INTO sessions(id, repo_id, mode, ask_prompt, may_store, created_at, updated_at)
                VALUES (?, ?, 'default', 1, 0, ?, ?)
                """,
                (session_id, identity.repo_id, stamp, stamp),
            )
            return {"id": session_id, "repo_id": identity.repo_id, "mode": "default", "resumed": False}

    def consent_view(self, session_id: str) -> dict:
        with self.db.read() as conn:
            return _consent_view(conn, session_id)

    def stats_for(self, repo_id: str) -> dict:
        with self.db.read() as conn:
            return _stats(conn, repo_id)

    def repo_map(self, repo_id: str) -> list[dict]:
        with self.db.read() as conn:
            rows = conn.execute(
                """
                SELECT e.name, e.entity_type, COUNT(*) AS n
                FROM entities e
                JOIN fact_entities fe ON fe.entity_id = e.id
                JOIN facts f ON f.id = fe.fact_id
                WHERE e.repo_id = ? AND f.status != 'rejected'
                GROUP BY e.id
                ORDER BY n DESC, e.name ASC
                LIMIT 32
                """,
                (repo_id,),
            ).fetchall()
        return [
            {"name": row["name"], "entity_type": row["entity_type"], "count": row["n"]}
            for row in rows
        ]

    def record_consent(self, session_id: str, decision: str) -> dict:
        decision = (decision or "").strip()
        stamp = _now()
        with self.db.write() as conn:
            session = conn.execute(
                "SELECT id, mode FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if session is None:
                raise SessionNotFound(session_id)
            grant_id = _new_id("grant")
            if decision == "approve_session":
                conn.execute(
                    """
                    UPDATE sessions
                    SET mode = 'session_all', may_store = 1, ask_prompt = 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (stamp, session_id),
                )
            elif decision == "reject_session":
                conn.execute(
                    """
                    UPDATE sessions
                    SET mode = 'session_rejected', may_store = 0, ask_prompt = 0, updated_at = ?
                    WHERE id = ?
                    """,
                    (stamp, session_id),
                )
            elif decision not in ("approve_run", "reject"):
                # Unknown decisions are audited and cannot authorize a write.
                pass
            conn.execute(
                """
                INSERT INTO consent_grants(id, session_id, decision, consumed, created_at)
                VALUES (?, ?, ?, 0, ?)
                """,
                (grant_id, session_id, decision or "unspecified", stamp),
            )
            view = _consent_view(conn, session_id)
        authorizes = decision in ("approve_run", "approve_session")
        return {
            "ok": True,
            "grant_id": grant_id,
            "decision": decision,
            "authorizes_storage": authorizes,
            "consent": view,
        }

    def prepare(self, session_id: str, candidates: list[CandidateIn]) -> dict:
        repo_id = self._repo_id(session_id)
        vectors = [_embed(self.embedder, f"{item.title}\n{item.body}") for item in candidates]
        with self.db.read() as conn:
            facts = _load_facts(conn, repo_id)
            prepared = []
            for index, candidate in enumerate(candidates):
                lexical = _lexical_scores(conn, candidate.title + "\n" + candidate.body, facts)
                names = _candidate_names(candidate)
                matches = placement_scores(
                    vectors[index],
                    names,
                    candidate.fact_type,
                    candidate.action,
                    lexical,
                    facts,
                )
                best = matches[0] if matches else None
                classification = best["classification"] if best else "new"
                recommended = recommend_action(classification)
                staged = candidate.model_dump()
                staged["action"] = candidate.action if candidate.action not in ("", "new") else recommended
                if best and classification in ("likely_duplicate", "possible_supersede"):
                    staged["target_fact_id"] = best["fact_id"]
                    if classification == "possible_supersede":
                        staged["supersede_existing_fact_id"] = best["fact_id"]
                prepared.append(
                    {
                        "candidate_index": index,
                        "title": candidate.title,
                        "fact_type": candidate.fact_type,
                        "classification": classification,
                        "recommended_action": recommended,
                        "matches": matches,
                        "placement": staged,
                    }
                )
        prep = Preparation(
            session_id=session_id,
            candidates=[item.model_dump() for item in candidates],
            placements=[item["placement"] for item in prepared],
        )
        prep_id = self.preps.put("prep", prep, session_id)
        return {"ok": True, "prep_id": prep_id, "exchange_id": prep_id, "placements": prepared}

    def stage_placements(self, session_id: str, exchange_id: str, placements: list[dict]) -> dict:
        prep = self._prep(session_id, exchange_id)
        if prep is None:
            prep = Preparation(session_id=session_id, placements=list(placements))
            exchange_id = self.preps.put("prep", prep, session_id)
        else:
            prep.placements = list(placements)
        return {
            "ok": True,
            "stored": False,
            "exchange_id": exchange_id,
            "staged_placements": len(placements),
        }

    def stage_messages(self, session_id: str, exchange_id: str, messages: list[dict]) -> dict:
        prep = self._prep(session_id, exchange_id)
        if prep is None:
            prep = Preparation(session_id=session_id, messages=list(messages))
            exchange_id = self.preps.put("prep", prep, session_id)
        else:
            prep.messages = list(messages)
        return {
            "ok": True,
            "stored": False,
            "exchange_id": exchange_id,
            "staged_messages": len(messages),
        }

    def cancel(self, session_id: str, exchange_id: str) -> dict:
        prep = self.preps.get(exchange_id) if exchange_id else None
        if prep is None or prep.session_id != session_id:
            return {"ok": True, "cancelled": False}
        self.preps.pop(exchange_id)
        return {"ok": True, "cancelled": True}

    def discard(self, session_id: str) -> dict:
        dropped = self.preps.drop_session(session_id) + self.provs.drop_session(session_id)
        return {"ok": True, "discarded": dropped, "stored": False}

    def commit(
        self,
        session_id: str,
        exchange_id: str,
        messages: list[MessageIn],
        placements: list[PlacementIn],
    ) -> dict:
        prep = self._prep(session_id, exchange_id)
        if not placements and prep is not None:
            placements = [PlacementIn.model_validate(item) for item in prep.placements]
        if not messages and prep is not None:
            messages = [MessageIn.model_validate(item) for item in prep.messages]
        actionable = [
            item for item in placements if (item.action or "create") != "drop" and item.title.strip()
        ]
        if len(actionable) > 30:
            return {
                "ok": False,
                "error": "too_many_placements",
                "message": "at most 30 facts can be stored in one commit",
            }
        for item in actionable:
            if len(item.title) > 500 or len(item.body) > 20000:
                return {
                    "ok": False,
                    "error": "invalid_placement",
                    "message": "title or body exceeds the size limit",
                }
        if not actionable and not messages:
            return {"ok": True, "stored": False, "reason": "nothing_to_store", "facts": []}
        if not exchange_id:
            exchange_id = _new_id("exch")
        vectors = [
            _embed(self.embedder, f"{item.title}\n{item.body}") for item in actionable
        ]
        provider_name_value = getattr(self.embedder, "name", "none")
        try:
            with self.db.write() as conn:
                result = self._commit_locked(
                    conn,
                    session_id,
                    exchange_id,
                    messages,
                    actionable,
                    vectors,
                    provider_name_value,
                )
        except ConsentDenied as exc:
            return {"ok": False, "error": "consent_required", "message": str(exc)}
        except ValueError as exc:
            return {"ok": False, "error": "invalid_placement", "message": str(exc)}
        if result.get("stored"):
            self.preps.pop(exchange_id)
        return result

    def search(
        self,
        session_id: str,
        query: str,
        entity_names: list[str] | None,
        limit: int,
    ) -> str:
        limit = max(1, min(int(limit or 8), 20))
        query_vec = _embed(self.embedder, query)
        with self.db.read() as conn:
            session = conn.execute(
                "SELECT repo_id FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if session is None:
                raise SessionNotFound(session_id)
            repo_id = session["repo_id"]
            facts = _load_facts(conn, repo_id)
            lexical = _lexical_scores(conn, query, facts)
            edge_rows = conn.execute(
                "SELECT src_fact_id, dst_fact_id, weight FROM edges WHERE repo_id = ?",
                (repo_id,),
            ).fetchall()
        edges = [(row["src_fact_id"], row["dst_fact_id"], float(row["weight"])) for row in edge_rows]
        ranked = rank_facts(query, query_vec, entity_names, lexical, facts)
        seeds = ranked[:limit]
        by_id = {fact.id: fact for fact in facts}
        included, traversed = expand_neighborhood(seeds, by_id, edges)
        clusters = connected_components(included, traversed)
        fact_ids = [fact.fact_id for cluster in clusters for fact in cluster.facts]
        self.provs.put(
            "prov",
            ProviderApproval(session_id=session_id, query=query, fact_ids=fact_ids),
            session_id,
        )
        # Bodies are reloaded from ids this process just stored, never from a caller list.
        allowed = set(fact_ids)
        for cluster in clusters:
            cluster.facts = [fact for fact in cluster.facts if fact.fact_id in allowed]
        return render_memory(
            [cluster for cluster in clusters if cluster.facts],
            mode=auto_inject_mode(),
            max_tokens=auto_inject_max_tokens(),
        )

    def recent_text(self, session_id: str) -> str:
        """Newest active facts, used when a broad query is below the score floor."""
        with self.db.read() as conn:
            session = conn.execute(
                "SELECT repo_id FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if session is None:
                return ""
            rows = conn.execute(
                """
                SELECT id, title, body, fact_type, status, confidence, disputed, session_id
                FROM facts
                WHERE repo_id = ? AND status NOT IN ('rejected', 'superseded')
                ORDER BY updated_at DESC
                LIMIT 8
                """,
                (session["repo_id"],),
            ).fetchall()
        if not rows:
            return ""
        facts = [
            ScoredFact(
                fact_id=row["id"],
                title=row["title"],
                body=row["body"],
                fact_type=row["fact_type"],
                status=row["status"],
                confidence=float(row["confidence"]),
                disputed=bool(row["disputed"]),
                session_id=row["session_id"],
                score=1.0,
                entities=[],
            )
            for row in rows
        ]
        label = facts[0].title
        return render_memory(
            [Cluster(label, facts)],
            mode=auto_inject_mode(),
            max_tokens=auto_inject_max_tokens(),
        )

    def list_conflicts(self, session_id: str) -> dict:
        with self.db.read() as conn:
            session = conn.execute(
                "SELECT repo_id FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if session is None:
                raise SessionNotFound(session_id)
            rows = conn.execute(
                """
                SELECT c.id, c.conflict_type, c.new_fact_id, c.existing_fact_id, c.created_at,
                       n.title AS new_title, e.title AS existing_title
                FROM conflicts c
                JOIN facts n ON n.id = c.new_fact_id
                JOIN facts e ON e.id = c.existing_fact_id
                WHERE c.repo_id = ? AND c.status = 'open'
                ORDER BY c.created_at ASC
                """,
                (session["repo_id"],),
            ).fetchall()
        return {
            "ok": True,
            "conflicts": [
                {
                    "id": row["id"],
                    "conflict_type": row["conflict_type"],
                    "new_fact_id": row["new_fact_id"],
                    "new_title": row["new_title"],
                    "existing_fact_id": row["existing_fact_id"],
                    "existing_title": row["existing_title"],
                    "created_at": row["created_at"],
                }
                for row in rows
            ],
        }

    def resolve(self, session_id: str, resolution: str, conflict_ids: list[str]) -> dict:
        resolution = (resolution or "").strip()
        if resolution not in CONFLICT_RESOLUTIONS:
            return {
                "ok": False,
                "error": "unknown_resolution",
                "message": "resolution must be new, old, existing, or both",
            }
        stamp = _now()
        resolved = []
        with self.db.write() as conn:
            session = conn.execute(
                "SELECT repo_id FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if session is None:
                raise SessionNotFound(session_id)
            repo_id = session["repo_id"]
            for conflict_id in conflict_ids:
                row = conn.execute(
                    """
                    SELECT * FROM conflicts
                    WHERE id = ? AND repo_id = ? AND status = 'open'
                    """,
                    (conflict_id, repo_id),
                ).fetchone()
                if row is None:
                    continue
                new_id = row["new_fact_id"]
                old_id = row["existing_fact_id"]
                if resolution == "both":
                    conn.execute(
                        "UPDATE facts SET disputed = 0, updated_at = ? WHERE id IN (?, ?)",
                        (stamp, new_id, old_id),
                    )
                else:
                    if resolution == "new":
                        winner, loser = new_id, old_id
                    else:
                        winner, loser = old_id, new_id
                    conn.execute(
                        """
                        UPDATE facts
                        SET status = 'superseded', superseded_by = ?, disputed = 0, updated_at = ?
                        WHERE id = ?
                        """,
                        (winner, stamp, loser),
                    )
                    conn.execute(
                        "UPDATE facts SET disputed = 0, updated_at = ? WHERE id = ?",
                        (stamp, winner),
                    )
                    _insert_edge(
                        conn,
                        repo_id,
                        winner,
                        loser,
                        "supersedes",
                        EDGE_WEIGHTS["supersedes"],
                        stamp,
                    )
                conn.execute(
                    """
                    UPDATE conflicts
                    SET status = 'resolved', resolution = ?, resolved_at = ?
                    WHERE id = ?
                    """,
                    (resolution, stamp, conflict_id),
                )
                resolved.append(conflict_id)
        return {"ok": True, "resolution": resolution, "resolved": resolved}

    def counts(self, repo_id: str) -> dict:
        try:
            with self.db.read() as conn:
                return _stats(conn, repo_id)
        except DatabaseMissing:
            return _empty_stats()

    def wipe(self, repo_id: str) -> dict:
        with self.db.write() as conn:
            counts = _stats(conn, repo_id)
            fact_ids = [
                row["id"]
                for row in conn.execute("SELECT id FROM facts WHERE repo_id = ?", (repo_id,))
            ]
            for fact_id in fact_ids:
                conn.execute("DELETE FROM facts_fts WHERE fact_id = ?", (fact_id,))
            conn.execute("DELETE FROM conflicts WHERE repo_id = ?", (repo_id,))
            conn.execute("DELETE FROM edges WHERE repo_id = ?", (repo_id,))
            conn.execute(
                "DELETE FROM fact_entities WHERE fact_id IN (SELECT id FROM facts WHERE repo_id = ?)",
                (repo_id,),
            )
            conn.execute(
                "DELETE FROM fact_embeddings WHERE fact_id IN (SELECT id FROM facts WHERE repo_id = ?)",
                (repo_id,),
            )
            conn.execute("DELETE FROM facts WHERE repo_id = ?", (repo_id,))
            conn.execute(
                "DELETE FROM messages WHERE exchange_id IN (SELECT id FROM exchanges WHERE session_id IN (SELECT id FROM sessions WHERE repo_id = ?))",
                (repo_id,),
            )
            conn.execute(
                "DELETE FROM exchanges WHERE session_id IN (SELECT id FROM sessions WHERE repo_id = ?)",
                (repo_id,),
            )
            conn.execute(
                "DELETE FROM consent_grants WHERE session_id IN (SELECT id FROM sessions WHERE repo_id = ?)",
                (repo_id,),
            )
            conn.execute("DELETE FROM sessions WHERE repo_id = ?", (repo_id,))
            conn.execute(
                """
                DELETE FROM entities
                WHERE repo_id = ?
                  AND id NOT IN (SELECT entity_id FROM fact_entities)
                """,
                (repo_id,),
            )
            conn.execute("DELETE FROM repositories WHERE id = ?", (repo_id,))
        return counts

    def _commit_locked(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        exchange_id: str,
        messages: list[MessageIn],
        placements: list[PlacementIn],
        vectors: list[list[float] | None],
        provider_name_value: str,
    ) -> dict:
        session = conn.execute(
            "SELECT id, repo_id, mode FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if session is None:
            raise SessionNotFound(session_id)
        existing = conn.execute(
            "SELECT session_id FROM exchanges WHERE id = ?",
            (exchange_id,),
        ).fetchone()
        if existing is not None:
            if existing["session_id"] != session_id:
                raise ValueError("exchange belongs to another session")
            facts = conn.execute(
                "SELECT id, title, status FROM facts WHERE exchange_id = ?",
                (exchange_id,),
            ).fetchall()
            return {
                "ok": True,
                "stored": True,
                "already_committed": True,
                "exchange_id": exchange_id,
                "session_id": session_id,
                "grant_consumed": None,
                "facts": [
                    {"id": row["id"], "title": row["title"], "status": row["status"]}
                    for row in facts
                ],
                "conflicts_opened": [],
                "edges_created": 0,
            }
        repo_id = session["repo_id"]
        grant_id = _lock_grant(conn, session_id, session["mode"])
        stamp = _now()
        conn.execute(
            "INSERT INTO exchanges(id, session_id, created_at) VALUES (?, ?, ?)",
            (exchange_id, session_id, stamp),
        )
        for ordinal, message in enumerate(messages):
            conn.execute(
                "INSERT INTO messages(exchange_id, role, content, ordinal) VALUES (?, ?, ?, ?)",
                (exchange_id, message.role, message.content, ordinal),
            )
        facts = _load_facts(conn, repo_id)
        summaries = []
        new_ids: list[str] = []
        conflicts: list[dict] = []
        edges = 0
        for placement, vector in zip(placements, vectors):
            summary, created_edges = self._apply_placement(
                conn,
                repo_id,
                session_id,
                exchange_id,
                placement,
                vector,
                provider_name_value,
                facts,
                stamp,
                new_ids,
                conflicts,
            )
            edges += created_edges
            summaries.append(summary)
            if summary.get("id") and summary.get("created"):
                facts = _load_facts(conn, repo_id)
        fresh_ids = [item["id"] for item in summaries if item.get("created") and item.get("id")]
        for left, right in itertools.combinations(fresh_ids, 2):
            edges += _insert_edge(
                conn, repo_id, left, right, "relates_to", cocommit_weight(), stamp
            )
        for fact_id in fresh_ids:
            edges += _link_shared_entities(conn, repo_id, fact_id, stamp)
        if grant_id is not None:
            updated = conn.execute(
                """
                UPDATE consent_grants
                SET consumed = 1, consumed_at = ?
                WHERE id = ? AND consumed = 0
                """,
                (stamp, grant_id),
            )
            if updated.rowcount != 1:
                raise ConsentDenied("approve_run grant could not be consumed")
        debug(f"commit exchange={exchange_id} facts={len(summaries)} grant={grant_id}")
        return {
            "ok": True,
            "stored": True,
            "already_committed": False,
            "exchange_id": exchange_id,
            "session_id": session_id,
            "grant_consumed": grant_id,
            "session_mode": session["mode"],
            "facts": summaries,
            "conflicts_opened": conflicts,
            "edges_created": edges,
        }

    def _apply_placement(
        self,
        conn: sqlite3.Connection,
        repo_id: str,
        session_id: str,
        exchange_id: str,
        placement: PlacementIn,
        vector: list[float] | None,
        provider_name_value: str,
        facts: list[FactRecord],
        stamp: str,
        new_ids: list[str],
        conflicts: list[dict],
    ) -> tuple[dict, int]:
        action = placement.action or "create"
        if action == "new":
            action = "create"
        confidence = clamp_confidence(placement.confidence)
        lexical = _lexical_scores(conn, placement.title + "\n" + placement.body, facts)
        matches = placement_scores(
            vector,
            _candidate_names(placement),
            placement.fact_type,
            action,
            lexical,
            facts,
        )
        edges = 0
        if action in ("merge", "update"):
            target = placement.target_fact_id or _best_id(matches)
            if target and _fact_exists(conn, target, repo_id):
                summary = _merge_or_update(
                    conn,
                    self.embedder,
                    provider_name_value,
                    target,
                    placement,
                    action,
                    stamp,
                )
                return summary, 0
            action = "create"
        if action in ("replace", "supersede") or placement.supersede_existing_fact_id:
            high = [
                match
                for match in matches
                if match["score"] >= SUPERSEDE_SIM and match["status"] != "superseded"
            ]
            explicit = bool(placement.supersede_existing_fact_id)
            target = placement.supersede_existing_fact_id or placement.target_fact_id
            ambiguous = False
            if not explicit:
                if len(high) >= 2:
                    ambiguous = True
                    target = high[0]["fact_id"]
                elif target is None and len(high) == 1:
                    target = high[0]["fact_id"]
            if target:
                if not _fact_exists(conn, target, repo_id):
                    raise ValueError(f"supersede target {target} does not exist")
                fact_id = _insert_fact(
                    conn,
                    repo_id,
                    session_id,
                    exchange_id,
                    placement,
                    vector,
                    provider_name_value,
                    stamp,
                    disputed=not should_auto_supersede(confidence, ambiguous, True),
                    status=index_status(confidence, action),
                )
                new_ids.append(fact_id)
                if should_auto_supersede(confidence, ambiguous, True):
                    conn.execute(
                        """
                        UPDATE facts
                        SET status = 'superseded', superseded_by = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (fact_id, stamp, target),
                    )
                    edges += _insert_edge(
                        conn,
                        repo_id,
                        fact_id,
                        target,
                        "supersedes",
                        EDGE_WEIGHTS["supersedes"],
                        stamp,
                    )
                    return {
                        "id": fact_id,
                        "title": placement.title,
                        "action": "supersede",
                        "status": index_status(confidence, action),
                        "created": True,
                        "superseded": target,
                    }, edges
                edges += _insert_edge(
                    conn,
                    repo_id,
                    fact_id,
                    target,
                    "contradicts",
                    EDGE_WEIGHTS["contradicts"],
                    stamp,
                )
                conflict_id = _open_conflict(
                    conn,
                    repo_id,
                    session_id,
                    "supersession" if action in ("replace", "supersede") or explicit else "contradiction",
                    fact_id,
                    target,
                    stamp,
                )
                conflicts.append(
                    {
                        "id": conflict_id,
                        "new_fact_id": fact_id,
                        "existing_fact_id": target,
                    }
                )
                return {
                    "id": fact_id,
                    "title": placement.title,
                    "action": "conflict",
                    "status": index_status(confidence, action),
                    "created": True,
                    "disputed": True,
                }, edges
        fact_id = _insert_fact(
            conn,
            repo_id,
            session_id,
            exchange_id,
            placement,
            vector,
            provider_name_value,
            stamp,
            disputed=False,
            status=index_status(confidence, action),
        )
        new_ids.append(fact_id)
        return {
            "id": fact_id,
            "title": placement.title,
            "action": action,
            "status": index_status(confidence, action),
            "created": True,
        }, edges

    def _repo_id(self, session_id: str) -> str:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT repo_id FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            raise SessionNotFound(session_id)
        return row["repo_id"]

    def _prep(self, session_id: str, exchange_id: str) -> Preparation | None:
        if not exchange_id:
            return None
        prep = self.preps.get(exchange_id)
        if prep is None or prep.session_id != session_id:
            return None
        return prep


def _embed(provider: EmbeddingProvider | None, text: str) -> list[float] | None:
    if provider is None:
        return None
    return provider.get_embedding(text)


def _candidate_names(candidate: CandidateIn) -> set[str]:
    names = {normalize_name(entity.name) for entity in candidate.entities if entity.name.strip()}
    for name, _kind in infer_entities(f"{candidate.title}\n{candidate.body}"):
        names.add(normalize_name(name))
    names.discard("")
    return names


def _lock_grant(conn: sqlite3.Connection, session_id: str, mode: str) -> str | None:
    if mode == "session_rejected":
        raise ConsentDenied("reject_session is in effect; this session cannot store memory")
    if mode == "session_all":
        return None
    row = conn.execute(
        """
        SELECT id FROM consent_grants
        WHERE session_id = ? AND decision = 'approve_run' AND consumed = 0
        ORDER BY created_at ASC
        LIMIT 1
        """,
        (session_id,),
    ).fetchone()
    if row is None:
        raise ConsentDenied(
            "storage is blocked until approve_run or approve_session is granted"
        )
    return row["id"]


def _consent_view(conn: sqlite3.Connection, session_id: str) -> dict:
    session = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if session is None:
        raise SessionNotFound(session_id)
    pending = conn.execute(
        """
        SELECT COUNT(*) AS n FROM consent_grants
        WHERE session_id = ? AND decision = 'approve_run' AND consumed = 0
        """,
        (session_id,),
    ).fetchone()["n"]
    mode = session["mode"]
    rejected = mode == "session_rejected"
    may_store = (mode == "session_all" or pending > 0) and not rejected
    return {
        "mode": mode,
        "may_store": may_store,
        "ask_prompt": (not rejected) and bool(session["ask_prompt"]),
        "unconsumed_grants": int(pending),
    }


def _stats(conn: sqlite3.Connection, repo_id: str) -> dict:
    def count(sql: str, params: tuple) -> int:
        return int(conn.execute(sql, params).fetchone()[0])

    return {
        "exchanges": count(
            "SELECT COUNT(*) FROM exchanges WHERE session_id IN (SELECT id FROM sessions WHERE repo_id = ?)",
            (repo_id,),
        ),
        "facts_active": count(
            "SELECT COUNT(*) FROM facts WHERE repo_id = ? AND status NOT IN ('rejected', 'superseded')",
            (repo_id,),
        ),
        "facts_superseded": count(
            "SELECT COUNT(*) FROM facts WHERE repo_id = ? AND status = 'superseded'",
            (repo_id,),
        ),
        "facts_total": count("SELECT COUNT(*) FROM facts WHERE repo_id = ?", (repo_id,)),
        "edges": count("SELECT COUNT(*) FROM edges WHERE repo_id = ?", (repo_id,)),
        "entities": count("SELECT COUNT(*) FROM entities WHERE repo_id = ?", (repo_id,)),
        "conflicts_open": count(
            "SELECT COUNT(*) FROM conflicts WHERE repo_id = ? AND status = 'open'",
            (repo_id,),
        ),
    }


def _empty_stats() -> dict:
    return {
        "exchanges": 0,
        "facts_active": 0,
        "facts_superseded": 0,
        "facts_total": 0,
        "edges": 0,
        "entities": 0,
        "conflicts_open": 0,
    }


def _load_facts(conn: sqlite3.Connection, repo_id: str) -> list[FactRecord]:
    rows = conn.execute("SELECT * FROM facts WHERE repo_id = ?", (repo_id,)).fetchall()
    embeddings = {
        row["fact_id"]: _unpack(row["vector"])
        for row in conn.execute(
            """
            SELECT fe.fact_id, fe.vector
            FROM fact_embeddings fe
            JOIN facts f ON f.id = fe.fact_id
            WHERE f.repo_id = ?
            """,
            (repo_id,),
        )
    }
    linked: dict[str, list[tuple[str, str]]] = {}
    for row in conn.execute(
        """
        SELECT fe.fact_id, e.name, e.entity_type
        FROM fact_entities fe
        JOIN entities e ON e.id = fe.entity_id
        WHERE e.repo_id = ?
        """,
        (repo_id,),
    ):
        linked.setdefault(row["fact_id"], []).append((row["name"], row["entity_type"]))
    facts = []
    for row in rows:
        facts.append(
            FactRecord(
                id=row["id"],
                repo_id=row["repo_id"],
                session_id=row["session_id"],
                exchange_id=row["exchange_id"],
                title=row["title"],
                body=row["body"],
                fact_type=row["fact_type"],
                status=row["status"],
                confidence=float(row["confidence"]),
                disputed=bool(row["disputed"]),
                promoted=bool(row["promoted"]),
                superseded_by=row["superseded_by"],
                entities=linked.get(row["id"], []),
                embedding=embeddings.get(row["id"]),
            )
        )
    return facts


def _lexical_scores(
    conn: sqlite3.Connection,
    text: str,
    facts: list[FactRecord],
) -> dict[str, float]:
    query = fts_query(text)
    if not query:
        return {}
    allowed = {fact.id for fact in facts}
    try:
        rows = conn.execute(
            "SELECT fact_id, bm25(facts_fts) AS score FROM facts_fts WHERE facts_fts MATCH ?",
            (query,),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    raw = {
        row["fact_id"]: float(row["score"])
        for row in rows
        if row["fact_id"] in allowed
    }
    return normalize_bm25(raw)


def _fact_exists(conn: sqlite3.Connection, fact_id: str, repo_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM facts WHERE id = ? AND repo_id = ?",
        (fact_id, repo_id),
    ).fetchone()
    return row is not None


def _best_id(matches: list[dict]) -> str | None:
    if not matches:
        return None
    return matches[0]["fact_id"]


def _insert_fact(
    conn: sqlite3.Connection,
    repo_id: str,
    session_id: str,
    exchange_id: str,
    placement: PlacementIn,
    vector: list[float] | None,
    provider_name_value: str,
    stamp: str,
    *,
    disputed: bool,
    status: str,
) -> str:
    fact_id = _new_id("fact")
    promoted = 0 if status in ("draft", "proposed") else 1
    conn.execute(
        """
        INSERT INTO facts(
            id, repo_id, session_id, exchange_id, title, body, fact_type, status,
            confidence, disputed, promoted, superseded_by, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
        """,
        (
            fact_id,
            repo_id,
            session_id,
            exchange_id,
            placement.title.strip(),
            placement.body,
            placement.fact_type or "concept",
            status,
            clamp_confidence(placement.confidence),
            1 if disputed else 0,
            promoted,
            stamp,
            stamp,
        ),
    )
    if vector:
        conn.execute(
            """
            INSERT INTO fact_embeddings(fact_id, dim, vector, provider)
            VALUES (?, ?, ?, ?)
            """,
            (fact_id, len(vector), _pack(vector), provider_name_value),
        )
    conn.execute(
        "INSERT INTO facts_fts(title, body, fact_id) VALUES (?, ?, ?)",
        (placement.title.strip(), placement.body, fact_id),
    )
    _attach_entities(conn, repo_id, fact_id, placement)
    return fact_id


def _attach_entities(
    conn: sqlite3.Connection,
    repo_id: str,
    fact_id: str,
    placement: PlacementIn,
) -> None:
    seen: set[tuple[str, str]] = set()
    items = [(entity.name, entity.entity_type or "concept") for entity in placement.entities]
    items.extend(infer_entities(f"{placement.title}\n{placement.body}"))
    for name, entity_type in items:
        if not name or not str(name).strip():
            continue
        normalized = normalize_name(name)
        if not normalized:
            continue
        key = (normalized, entity_type)
        if key in seen:
            continue
        seen.add(key)
        if len(seen) > 16:
            break
        entity_id = "ent_" + hashlib.sha256(
            f"{repo_id}|{normalized}|{entity_type}".encode("utf-8")
        ).hexdigest()[:16]
        conn.execute(
            """
            INSERT OR IGNORE INTO entities(id, repo_id, name, normalized_name, entity_type)
            VALUES (?, ?, ?, ?, ?)
            """,
            (entity_id, repo_id, name.strip(), normalized, entity_type),
        )
        conn.execute(
            "INSERT OR IGNORE INTO fact_entities(fact_id, entity_id) VALUES (?, ?)",
            (fact_id, entity_id),
        )


def _merge_or_update(
    conn: sqlite3.Connection,
    embedder: EmbeddingProvider | None,
    provider_name_value: str,
    target: str,
    placement: PlacementIn,
    action: str,
    stamp: str,
) -> dict:
    current = conn.execute("SELECT title, body, repo_id FROM facts WHERE id = ?", (target,)).fetchone()
    title = placement.title.strip() or current["title"]
    body = current["body"] or ""
    if action == "merge" and placement.body and placement.body not in body:
        body = (body.rstrip() + "\n\n" + placement.body).strip()
    elif action == "update":
        body = placement.body
    conn.execute(
        "UPDATE facts SET title = ?, body = ?, updated_at = ? WHERE id = ?",
        (title, body, stamp, target),
    )
    conn.execute("DELETE FROM facts_fts WHERE fact_id = ?", (target,))
    conn.execute(
        "INSERT INTO facts_fts(title, body, fact_id) VALUES (?, ?, ?)",
        (title, body, target),
    )
    vector = _embed(embedder, f"{title}\n{body}")
    if vector:
        conn.execute(
            """
            INSERT INTO fact_embeddings(fact_id, dim, vector, provider)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(fact_id) DO UPDATE SET
                dim = excluded.dim,
                vector = excluded.vector,
                provider = excluded.provider
            """,
            (target, len(vector), _pack(vector), provider_name_value),
        )
    _attach_entities(conn, current["repo_id"], target, placement)
    return {
        "id": target,
        "title": title,
        "action": action,
        "status": "updated",
        "created": False,
    }


def _insert_edge(
    conn: sqlite3.Connection,
    repo_id: str,
    src: str,
    dst: str,
    edge_type: str,
    weight: float,
    stamp: str,
) -> int:
    if not src or not dst or src == dst:
        return 0
    found = conn.execute(
        """
        SELECT 1 FROM edges
        WHERE edge_type = ?
          AND (
            (src_fact_id = ? AND dst_fact_id = ?)
            OR (src_fact_id = ? AND dst_fact_id = ?)
          )
        """,
        (edge_type, src, dst, dst, src),
    ).fetchone()
    if found:
        return 0
    try:
        conn.execute(
            """
            INSERT INTO edges(id, repo_id, src_fact_id, dst_fact_id, edge_type, weight, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (_new_id("edge"), repo_id, src, dst, edge_type, float(weight), stamp),
        )
    except sqlite3.IntegrityError:
        return 0
    return 1


def _link_shared_entities(
    conn: sqlite3.Connection,
    repo_id: str,
    fact_id: str,
    stamp: str,
) -> int:
    rows = conn.execute(
        """
        SELECT DISTINCT fe2.fact_id AS other_id
        FROM fact_entities fe
        JOIN fact_entities fe2 ON fe2.entity_id = fe.entity_id
        WHERE fe.fact_id = ? AND fe2.fact_id != ?
        LIMIT 32
        """,
        (fact_id, fact_id),
    ).fetchall()
    created = 0
    for row in rows:
        created += _insert_edge(
            conn,
            repo_id,
            fact_id,
            row["other_id"],
            "mentions",
            SHARED_ENTITY_WEIGHT,
            stamp,
        )
    return created


def _open_conflict(
    conn: sqlite3.Connection,
    repo_id: str,
    session_id: str,
    conflict_type: str,
    new_fact_id: str,
    existing_fact_id: str,
    stamp: str,
) -> str:
    existing = conn.execute(
        """
        SELECT id FROM conflicts
        WHERE new_fact_id = ? AND existing_fact_id = ? AND status = 'open'
        """,
        (new_fact_id, existing_fact_id),
    ).fetchone()
    if existing is not None:
        return existing["id"]
    conflict_id = _new_id("conf")
    conn.execute(
        """
        INSERT INTO conflicts(
            id, repo_id, session_id, conflict_type, new_fact_id, existing_fact_id,
            status, resolution, created_at, resolved_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'open', NULL, ?, NULL)
        """,
        (conflict_id, repo_id, session_id, conflict_type, new_fact_id, existing_fact_id, stamp),
    )
    return conflict_id


def _memory_for_db(db: Database) -> Memory:
    embedding_status()
    return Memory(db, REGISTRY.embedder, REGISTRY.preps, REGISTRY.provs)


def _memory_for_session(session_id: str) -> Memory:
    _ensure_env_db()
    with REGISTRY.lock:
        databases = list(REGISTRY.dbs.values())
    last_error: Exception | None = None
    for db in databases:
        try:
            with db.read() as conn:
                row = conn.execute(
                    "SELECT 1 FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
            if row is not None:
                return _memory_for_db(db)
        except DatabaseMissing:
            continue
        except (DatabaseCorrupt, DatabaseBusy) as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise SessionNotFound(session_id)


def _coerce_candidates(candidates: list) -> list[CandidateIn]:
    return [item if isinstance(item, CandidateIn) else CandidateIn.model_validate(item) for item in candidates or []]


def _coerce_messages(messages: list) -> list[MessageIn]:
    return [item if isinstance(item, MessageIn) else MessageIn.model_validate(item) for item in messages or []]


def _coerce_placements(placements: list) -> list[PlacementIn]:
    return [item if isinstance(item, PlacementIn) else PlacementIn.model_validate(item) for item in placements or []]


def get_session(path: str) -> dict:
    """Start or resume a session. Database and embedding status are independent."""
    embeddings = embedding_status()
    base = {
        "ok": False,
        "session_id": None,
        "repo_id": None,
        "repo_root": None,
        "identity_method": None,
        "resumed": False,
        "database": {"ok": False, "path": None, "schema_version": SCHEMA_VERSION},
        "embeddings": embeddings,
        "stats": _empty_stats(),
        "repo_map": [],
        "consent": {"mode": "default", "may_store": False, "ask_prompt": True, "unconsumed_grants": 0},
        "auto_inject": {
            "mode": auto_inject_mode(),
            "text": "",
            "tokens": 0,
            "degraded": False,
            "reason": None,
        },
        "vocabulary": vocabulary(),
    }
    try:
        identity = resolve_repository(path)
    except Exception as exc:
        base["database"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "path": None}
        return base
    db_path = database_path(identity.root)
    base["repo_id"] = identity.repo_id
    base["repo_root"] = str(identity.root)
    base["identity_method"] = identity.method
    base["database"] = {"ok": False, "path": str(db_path), "schema_version": SCHEMA_VERSION}
    try:
        memory = _memory_for_db(_database(db_path))
        session = memory.open_session(identity)
        stats = memory.stats_for(session["repo_id"])
        repo_map = memory.repo_map(session["repo_id"])
        consent = memory.consent_view(session["id"])
        auto = {
            "mode": auto_inject_mode(),
            "text": "",
            "tokens": 0,
            "degraded": False,
            "reason": None,
        }
        if stats["facts_total"]:
            try:
                text = memory.search(
                    session["id"],
                    f"{identity.root.name} architecture decisions constraints failures",
                    None,
                    8,
                )
                auto["text"] = text
                auto["tokens"] = estimate_tokens(text)
                auto["degraded"] = "<color/preview>" in text
                if auto["degraded"]:
                    auto["reason"] = "preview"
            except Exception as exc:
                auto["degraded"] = True
                auto["reason"] = f"{type(exc).__name__}: {exc}"
        base.update(
            {
                "ok": True,
                "session_id": session["id"],
                "resumed": session["resumed"],
                "database": {
                    "ok": True,
                    "path": str(db_path),
                    "schema_version": SCHEMA_VERSION,
                    "exists": True,
                },
                "stats": stats,
                "repo_map": repo_map,
                "consent": consent,
                "auto_inject": auto,
            }
        )
        return base
    except (DatabaseCorrupt, DatabaseBusy, DatabaseMissing) as exc:
        base["database"] = {
            "ok": False,
            "path": str(db_path),
            "schema_version": SCHEMA_VERSION,
            "error": f"{type(exc).__name__}: {exc}",
        }
        base["error"] = type(exc).__name__
        base["message"] = str(exc)
        return base
    except Exception as exc:
        base["database"] = {
            "ok": False,
            "path": str(db_path),
            "schema_version": SCHEMA_VERSION,
            "error": f"{type(exc).__name__}: {exc}",
        }
        base["error"] = "internal"
        base["message"] = str(exc)
        return base


def prepare_placements(session_id: str, candidates: list) -> dict:
    return _memory_for_session(session_id).prepare(session_id, _coerce_candidates(candidates))


def commit_placements(session_id: str, exchange_id: str, placements: list) -> dict:
    raw = [item if isinstance(item, dict) else PlacementIn.model_validate(item).model_dump() for item in placements or []]
    return _memory_for_session(session_id).stage_placements(session_id, exchange_id, raw)


def record_provenance(session_id: str, exchange_id: str, messages: list) -> dict:
    raw = [item if isinstance(item, dict) else MessageIn.model_validate(item).model_dump() for item in messages or []]
    return _memory_for_session(session_id).stage_messages(session_id, exchange_id, raw)


def commit_step(session_id: str, exchange_id: str, messages: list, placements: list) -> dict:
    return _memory_for_session(session_id).commit(
        session_id,
        exchange_id,
        _coerce_messages(messages),
        _coerce_placements(placements),
    )


def cancel_step(session_id: str, exchange_id: str) -> dict:
    try:
        memory = _memory_for_session(session_id)
    except SessionNotFound:
        return {"ok": True, "cancelled": False}
    return memory.cancel(session_id, exchange_id)


def approve_consent(session_id: str, grant: str) -> dict:
    return _memory_for_session(session_id).record_consent(session_id, grant)


def search_context(session_id: str, query: str, entity_names: list[str] | None = None, limit: int = 8) -> str:
    try:
        return _memory_for_session(session_id).search(session_id, query, entity_names, limit)
    except SessionNotFound:
        return "engineering memory unavailable: session not found\n"
    except (DatabaseCorrupt, DatabaseBusy) as exc:
        return f"engineering memory unavailable: {exc}\n"


def discard_proposal(session_id: str) -> dict:
    try:
        return _memory_for_session(session_id).discard(session_id)
    except SessionNotFound:
        return {"ok": True, "discarded": 0, "stored": False}


def list_conflicts(session_id: str) -> dict:
    return _memory_for_session(session_id).list_conflicts(session_id)


def resolve_conflict(session_id: str, resolution: str, conflicts: list[str]) -> dict:
    if isinstance(conflicts, str):
        conflict_ids = [conflicts]
    else:
        conflict_ids = [str(item) for item in (conflicts or [])]
    return _memory_for_session(session_id).resolve(session_id, resolution, conflict_ids)


def stats(session_id: str) -> dict:
    memory = _memory_for_session(session_id)
    with memory.db.read() as conn:
        row = conn.execute(
            "SELECT repo_id FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    if row is None:
        raise SessionNotFound(session_id)
    return {"ok": True, "repo_id": row["repo_id"], "stats": memory.stats_for(row["repo_id"])}


def preview_repository(root: Path, query: str | None = None) -> str:
    identity = resolve_repository(root)
    path = database_path(identity.root)
    if not path.exists():
        return ""
    try:
        status = embedding_status()
        embedder = REGISTRY.embedder if status.get("ok") else None
        memory = Memory(Database(path), embedder, TtlStaged(), TtlStaged())
        with memory.db.read() as conn:
            row = conn.execute(
                "SELECT id FROM sessions WHERE repo_id = ? ORDER BY updated_at DESC LIMIT 1",
                (identity.repo_id,),
            ).fetchone()
            if row is None:
                return ""
            session_id = row["id"]
        text = memory.search(
            session_id,
            query or f"{identity.root.name} architecture decisions constraints failures",
            None,
            8,
        )
        if text.strip():
            return text
        return memory.recent_text(session_id)
    except (DatabaseMissing, DatabaseCorrupt, DatabaseBusy, SessionNotFound):
        return ""


def status_for_root(root: Path) -> dict | None:
    identity = resolve_repository(root)
    path = database_path(identity.root)
    if not path.exists():
        return None
    try:
        db = Database(path)
        with db.read() as conn:
            row = conn.execute(
                "SELECT id FROM repositories WHERE id = ?",
                (identity.repo_id,),
            ).fetchone()
            if row is None:
                return None
            return _stats(conn, identity.repo_id)
    except (DatabaseMissing, DatabaseCorrupt, DatabaseBusy):
        return None


def clear_root(root: Path, yes: bool) -> dict | None:
    identity = resolve_repository(root)
    path = database_path(identity.root)
    if not path.exists():
        return None
    memory = Memory(Database(path), None, TtlStaged(), TtlStaged())
    try:
        counts = memory.counts(identity.repo_id)
    except (DatabaseMissing, DatabaseCorrupt, DatabaseBusy):
        return None
    if not yes:
        return {"dry_run": True, **counts}
    memory.wipe(identity.repo_id)
    return {"dry_run": False, **counts}
