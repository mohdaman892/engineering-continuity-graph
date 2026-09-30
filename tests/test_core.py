from eng_graph.core import (
    blend,
    classify_match,
    connected_components,
    expand_neighborhood,
    infer_entities,
    jaro_winkler,
    levenshtein,
    render_memory,
    typo_bonus,
)
from eng_graph.embeddings.testing import TestingProvider
from eng_graph.models import EDGE_WEIGHTS, FactRecord, ScoredFact


def test_testing_embeddings_are_deterministic_and_identical_for_same_text():
    provider = TestingProvider()
    left = provider.get_embedding("authentication middleware rejects expired tokens")
    right = provider.get_embedding("authentication middleware rejects expired tokens")
    assert left == right
    assert abs(sum(v * v for v in left) - 1.0) < 1e-6
    other = provider.get_embedding("postgres is the primary datastore")
    assert provider.distance(left, left) == 0.0
    assert provider.distance(left, other) > 0.2


def test_jaro_and_levenshtein_and_typo_bonus():
    assert jaro_winkler("authentication", "authentication") == 1.0
    assert levenshtein("kitten", "sitting") == 3
    assert typo_bonus("autentication", "authentication") > 0


def test_blend_reaches_duplicate_without_entities():
    assert blend(1.0, 1.0, 0.0, False) == 1.0
    assert abs(blend(1.0, 1.0, 1.0, True) - 1.0) < 1e-9


def test_classify_duplicate_supersede_and_demoted_history():
    assert classify_match(0.9, True, "approved", "create") == "likely_duplicate"
    assert classify_match(0.8, True, "approved", "supersede") == "possible_supersede"
    assert classify_match(0.95, True, "superseded", "create") == "related"
    assert classify_match(0.2, True, "approved", "create") == "new"


def test_infer_entities_finds_paths_and_identifiers():
    found = dict(infer_entities("Edit `src/auth/middleware.py` and AuthService_token."))
    assert "src/auth/middleware.py" in found
    assert found["src/auth/middleware.py"] == "file"


def test_expansion_and_components_keep_whole_facts():
    seeds = [
        ScoredFact("a", "Alpha", "body-a", "decision", "approved", 0.9, False, "s", 0.9, ["alpha"])
    ]
    other = FactRecord(
        "b", "repo", "s", None, "Beta", "body-b", "decision", "approved", 0.9, False, True, None, [("alpha", "concept")], None
    )
    included, edges = expand_neighborhood(
        seeds,
        {"b": other},
        [("a", "b", EDGE_WEIGHTS["depends_on"]), ("a", "c", 0.1)],
    )
    assert set(included) == {"a", "b"}
    assert included["b"].body == "body-b"
    assert edges == [("a", "b")]
    clusters = connected_components(included, edges)
    assert len(clusters) == 1
    assert clusters[0].label == "alpha"


def test_render_degrades_when_over_token_cap():
    fact = ScoredFact(
        "fact_1",
        "Use tokens",
        "BODY_SENTINEL " + ("word " * 400),
        "decision",
        "approved",
        0.9,
        True,
        "sess_1",
        0.8,
        ["auth"],
    )
    from eng_graph.models import Cluster

    text = render_memory([Cluster("auth", [fact])], mode="chat", max_tokens=30)
    assert "<color/preview>" in text
    assert "BODY_SENTINEL" not in text
    assert "Use tokens" in text
    assert "token cap" in text
    full = render_memory([Cluster("auth", [fact])], mode="chat", max_tokens=5000)
    assert "<engineering-memory>" in full
    assert "BODY_SENTINEL" in full
    assert "disputed: yes" in full
    assert "sess_1" in full
