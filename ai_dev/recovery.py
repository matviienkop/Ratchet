"""One independently diagnosed recovery pass at an acceptance boundary."""
import hashlib
import json
import time

from . import gitio, policy
from .redact import sanitize
from .recovery_wake import native_show, wait_decision
from .store import CardError, ID, record

KINDS = ("verification", "review", "provider")


def trigger(state, settings, kind):
    rnd = state["rounds"][-1] if state["rounds"] else {}
    if kind == "verification":
        if state["counters"]["defect"] >= settings["max_defect_failures"] + state["extra_failures"]:
            last = next((v for v in reversed(state["verifications"]) if v["result"] == "defect"), None)
            if last:
                failed = [{"check": c["name"], "problem": c["problem"], "log": c["log"]}
                          for c in last["checks"] if c["klass"] != "pass"]
                return {"receipt": last["id"], "tree": last["tree"], "failed": failed}
    if kind == "review" and rnd.get("outcome") == "owner":
        return {"round": rnd["n"], "tree": rnd["tree"], "blocking": rnd["blocking"], "brief": rnd["fix_brief"]}
    if kind == "provider" and settings["review"]["panel_required"] and rnd.get("outcome") is None:
        failed = {name: r["reason"] for name, r in rnd.get("panel", {}).items() if r.get("status") == "unavailable"}
        if failed:
            return {"round": rnd["n"], "tree": rnd["tree"], "failed": failed}
    return None


def confirmed_child(card, child, marker):
    if not ID.fullmatch(child) or child == card.id:
        raise CardError("Pass the actual ID of the linked diagnosis card.")
    try:
        detail = native_show(card.board, child)
        task = detail["task"]
        summary = detail.get("latest_summary") or task.get("result")
        if (task["id"] == child and task["status"] == "done" and card.id in detail["children"]
                and marker in (task.get("body") or "").splitlines() and "ai-dev-repair" in (task.get("skills") or [])
                and isinstance(summary, str) and summary.strip()):
            return sanitize(summary.strip()[:4000])
    except (KeyError, TypeError, ValueError) as error:
        raise CardError("Cannot read the native diagnosis card. Inspect it with kanban_show.") from error
    raise CardError("The diagnosis must be the linked, completed repair card with a concrete handoff. Inspect kanban_show.")


def progressed(state, kind, before, current):
    """A renamed tree/round is insufficient: require a receipt clearing the previous fault."""
    if kind == "review":
        old = set(before["blocking"])
        return (old.isdisjoint(current["blocking"])
                and all(state["ledger"].get(k, {}).get("status") in ("fixed", "resolved", "waived") for k in old))
    if kind == "verification":
        old = {c["check"] for c in before["failed"]}
        return any(v["id"] > before["receipt"] and old <= {c["name"] for c in v["checks"] if c["klass"] == "pass"}
                   for v in state["verifications"])
    old = set(before["failed"])
    return any(r["n"] >= before["round"] and
               all(r["panel"].get(k, {}).get("status") == "ok" and r["panel"][k].get("runtime", {}).get("session_id")
                   for k in old) for r in state["rounds"])


