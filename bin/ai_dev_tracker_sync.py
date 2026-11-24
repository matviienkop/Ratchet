"""Mirror confirmed native Kanban status to project-bound tracker tickets.

Converge on the latest confirmed column, including after an offline period.
Keep a local receipt and back off transient failures independently per binding.
"""
from contextlib import closing
import fcntl
import hashlib
import http.client
import json
from pathlib import Path
import re
import sqlite3
import time


def native_phase(home, board, card):
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]*", board):
        raise ValueError("Invalid board name")
    directory = home if board == "default" else home / "kanban/boards" / board
    path = directory / "kanban.db"
    if path.resolve() != path:
        raise ValueError("Unexpected symlink in Kanban database path")
    # Read one consistent SQLite snapshot, including the claim's native source column.
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
        row = db.execute("""SELECT status, (SELECT payload FROM task_events
                     WHERE task_id=tasks.id AND run_id=tasks.current_run_id AND kind='claimed'
                     ORDER BY id DESC LIMIT 1)
                     FROM tasks WHERE id=?""", (card,)).fetchone()
    if row is None:
        raise ValueError("Bound native card missing")
    status, claim = row
    if status == "running" and claim and json.loads(claim).get("source_status") == "review":
        return "review"
    if status in ("triage", "todo", "scheduled"):
        return "backlog"
    if status in ("ready", "running", "review", "blocked", "done", "archived"):
        return status
    raise ValueError("Unrecognized native column")


def synchronize(project, db, adapter, home, now):
    db.execute("""CREATE TABLE IF NOT EXISTS mirror (
             board TEXT, card TEXT, config TEXT, phase TEXT, retry_at INTEGER,
             failures INTEGER, confirmed INTEGER, receipt TEXT, PRIMARY KEY(board,card))""")
    db.execute("""CREATE TABLE IF NOT EXISTS mirror_history (
             id INTEGER PRIMARY KEY, at INTEGER, board TEXT, card TEXT, phase TEXT, receipt TEXT)""")
    configuration = hashlib.sha256(json.dumps(adapter.tasks, sort_keys=True).encode()).hexdigest()
    receipts = []
    for board, card, provider, destination, ticket, remote_id in db.execute("SELECT * FROM bindings").fetchall():
        old = db.execute("SELECT config,phase,retry_at,failures,confirmed FROM mirror WHERE board=? AND card=?",
                         (board, card)).fetchone()
        phase = None
        try:
            phase = native_phase(home, board, card)
            same = old and old[:2] == (configuration, phase)
            if same and (old[4] or now < old[2]):
                continue
            if (provider, destination) != (adapter.provider, adapter.destination):
                raise ValueError("Registered tracker changed; reconcile the existing binding")
            if phase == "archived":
                receipt = {"status": "archived"}
            else:
                receipt = adapter.stage(ticket, phase, remote_id)
        except (ValueError, OSError, KeyError, TypeError, sqlite3.Error, http.client.HTTPException) as exc:
            receipt = {"status": "error", "reason": str(exc)}
        confirmed = receipt.get("status") in ("confirmed", "unconfigured", "archived")
        failures = 0 if confirmed else ((old[3] if old and old[:2] == (configuration, phase) else 0) + 1)
        if phase is None and old and old[:2] == (configuration, phase) and now < old[2]:
            continue
        retry_at = 0 if confirmed else now + min(900, 30 * 2 ** min(5, failures - 1))
        serialized = json.dumps(receipt, ensure_ascii=False)
        with db:
            db.execute("INSERT OR REPLACE INTO mirror VALUES (?,?,?,?,?,?,?,?)",
                       (board, card, configuration, phase, retry_at, failures, confirmed, serialized))
            db.execute("INSERT INTO mirror_history(at,board,card,phase,receipt) VALUES (?,?,?,?,?)",
                       (now, board, card, phase, serialized))
        receipts.append({"project": project.name, "board": board, "card": card, "ticket": ticket,
                         "phase": phase, "receipt": receipt})
    return receipts


def tick(workspace, home, tracker):
    if not workspace.is_dir():
        raise ValueError("Tracker workspace does not exist")
    output = []
    for binding_path in sorted(workspace.glob("*/.tracker-bindings.sqlite3")):
        project = binding_path.parent
        if binding_path.resolve() != binding_path:
            continue
        try:
            # Lock the database inode across bridge processes; SQLite still handles local transactions.
            with binding_path.open("rb") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                with closing(sqlite3.connect(binding_path, timeout=10)) as db:
                    output.extend(synchronize(project, db, tracker(project), home, int(time.time())))
        except (ValueError, OSError, KeyError, TypeError, sqlite3.Error, http.client.HTTPException) as exc:
            output.append({"project": project.name, "receipt": {"status": "error", "reason": str(exc)}})
    return output


def watch(workspace, args, tracker):
    if not 1 <= args.interval <= 3600:
        raise ValueError("Polling interval must be between 1 and 3600 seconds")
    home = args.kanban_home.expanduser().resolve()
    while True:
        updates = tick(workspace, home, tracker)
        if args.once:
            return {"updates": updates}
        for entry in updates:
            print(json.dumps(entry, ensure_ascii=False), flush=True)
        time.sleep(args.interval)
