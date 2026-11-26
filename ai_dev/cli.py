"""`ai-dev-card`: what a worker runs inside its card. Every command prints JSON and names the next step."""

import argparse
import json
import os
from pathlib import Path
import shutil
import sys

from . import __version__, gate, gitio, policy as policies, release, review, severity, stands, verify
from . import recovery, recovery_wake, findings, continuity
from .stand_cli import add_command as add_stand_command
from .history import add_commands as add_history_commands
from .store import Card, CardError, home, identity


def _card(args):
    return Card(*identity(args.card, args.board))


def _workspace(args):
    return args.workspace or os.environ.get("HERMES_KANBAN_WORKSPACE") or os.getcwd()


def status(args):
    card = _card(args)
    state = card.load()
    lane = gate.role(plugin_dir=os.environ.get("AI_DEV_PLUGIN_DIR"))
    result = {"card": card.id, "board": card.board, "role": lane, "contract": bool(state["contract"]),
              "frozen": state["frozen"], "base": state["base"], "counters": state["counters"],
              "rounds": [{k: r.get(k) for k in ("n", "mode", "outcome", "tree", "blocking")} for r in state["rounds"]]}
    tree = settings = None
    try:
        workspace = gitio.repo_root(_workspace(args))
        tree = gitio.snapshot(workspace)
        settings = policies.load(repo=gitio.main_repo(workspace), origin=gitio.origin(workspace))
        result["reviewer"] = settings["review"]["reviewer"]
        result["delivery"] = settings["delivery"]
        rule = release.selected(state.get("contract"), settings)
        if rule:
            result["delivery"] = "release"
            result["release"] = rule
            result["release_receipt"] = state.get("release_receipt")
    except CardError as error:
        result["workspace_problem"] = str(error)
    result["tree"] = tree
    last = state["verifications"][-1] if state["verifications"] else None
    if last:
        result["last_verification"] = {
            "id": last["id"], "stage": last["stage"], "result": last["result"], "current_tree": last["tree"] == tree,
            "problem": last["problem"],
            "failed": [{k: c[k] for k in ("name", "klass", "problem", "log")} for c in last["checks"] if c["klass"] != "pass"]}
    rnd = state["rounds"][-1] if state["rounds"] else None
    verified = bool(tree and settings and state["contract"] and verify.passing_receipt(state, settings, tree, "pre_review"))
    if not state["contract"]:
        step = "Write the contract and run `ai-dev-card contract set --file FILE`."
    elif lane == "reviewer":
        if rnd and rnd["tree"] == tree and rnd["outcome"]:
            step = f"The round settled as {rnd['outcome']}. Follow the `next` line of `ai-dev-card review settle`."
        else:
            step = "Run `ai-dev-card review brief` and review the candidate."
    elif rnd and rnd["outcome"] == "changes" and rnd["tree"] == tree:
        step = "The reviewer asked for changes. Read `ai-dev-card review fix-brief`, fix every item, then verify."
    elif verified:
        step = "The tree is verified. Call kanban_request_review."
    elif last and last["tree"] == tree and last["result"] == "environment":
        result["repair_requests"] = last.get("repair_requests", [])
        step = "Classify the failure from the log, then follow the recovery section of the ai-dev-card skill."
    else:
        step = "Implement the contract, then run `ai-dev-card verify`."
    if settings:
        for boundary in ("provider", "review", "verification"):
            if recovery.trigger(state, settings, boundary):
                incident = state.get("recoveries", {}).get(boundary, {})
                if boundary == "review" and incident.get("child") and incident["facts"]["round"] == rnd["n"]:
                    step = "Run ai-dev-card review settle using the completed diagnosis."
                elif boundary == "provider" and incident.get("child") and not incident.get("retry_started"):
                    step = "Run ai-dev-card review panel once using the completed diagnosis."
                elif boundary != "verification" or not verified:
                    step = f"Run ai-dev-card recover {boundary}. Resume with the linked child ID when it is done."
                break
    result["next"] = step
    return result, 0


def contract(args):
    card = _card(args)
    if args.action == "inherit":
        if not args.from_card:
            raise CardError("Run ai-dev-card contract inherit --from-card ID [--from-board BOARD].")
        source = Card(*identity(args.from_card, args.from_board or card.board))
        return continuity.inherit(card, source), 0
    if args.action == "show":
        state = card.load()
        return {"contract": state["contract"], "frozen": state["frozen"]}, 0
    try:
        data = json.loads(Path(args.file).read_bytes())
    except (OSError, ValueError) as error:
        raise CardError(f"Cannot read the contract {args.file}: {error}") from error
    with card.locked() as state:
        return verify.set_contract(state, data, owner_ref=args.owner_ref), 0


