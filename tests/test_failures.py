import os
import subprocess
import sys
import time
from pathlib import Path

from eng_graph.state import get_session

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def _env(tmp_path: Path) -> dict:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC)
    env["ECG_EMBEDDING_PROVIDER"] = "testing"
    env.pop("ECG_DB_PATH", None)
    return env


def test_corrupt_database_returns_quickly(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    db = root / ".convo" / "memory.db"
    db.parent.mkdir()
    db.write_bytes(b"this is not a sqlite database" * 20)
    started = time.monotonic()
    opened = get_session(str(root))
    elapsed = time.monotonic() - started
    assert elapsed < 5
    assert opened["ok"] is False
    assert opened["database"]["ok"] is False
    assert opened["embeddings"]["ok"] is True
    assert opened["embeddings"]["provider"] == "testing"


def test_locked_database_does_not_hang(tmp_path):
    import sqlite3

    root = tmp_path / "repo"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    first = get_session(str(root))
    assert first["database"]["ok"] is True
    db_path = first["database"]["path"]
    holder = sqlite3.connect(db_path, timeout=0.2)
    holder.execute("BEGIN EXCLUSIVE")
    started = time.monotonic()
    try:
        opened = get_session(str(root))
    finally:
        holder.rollback()
        holder.close()
    assert time.monotonic() - started < 6
    assert opened["database"]["ok"] is False


def test_cli_bad_args_missing_dir_and_corrupt_db_exit_zero(tmp_path):
    env = _env(tmp_path)
    bad = subprocess.run(
        [sys.executable, "-m", "eng_graph", "--not-a-real-flag", "status"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert bad.returncode == 0

    missing = subprocess.run(
        [sys.executable, "-m", "eng_graph", "--repo", str(tmp_path / "nope"), "status"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert missing.returncode == 0
    assert missing.stdout == ""

    root = tmp_path / "repo"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    db = root / ".convo" / "memory.db"
    db.parent.mkdir()
    db.write_bytes(b"garbage-database")
    corrupt = subprocess.run(
        [sys.executable, "-m", "eng_graph", "--repo", str(root), "status", "-v"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert corrupt.returncode == 0


def test_garbage_on_stdio_does_not_hang(tmp_path):
    env = _env(tmp_path)
    env["ECG_DB_PATH"] = str(tmp_path / "memory.db")
    proc = subprocess.Popen(
        [sys.executable, "-c", "from eng_graph.__main__ import main; main()"],
        cwd=ROOT,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdin is not None
    proc.stdin.write(b"this is not json-rpc\n{]\n")
    proc.stdin.close()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
        raise AssertionError("MCP server hung after garbage on stdio")


def test_no_subprocess_in_server_package():
    banned = ("subprocess", "os.system", "os.popen", "Popen(")
    for path in (SRC / "eng_graph").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for token in banned:
            assert token not in text, f"{path.name} contains {token}"


def test_sentence_provider_fails_fast_when_unavailable(monkeypatch):
    monkeypatch.setenv("ECG_EMBEDDING_PROVIDER", "sentence-transformers")
    from eng_graph.state import embedding_status, reset_registry

    reset_registry()
    started = time.monotonic()
    info = embedding_status()
    assert time.monotonic() - started < 5
    if info["ok"]:
        assert info["provider"] == "sentence-transformers"
    else:
        assert "provider" in info
    reset_registry()
