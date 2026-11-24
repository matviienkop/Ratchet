"""Wake only scheduled review-capacity waits that have reached their deadline."""
import json
import re
import subprocess
import time

from .store import Card, CardError, ID, home, record

DELAYS = (900, 3600, 14400)
TRANSIENT = re.compile(r"(?i)\b(429|50[234]|rate.?limit(?:ed|ing)?|temporar(?:y|ily)|quota|capacity|usage limit|timed? ?out|overloaded|connection reset)\b")


def wait_decision(incident, failures):
    if incident.get("child") or not all(TRANSIENT.search(str(reason)) for reason in failures.values()):
        return None
    waits = incident.setdefault("waits", [])
    if not waits or waits[-1].get("claimed"):
        if len(waits) >= len(DELAYS):
            return None
        retry = time.time() + DELAYS[len(waits)]
        waits.append({"due": retry, "marker": f"{incident['marker']}-wait-{len(waits)+1}"})
    wait = waits[-1]
    if wait["due"] <= time.time():
        return {"action": "resume", "next": "Retry only the missing member with ai-dev-card review panel."}
    return {"action": "schedule", "due": wait["due"], "reason": wait["marker"],
            "next": "Persist a handoff, then call kanban_schedule with exactly the returned reason. "
                    "The recovery timer wakes this card when due; keep completed reports."}


def native_show(board, task):
    try:
        result = subprocess.run(["hermes", "kanban", "--board", board, "show", task, "--json"],
                                capture_output=True, text=True, check=True, timeout=30)
        return json.loads(result.stdout)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        raise CardError("Cannot read the native card. Inspect it with kanban_show.") from error


def wake(card):
    def due(state):
        incident = state.get("recoveries", {}).get("provider", {})
        waits = incident.get("waits")
        if waits and not incident.get("child") and not waits[-1].get("claimed") and waits[-1]["due"] <= time.time():
            return waits[-1]
        return None

    if not due(card.load()):
        return False
    with card.locked() as state:
        wait = due(state)
        if not wait:
            return False
        detail = native_show(card.board, card.id)
        if detail["task"]["status"] != "scheduled":
            return False
        event = next((e for e in reversed(detail["events"]) if e.get("kind") == "scheduled"), {})
        if (event.get("payload") or {}).get("reason") != wait["marker"]:
            return False
        try:
            subprocess.run(["hermes", "kanban", "--board", card.board, "unblock", card.id],
                           capture_output=True, check=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as error:
            raise CardError("Hermes did not acknowledge the scheduled wake.") from error
        record(state, "recovery.capacity-wake", marker=wait["marker"])
        return True


def tick():
    result = {"woke": [], "errors": []}
    for path in (home() / "cards").glob("*/*/card.json"):
        board, task = path.parent.parent.name, path.parent.name
        if not ID.fullmatch(board) or not ID.fullmatch(task):
            continue
        try:
            if wake(Card(board, task)):
                result["woke"].append([board, task])
        except (CardError, OSError, KeyError, ValueError) as error:
            result["errors"].append([board, task, str(error)])
    return result, int(bool(result["errors"]))
