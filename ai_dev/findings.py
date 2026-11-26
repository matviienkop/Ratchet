"""Explicit dispositions for panel and hosted findings, including bounded late returns."""

import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from . import gitio, review, verify
from .store import CardError, now, record


def contractual(entry, contract):
    value = entry.get("violates")
    return value != "regression" and review._valid_violation(value, contract)


def pending(state):
    result = []
    for key, entry in state["ledger"].items():
        if entry["status"] in ("fixed", "disproved", "waived", "resolved", "deferred"):
            continue
        required = (entry["status"] in (*review.BLOCKING, "candidate")
                    or contractual(entry, state["contract"])
                    or entry.get("severity") in ("medium", "high", "critical"))
        if required and not entry.get("disposition"):
            result.append(key)
    return sorted(result)


def completion_issue(state):
    keys = pending(state)
    if keys:
        return ("Findings still need an explicit disposition: " + ", ".join(keys)
                + ". Run ai-dev-card finding list, then confirm, resolve or waive each finding; "
                  "a follow-up cannot discharge an unmet contract criterion.")
    return None


def import_finding(card, workspace, filename):
    try:
        payload = json.loads(Path(filename).read_bytes())
    except (OSError, ValueError) as error:
        raise CardError("Read the hosted finding JSON and retry ai-dev-card finding import --file FILE.") from error
    if not isinstance(payload, dict) or set(payload) != {"source", "tree", "finding"}:
        raise CardError("Run ai-dev-card finding import --file FILE with source URL, reviewed tree and finding.")
    source = urlsplit(payload["source"] if isinstance(payload["source"], str) else "")
    if source.scheme != "https" or not source.hostname or source.username or source.password:
        raise CardError("Use an HTTPS comment URL without credentials in ai-dev-card finding import --file FILE.")
    report = review.validate_report({"findings": [payload["finding"]]})
    with card.locked() as state:
        _, _, tree = verify.prepare(state, workspace)
        rnd = state["rounds"][-1] if state["rounds"] else None
        if not rnd or rnd["tree"] != tree or payload["tree"] != tree or rnd["outcome"] == "owner":
            raise CardError("Import against the current reviewed tree. Run ai-dev-card review brief; at the cap run ai-dev-card recover review.")
        digest = verify.digest(payload)
        for entry in state["ledger"].values():
            if any(s["digest"] == digest for s in entry.get("sources", [])):
                return {"key": entry["key"], "status": entry["status"], "duplicate": True}
        key = review._merge(state, rnd, "hosted", report)[0]
        entry = state["ledger"][key]
        entry.setdefault("sources", []).append({"url": payload["source"], "tree": tree, "digest": digest, "at": now()})
        # New evidence requires a fresh decision even when its mechanism has been seen before.
        entry.pop("disposition", None)
        if entry["status"] in ("fixed", "disproved", "waived", "resolved", "deferred"):
            entry["status"] = "note" if payload["finding"]["class"] == "blocker" else payload["finding"]["class"]
            entry.update({k: payload["finding"].get(k) for k in review.FIELDS})
        record(state, "finding.imported", key=key, source=payload["source"], tree=tree, digest=digest)
        return {"key": key, "status": entry["status"], "next": "Run ai-dev-card finding list; assess and confirm blocker candidates, then record dispositions."}


def evidence_reference(card, workspace, rnd, evidence):
    path = Path(evidence)
    if path.is_absolute():
        if not path.is_file() or not path.resolve().is_relative_to(card.evidence().resolve()):
            raise CardError("Cite a file in this card's evidence directory with ai-dev-card finding resolve --evidence FILE.")
        if path.resolve().relative_to(card.evidence().resolve()).parts[0].startswith("round-"):
            raise CardError("Cite verification evidence, not the review verdict, with ai-dev-card finding resolve --evidence FILE.")
        digest = verify.file_digest(path)
        current = any(v["tree"] == rnd["tree"] and any(c.get("log") == str(path) and c.get("log_sha256") == digest for c in v["checks"])
                      for v in card.load()["verifications"])
        if not current:
            try:
                proof = json.loads(path.read_bytes())
            except (OSError, ValueError):
                proof = None
            if (not isinstance(proof, dict) or proof.get("tree") != rnd["tree"]
                    or not isinstance(proof.get("observed"), str) or not proof["observed"].strip()):
                raise CardError("Evidence is not tied to this candidate. Run ai-dev-card verify or record manual JSON with the current tree and observed result.")
        return {"path": str(path), "sha256": digest, "tree": rnd["tree"]}
    if ".." in path.parts or gitio.git(workspace, "cat-file", "-t", rnd["commit"] + ":" + str(path), check=False).stdout.strip() != b"blob":
        raise CardError("Cite a tracked file of this candidate with ai-dev-card finding resolve --evidence FILE.")
    return {"path": str(path), "commit": rnd["commit"], "tree": rnd["tree"]}


