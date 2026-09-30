"""Placement, recall ranking, neighborhood expansion, and rendering.

No tokenizer. Placement actions are a small keyword heuristic plus the scores
below. Similarity is a blend of cosine (50%), normalized FTS BM25 (20%), and
entity-name overlap (30%). When either side has no entities, the entity share
is folded back into cosine and lexical at the same 50:20 ratio so a text-only
duplicate can still clear 0.85.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict, deque

from eng_graph.embeddings import cosine_similarity
from eng_graph.models import (
    AUTO_SUPERSEDE_CONFIDENCE,
    COCOMMIT_WEIGHT,
    DISPUTED_DEMOTION,
    DUPLICATE_SIM,
    MAX_DEPTH,
    MIN_EDGE_WEIGHT,
    RELATED_SIM,
    SCORE_FLOOR,
    SUPERSEDED_DEMOTION,
    SUPERSEDE_SIM,
    UNPROMOTED_DEMOTION,
    UNPROMOTED_STATUSES,
    WEIGHT_COSINE,
    WEIGHT_ENTITY,
    WEIGHT_LEXICAL,
    Cluster,
    FactRecord,
    ScoredFact,
)

_PATH = re.compile(r"\b[\w.-]+(?:/[\w.-]+)+\b")
_SNAKE = re.compile(r"\b[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+\b")
_CAMEL = re.compile(r"\b[A-Z][a-z]+(?:[A-Z][a-z0-9]+)+\b")
_DOTTED = re.compile(r"\b[A-Za-z_][\w]*\.[\w.]+\b")
_TICK = re.compile(r"`([^`]{1,80})`")
_FTS_WORD = re.compile(r"[A-Za-z0-9_]+")

_STOP = frozenset(
    """
    the and for with this that from into using use not are was were have has
    had but you your our their about when then than just only also can will
    should would could file code data true false none null how what why who
    """.split()
)


def clamp01(value: float) -> float:
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def clamp_confidence(value: object) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    if number != number:
        return 0.0
    return clamp01(number)


def normalize_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


def fts_query(text: str) -> str:
    blocked = _STOP | {"and", "or", "not", "near"}
    words = []
    for token in _FTS_WORD.findall(text.lower()):
        if len(token) < 2 or token in blocked:
            continue
        words.append(token)
        if len(words) >= 24:
            break
    return " OR ".join(words)


def normalize_bm25(scores: dict[str, float]) -> dict[str, float]:
    """Map SQLite BM25 (more negative is better) onto 0..1."""
    if not scores:
        return {}
    values = list(scores.values())
    best = min(values)
    worst = max(values)
    if abs(best - worst) < 1e-9:
        return {key: 1.0 for key in scores}
    span = worst - best
    return {key: (worst - value) / span for key, value in scores.items()}


def jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


def blend(cosine: float, lexical: float, entity: float, entity_signal: bool) -> float:
    cosine = clamp01(cosine)
    lexical = clamp01(lexical)
    entity = clamp01(entity)
    if entity_signal:
        return (
            WEIGHT_COSINE * cosine
            + WEIGHT_LEXICAL * lexical
            + WEIGHT_ENTITY * entity
        )
    scale = WEIGHT_COSINE + WEIGHT_LEXICAL
    return (WEIGHT_COSINE / scale) * cosine + (WEIGHT_LEXICAL / scale) * lexical


def infer_entities(text: str) -> list[tuple[str, str]]:
    """Pull path and identifier shapes out of text. Not a general tokenizer."""
    found: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(name: str, entity_type: str) -> None:
        cleaned = name.strip().strip("`").strip()
        if len(cleaned) < 2 or cleaned.lower() in _STOP:
            return
        key = (normalize_name(cleaned), entity_type)
        if key in seen or not key[0]:
            return
        seen.add(key)
        found.append((cleaned, entity_type))

    for match in _TICK.findall(text or ""):
        kind = "file" if "/" in match else "symbol"
        add(match, kind)
    for match in _PATH.findall(text or ""):
        add(match, "file")
    for match in _DOTTED.findall(text or ""):
        add(match, "symbol")
    for match in _SNAKE.findall(text or ""):
        add(match, "symbol")
    for match in _CAMEL.findall(text or ""):
        add(match, "symbol")
    return found


def entity_names_for_query(query: str, explicit: list[str] | None) -> set[str]:
    names = {normalize_name(name) for name in (explicit or []) if name.strip()}
    for name, _kind in infer_entities(query):
        names.add(normalize_name(name))
    names.discard("")
    return names


def levenshtein(left: str, right: str) -> int:
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for i, ca in enumerate(left, 1):
        current = [i]
        for j, cb in enumerate(right, 1):
            insert = current[j - 1] + 1
            delete = previous[j] + 1
            substitute = previous[j - 1] + (ca != cb)
            current.append(min(insert, delete, substitute))
        previous = current
    return previous[-1]


def jaro_winkler(left: str, right: str, prefix_scale: float = 0.1) -> float:
    if left == right:
        return 1.0
    jaro = _jaro(left, right)
    prefix = 0
    for a, b in zip(left, right):
        if a != b or prefix == 4:
            break
        prefix += 1
    return jaro + prefix * prefix_scale * (1.0 - jaro)


def _jaro(left: str, right: str) -> float:
    len_left, len_right = len(left), len(right)
    if len_left == 0 or len_right == 0:
        return 0.0
    match_distance = max(len_left, len_right) // 2 - 1
    if match_distance < 0:
        match_distance = 0
    left_matches = [False] * len_left
    right_matches = [False] * len_right
    matches = 0
    for i, ch in enumerate(left):
        start = max(0, i - match_distance)
        end = min(i + match_distance + 1, len_right)
        for j in range(start, end):
            if right_matches[j] or right[j] != ch:
                continue
            left_matches[i] = True
            right_matches[j] = True
            matches += 1
            break
    if matches == 0:
        return 0.0
    k = 0
    transpositions = 0.0
    for i, ch in enumerate(left):
        if not left_matches[i]:
            continue
        while not right_matches[k]:
            k += 1
        if ch != right[k]:
            transpositions += 1
        k += 1
    transpositions /= 2.0
    return (
        matches / len_left
        + matches / len_right
        + (matches - transpositions) / matches
    ) / 3.0


def typo_bonus(query: str, title: str) -> float:
    left = query.strip().lower()[:80]
    right = title.strip().lower()[:80]
    if not left or not right:
        return 0.0
    score = jaro_winkler(left, right)
    bonus = 0.0
    if score >= 0.92:
        bonus = 0.08
    elif score >= 0.84:
        bonus = 0.04
    distance = levenshtein(left, right)
    limit = max(2, min(len(left), len(right)) // 8)
    if distance <= limit and min(len(left), len(right)) >= 5:
        bonus = max(bonus, 0.05)
    return bonus


def classify_match(score: float, same_type: bool, status: str, action: str) -> str:
    adjusted = score
    if status == "superseded":
        adjusted -= SUPERSEDED_DEMOTION
        if adjusted >= RELATED_SIM:
            return "related"
        return "new"
    supersede = action in ("supersede", "replace")
    if supersede and adjusted >= SUPERSEDE_SIM:
        return "possible_supersede"
    if adjusted >= DUPLICATE_SIM and same_type:
        return "likely_duplicate"
    if adjusted >= RELATED_SIM:
        return "related"
    return "new"


def recommend_action(classification: str) -> str:
    if classification == "likely_duplicate":
        return "merge"
    if classification == "possible_supersede":
        return "replace"
    if classification == "related":
        return "new"
    return "create"


def index_status(confidence: float, action: str) -> str:
    """Conservative draft/proposed/approved choice. Not a tokenizer."""
    if action == "drop":
        return "rejected"
    if confidence < 0.5:
        return "draft"
    if confidence < AUTO_SUPERSEDE_CONFIDENCE:
        return "proposed"
    return "approved"


def placement_scores(
    candidate_vec: list[float] | None,
    candidate_entities: set[str],
    candidate_type: str,
    action: str,
    lexical: dict[str, float],
    facts: list[FactRecord],
) -> list[dict]:
    matches = []
    for fact in facts:
        if fact.status == "rejected":
            continue
        if candidate_vec and fact.embedding and len(candidate_vec) == len(fact.embedding):
            cosine = cosine_similarity(candidate_vec, fact.embedding)
        else:
            cosine = 0.0
        lex = lexical.get(fact.id, 0.0)
        fact_names = {normalize_name(name) for name, _kind in fact.entities}
        signal = bool(candidate_entities) and bool(fact_names)
        overlap = jaccard(candidate_entities, fact_names) if signal else 0.0
        score = blend(cosine, lex, overlap, signal)
        if fact.status == "superseded":
            score -= SUPERSEDED_DEMOTION
        kind = classify_match(score, fact.fact_type == candidate_type, fact.status, action)
        if kind == "new" and score < RELATED_SIM:
            continue
        matches.append(
            {
                "fact_id": fact.id,
                "title": fact.title,
                "fact_type": fact.fact_type,
                "status": fact.status,
                "score": round(score, 4),
                "classification": kind,
            }
        )
    matches.sort(key=lambda item: item["score"], reverse=True)
    return matches[:8]


def rank_facts(
    query: str,
    query_vec: list[float] | None,
    explicit_entities: list[str] | None,
    lexical: dict[str, float],
    facts: list[FactRecord],
) -> list[ScoredFact]:
    query_names = entity_names_for_query(query, explicit_entities)
    ranked: list[ScoredFact] = []
    for fact in facts:
        if fact.status == "rejected":
            continue
        if query_vec and fact.embedding and len(query_vec) == len(fact.embedding):
            cosine = cosine_similarity(query_vec, fact.embedding)
        else:
            cosine = 0.0
        lex = lexical.get(fact.id, 0.0)
        fact_names = {normalize_name(name) for name, _kind in fact.entities}
        signal = bool(query_names) and bool(fact_names)
        overlap = jaccard(query_names, fact_names) if signal else 0.0
        score = blend(cosine, lex, overlap, signal)
        if fact.status == "superseded":
            score -= SUPERSEDED_DEMOTION
        if (not fact.promoted) or fact.status in UNPROMOTED_STATUSES:
            score -= UNPROMOTED_DEMOTION
        if fact.disputed:
            score -= DISPUTED_DEMOTION
        score += typo_bonus(query, fact.title)
        if score > 1.0:
            score = 1.0
        if score < SCORE_FLOOR:
            continue
        ranked.append(
            ScoredFact(
                fact_id=fact.id,
                title=fact.title,
                body=fact.body,
                fact_type=fact.fact_type,
                status=fact.status,
                confidence=fact.confidence,
                disputed=fact.disputed,
                session_id=fact.session_id,
                score=score,
                entities=[name for name, _kind in fact.entities],
            )
        )
    ranked.sort(key=lambda item: item.score, reverse=True)
    return ranked


def expand_neighborhood(
    seeds: list[ScoredFact],
    facts_by_id: dict[str, FactRecord],
    edges: list[tuple[str, str, float]],
    max_depth: int = MAX_DEPTH,
    min_weight: float = MIN_EDGE_WEIGHT,
) -> tuple[dict[str, ScoredFact], list[tuple[str, str]]]:
    """Breadth-first expansion. Each hop keeps the whole fact, never a snippet."""
    included: dict[str, ScoredFact] = {fact.fact_id: fact for fact in seeds}
    traversed: list[tuple[str, str]] = []
    if not seeds:
        return included, traversed
    adjacency: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for src, dst, weight in edges:
        if weight < min_weight:
            continue
        adjacency[src].append((dst, weight))
        adjacency[dst].append((src, weight))
    seen_depth = {fact.fact_id: 0 for fact in seeds}
    queue: deque[tuple[str, int]] = deque((fact.fact_id, 0) for fact in seeds)
    while queue:
        fact_id, depth = queue.popleft()
        if depth >= max_depth:
            continue
        for other, weight in adjacency.get(fact_id, []):
            hop = depth + 1
            if other in seen_depth and seen_depth[other] <= hop:
                continue
            record = facts_by_id.get(other)
            if record is None or record.status == "rejected":
                continue
            seen_depth[other] = hop
            decayed = weight * (0.5**hop)
            if other not in included:
                included[other] = ScoredFact(
                    fact_id=record.id,
                    title=record.title,
                    body=record.body,
                    fact_type=record.fact_type,
                    status=record.status,
                    confidence=record.confidence,
                    disputed=record.disputed,
                    session_id=record.session_id,
                    score=decayed,
                    entities=[name for name, _kind in record.entities],
                )
            traversed.append((fact_id, other))
            queue.append((other, hop))
    return included, traversed


class _UnionFind:
    def __init__(self, items: list[str]) -> None:
        self.parent = {item: item for item in items}

    def find(self, item: str) -> str:
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, left: str, right: str) -> None:
        ra, rb = self.find(left), self.find(right)
        if ra != rb:
            self.parent[rb] = ra


def connected_components(
    facts: dict[str, ScoredFact],
    edges: list[tuple[str, str]],
) -> list[Cluster]:
    ids = list(facts)
    if not ids:
        return []
    union = _UnionFind(ids)
    present = set(ids)
    for src, dst in edges:
        if src in present and dst in present:
            union.union(src, dst)
    groups: dict[str, list[ScoredFact]] = defaultdict(list)
    for fact_id, fact in facts.items():
        groups[union.find(fact_id)].append(fact)
    clusters: list[Cluster] = []
    for members in groups.values():
        members.sort(key=lambda item: item.score, reverse=True)
        clusters.append(Cluster(label=_cluster_label(members), facts=members))
    clusters.sort(key=lambda cluster: cluster.facts[0].score, reverse=True)
    return clusters


def _cluster_label(members: list[ScoredFact]) -> str:
    counts: Counter[str] = Counter()
    for fact in members:
        for name in fact.entities:
            counts[name] += 1
    if counts:
        top = counts.most_common()
        best = top[0][1]
        names = sorted(name for name, count in top if count == best)
        return names[0]
    return members[0].title


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def render_memory(
    clusters: list[Cluster],
    *,
    mode: str,
    max_tokens: int,
) -> str:
    """Full bodies in ``chat`` mode, titles in ``preview`` or when over the cap."""
    if not clusters:
        return ""
    full = _render(clusters, titles_only=False, reason=None)
    if mode == "preview":
        return _render(
            clusters,
            titles_only=True,
            reason="ECG_AUTO_INJECT=preview, so only titles were loaded.",
        )
    if estimate_tokens(full) > max_tokens:
        return _render(
            clusters,
            titles_only=True,
            reason=(
                f"Estimated {estimate_tokens(full)} tokens exceeds the "
                f"{max_tokens} token cap, so bodies were omitted."
            ),
        )
    return full


def _render(clusters: list[Cluster], *, titles_only: bool, reason: str | None) -> str:
    tag = "color/preview" if titles_only else "engineering-memory"
    lines = [f"<{tag}>", "# Engineering memory" if not titles_only else "# Engineering memory preview"]
    if reason:
        lines.append(reason)
    total = sum(len(cluster.facts) for cluster in clusters)
    lines.append(f"clusters: {len(clusters)}; facts: {total}")
    for cluster in clusters:
        lines.append("")
        lines.append(f"## Cluster: {cluster.label} ({len(cluster.facts)} facts)")
        grouped: dict[str, list[ScoredFact]] = defaultdict(list)
        for fact in cluster.facts:
            grouped[fact.fact_type].append(fact)
        for fact_type in sorted(grouped):
            if titles_only:
                for fact in grouped[fact_type]:
                    flag = " disputed" if fact.disputed else ""
                    lines.append(
                        f"- {fact_type}: {fact.title} (id: {fact.fact_id}){flag}"
                    )
                continue
            lines.append(f"### {fact_type}")
            for fact in grouped[fact_type]:
                entities = ", ".join(fact.entities) if fact.entities else "none"
                lines.append(f"#### {fact.title}")
                lines.append(f"- id: {fact.fact_id}")
                lines.append(f"- status: {fact.status}")
                lines.append(f"- disputed: {'yes' if fact.disputed else 'no'}")
                lines.append(f"- confidence: {fact.confidence:.2f}")
                lines.append(f"- source_session: {fact.session_id}")
                lines.append(f"- entities: {entities}")
                lines.append("")
                lines.append(fact.body or "")
                lines.append("")
    lines.append(f"</{tag}>")
    return "\n".join(lines).rstrip() + "\n"


def should_auto_supersede(confidence: float, ambiguous: bool, has_target: bool) -> bool:
    return has_target and (not ambiguous) and confidence >= AUTO_SUPERSEDE_CONFIDENCE


def cocommit_weight() -> float:
    return COCOMMIT_WEIGHT
