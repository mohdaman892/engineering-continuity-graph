from pathlib import Path

from eng_graph.repo import normalize_remote, resolve_repository


def test_normalize_github_urls():
    assert normalize_remote("git@github.com:Foo/Bar.git") == "foo/bar"
    assert normalize_remote("https://github.com/Foo/Bar.git") == "foo/bar"
    assert normalize_remote("ssh://git@github.com/Foo/Bar.git") == "foo/bar"
    assert normalize_remote("https://gitlab.com/group/sub/repo.git") == "group/sub/repo"


def test_normalize_local_remote_is_home_relative(tmp_path, monkeypatch):
    home = tmp_path / "home"
    project = home / "work" / "app"
    project.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    assert normalize_remote(str(project)) == "~/work/app"
    assert normalize_remote(f"file://{project}/.git") == "~/work/app"


def test_remote_from_git_config(tmp_path):
    root = tmp_path / "proj"
    git = root / ".git"
    git.mkdir(parents=True)
    (git / "config").write_text(
        '[core]\n\trepositoryformatversion = 0\n[remote "origin"]\n\turl = git@github.com:Acme/ECG.git\n',
        encoding="utf-8",
    )
    identity = resolve_repository(root / "src" / "main.py")
    assert identity.repo_id == "acme/ecg"
    assert identity.method == "remote"
    assert identity.root == root.resolve()


def test_worktree_gitdir_file(tmp_path):
    gitdir = tmp_path / "gitdir"
    gitdir.mkdir()
    (gitdir / "config").write_text(
        '[remote "origin"]\n\turl = https://github.com/Acme/Worktree.git\n',
        encoding="utf-8",
    )
    root = tmp_path / "worktree"
    root.mkdir()
    (root / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    identity = resolve_repository(root)
    assert identity.repo_id == "acme/worktree"
    assert identity.method == "remote"


def test_relative_gitdir_and_commondir(tmp_path):
    common = tmp_path / "module" / "common"
    common.mkdir(parents=True)
    (common / "config").write_text(
        '[remote "origin"]\n\turl = git@github.com:Acme/Sub.git\n',
        encoding="utf-8",
    )
    gitdir = tmp_path / "module" / ".git"
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../common\n", encoding="utf-8")
    identity = resolve_repository(tmp_path / "module")
    assert identity.repo_id == "acme/sub"


def test_hash_fallback_without_remote(tmp_path):
    root = tmp_path / "local"
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "config").write_text("[core]\n\tbare = false\n", encoding="utf-8")
    identity = resolve_repository(root)
    assert identity.method == "git_root"
    assert identity.repo_id.startswith("path:")
    again = resolve_repository(root)
    assert again.repo_id == identity.repo_id


def test_marker_fallback_and_convo_dir(tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='app'\n", encoding="utf-8")
    nested = root / "pkg" / "mod"
    nested.mkdir(parents=True)
    (root / ".convo").mkdir()
    (root / ".convo" / "dir").write_text(str(root), encoding="utf-8")
    identity = resolve_repository(nested / "file.py")
    assert identity.root == root.resolve()
    assert identity.method == "marker"
    assert identity.repo_id.startswith("path:")
