"""Per-card worker activity, handoffs and an optional snapshot of the Hermes board."""
from contextlib import closing
import json
import os
import sqlite3
import subprocess

from .notifications import pump
from .redact import sanitize
from .severity import describe
from .store import CardError, now, record


def observe(card, tool_name, args=None, **meta):
    # stdout and arbitrary argument values can contain private source, prompts and credentials.
    # Keep structured provenance; native run transcripts and verification logs hold the details.
    args = args if isinstance(args, dict) else {}
    event = {"at": now(), "tool": tool_name, "role": os.getenv("AI_DEV_ROLE"),
             "run": os.getenv("HERMES_KANBAN_RUN_ID"), "session": meta.get("session_id"),
             "turn": meta.get("turn_id"), "call": meta.get("tool_call_id"),
             "status": meta.get("status"), "duration_ms": meta.get("duration_ms"),
             "error_type": meta.get("error_type")}
    if tool_name in ("read_file", "write_file", "patch"):
        event["path"] = args.get("path")
    if tool_name in ("kanban_request_review", "kanban_complete", "kanban_request_changes",
                     "kanban_block", "kanban_comment", "delegate_task"):
        for key in ("summary", "reason", "body", "goal", "reviewer", "task_id"):
            if isinstance(args.get(key), str):
                event[key] = args[key][:2000]
    event = sanitize({k: v for k, v in event.items() if v is not None})
    card.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with closing(sqlite3.connect(card.dir / "activity.sqlite", timeout=15)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS activity (id INTEGER PRIMARY KEY, entry TEXT NOT NULL)")
        db.execute("INSERT INTO activity(entry) VALUES (?)", (json.dumps(event, ensure_ascii=False),))


def handoff(card, args):
    values = sanitize({k: getattr(args, k) for k in ("finished", "result", "stage", "actor", "inputs", "reason")})
    if any(not v.strip() or len(v) > 4000 for v in values.values()):
        raise CardError("Each handoff field must contain between 1 and 4000 characters.")
    with card.locked() as state:
        last = next((e for e in reversed(state["log"]) if e["event"] == "handoff"), {})
        if last.get("run") != os.getenv("HERMES_KANBAN_RUN_ID") or not all(last.get(k) == v for k, v in values.items()):
            record(state, "handoff", delivery={"status": "pending", "id": f"h{len(state['log']) + 1}"}, **values)
            last = state["log"][-1]
        delivery = last.get("delivery", {}).get("status", "historical")
        if delivery == "pending" and not state.get("conversation"):
            delivery = "waiting_for_conversation"
        return {"handoff": values, "notification": delivery}, 0


def subscribed_route(card):
    """Some terminal backends isolate gateway variables. Hermes already subscribed the creator."""
    try:
        done = subprocess.run(["hermes", "kanban", "--board", card.board, "notify-list", card.id, "--json"],
                              capture_output=True, text=True, timeout=30, check=True)
        subs = json.loads(done.stdout)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise CardError("Cannot read the subscription. Check hermes kanban notify-list for this card.") from error
    if not isinstance(subs, list) or len(subs) != 1:
        raise CardError("Cannot determine one originating subscription. Bind from the owner's gateway thread.")
    sub = subs[0]
    if not isinstance(sub, dict) or sub.get("platform") not in ("slack", "telegram") or not sub.get("chat_id"):
        raise CardError("No Slack or Telegram origin. Create the card in the owner's gateway session.")
    if sub["platform"] == "slack" and not sub.get("thread_id"):
        raise CardError("The subscription points at a Slack channel. Bind from the original task thread.")
    return {"platform": sub["platform"], "chat": str(sub["chat_id"]), "thread": sub.get("thread_id"), "session": None}


def bind(card):
    """Subscribe the owner conversation using routing supplied by the gateway."""
    platform = os.getenv("HERMES_SESSION_PLATFORM")
    chat = os.getenv("HERMES_SESSION_CHAT_ID")
    session = os.getenv("HERMES_SESSION_ID")
    if os.getenv("HERMES_KANBAN_TASK"):
        raise CardError("Run bind in the Slack or Telegram planner chat where the owner opened the card.")
    thread = os.getenv("HERMES_SESSION_THREAD_ID")
    if platform == "slack" and not thread:
        thread = os.getenv("HERMES_SESSION_MESSAGE_ID")
        if not thread:
            raise CardError("Open the card in its Slack thread, then run bind again.")
    live_chat = platform in ("slack", "telegram") and bool(chat)
    route = ({"platform": platform, "chat": chat, "thread": thread, "session": session}
             if live_chat else subscribed_route(card))
    argv = ["hermes", "kanban", "--board", card.board, "notify-subscribe", card.id,
            "--platform", platform, "--chat-id", chat]
    if thread:
        argv.extend(["--thread-id", thread])
    with card.locked() as state:
        existing = state.get("conversation")
        if existing and any(existing.get(k) != route[k] for k in ("platform", "chat", "thread")):
            raise CardError("This card already belongs to a different conversation. Resume its original conversation.")
        if live_chat:
            try:
                result = subprocess.run(argv, capture_output=True, timeout=30)
            except (OSError, subprocess.TimeoutExpired) as error:
                raise CardError("Hermes did not confirm the subscription. Retry bind from the original conversation.") from error
            if result.returncode:
                raise CardError("Hermes refused the subscription. Inspect hermes kanban notify-subscribe --help and retry.")
        if not existing:
            state["conversation"] = route
            record(state, "conversation.bound", **route)
    return {"conversation": existing or route, "subscribed": True}, 0


def history(card, args):
    state = card.load()
    events = state["log"]
    if getattr(args, "reviews", False):
        events = [event for event in events if event["event"] == "review.round-summary"]
    output = {"board": card.board, "card": card.id, "total_events": len(events), "conversation": state.get("conversation"),
              "events": events[-args.limit:]}
    if args.details:
        output["evidence"] = {k: state[k] for k in ("base", "contract", "verifications", "rounds", "ledger")}
        output["evidence"].update({k: state.get(k, []) for k in ("analyses", "plans", "preflight_history")})
        output["evidence"]["ledger"] = {k: {**e, **describe(e)} for k, e in state["ledger"].items()}
        activity = card.dir / "activity.sqlite"
        output["tools"] = []
        if activity.is_file():
            with closing(sqlite3.connect(f"{activity.resolve().as_uri()}?mode=ro", uri=True)) as db:
                rows = db.execute("SELECT entry FROM activity ORDER BY id DESC LIMIT ?", (args.limit,))
                output["tools"] = [json.loads(x[0]) for x in rows][::-1]
    if args.native:
        try:
            done = subprocess.run(["hermes", "kanban", "--board", card.board, "show", card.id, "--json"],
                                  capture_output=True, text=True, timeout=30)
            if done.returncode:
                raise CardError("Hermes could not read this board/card. Check the active profile and board.")
            output["hermes"] = json.loads(done.stdout)
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            raise CardError("Cannot read native history. Run hermes kanban show for this card.") from error
    return sanitize(output), 0


def add_commands(commands, get_card):
    def limit(value):
        n = int(value)
        if not 1 <= n <= 10000:
            raise ValueError("limit must be between 1 and 10000")
        return n

    sub = commands.add_parser("notifications", help="Deliver queued handoffs from the planner profile.")
    sub.add_argument("--watch", action="store_true", help="Continue processing pending handoffs.")
    sub.set_defaults(run=pump)
    sub = commands.add_parser("bind", help="Bind card notifications to the current planner chat or thread.")
    sub.set_defaults(run=lambda a: bind(get_card(a)))
    sub = commands.add_parser("history", help="Card events, verification evidence and native Hermes handoffs.")
    sub.add_argument("--limit", type=limit, default=20)
    sub.add_argument("--reviews", action="store_true", help="Show review round summaries, including earlier severity decisions.")
    sub.add_argument("--details", action="store_true", help="Include receipts, review and recent worker tool calls.")
    sub.add_argument("--native", action="store_true", help="Join the live board, comments, events and runs via Hermes.")
    sub.set_defaults(run=lambda a: history(get_card(a), a))
    sub = commands.add_parser("handoff", help="Record the finished stage, evidence and next worker's input.")
    for field in ("finished", "result", "stage", "actor", "inputs", "reason"):
        sub.add_argument("--" + field, required=True)
    sub.set_defaults(run=lambda a: handoff(get_card(a), a))
