import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def _payload(result) -> dict | str:
    if getattr(result, "isError", False):
        text = "\n".join(getattr(block, "text", "") for block in result.content)
        raise AssertionError(text)
    texts = [getattr(block, "text", "") for block in result.content]
    raw = "\n".join(texts).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


@pytest.mark.asyncio
async def test_stdio_commit_survives_process_restart(tmp_path):
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname='repo'\n", encoding="utf-8")
    db_path = tmp_path / "memory.db"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC)
    env["ECG_DB_PATH"] = str(db_path)
    env["ECG_EMBEDDING_PROVIDER"] = "testing"
    env["ECG_AUTO_INJECT"] = "chat"
    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", "from eng_graph.__main__ import main; main()"],
        env=env,
        cwd=str(ROOT),
    )

    async def call(session, name, arguments):
        result = await session.call_tool(name, arguments)
        return _payload(result)

    body = "RESTART_BODY tokens expire after fifteen minutes in this service."
    prep_id = None
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = {tool.name for tool in (await session.list_tools()).tools}
            assert "ecg_get_session" in tools
            assert "ecg_commit_step" in tools
            assert "stats" not in tools
            assert "login" not in tools
            opened = await call(session, "ecg_get_session", {"path": str(repo)})
            assert opened["database"]["ok"] is True
            session_id = opened["session_id"]
            consent = await call(
                session, "ecg_approve_consent", {"session_id": session_id, "grant": "approve_run"}
            )
            assert consent["authorizes_storage"] is True
            prepared = await call(
                session,
                "ecg_prepare_placements",
                {
                    "session_id": session_id,
                    "candidates": [
                        {
                            "title": "Expire access tokens",
                            "body": body,
                            "fact_type": "decision",
                            "confidence": 0.93,
                            "action": "create",
                            "entities": [{"name": "src/auth.py", "entity_type": "file"}],
                        }
                    ],
                },
            )
            prep_id = prepared["prep_id"]
            stored = await call(
                session,
                "ecg_commit_step",
                {
                    "session_id": session_id,
                    "exchange_id": prep_id,
                    "messages": [{"role": "user", "content": "remember the token lifetime"}],
                    "placements": [
                        {
                            "title": "Expire access tokens",
                            "body": body,
                            "fact_type": "decision",
                            "confidence": 0.93,
                            "action": "create",
                            "entities": [{"name": "src/auth.py", "entity_type": "file"}],
                        }
                    ],
                },
            )
            assert stored["ok"] is True

    assert prep_id is not None
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            opened = await call(session, "ecg_get_session", {"path": str(repo)})
            assert opened["stats"]["facts_total"] >= 1
            session_id = opened["session_id"]
            recalled = await call(
                session,
                "ecg_search_context",
                {"session_id": session_id, "query": "access tokens expire", "limit": 5},
            )
            assert isinstance(recalled, str)
            assert "RESTART_BODY" in recalled
            cancelled = await call(
                session,
                "ecg_cancel_step",
                {"session_id": session_id, "exchange_id": prep_id},
            )
            assert cancelled["cancelled"] is False
            still = await call(
                session,
                "ecg_search_context",
                {"session_id": session_id, "query": "access tokens expire", "limit": 5},
            )
            assert "RESTART_BODY" in still