def resolve(card, workspace, key, outcome, reason, evidence=None, ticket=None):
    if not reason or outcome not in ("resolved", "deferred", "not-planned"):
        raise CardError("Run ai-dev-card finding resolve KEY --outcome resolved|deferred|not-planned --reason TEXT --evidence FILE or --ticket URL.")
    with card.locked() as state:
        workspace, settings, tree = verify.prepare(state, workspace)
        rnd = state["rounds"][-1] if state["rounds"] else None
        entry = state["ledger"].get(key)
        if not entry or not rnd or rnd["tree"] != tree:
            raise CardError("Run ai-dev-card review brief and ai-dev-card finding list for the current candidate.")
        proof = {"outcome": outcome, "reason": reason, "tree": tree, "at": now()}
        if outcome == "deferred":
            site = urlsplit(ticket or "")
            if site.scheme != "https" or not site.hostname or site.username or site.password:
                raise CardError("Run ai-dev-card finding resolve KEY --outcome deferred --ticket HTTPS_URL --reason TEXT.")
            late_regression = (entry["status"] == "confirmed" and entry.get("sources")
                               and len(state.get("late_returns", [])) >= 2)
            if (contractual(entry, state["contract"]) or entry["status"] in ("gap", "candidate")
                    or entry["status"] == "confirmed" and not late_regression):
                raise CardError("This finding still blocks acceptance. Fix it or use ai-dev-card finding waive KEY --owner-ref MESSAGE --reason TEXT; a ticket alone cannot discharge it.")
            proof["ticket"] = ticket
        elif outcome == "not-planned":
            if contractual(entry, state["contract"]) or entry.get("severity") != "low" or entry["status"] not in ("note", "discovery"):
                raise CardError("Only low non-contract notes can be not-planned. Run ai-dev-card finding list and confirm, fix or defer this finding.")
        else:
            if entry["status"] not in ("gap", "note", "discovery") or not evidence:
                raise CardError("A defect needs a reproduction, not a closure claim. Run ai-dev-card confirm --finding KEY -- COMMAND; for gaps use finding resolve --evidence FILE.")
            if not verify.passing_receipt(state, settings, tree, "pre_review"):
                raise CardError("Run ai-dev-card verify before recording current-candidate evidence.")
            proof["evidence"] = evidence_reference(card, workspace, rnd, evidence)
        entry["disposition"] = proof
        entry["status"] = "deferred" if outcome == "deferred" else "resolved"
        entry["needs_assessment"] = False
        entry["history"].append({"round": rnd["n"], "status": entry["status"], "by": "disposition", **proof})
        record(state, "finding.disposition", key=key, **proof)
        return {"key": key, "status": entry["status"], "disposition": proof}


def late_return(card, workspace, reason):
    if not reason:
        raise CardError("Run ai-dev-card finding return --reason TEXT before the native late return.")
    with card.locked() as state:
        _, _, tree = verify.prepare(state, workspace)
        rnd = state["rounds"][-1] if state["rounds"] else None
        keys = sorted(k for k, e in state["ledger"].items() if e["status"] == "confirmed" and e.get("sources"))
        if not rnd or rnd["tree"] != tree or rnd["outcome"] != "accept" or not keys:
            raise CardError("Confirm the late defect on the accepted tree with ai-dev-card confirm --finding KEY -- COMMAND.")
        returns = state.setdefault("late_returns", [])
        run = os.environ.get("HERMES_KANBAN_RUN_ID") or os.environ.get("HERMES_SESSION_ID")
        if not run:
            raise CardError("Run ai-dev-card finding return inside the active Hermes worker or planner session.")
        if returns and returns[-1]["tree"] == tree and returns[-1].get("run") == run:
            return returns[-1]
        if len(returns) >= 2:
            raise CardError("Two late returns are already recorded. Use finding resolve --outcome deferred --ticket URL for residual regressions; contract violations require finding waive with an owner reference.")
        entry = {"number": len(returns) + 1, "tree": tree, "run": run, "findings": keys, "reason": reason, "at": now(),
                 "next": "Call kanban_request_changes with these finding keys and their reproduction evidence; the planner can use native reopen-review if the card is already done."}
        returns.append(entry)
        record(state, "review.late-return", **entry)
        return entry