def run_verify(args):
    receipt, code = verify.verify(_card(args), _workspace(args), stage=args.stage, rerun=args.rerun)
    hint = {0: "Passed. Call kanban_request_review." if args.stage == "pre_review" else "Passed. Run ai-dev-card review settle for the delivery step.",
            1: "A check failed on your change. Read the log, fix the code and verify again.",
            2: "The checks could not judge the change (environment, or a failure that the base has too). "
               "Follow the recovery section of the ai-dev-card skill with the receipt and repair_requests."}[code]
    return {**receipt, "next": hint}, code


def run_review(args):
    card = _card(args)
    if args.action == "brief":
        return review.brief(card, _workspace(args)), 0
    if args.action == "submit":
        reviewer = args.reviewer or os.environ.get("AI_DEV_REVIEWER")
        return review.submit(card, args.report, reviewer), 0
    if args.action == "panel":
        result = review.panel(card, _workspace(args))
        if any(r.get("status") == "unavailable" for r in result["panel"]):
            result.setdefault("next", "If the panel is required run ai-dev-card recover provider; otherwise settle this round.")
        return result, 0
    if args.action == "settle":
        return review.settle(card, _workspace(args)), 0
    if args.action == "fix-brief":
        return review.fix_brief(card), 0
    return review.extend(card, args.owner_ref, rounds=args.rounds, failures=args.failures), 0


def run_confirm(args):
    files = []
    for item in args.file:
        source, _, destination = item.partition(":")
        files.append((source, destination or Path(source).name))
    return review.confirm(_card(args), _workspace(args), args.finding, files=files, argv=args.argv,
                          timeout=args.timeout, cannot=args.cannot_reproduce), 0


def finding(args):
    card = _card(args)
    if args.action == "waive":
        return review.waive(card, args.key, args.owner_ref, args.reason), 0
    if args.action == "assess":
        return review.assess(card, _workspace(args), args.key, args.severity, args.impact, args.evidence), 0
    if args.action == "import":
        return findings.import_finding(card, _workspace(args), args.file), 0
    if args.action == "resolve":
        return findings.resolve(card, _workspace(args), args.key, args.outcome, args.reason, args.evidence, args.ticket), 0
    if args.action == "return":
        return findings.late_return(card, _workspace(args), args.reason), 0
    ledger = card.load()["ledger"]
    return {"findings": [{**severity.describe(e), **{k: e.get(k) for k in ("violates", "location", "first_round", "sources", "disposition", "confirm", "inherited_from")}}
                         for e in ledger.values()]}, 0


