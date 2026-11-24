"""Git plumbing: exact tree snapshots, anchored commits, diffs and scratch checkouts."""

from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading

from .store import CardError

# Parallel reviewers each need a scratch checkout; git serializes worktree metadata poorly under contention.
_WORKTREES = threading.Lock()

IDENTITY = {"GIT_AUTHOR_NAME": "ai-dev", "GIT_AUTHOR_EMAIL": "ai-dev@localhost",
            "GIT_COMMITTER_NAME": "ai-dev", "GIT_COMMITTER_EMAIL": "ai-dev@localhost"}


def environment(extra=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
    env.update(extra or {})
    return env


def git(cwd, *args, env=None, data=None, check=True):
    result = subprocess.run(["git", *args], cwd=cwd, env=environment(env), input=data, capture_output=True)
    if check and result.returncode:
        detail = result.stderr.decode(errors="replace").strip().splitlines()[-1:] or [""]
        raise CardError(f"git {args[0]} failed in {cwd}: {detail[0]}")
    return result.stdout if check else result


def text(cwd, *args):
    return git(cwd, *args).decode().strip()


def repo_root(path):
    path = Path(path)
    if not path.is_dir():
        raise CardError(f"Workspace {path} does not exist.")
    result = git(path, "rev-parse", "--show-toplevel", check=False)
    if result.returncode:
        raise CardError(f"Workspace {path} is not inside a git repository.")
    return Path(result.stdout.decode().strip())


def origin(worktree):
    result = git(worktree, "config", "--get", "remote.origin.url", check=False)
    return result.stdout.decode().strip() or None


def main_repo(worktree):
    """The repository that owns this worktree, used to match policy entries."""
    common = Path(text(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    return common.parent if common.name == ".git" else common


def snapshot(worktree):
    """Tree ID of the working copy as it is now: tracked, staged and untracked files, minus ignored ones."""
    with tempfile.TemporaryDirectory() as scratch:
        index = Path(scratch) / "index"
        live = Path(text(worktree, "rev-parse", "--path-format=absolute", "--git-path", "index"))
        env = {"GIT_INDEX_FILE": str(index)}
        if live.is_file():
            shutil.copyfile(live, index)
        elif git(worktree, "rev-parse", "--verify", "-q", "HEAD", check=False).returncode == 0:
            git(worktree, "read-tree", "HEAD", env=env)
        git(worktree, "add", "-A", "--", ".", env=env)
        return git(worktree, "write-tree", env=env).decode().strip()


def anchor(worktree, ref, tree, parent):
    """Commit a snapshot under refs/ai-dev/ so its objects survive and can be checked out."""
    commit = git(worktree, "commit-tree", tree, "-p", parent, "-m", "ai-dev snapshot", env=IDENTITY).decode().strip()
    git(worktree, "update-ref", f"refs/ai-dev/{ref}", commit)
    return commit


def resolve_base(worktree, target=None):
    """Merge base of HEAD and the branch the change is meant for."""
    candidates = [target] if target else []
    head = git(worktree, "symbolic-ref", "-q", "refs/remotes/origin/HEAD", check=False)
    if head.returncode == 0:
        candidates.append(head.stdout.decode().strip())
    candidates += ["origin/main", "origin/master", "main", "master"]
    for name in candidates:
        if git(worktree, "rev-parse", "--verify", "-q", name + "^{commit}", check=False).returncode:
            continue
        base = git(worktree, "merge-base", "HEAD", name, check=False)
        if base.returncode == 0:
            return base.stdout.decode().strip()
    raise CardError("Cannot find the base revision. Set target_branch for this repository in the policy file.")


def diff(worktree, old, new):
    return git(worktree, "diff", "--binary", "--full-index", "--no-ext-diff", "--no-textconv", "--no-renames",
               "--no-color", "--src-prefix=a/", "--dst-prefix=b/", "--submodule=short", old, new, "--")


def changed_files(worktree, old, new):
    names = git(worktree, "diff", "--no-ext-diff", "--no-renames", "--name-only", "-z", old, new)
    return sorted(name for name in names.decode().split("\0") if name)


@contextmanager
def checkout(worktree, commit):
    """A detached scratch checkout of `commit`, removed afterwards."""
    parent = Path(tempfile.mkdtemp(prefix="ai-dev-checkout-"))
    target = parent / "tree"
    try:
        with _WORKTREES:
            git(worktree, "worktree", "add", "--detach", "--force", str(target), commit)
        yield target
    finally:
        with _WORKTREES:
            git(worktree, "worktree", "remove", "--force", str(target), check=False)
            shutil.rmtree(parent, ignore_errors=True)
            git(worktree, "worktree", "prune", check=False)
