import os
import re
import subprocess
import time

import pytest
from starlette.testclient import TestClient

from commit_rewriter import GitError, Repository, create_app


def git(path, *args, env=None):
    return (
        subprocess.check_output(["git", "-C", str(path), *args], env=env)
        .decode()
        .strip()
    )


def commit(path, message):
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Original Author",
        "GIT_AUTHOR_EMAIL": "author@example.org",
        "GIT_COMMITTER_NAME": "Different Committer",
        "GIT_COMMITTER_EMAIL": "committer@example.org",
        "GIT_AUTHOR_DATE": "2001-02-03T04:05:06 +0530",
        "GIT_COMMITTER_DATE": "2002-03-04T05:06:07 -0700",
    }
    git(path, "commit", "--allow-empty", "-m", message, env=env)
    return git(path, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "user.email", "test@example.org")
    git(tmp_path, "config", "commit.gpgsign", "false")
    commit(tmp_path, "First")
    commit(tmp_path, "Second")
    commit(tmp_path, "Third")
    return Repository(tmp_path)


def run(repo, changes):
    old = repo.tip()
    job = {}
    repo.rewrite(old, repo.prepare(old, changes), lambda **fields: job.update(fields))
    return old, job


def header(raw):
    return raw.split(b"\n\n", 1)[0].splitlines()


def test_preserves_metadata_and_trees_and_backup(repo):
    commits = repo.commits()
    oldest = commits[-1]["oid"]
    original = {c["oid"]: repo.raw(c["oid"]) for c in commits}
    old, job = run(repo, {oldest: "Rewritten subject\n\nBody with café\n"})
    assert job["status"] == "complete"
    assert job["done"] == job["total"] == 3
    assert git(repo.path, "rev-parse", job["backup"]) == old
    assert repo.tip() != old
    for before, after in job["mapping"].items():
        for prefix in (b"author ", b"committer ", b"tree "):
            assert next(
                x for x in header(original[before]) if x.startswith(prefix)
            ) == next(x for x in header(repo.raw(after)) if x.startswith(prefix))
        if before != oldest:
            assert (
                original[before].split(b"\n\n", 1)[1]
                == repo.raw(after).split(b"\n\n", 1)[1]
            )
    assert repo.raw(job["mapping"][oldest]).endswith(
        "Rewritten subject\n\nBody with café\n".encode()
    )
    assert git(repo.path, "status", "--porcelain") == ""


def test_merge_topology_and_side_branch_edit(repo):
    main_parent = repo.tip()
    git(repo.path, "checkout", "-b", "feature", "HEAD~1")
    feature = commit(repo.path, "Feature")
    git(repo.path, "checkout", "main")
    git(repo.path, "merge", "--no-ff", "feature", "-m", "Merge feature")
    merge = repo.tip()
    _, job = run(repo, {feature: "Better feature message"})
    assert len(job["mapping"]) == 2
    parents = git(repo.path, "show", "-s", "--format=%P", repo.tip()).split()
    assert parents == [main_parent, job["mapping"][feature]]
    assert git(repo.path, "rev-parse", "feature") == feature
    assert job["mapping"][merge] == repo.tip()


@pytest.mark.parametrize("message", ["", " \n\t", "nul\x00message"])
def test_empty_or_nul_rejected(repo, message):
    old = repo.tip()
    with pytest.raises(GitError):
        repo.prepare(old, {old: message})
    assert repo.tip() == old
    assert git(repo.path, "branch", "--list", "commit-message-backup/*") == ""


def test_long_subject_allowed(repo):
    _, job = run(repo, {repo.tip(): "x" * 200})
    assert job["status"] == "complete"


def test_stale_tip_and_invalid_commit(repo):
    old = repo.tip()
    commit(repo.path, "New commit")
    with pytest.raises(GitError, match="main changed"):
        repo.prepare(old, {old: "Edit"})
    with pytest.raises(GitError, match="latest 100"):
        repo.prepare(repo.tip(), {"not-a-hash": "Edit"})


