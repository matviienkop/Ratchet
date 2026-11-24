"""Verification stands that the host operator registered for a project."""

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat
import subprocess
import tempfile

from .policy import NAME
from .store import CardError, home

ROOT = Path("/etc/ai-dev/stands")
PREPARE_TIMEOUT = 1800


def trusted_directory(directory):
    for parent in (*reversed(directory.parents), directory):
        info = parent.lstat()
        if info.st_uid != 0 or info.st_mode & 0o022 or not stat.S_ISDIR(info.st_mode):
            raise CardError(f"Untrusted stand directory: {parent}")
    return directory


def registered(project, name):
    """A root-owned driver at /etc/ai-dev/stands/PROJECT/NAME. A checkout or a worker cannot register one."""
    if not project or not NAME.fullmatch(project) or not NAME.fullmatch(name):
        raise CardError("A stand check needs `project` in the policy and a valid stand name.")
    try:
        driver = trusted_directory(ROOT / project) / name
        info = driver.lstat()
    except FileNotFoundError:
        raise CardError(f"Stand {name} is not registered for project {project}.") from None
    if info.st_uid != 0 or info.st_mode & 0o022 or not stat.S_ISREG(info.st_mode) or not os.access(driver, os.X_OK):
        raise CardError(f"Untrusted or non-executable stand driver: {driver}")
    return driver


def available(project):
    try:
        directory = trusted_directory(ROOT / project)
    except (FileNotFoundError, CardError):
        return []
    names = []
    for entry in directory.iterdir():
        try:
            registered(project, entry.name)
        except CardError:
            continue
        names.append(entry.name)
    return sorted(names)


@contextmanager
def exclusive(project, name):
    """One shared host lock per registered guest for diagnostics and the whole verification."""
    if not isinstance(project, str) or not NAME.fullmatch(project) or not NAME.fullmatch(name):
        raise CardError("Invalid stand project or name.")
    directory = home() / "stand-locks" / project
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    with (directory / name).open("a+b") as guard:
        try:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CardError(f"Stand {name} is busy with another operation. Retry after that operation finishes.") from None
        yield


def call(driver, *args, timeout=PREPARE_TIMEOUT):
    result = subprocess.run([str(driver), *map(str, args)], cwd=driver.parent, stdin=subprocess.DEVNULL,
                            capture_output=True, timeout=timeout)
    tail = (result.stdout + result.stderr).decode(errors="replace").strip()[-2000:]
    return result.returncode, tail


def prepare(driver, worktree, base, patch):
    """Start the stand and hand it the exact patch through a sealed memory file."""
    for operation in ("start", "status"):
        try:
            code, tail = call(driver, operation)
        except subprocess.TimeoutExpired:
            return f"Stand {driver.name} {operation} timed out."
        if code:
            return f"Stand {driver.name} {operation} failed: {tail}"
    if not hasattr(os, "memfd_create"):
        return "Stand checks need a Linux host with sealed memory files."
    fd = os.memfd_create("candidate.patch", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    with os.fdopen(fd, "wb") as stream, tempfile.TemporaryFile() as log:
        stream.write(patch)
        stream.flush()
        os.fchmod(fd, 0o400)
        fcntl.fcntl(fd, fcntl.F_ADD_SEALS, fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL)
        argv = [str(driver), "prepare", str(worktree), base, f"/proc/self/fd/{fd}"]
        try:
            code = subprocess.run(argv, cwd=driver.parent, stdin=subprocess.DEVNULL, stdout=log,
                                  stderr=subprocess.STDOUT, pass_fds=(fd,), timeout=PREPARE_TIMEOUT).returncode
        except subprocess.TimeoutExpired:
            return f"Stand {driver.name} preparation timed out."
        if code:
            log.seek(max(0, log.tell() - 2000))
            return f"Stand {driver.name} preparation failed: {log.read().decode(errors='replace').strip()}"
    return None