def recover(card, worktree, kind, child=None):
    repo = gitio.repo_root(worktree)
    settings = policy.load(repo=gitio.main_repo(repo), origin=gitio.origin(repo))
    # No provider, Hermes or network call while holding the card lock. The next transaction
    # rechecks the triggering condition, so a stale child result cannot extend another round.
    previous = card.load().get("recoveries", {}).get(kind, {})
    if child and not previous:
        raise CardError("Run recover for this boundary first and dispatch its linked diagnosis.")
    evidence = confirmed_child(card, child, previous["marker"]) if child else None
    with card.locked() as state:
        incidents = state.setdefault("recoveries", {})
        incident = incidents.get(kind)
        facts = trigger(state, settings, kind)
        if incident and facts and progressed(state, kind, incident["facts"], facts):
            state.setdefault("recovery_archive", []).append({"kind": kind, **incident})
            record(state, "recovery.previous-cleared", kind=kind, marker=incident["marker"])
            incident = None
            del incidents[kind]
            child = None
        if incident and facts and facts != incident.get("observed", incident["facts"]):
            incident["observed"] = facts
            record(state, "recovery.evidence-updated", kind=kind, marker=incident["marker"], evidence=facts)
        if incident and incident.get("child") and (not facts or
                kind == "review" and facts == incident.get("granted_facts", incident["facts"]) or
                kind == "provider" and not incident.get("retry_started")):
            return {"action": "resume", "child": incident["child"], "diagnosis": incident["diagnosis"],
                    "next": incident["next"]}, 0
        if not facts:
            return {"action": "none", "next": "No active failure at this boundary. Run ai-dev-card status."}, 0
        if incident and incident.get("child"):
            if incident.get("handoff") != facts:
                incident["handoff"] = facts
                record(state, "recovery.handoff", kind=kind, child=incident["child"], evidence=facts)
            return {"action": "handoff", "boundary": kind, "facts": facts, "diagnosis": incident["diagnosis"],
                    "child": incident["child"], "next": "Hand the PR and evidence to human review. Name the unresolved "
                    "required check or review, its impact and the decision needed. Keep merge closed. "
                    "Use kanban_block(kind='needs_input') with those details."}, 3
        if not incident:
            signature = hashlib.sha256(json.dumps([card.board, card.id, kind, facts], sort_keys=True).encode()).hexdigest()
            marker = "ai-dev-recovery-" + signature
            incident = incidents[kind] = {"marker": marker, "facts": facts}
            record(state, "recovery.requested", kind=kind, marker=marker, evidence=facts)
        if kind == "provider":
            count = len(incident.get("waits", []))
            choice = wait_decision(incident, facts["failed"])
            if len(incident.get("waits", [])) > count:
                record(state, "recovery.capacity-wait", due=choice["due"], marker=choice["reason"])
            if choice:
                return choice, 0
        if not child:
            body = (f"{incident['marker']}\nInvestigate the {kind} boundary for {card.board}/{card.id}. "
                    "Read the parent history, previous handoff and cited evidence. Diagnose the specific cause "
                    "and validate a different correction path. Return the evidence and exact next step to the "
                    "original worker. Repair only an independent prerequisite within existing authority. "
                    "Keep code fixes with the author, keep acceptance rules and required reviewers intact.\n"
                    + json.dumps(facts, ensure_ascii=False))
            return {"action": "dispatch", "repair_request": {"title": f"Diagnose {kind} failure for {card.id}",
                    "body": body, "idempotency_key": incident["marker"], "skills": ["ai-dev-repair"],
                    "workspace_kind": "scratch", "assignee": "default"}, "next":
                    f"Create or reuse this repair card, link it as parent of {card.id}, record its ID, and wait on "
                    f"that dependency. Once complete run ai-dev-card recover {kind} --resolved CHILD_ID."}, 0
        if kind == "verification":
            state["extra_failures"] += 1
            step = "The author applies the diagnosed correction and runs ai-dev-card verify at the failed stage."
        elif kind == "review":
            state["extra_rounds"] += 1
            step = "Run ai-dev-card review settle, then request changes with the diagnosis and existing fix brief."
        else:
            step = "Run ai-dev-card review panel once; completed reviewer reports remain in the same round."
        incident.update(child=child, diagnosis=evidence, next=step, granted_facts=facts)
        record(state, "recovery.granted", kind=kind, child=child, diagnosis=evidence)
        return {"action": "resume", "next": step, "diagnosis": evidence}, 0


def claim_provider_retry(card, rnd, pending):
    failed = [m["name"] for m in pending if rnd["panel"].get(m["name"], {}).get("status") == "unavailable"]
    if not failed:
        return
    with card.locked() as state:
        incident = state.get("recoveries", {}).get("provider", {})
        waits = incident.get("waits", [])
        observed = incident.get("observed", incident.get("facts", {}))
        if (observed.get("round"), observed.get("tree")) != (rnd["n"], rnd["tree"]):
            raise CardError("This failure has no current recovery decision. Run ai-dev-card recover provider.", code=2)
        if waits and not incident.get("child") and not waits[-1].get("claimed") and waits[-1]["due"] <= time.time():
            waits[-1]["claimed"] = True
            record(state, "recovery.capacity-retry", marker=waits[-1]["marker"], reviewers=failed)
            return
        if not incident.get("child") or incident.get("retry_started"):
            raise CardError("Required reviewers returned errors. Run ai-dev-card recover provider for the next action.", code=2)
        incident["retry_started"] = True
        record(state, "recovery.provider-retry", child=incident["child"], reviewers=failed, round=rnd["n"])