@pytest.mark.parametrize("checkout", ["main", "feature", "detached"])
def test_rewrite_preserves_uncommitted_changes(repo, checkout):
    for name in ("partial.txt", "staged-delete.txt", "unstaged-delete.txt"):
        (repo.path / name).write_text("Original contents\n")
    git(repo.path, "add", ".")
    earlier = commit(repo.path, "Add files")
    (repo.path / "history.txt").write_text("Only in the later commit\n")
    git(repo.path, "add", "history.txt")
    commit(repo.path, "Add another file")
    if checkout == "feature":
        git(repo.path, "checkout", "-b", "feature", earlier)
    elif checkout == "detached":
        git(repo.path, "checkout", "--detach", earlier)

    (repo.path / "partial.txt").write_text("Staged contents\n")
    (repo.path / "added.txt").write_text("Staged addition\n")
    (repo.path / "staged-delete.txt").unlink()
    git(repo.path, "add", "partial.txt", "added.txt", "staged-delete.txt")
    (repo.path / "partial.txt").write_text("Unstaged contents\n")
    (repo.path / "unstaged-delete.txt").unlink()
    (repo.path / "untracked").mkdir()
    (repo.path / "untracked" / "nested.bin").write_bytes(b"\x00\xffuntracked\n")

    def worktree_contents():
        return {
            path.relative_to(repo.path): path.read_bytes()
            for path in repo.path.rglob("*")
            if path.is_file() and ".git" not in path.relative_to(repo.path).parts
        }

    def refs():
        return dict(
            line.split()
            for line in git(
                repo.path, "for-each-ref", "--format=%(refname) %(objectname)"
            ).splitlines()
        )

    head_before = git(repo.path, "rev-parse", "HEAD")
    refs_before = refs()
    cached_before = repo.git("diff", "--cached", "--binary")
    unstaged_before = repo.git("diff", "--binary")
    files_before = worktree_contents()
    index = repo.path / git(repo.path, "rev-parse", "--git-path", "index")
    index_before = index.read_bytes()

    old, job = run(repo, {earlier: "Describe the original files better"})

    assert job["status"] == "complete"
    assert len(job["mapping"]) == 2
    assert repo.tip() != old
    assert index.read_bytes() == index_before
    assert worktree_contents() == files_before
    assert repo.git("diff", "--cached", "--binary") == cached_before
    assert repo.git("diff", "--binary") == unstaged_before
    for before, after in job["mapping"].items():
        assert git(repo.path, "rev-parse", f"{before}^{{tree}}") == git(
            repo.path, "rev-parse", f"{after}^{{tree}}"
        )
    assert git(repo.path, "rev-parse", "HEAD") == (
        job["tip"] if checkout == "main" else head_before
    )
    refs_after = refs()
    assert refs_after.pop(f"refs/heads/{job['backup']}") == old
    refs_before[repo.ref] = job["tip"]
    assert refs_after == refs_before


def test_unmerged_index_without_operation_markers_rejected(repo):
    old = repo.tip()
    blob = repo.git("hash-object", "-w", "--stdin", data=b"Conflicted\n").strip()
    repo.git(
        "update-index",
        "--index-info",
        data=b"".join(
            b"100644 " + blob + f" {stage}\tconflicted.txt\n".encode()
            for stage in (1, 2, 3)
        ),
    )
    assert repo.git("ls-files", "--unmerged")
    with pytest.raises(GitError, match="Resolve index conflicts"):
        repo.prepare(old, {old: "Edit"})
    assert repo.tip() == old
    assert git(repo.path, "branch", "--list", "commit-message-backup/*") == ""


@pytest.mark.parametrize(
    "marker",
    [
        "MERGE_HEAD",
        "CHERRY_PICK_HEAD",
        "REVERT_HEAD",
        "rebase-merge",
        "rebase-apply",
        "BISECT_LOG",
        "sequencer",
    ],
)
def test_active_git_operation_rejected(repo, marker):
    old = repo.tip()
    path = repo.path / git(repo.path, "rev-parse", "--git-path", marker)
    if marker in {"rebase-merge", "rebase-apply", "sequencer"}:
        path.mkdir()
    else:
        path.write_text(old + "\n")
    with pytest.raises(GitError, match="Finish the active Git operation"):
        repo.prepare(old, {old: "Edit"})
    assert repo.tip() == old
    assert git(repo.path, "branch", "--list", "commit-message-backup/*") == ""


def test_git_operation_started_during_rewrite_keeps_main_and_backup(repo):
    old = repo.tip()
    edits = repo.prepare(old, {old: "Edit"})
    sequencer = repo.path / git(repo.path, "rev-parse", "--git-path", "sequencer")
    job = {}

    def report(**fields):
        job.update(fields)
        if fields.get("done") == 1:
            sequencer.mkdir()

    with pytest.raises(GitError, match="Finish the active Git operation"):
        repo.rewrite(old, edits, report)
    assert repo.tip() == old
    assert git(repo.path, "rev-parse", job["backup"]) == old


def test_failure_keeps_main_and_backup(repo, monkeypatch):
    old = repo.tip()
    edits = repo.prepare(old, {old: "New"})
    real = repo.git

    def fail(*args, **kwargs):
        if args[0] == "hash-object":
            raise GitError("simulated failure")
        return real(*args, **kwargs)

    monkeypatch.setattr(repo, "git", fail)
    with pytest.raises(GitError, match="simulated"):
        repo.rewrite(old, edits, lambda **fields: None)
    assert repo.tip() == old
    branches = (
        git(repo.path, "branch", "--list", "commit-message-backup/*")
        .strip()
        .splitlines()
    )
    assert len(branches) == 1
    assert git(repo.path, "rev-parse", branches[0].strip()) == old