def doctor(args):
    problems, facts = [], {"version": __version__, "home": str(home()), "policy": str(policies.policy_file())}
    for tool in ("git",):
        if not shutil.which(tool):
            problems.append(f"{tool} is not on PATH.")
    try:
        settings = policies.load()
        facts["default_checks"] = [c["name"] for c in settings["checks"]]
        facts["review"] = {k: settings["review"][k] for k in ("max_rounds", "reviewer", "panel_required")}
        facts["panel"] = [m["name"] for m in settings["review"]["panel"]]
        for member in settings["review"]["panel"]:
            if not shutil.which(member["argv"][0]):
                problems.append(f"Panel reviewer {member['name']}: {member['argv'][0]} is not on PATH.")
        if settings["project"]:
            facts["stands"] = stands.available(settings["project"])
    except CardError as error:
        problems.append(str(error))
    if not policies.policy_file().is_file():
        problems.append(f"No policy file at {policies.policy_file()}: no default checks are defined.")
    facts["role"] = gate.role(plugin_dir=os.environ.get("AI_DEV_PLUGIN_DIR"))
    facts["problems"] = problems
    return facts, 1 if problems else 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="ai-dev-card", description=__doc__)
    parser.add_argument("--card", help="Card id. Defaults to HERMES_KANBAN_TASK.")
    parser.add_argument("--board", help="Board slug. Defaults to HERMES_KANBAN_BOARD, then `default`.")
    parser.add_argument("--workspace", help="Working tree. Defaults to HERMES_KANBAN_WORKSPACE, then the current directory.")
    commands = parser.add_subparsers(dest="command", required=True)
    add_history_commands(commands, _card)
    add_stand_command(commands, _card, _workspace)
    commands.add_parser("recovery-wake", help="Wake due provider-capacity waits.").set_defaults(run=lambda _: recovery_wake.tick())
    sub = commands.add_parser("recover", help="Dispatch one diagnosis and resume the failed acceptance step.")
    sub.add_argument("boundary", choices=recovery.KINDS)
    sub.add_argument("--resolved", metavar="CHILD_ID", help="Read the completed native diagnosis card.")
    sub.set_defaults(run=lambda a: recovery.recover(_card(a), _workspace(a), a.boundary, a.resolved))
    commands.add_parser("status", help="Where the card stands and the next step.").set_defaults(run=status)
    commands.add_parser("doctor", help="Check the installation and the policy.").set_defaults(run=doctor)

    sub = commands.add_parser("contract", help="Criteria, scenarios and checks the card is accepted against.")
    sub.add_argument("action", choices=("set", "show", "inherit"))
    sub.add_argument("--file")
    sub.add_argument("--owner-ref", help="The owner's message that approved weakening a frozen contract.")
    sub.add_argument("--from-card")
    sub.add_argument("--from-board")
    sub.set_defaults(run=contract)

    sub = commands.add_parser("verify", help="Run the approved checks on the working tree.")
    sub.add_argument("--stage", choices=policies.STAGES, default="pre_review")
    sub.add_argument("--rerun", action="store_true", help="Run again even when this tree already passed.")
    sub.set_defaults(run=run_verify)

    sub = commands.add_parser("release", help="Verify published packages against a merged pull request.")
    sub.add_argument("action", choices=("check-pr", "verify"))
    sub.add_argument("--pr", required=True)
    sub.set_defaults(run=lambda args: (release.verify_release(_card(args), _workspace(args), args.pr, before_merge=args.action == "check-pr"), 0))

    sub = commands.add_parser("review", help="Review rounds.")
    sub.add_argument("action", choices=("brief", "submit", "panel", "settle", "fix-brief", "extend"))
    sub.add_argument("--report")
    sub.add_argument("--reviewer")
    sub.add_argument("--rounds", type=int, default=0)
    sub.add_argument("--failures", type=int, default=0, help="Extra failed verifications the owner allows.")
    sub.add_argument("--owner-ref")
    sub.set_defaults(run=run_review)

    sub = commands.add_parser("confirm", help="Reproduce a blocker on the candidate and on the base.")
    sub.add_argument("--finding", required=True)
    sub.add_argument("--file", action="append", default=[], metavar="SOURCE[:REPO_PATH]",
                     help="A test file to place into both checkouts before the command runs.")
    sub.add_argument("--timeout", type=int)
    sub.add_argument("--cannot-reproduce", metavar="REASON")
    sub.add_argument("argv", nargs=argparse.REMAINDER, help="-- COMMAND that fails when the defect is present.")
    sub.set_defaults(run=run_confirm)

    sub = commands.add_parser("finding", help="The findings ledger.")
    sub.add_argument("action", choices=("list", "waive", "assess", "import", "resolve", "return"))
    sub.add_argument("key", nargs="?")
    sub.add_argument("--owner-ref")
    sub.add_argument("--reason")
    sub.add_argument("--severity", choices=severity.LEVELS)
    sub.add_argument("--impact")
    sub.add_argument("--evidence")
    sub.add_argument("--file")
    sub.add_argument("--outcome", choices=("resolved", "deferred", "not-planned"))
    sub.add_argument("--ticket")
    sub.set_defaults(run=finding)

    args = parser.parse_args(argv)
    if getattr(args, "argv", None) and args.argv[0] == "--":
        args.argv = args.argv[1:]
    try:
        if args.command == "contract" and args.action == "set" and not args.file:
            raise CardError("contract set needs --file.")
        if args.command == "review" and args.action == "submit" and not args.report:
            raise CardError("review submit needs --report.")
        if args.command == "review" and args.action == "extend" and (not args.owner_ref or args.rounds + args.failures < 1):
            raise CardError("review extend needs --owner-ref and --rounds or --failures.")
        if args.command == "finding" and args.action == "waive" and not (args.key and args.owner_ref and args.reason):
            raise CardError("finding waive needs KEY, --owner-ref and --reason.")
        if args.command == "finding" and args.action == "import" and not args.file:
            raise CardError("Run ai-dev-card finding import --file FILE.")
        result, code = args.run(args)
    except CardError as error:
        print(f"ai-dev-card: {error}", file=sys.stderr)
        return error.code
    print(json.dumps(result, indent=1, ensure_ascii=False))
    return code
