"""Durable state of one card, shared by every profile that works it."""

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile

ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class CardError(Exception):
    """A refusal the caller can act on. The message says what to do next."""

    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


def home():
    return Path(os.environ.get("AI_DEV_HOME") or Path.home() / ".ai-dev")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def identity(card=None, board=None, env=None):
    env = os.environ if env is None else env
    card = card or env.get("HERMES_KANBAN_TASK")
    board = board or env.get("HERMES_KANBAN_BOARD") or "default"
    if not card:
        raise CardError("No card: run inside a Kanban worker or pass --card.")
    if not ID.fullmatch(card) or not ID.fullmatch(board):
        raise CardError("Invalid card or board identifier.")
    return board, card


def empty(board, card):
    return {"version": 1, "board": board, "card": card, "contract": None, "frozen": False,
            "repo": None, "base": None, "verifications": [], "probes": {},
            "counters": {"defect": 0, "environment": 0}, "extra_failures": 0,
            "rounds": [], "extra_rounds": 0, "ledger": {}, "log": []}


class Card:
    def __init__(self, board, card):
        self.board, self.id = board, card
        self.dir = home() / "cards" / board / card
        self.file = self.dir / "card.json"

    @property
    def exists(self):
        return self.file.is_file()

    def evidence(self, *parts):
        path = self.dir.joinpath("evidence", *parts)
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        return path

    def load(self):
        """Read without the lock. The file is replaced atomically, so a reader sees one whole state."""
        if not self.exists:
            return empty(self.board, self.id)
        return json.loads(self.file.read_bytes())

    @contextmanager
    def locked(self):
        """Yield the state for mutation and save it when the block ends without an error."""
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(self.dir / "card.lock", "a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = self.load()
            yield state
            self.save(state)

    def save(self, state):
        data = json.dumps(state, indent=1, sort_keys=True, ensure_ascii=False).encode()
        with tempfile.NamedTemporaryFile(dir=self.dir, delete=False) as temp:
            try:
                temp.write(data)
                temp.flush()
                os.fsync(temp.fileno())
                os.replace(temp.name, self.file)
            except BaseException:
                Path(temp.name).unlink(missing_ok=True)
                raise
        directory = os.open(self.dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def record(state, event, **fields):
    from .redact import sanitize
    context = {k: os.environ[v] for k, v in (("run", "HERMES_KANBAN_RUN_ID"),
               ("session", "HERMES_SESSION_ID"), ("role", "AI_DEV_ROLE")) if os.getenv(v)}
    state["log"].append({"at": now(), "event": event, **sanitize({**context, **fields})})