def test_concurrent_ref_change_not_overwritten(repo):
    old = repo.tip()
    previous = git(repo.path, "rev-parse", "HEAD~1")
    edits = repo.prepare(old, {old: "New"})

    def report(**fields):
        if fields.get("done") == 1:
            git(repo.path, "update-ref", "refs/heads/main", previous, old)

    with pytest.raises(GitError, match="main changed"):
        repo.rewrite(old, edits, report)
    assert repo.tip() == previous


def test_latest_100_and_outside_window(repo):
    oldest = repo.commits()[-1]["oid"]
    for i in range(99):
        commit(repo.path, f"Commit {i}")
    assert len(repo.commits()) == 100
    with pytest.raises(GitError, match="latest 100"):
        repo.prepare(repo.tip(), {oldest: "Outside window"})


def test_http_validation_progress_and_csrf(repo):
    with TestClient(create_app(repo.path)) as client:
        page = client.get("/")
        assert page.status_code == 200
        token = re.search(r"const TOKEN\s*=\s*['\"]([^'\"]+)['\"]", page.text).group(1)
        state = client.get("/api/state").json()
        payload = {"tip": state["tip"], "edits": {state["tip"]: "Updated via API"}}
        assert client.post("/api/rewrite", json=payload).status_code == 403
        headers = {"X-CSRF-Token": token}
        assert client.post("/api/rewrite", json=[], headers=headers).status_code == 400
        assert client.get("/", headers={"Host": "evil.example"}).status_code == 400
        assert (
            client.post("/api/rewrite", json=payload, headers=headers).status_code
            == 202
        )
        for _ in range(200):
            job = client.get("/api/progress").json()
            if job["status"] != "running":
                break
            time.sleep(0.01)
        assert job["status"] == "complete"
        assert job["done"] == job["total"] == 1
        assert repo.commits()[0]["message"] == "Updated via API"


def test_file_contents_and_signature_removal(repo):
    (repo.path / "example.txt").write_text("Keep these exact contents\n")
    git(repo.path, "add", "example.txt")
    original = commit(repo.path, "Add file")
    raw_header, message = repo.raw(original).split(b"\n\n", 1)
    signed = (
        raw_header
        + b"\ngpgsig -----BEGIN PGP SIGNATURE-----\n test-only-placeholder\n -----END PGP SIGNATURE-----\n\n"
        + message
    )
    signed_oid = (
        repo.git("hash-object", "-t", "commit", "-w", "--stdin", data=signed)
        .decode()
        .strip()
    )
    git(repo.path, "update-ref", "refs/heads/main", signed_oid, original)
    _, job = run(repo, {signed_oid: "Describe the file better"})
    assert job["signatures_removed"] == 1
    assert b"gpgsig " not in repo.raw(repo.tip())
    assert git(repo.path, "show", "main:example.txt") == "Keep these exact contents"
    assert (repo.path / "example.txt").read_text() == "Keep these exact contents\n"
    assert git(repo.path, "diff", job["backup"], "main") == ""


@pytest.mark.parametrize(
    ("arguments", "expected"), [([], "."), (["/some/repository"], "/some/repository")]
)
def test_cli_repository_path(monkeypatch, arguments, expected):
    import commit_rewriter
    import uvicorn

    captured = {}

    def app(path):
        captured["path"] = path
        return "test-app"

    monkeypatch.setattr(commit_rewriter, "create_app", app)
    monkeypatch.setattr(
        uvicorn, "run", lambda app, **kwargs: captured.update(server=kwargs)
    )
    monkeypatch.setattr("sys.argv", ["commit-rewriter", *arguments])
    commit_rewriter.main()
    assert captured["path"] == expected
    assert captured["server"] == {"host": "127.0.0.1", "port": 8000}


def test_installed_cli_entry_point(tmp_path):
    import shutil

    executable = shutil.which("commit-rewriter")
    assert executable is not None
    result = subprocess.run(
        [executable, "--help"], cwd=tmp_path, capture_output=True, text=True
    )
    assert result.returncode == 0
    assert "[path]" in result.stdout
    assert "--port" in result.stdout
    assert "--repo" not in result.stdout
    rejected = subprocess.run(
        [executable, "--repo", str(tmp_path)], capture_output=True, text=True
    )
    assert rejected.returncode == 2
    assert "unrecognized arguments: --repo" in rejected.stderr
