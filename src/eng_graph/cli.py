"""Local memory hook. No MCP round-trip, no prompt toolkit.

Every path exits 0, including bad arguments and exceptions, so a prompt hook
cannot block the user on stderr or a non-zero status.
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

from eng_graph.state import clear_root, preview_repository, status_for_root


def main(argv: list[str] | None = None) -> None:
    try:
        _run(argv)
    except Exception:
        args = argv if argv is not None else sys.argv[1:]
        if "-v" in args or "--verbose" in args:
            traceback.print_exc(file=sys.stderr)
    sys.exit(0)


def _run(argv: list[str] | None) -> None:
    parser = argparse.ArgumentParser(prog="eng_graph", add_help=True)
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--repo", default=".")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("resolve_conflict")
    sub.add_parser("status")
    clear = sub.add_parser("clear")
    clear.add_argument("--yes", action="store_true")
    args, _unknown = parser.parse_known_args(argv)
    command = args.cmd or "resolve_conflict"
    root = Path(args.repo).expanduser()
    if command == "status":
        _status(root)
    elif command == "clear":
        _clear(root, yes=bool(args.yes), verbose=bool(args.verbose))
    else:
        _preview(root)


def _preview(root: Path) -> None:
    if not root.exists():
        return
    text = preview_repository(root)
    if text:
        sys.stdout.write(text if text.endswith("\n") else text + "\n")


def _status(root: Path) -> None:
    if not root.exists():
        return
    counts = status_for_root(root)
    if not counts:
        sys.stdout.write("engineering memory: none\n")
        return
    sys.stdout.write(
        "engineering memory: "
        f"{counts['facts_active']} active, "
        f"{counts['facts_superseded']} superseded, "
        f"{counts['facts_total']} total facts; "
        f"{counts['exchanges']} exchanges; "
        f"{counts['edges']} edges; "
        f"{counts['entities']} entities; "
        f"{counts['conflicts_open']} open conflicts\n"
    )


def _clear(root: Path, *, yes: bool, verbose: bool) -> None:
    if not root.exists():
        return
    result = clear_root(root, yes=yes)
    if not result:
        return
    if result.get("dry_run"):
        sys.stdout.write(
            "dry run: would delete "
            f"{result['facts_total']} facts, "
            f"{result['exchanges']} exchanges, "
            f"{result['entities']} entities, "
            f"{result['edges']} edges, "
            f"{result['conflicts_open']} open conflicts\n"
        )
        if verbose:
            sys.stdout.write("pass --yes to delete this repository's memory\n")
        return
    sys.stdout.write(
        "deleted "
        f"{result['facts_total']} facts, "
        f"{result['exchanges']} exchanges, "
        f"{result['entities']} entities\n"
    )


if __name__ == "__main__":
    main()
