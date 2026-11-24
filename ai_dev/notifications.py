"""Deliver queued stage handoffs through the planner profile's Hermes gateway configuration."""
import fcntl
import json
import re
import subprocess
import sys
import time

from .store import Card, CardError, ID, home, now


def target(route):
    platform, chat, thread = route["platform"], str(route["chat"]), str(route.get("thread") or "")
    if platform == "slack" and re.fullmatch(r"[CGD][A-Z0-9]{8,}", chat) and re.fullmatch(r"\d+\.\d+", thread):
        return f"slack:{chat}:{thread}"
    if platform == "telegram" and re.fullmatch(r"-?\d+", chat) and (not thread or thread.isdigit()):
        return f"telegram:{chat}" + (f":{thread}" if thread else "")
    raise CardError("Invalid target for stage delivery. Check the originating thread's subscription.")


def message(card, event):
    # The native sender interprets MEDIA directives as requests to attach local files.
    # Summaries and evidence names are untrusted ordinary text.
    def plain(key):
        value = " ".join(str(event[key]).split())[:420]
        return re.sub(r"(?i)media\s*:", "MEDIA：", value)

    if event.get("event") == "model.switch":
        return f"[{card.board}/{card.id}] {plain('text')}"
    if event.get("event") == "review.summary-message":
        text = re.sub(r"(?i)media\s*:", "MEDIA：", event["text"])
        return (f"[{card.board}/{card.id} · {event['delivery']['id']} · {event['part']}/{event['parts']}]\n" + text)
    return (f"[{card.board}/{card.id} · {event['delivery']['id']}]\n"
            f"✓ {plain('finished')} → {plain('result')}\n"
            f"→ {plain('stage')} · {plain('actor')}\n"
            f"↳ {plain('inputs')}\n∵ {plain('reason')}")


def send(card, route, event):
    try:
        done = subprocess.run(["hermes", "send", "--to", target(route), "--json"],
                              input=message(card, event), text=True, capture_output=True, timeout=40)
        reply = json.loads(done.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        raise CardError(f"Could not confirm delivery ({type(error).__name__}).") from error
    if done.returncode or not isinstance(reply, dict) or not reply.get("success") or reply.get("skipped") or reply.get("error"):
        raise CardError("Hermes did not acknowledge stage delivery.")
    return {k: reply[k] for k in ("message_id", "mirrored") if k in reply}


def pending(state):
    return next((e for e in state["log"] if e.get("delivery", {}).get("status") == "pending"), None)


def deliver(card):
    state = card.load()
    if not state.get("conversation") or not pending(state):
        return "idle"
    # Separate lock: simultaneous timers cannot double-send, and a slow network never locks
    # out the worker writing its next stage. flock is released on process termination.
    with (card.dir / "delivery.lock").open("a+b") as guard:
        try:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "busy"
        state = card.load()
        event = pending(state)
        if not event or event["delivery"].get("retry_at", 0) > time.time():
            return "idle"
        try:
            receipt, problem = send(card, state["conversation"], event), None
        except CardError as error:
            receipt, problem = {}, str(error)
        with card.locked() as state:
            delivery = pending(state)["delivery"]
            assert delivery["id"] == event["delivery"]["id"]
            count = delivery.get("attempts", 0) + 1
            delivery.update(attempts=count, last_attempt=now())
            if problem:
                delivery.update(last_error=problem, retry_at=time.time() + min(300, 5 * 2 ** min(count, 6)))
                return "retry"
            delivery.update(status="sent", **receipt)
            delivery.pop("retry_at", None)
            delivery.pop("last_error", None)
            return "sent"


def pump(args):
    while True:
        counts = {}
        for path in sorted((home() / "cards").glob("*/*/card.json")):
            board, task = path.parent.parent.name, path.parent.name
            if not ID.fullmatch(board) or not ID.fullmatch(task):
                continue
            try:
                result = deliver(Card(board, task))
            except (ValueError, KeyError, OSError) as error:
                print(f"stage delivery: {board}/{task}: {type(error).__name__}", file=sys.stderr)
                result = "error"
            counts[result] = counts.get(result, 0) + 1
        if not args.watch:
            return counts, int(bool(counts.get("error") or counts.get("retry")))
        if not counts.get("sent"):
            time.sleep(10)
