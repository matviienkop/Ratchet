"""Versioned task analysis and implementation plans, independent of the intake channel."""
import json
from pathlib import Path

from . import gitio
from .redact import sanitize
from .store import CardError, now, record


def text(value):
    return isinstance(value, str) and bool(value.strip())


def strings(value, nonempty=False):
    return isinstance(value, list) and (bool(value) or not nonempty) and all(text(v) for v in value)


def validate(data):
    if not isinstance(data, dict):
        raise CardError("Run ai-dev-card analysis set --file FILE with the structured task analysis.")
    source = data.get("source")
    if (not isinstance(source, dict) or source.get("kind") not in ("chat", "ticket", "followup")
            or not all(text(source.get(k)) for k in ("reference", "text"))):
        raise CardError("Record source kind, reference and original text with ai-dev-card analysis set --file FILE.")
    if not all(text(data.get(k)) for k in ("title", "problem", "outcome", "repository")):
        raise CardError("Analysis needs title, problem, outcome and repository. Run ai-dev-card analysis set --file FILE.")
    for field in ("scope", "non_goals", "criteria", "constraints", "facts", "hypotheses"):
        if not strings(data.get(field), field == "scope"):
            raise CardError(f"Analysis {field} must be a list of statements. Run ai-dev-card analysis set --file FILE.")
    for field, required in (("evidence", ("source", "question", "summary")), ("unknowns", ("question", "next_step")),
                            ("risks", ("area", "assessment"))):
        if not isinstance(data.get(field), list):
            raise CardError(f"Analysis needs a {field} list. Run ai-dev-card analysis set --file FILE.")
        for row in data[field]:
            if not isinstance(row, dict) or not all(text(row.get(k)) for k in required):
                raise CardError(f"Complete the {field} records with ai-dev-card analysis set --file FILE.")
    for evidence in data["evidence"]:
        if (evidence.get("status") not in ("checked", "unavailable", "not_needed")
                or evidence["status"] == "checked" and not text(evidence.get("reference"))):
            raise CardError("Evidence needs status and a reference for checked sources. Run ai-dev-card analysis set --file FILE.")
    if any(type(q.get("blocking")) is not bool for q in data["unknowns"]):
        raise CardError("Mark each unknown blocking=true or false. Run ai-dev-card analysis set --file FILE.")
    split = data.get("decomposition")
    if (not isinstance(split, dict) or split.get("decision") not in ("single", "split", "research")
            or not text(split.get("reason")) or not strings(split.get("children"))):
        raise CardError("Record decomposition decision, reason and child references. Run ai-dev-card analysis set --file FILE.")
    budget = data.get("budget")
    if (not isinstance(budget, dict) or type(budget.get("minutes")) is not int or not 1 <= budget["minutes"] <= 240
            or type(budget.get("used_minutes")) not in (int, float) or not 0 <= budget["used_minutes"] <= 100000):
        raise CardError("Record a 1..240 minute research budget and actual used_minutes. Run ai-dev-card analysis set --file FILE.")
    if data.get("readiness") not in ("ready", "needs_input", "blocked", "split"):
        raise CardError("Choose analysis readiness ready, needs_input, blocked or split with ai-dev-card analysis set --file FILE.")
    if data["readiness"] == "ready" and (not data["criteria"] or not data["evidence"] or not data["risks"]
            or any(q["blocking"] for q in data["unknowns"]) or split["decision"] != "single"):
        raise CardError("Resolve blocking unknowns, record criteria/evidence/risks and select one implementation scope before marking analysis ready.")
    if data["readiness"] == "split" and (split["decision"] != "split" or not split["children"]):
        raise CardError("Split analysis needs linked child references. Run ai-dev-card analysis set --file FILE.")
    return sanitize(data)


def save_analysis(card, data):
    data = validate(data)
    with card.locked() as state:
        revisions = state.setdefault("analyses", [])
        if revisions and data["source"] != revisions[0]["data"]["source"]:
            raise CardError("Preserve the original source unchanged. Put new requests in evidence and update analysis with ai-dev-card analysis set --file FILE.")
        if revisions and revisions[-1]["data"] == data:
            return revisions[-1]
        row = {"revision": len(revisions) + 1, "at": now(), "data": data}
        revisions.append(row)
        record(state, "analysis." + data["readiness"], revision=row["revision"], source_kind=data["source"]["kind"])
        return row


def save_plan(card, data):
    if (not isinstance(data, dict) or type(data.get("analysis_revision")) is not int
            or not strings(data.get("steps"), True) or not strings(data.get("validation"), True)
            or not text(data.get("rollback")) or not text(data.get("authority"))):
        raise CardError("Plan needs analysis_revision, steps, validation, rollback and authority. Run ai-dev-card plan set --file FILE.")
    with card.locked() as state:
        analysis = (state.get("analyses") or [{}])[-1]
        if analysis.get("data", {}).get("readiness") != "ready" or data["analysis_revision"] != analysis.get("revision"):
            raise CardError("Complete the current analysis before planning. Run ai-dev-card analysis show, then analysis set --file FILE.")
        plans = state.setdefault("plans", [])
        data = sanitize(data)
        if plans and plans[-1]["data"] == data:
            return plans[-1]
        row = {"revision": len(plans) + 1, "at": now(), "data": data}
        plans.append(row)
        record(state, "plan.recorded", revision=row["revision"], analysis_revision=analysis["revision"])
        return row


def issue(state, settings, workspace):
    if not settings.get("require_task_analysis"):
        return None
    analysis = (state.get("analyses") or [{}])[-1]
    data = analysis.get("data", {})
    if data.get("readiness") != "ready":
        return "The task is not ready for implementation. Ask the planner to complete ai-dev-card analysis set --file FILE."
    plan = (state.get("plans") or [{}])[-1].get("data", {})
    if plan.get("analysis_revision") != analysis.get("revision"):
        return "The implementation plan is missing or stale. Run ai-dev-card plan set --file FILE after analyzing the current request."
    contract = state.get("contract") or {}
    if data["criteria"] != contract.get("criteria") or data["non_goals"] != contract.get("non_goals", []):
        return "The contract and task analysis disagree. Reconcile ai-dev-card analysis set and contract set, then record the updated plan."
    if data["repository"] not in (str(gitio.main_repo(workspace)), gitio.origin(workspace)):
        return "Analysis targets another repository. Correct it with ai-dev-card analysis set --file FILE and record a new plan."
    return None


def command(args, card, kind):
    if args.action == "show":
        key = "analyses" if kind == "analysis" else "plans"
        return {kind: (card.load().get(key) or [None])[-1]}, 0
    try:
        data = json.loads(Path(args.file).read_bytes())
    except (OSError, ValueError, TypeError) as error:
        raise CardError(f"Read the JSON input, then run ai-dev-card {kind} set --file FILE.") from error
    return (save_analysis(card, data) if kind == "analysis" else save_plan(card, data)), 0


def add_commands(commands, get_card):
    for kind in ("analysis", "plan"):
        sub = commands.add_parser(kind, help="Record or inspect " + kind + " before implementation.")
        sub.add_argument("action", choices=("set", "show"))
        sub.add_argument("--file")
        sub.set_defaults(run=lambda args, kind=kind: command(args, get_card(args), kind))
