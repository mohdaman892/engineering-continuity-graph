import json
import os
import stat
import subprocess
import sys
from pathlib import Path

from eng_graph.state import approve_consent, commit_step, get_session

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def _env() -> dict:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC)
    env["ECG_EMBEDDING_PROVIDER"] = "testing"
    env.pop("ECG_DB_PATH", None)
    return env


def test_cli_status_clear_and_preview(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='repo'\n", encoding="utf-8")
    opened = get_session(str(root))
    session_id = opened["session_id"]
    approve_consent(session_id, "approve_run")
    commit_step(
        session_id,
        "exch_cli",
        [],
        [
            {
                "title": "Prefer WAL",
                "body": "CLI_BODY the memory database uses WAL mode.",
                "fact_type": "architecture",
                "confidence": 0.9,
                "action": "create",
            }
        ],
    )
    env = _env()
    status = subprocess.run(
        [sys.executable, "-m", "eng_graph", "--repo", str(root), "status"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert status.returncode == 0
    assert "1 active" in status.stdout

    preview = subprocess.run(
        [sys.executable, "-m", "eng_graph", "--repo", str(root), "resolve_conflict"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert preview.returncode == 0
    assert "CLI_BODY" in preview.stdout
    assert "<engineering-memory>" in preview.stdout

    dry = subprocess.run(
        [sys.executable, "-m", "eng_graph", "--repo", str(root), "clear"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert dry.returncode == 0
    assert "dry run" in dry.stdout
    assert "1 active" in subprocess.run(
        [sys.executable, "-m", "eng_graph", "--repo", str(root), "status"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    ).stdout

    wiped = subprocess.run(
        [sys.executable, "-m", "eng_graph", "--repo", str(root), "clear", "--yes"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert wiped.returncode == 0
    after = subprocess.run(
        [sys.executable, "-m", "eng_graph", "--repo", str(root), "status"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert after.returncode == 0
    assert "none" in after.stdout


def test_debug_hook_exits_zero(tmp_path):
    env = _env()
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "debug_hook.py"), "--repo", str(tmp_path / "missing")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0


def test_setup_print_and_merge(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    uv = bindir / "uv"
    uv.write_text("#!/bin/sh\necho uv\n", encoding="utf-8")
    uv.chmod(uv.stat().st_mode | stat.S_IEXEC)
    home = tmp_path / "home"
    home.mkdir()
    env = os.environ.copy()
    env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
    env["HOME"] = str(home)
    printed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "setup.py"), "--print"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert printed.returncode == 0, printed.stderr
    payload = json.loads(printed.stdout)
    entry = payload["mcpServers"]["engineering-continuity-graph"]
    assert entry["command"] == "uv"
    assert str(ROOT) in entry["args"]
    assert "ecg_commit_step" not in entry["alwaysAllow"]
    assert "ecg_approve_consent" not in entry["alwaysAllow"]
    assert "commit_memory" in entry["excludeFromAlwaysAllow"]
    assert not (home / ".claude_desktop" / "claude_desktop_config.json").exists()

    config = home / ".claude_desktop" / "claude_desktop_config.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"mcpServers": {"other": {"command": "echo"}}, "theme": "dark"}), encoding="utf-8")
    written = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "setup.py")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert written.returncode == 0, written.stderr
    merged = json.loads(config.read_text(encoding="utf-8"))
    assert merged["theme"] == "dark"
    assert merged["mcpServers"]["other"]["command"] == "echo"
    assert "engineering-continuity-graph" in merged["mcpServers"]
    kept = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "setup.py")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert kept.returncode == 0
    assert "kept existing" in kept.stdout


def test_setup_refuses_without_project(tmp_path):
    env = os.environ.copy()
    env["PATH"] = "/usr/bin:/bin"
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "setup.py"), "--print"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    assert "uv is not installed" in proc.stderr
