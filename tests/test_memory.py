from eng_graph.state import (
    approve_consent,
    commit_step,
    discard_proposal,
    get_session,
    list_conflicts,
    prepare_placements,
    resolve_conflict,
    search_context,
    stats,
)


def _project(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='repo'\n", encoding="utf-8")
    return root


def _session(root):
    opened = get_session(str(root))
    assert opened["ok"] is True
    assert opened["database"]["ok"] is True
    assert opened["embeddings"]["ok"] is True
    assert opened["embeddings"]["provider"] == "testing"
    return opened["session_id"]


def _fact(title, body, **extra):
    payload = {
        "title": title,
        "body": body,
        "fact_type": "decision",
        "confidence": 0.9,
        "action": "create",
        "entities": [{"name": "auth.py", "entity_type": "file"}],
    }
    payload.update(extra)
    return payload


def test_consent_blocks_then_one_grant_then_retry(tmp_path):
    root = _project(tmp_path)
    session_id = _session(root)
    placement = _fact("Use JWT", "Access tokens expire after fifteen minutes.")
    denied = commit_step(session_id, "exch_denied", [], [placement])
    assert denied["ok"] is False
    assert denied["error"] == "consent_required"
    assert stats(session_id)["stats"]["facts_total"] == 0

    grant = approve_consent(session_id, "approve_run")
    assert grant["authorizes_storage"] is True
    assert grant["consent"]["may_store"] is True

    missing = commit_step(
        session_id,
        "exch_bad",
        [],
        [_fact("Replace JWT", "Switch to sessions.", action="supersede", confidence=0.95, supersede_existing_fact_id="fact_missing")],
    )
    assert missing["ok"] is False
    assert get_session(str(root))["consent"]["unconsumed_grants"] == 1

    stored = commit_step(session_id, "exch_ok", [{"role": "user", "content": "ship jwt"}], [placement])
    assert stored["ok"] is True
    assert stored["facts"][0]["title"] == "Use JWT"
    assert "fifteen minutes" not in str(stored)
    again = commit_step(session_id, "exch_again", [], [placement])
    assert again["ok"] is False
    assert again["error"] == "consent_required"
    assert stats(session_id)["stats"]["facts_total"] == 1

    replay = commit_step(session_id, "exch_ok", [], [placement])
    assert replay["already_committed"] is True


def test_reject_never_authorizes_and_reject_session_disables_prompts(tmp_path):
    root = _project(tmp_path)
    session_id = _session(root)
    audit = approve_consent(session_id, "reject")
    assert audit["authorizes_storage"] is False
    denied = commit_step(session_id, "exch_r", [], [_fact("Nope", "Do not store")])
    assert denied["error"] == "consent_required"
    closed = approve_consent(session_id, "reject_session")
    assert closed["consent"]["ask_prompt"] is False
    assert closed["consent"]["may_store"] is False
    blocked = commit_step(session_id, "exch_b", [], [_fact("Still no", "Blocked")])
    assert blocked["ok"] is False
    assert stats(session_id)["stats"]["facts_total"] == 0


def test_duplicate_preview_and_low_confidence_supersede_conflict(tmp_path):
    root = _project(tmp_path)
    session_id = _session(root)
    approve_consent(session_id, "approve_session")
    original = _fact("Use JWT", "Access tokens expire after fifteen minutes.")
    first = commit_step(session_id, "exch_1", [], [original])
    assert first["ok"] is True
    prepared = prepare_placements(session_id, [original])
    assert prepared["placements"][0]["classification"] == "likely_duplicate"

    low = _fact(
        "Use server sessions",
        "JWT is replaced by server sessions.",
        action="supersede",
        confidence=0.4,
        supersede_existing_fact_id=first["facts"][0]["id"],
    )
    second = commit_step(session_id, "exch_2", [], [low])
    assert second["ok"] is True
    assert second["conflicts_opened"]
    counts = stats(session_id)["stats"]
    assert counts["facts_active"] == 2
    assert counts["facts_superseded"] == 0
    assert counts["conflicts_open"] == 1
    listed = list_conflicts(session_id)["conflicts"]
    resolved = resolve_conflict(session_id, "new", [listed[0]["id"]])
    assert resolved["ok"] is True
    after = stats(session_id)["stats"]
    assert after["facts_superseded"] == 1
    assert after["facts_active"] == 1
    assert after["conflicts_open"] == 0
    recalled = search_context(session_id, "JWT server sessions", None, 8)
    assert "Use server sessions" in recalled
    assert "JWT is replaced by server sessions." in recalled


def test_high_confidence_supersede_keeps_history(tmp_path):
    root = _project(tmp_path)
    session_id = _session(root)
    approve_consent(session_id, "approve_session")
    old = commit_step(session_id, "exch_old", [], [_fact("Use JWT", "Tokens live for a day.")])
    new = commit_step(
        session_id,
        "exch_new",
        [],
        [
            _fact(
                "Use short JWT",
                "Tokens live for fifteen minutes.",
                action="supersede",
                confidence=0.92,
                supersede_existing_fact_id=old["facts"][0]["id"],
            )
        ],
    )
    assert new["facts"][0]["action"] == "supersede"
    counts = stats(session_id)["stats"]
    assert counts["facts_total"] == 2
    assert counts["facts_superseded"] == 1
    assert counts["conflicts_open"] == 0


def test_discard_leaves_no_facts(tmp_path):
    root = _project(tmp_path)
    session_id = _session(root)
    prepared = prepare_placements(session_id, [_fact("Draft only", "This must not be stored.")])
    assert prepared["prep_id"].startswith("prep_")
    dropped = discard_proposal(session_id)
    assert dropped["discarded"] >= 1
    assert stats(session_id)["stats"]["facts_total"] == 0


def test_search_ignores_caller_fact_list_and_caps_bodies(tmp_path, monkeypatch):
    import inspect

    from eng_graph.__main__ import ecg_search_context

    assert "fact_ids" not in inspect.signature(ecg_search_context).parameters
    root = _project(tmp_path)
    session_id = _session(root)
    approve_consent(session_id, "approve_run")
    commit_step(
        session_id,
        "exch_cap",
        [],
        [_fact("Tiny title", "zzbodytoken " + ("pad " * 800))],
    )
    monkeypatch.setenv("ECG_AUTO_INJECT_MAX_TOKENS", "40")
    preview = search_context(session_id, "Tiny title", None, 4)
    assert "Tiny title" in preview
    assert "zzbodytoken" not in preview
    assert "<color/preview>" in preview
