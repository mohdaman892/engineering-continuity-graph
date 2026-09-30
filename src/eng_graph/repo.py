"""Repository identity without spawning git.

A child process would inherit an MCP client's stdio pipes, so this module only
reads files. It never starts another program, including git.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

_MARKERS = (
    ".git",
    ".clerk",
    "pyproject.toml",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "setup.py",
    "composer.json",
    "Gemfile",
)

_REMOTE_SECTION = re.compile(r'^\s*\[\s*remote\s+"origin"\s*\]\s*$', re.IGNORECASE)
_URL_LINE = re.compile(r'^\s*url\s*=\s*(.+?)\s*$', re.IGNORECASE)
_SECTION = re.compile(r"^\s*\[")


@dataclass(frozen=True)
class RepoIdentity:
    repo_id: str
    root: Path
    remote: str | None
    method: str


def resolve_repository(path: str | Path) -> RepoIdentity:
    """Return a stable id for ``path``.

    Order: origin remote from ``.git/config``, hash of the git root, then hash
    of the nearest directory that holds a project marker. A ``.convo/dir`` file
    overrides the walk with an explicit root.
    """
    start = Path(path).expanduser()
    try:
        start = start.resolve()
    except OSError:
        start = Path(path).expanduser()
    if start.is_file():
        start = start.parent
    start = _apply_convo_dir(start)
    git_root = _find_git_root(start)
    if git_root is not None:
        remote = _origin_url(git_root)
        if remote:
            slug = normalize_remote(remote)
            if slug:
                return RepoIdentity(slug, git_root, remote, "remote")
        return RepoIdentity(_path_id(git_root), git_root, None, "git_root")
    marker_root = _find_marker_root(start)
    return RepoIdentity(_path_id(marker_root), marker_root, None, "marker")


def normalize_remote(url: str) -> str:
    """Map a git remote to ``owner/repo`` or a home-relative local path."""
    raw = url.strip().strip('"').strip("'")
    if not raw:
        return ""
    local = _local_path(raw)
    if local is not None:
        return _display_path(local)
    scp = re.match(r"^[\w.+-]+@[\w.-]+:(?P<path>.+)$", raw)
    if scp:
        path = scp.group("path")
    else:
        without_scheme = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", raw)
        without_user = re.sub(r"^[^/@]+@", "", without_scheme)
        parts = [p for p in without_user.split("/") if p]
        if parts and "." in parts[0] and ":" not in parts[0]:
            parts = parts[1:]
        elif parts and re.match(r"^[\w.-]+:\d+$", parts[0]):
            parts = parts[1:]
        path = "/".join(parts)
    if path.endswith(".git"):
        path = path[: -len(".git")]
    path = path.strip("/")
    return path.lower()


def _local_path(url: str) -> Path | None:
    if url.startswith("file://"):
        raw = url[len("file://") :]
        if raw.startswith("localhost"):
            raw = raw[len("localhost") :]
        return Path(raw)
    if url.startswith("/") or url.startswith("~/"):
        return Path(url).expanduser()
    return None


def _display_path(path: Path) -> str:
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    text = str(resolved)
    if text.endswith("/.git"):
        text = text[: -len("/.git")]
        resolved = Path(text)
    home = str(Path.home())
    if text == home:
        return "~"
    if text.startswith(home + "/"):
        return "~/" + text[len(home) + 1 :]
    return text


def _path_id(path: Path) -> str:
    try:
        key = str(path.resolve())
    except OSError:
        key = str(path)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]
    return "path:" + digest


def _apply_convo_dir(start: Path) -> Path:
    current = start
    for _ in range(64):
        pointer = current / ".convo" / "dir"
        if pointer.is_file():
            raw = pointer.read_text(encoding="utf-8", errors="replace").strip()
            if not raw:
                return start
            target = Path(raw).expanduser()
            if not target.is_absolute():
                target = (pointer.parent / target).resolve()
            if target.exists():
                return target if target.is_dir() else target.parent
            return start
        if current.parent == current:
            break
        current = current.parent
    return start


def _find_git_root(start: Path) -> Path | None:
    current = start
    for _ in range(64):
        git_path = current / ".git"
        if git_path.exists():
            return current
        if current.parent == current:
            break
        current = current.parent
    return None


def _find_marker_root(start: Path) -> Path:
    current = start
    for _ in range(64):
        for name in _MARKERS:
            if (current / name).exists():
                return current
        if current.parent == current:
            break
        current = current.parent
    return start


def _git_config_path(git_root: Path) -> Path | None:
    git_path = git_root / ".git"
    git_dir: Path
    if git_path.is_dir():
        git_dir = git_path
    elif git_path.is_file():
        git_dir = _resolve_gitdir(git_path)
        if git_dir is None:
            return None
    else:
        return None
    common = git_dir / "commondir"
    if common.is_file():
        raw = common.read_text(encoding="utf-8", errors="replace").strip()
        if raw:
            target = Path(raw)
            if not target.is_absolute():
                target = (git_dir / target).resolve()
            config = target / "config"
            if config.is_file():
                return config
    config = git_dir / "config"
    if config.is_file():
        return config
    return None


def _resolve_gitdir(git_file: Path) -> Path | None:
    try:
        text = git_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("gitdir:"):
            raw = stripped.split(":", 1)[1].strip()
            target = Path(raw).expanduser()
            if not target.is_absolute():
                target = (git_file.parent / target).resolve()
            return target
    return None


def _origin_url(git_root: Path) -> str | None:
    config_path = _git_config_path(git_root)
    if config_path is None:
        return None
    try:
        lines = config_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    in_origin = False
    for line in lines:
        if _REMOTE_SECTION.match(line):
            in_origin = True
            continue
        if in_origin and _SECTION.match(line):
            break
        if in_origin:
            match = _URL_LINE.match(line)
            if match:
                return match.group(1).strip()
    return None
