"""Install the Engineering Continuity Graph into the local Claude desktop config.

Refuses to run when Python is older than 3.11, ``uv`` is missing, or no
``pyproject.toml`` for this project can be found. ``--print`` shows the server
entry and does not write. ``--force`` replaces an existing entry. Other
servers already in the file are preserved.

Auto-approved tools are read-only or stage-only. ``ecg_commit_step`` and
``ecg_approve_consent`` stay off that list so the client's approval dialog
remains the consent checkpoint (the same role as ``commit_memory`` and
``approve_consent_all``, which are not registered).
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

SERVER_KEY = "engineering-continuity-graph"

# Read-only or stage-only. Durable writes and consent grants are excluded.
ALWAYS_ALLOW = [
    "ecg_get_session",
    "ecg_stats",
    "ecg_search_context",
    "ecg_commit_placements",
    "ecg_record_provenance",
    "ecg_discard_proposal",
    "ecg_prepare_placements",
    "ecg_list_conflicts",
    "ecg_cancel_step",
]

EXCLUDED_CHECKPOINTS = [
    "ecg_commit_step",
    "ecg_approve_consent",
    "ecg_resolve_conflict",
    "commit_memory",
    "approve_consent_all",
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Register the ECG MCP server locally")
    parser.add_argument("--print", action="store_true", dest="print_only")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    problems = _problems()
    if problems:
        sys.stderr.write("ECG setup refused:\n")
        for problem in problems:
            sys.stderr.write(f"- {problem}\n")
        return 1
    repo = _find_repo(Path.cwd()) or _find_repo(Path(__file__).resolve().parent)
    assert repo is not None
    entry = _entry(repo)
    payload = {"mcpServers": {SERVER_KEY: entry}}
    if args.print_only:
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
        return 0
    destination = Path.home() / ".claude_desktop" / "claude_desktop_config.json"
    existing: dict = {}
    if destination.exists():
        try:
            existing = json.loads(destination.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            sys.stderr.write(f"ECG setup refused:\n- {destination} is not valid JSON\n")
            return 1
        if not isinstance(existing, dict):
            sys.stderr.write(f"ECG setup refused:\n- {destination} must be a JSON object\n")
            return 1
    servers = existing.get("mcpServers")
    if servers is None:
        servers = {}
        existing["mcpServers"] = servers
    if not isinstance(servers, dict):
        sys.stderr.write("ECG setup refused:\n- mcpServers is not an object\n")
        return 1
    if SERVER_KEY in servers and not args.force:
        sys.stdout.write(f"kept existing {SERVER_KEY} entry in {destination}\n")
        return 0
    servers[SERVER_KEY] = entry
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    sys.stdout.write(f"wrote {SERVER_KEY} into {destination}\n")
    return 0


def _problems() -> list[str]:
    found = []
    if sys.version_info < (3, 11):
        found.append(f"Python {sys.version.split()[0]} is older than 3.11")
    if shutil.which("uv") is None:
        found.append("uv is not installed")
    if _find_repo(Path.cwd()) is None and _find_repo(Path(__file__).resolve().parent) is None:
        found.append("no pyproject.toml for eng_graph was found")
    return found


def _find_repo(start: Path) -> Path | None:
    current = start.resolve()
    for candidate in [current, *current.parents]:
        if (candidate / "pyproject.toml").is_file() and (candidate / "src" / "eng_graph").is_dir():
            return candidate
    return None


def _entry(repo: Path) -> dict:
    return {
        "command": "uv",
        "args": ["run", "--directory", str(repo), "eng-server"],
        "env": {
            "PYTHONPATH": str(repo / "src"),
            "ECG_EMBEDDING_PROVIDER": "testing",
        },
        "alwaysAllow": list(ALWAYS_ALLOW),
        "excludeFromAlwaysAllow": list(EXCLUDED_CHECKPOINTS),
    }


if __name__ == "__main__":
    sys.exit(main())
