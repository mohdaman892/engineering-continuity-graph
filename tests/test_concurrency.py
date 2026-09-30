import threading

from eng_graph.state import approve_consent, commit_step, get_session, stats


def test_five_threads_commit_without_losing_grants(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='repo'\n", encoding="utf-8")
    opened = get_session(str(root))
    session_id = opened["session_id"]
    for _ in range(5):
        grant = approve_consent(session_id, "approve_run")
        assert grant["authorizes_storage"] is True

    barrier = threading.Barrier(5)
    results = []
    errors = []

    def worker(index: int) -> None:
        try:
            barrier.wait(timeout=5)
            result = commit_step(
                session_id,
                f"exch_thread_{index}",
                [{"role": "user", "content": f"note {index}"}],
                [
                    {
                        "title": f"Decision {index}",
                        "body": f"Thread {index} recorded a distinct constraint.",
                        "fact_type": "decision",
                        "confidence": 0.9,
                        "action": "create",
                        "entities": [{"name": f"mod_{index}.py", "entity_type": "file"}],
                    }
                ],
            )
            results.append(result)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert errors == []
    assert len(results) == 5
    assert all(item["ok"] is True for item in results)
    assert stats(session_id)["stats"]["facts_total"] == 5
    assert get_session(str(root))["consent"]["unconsumed_grants"] == 0
