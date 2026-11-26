"""Check the project's validation environment on its pinned base before implementation."""
from contextlib import ExitStack
import re
import subprocess
import time

from . import gitio, stands, verify
from .redact import sanitize
from .store import CardError, now, record

ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def validate(config):
    if config is None:
        return
    if not isinstance(config, dict) or set(config) - {"checks", "no_tests_reason"}:
        raise CardError("Configure preflight checks and optional no_tests_reason in the project policy; run ai-dev-card doctor.")
    checks = config.get("checks")
    from .policy import validate_checks
    if not isinstance(checks, list) or not checks:
        raise CardError("Configure at least one preflight check in the project policy; run ai-dev-card doctor.")
    validate_checks([{k: v for k, v in c.items() if k != "tests_pattern"} if isinstance(c, dict) else c for c in checks], "Preflight")
    for check in checks:
        if "stage" in check:
            raise CardError("Preflight checks have no review stage. Update the project policy and run ai-dev-card doctor.")
        if "tests_pattern" in check:
            pattern = check["tests_pattern"]
            try:
                if not isinstance(pattern, str) or not pattern.strip():
                    raise ValueError()
                re.compile(pattern)
            except (ValueError, re.error):
                raise CardError("Set a valid tests_pattern in project preflight policy; run ai-dev-card doctor.")
    reason = config.get("no_tests_reason")
    if reason is not None and (not isinstance(reason, str) or not reason.strip()):
        raise CardError("Explain no_tests_reason in project preflight policy; run ai-dev-card doctor.")
    if not any(c.get("tests_pattern") for c in checks) and not reason:
        raise CardError("Preflight needs a tests_pattern or an explicit no_tests_reason in project policy; run ai-dev-card doctor.")


def signature(state, settings):
    return verify.digest([state["base"], settings["project"], settings.get("preflight"), settings["check_timeout"]])


def issue(state, settings):
    if settings.get("preflight") is None:
        return None
    receipt = state.get("preflight") or {}
    if receipt.get("signature") != signature(state, settings):
        return "Run ai-dev-card preflight before implementation; its project environment evidence is missing or stale."
    if not receipt.get("ready"):
        return "Preflight needs repair or baseline-failure assessment. Run ai-dev-card preflight show, then preflight acknowledge or preflight --rerun."
    return None


def refresh(receipt):
    receipt["ready"] = all(c["klass"] == "pass" or (c["klass"] == "baseline_failure" and c.get("assessment"))
                           for c in receipt["checks"])


def count_tests(log, pattern):
    text = ANSI.sub("", log.read_text(errors="replace"))
    matches = list(re.finditer(pattern, text, re.MULTILINE))
    if "count" in re.compile(pattern).groupindex:
        try:
            counts = [int(m.group("count")) for m in matches]
            return sum(counts) if all(n >= 0 for n in counts) else None
        except (ValueError, TypeError):
            return None
    return len(matches)


def run(card, workspace, rerun=False):
    with card.locked() as state:
        workspace, settings, tree = verify.prepare(state, workspace)
        config = settings.get("preflight")
        if config is None:
            raise CardError("Ask the planner to configure project preflight checks, then run ai-dev-card preflight.")
        sig = signature(state, settings)
        previous = state.get("preflight")
        if previous and previous.get("signature") == sig and not rerun:
            return previous, 0 if previous["ready"] else 2
        number = 1 + state.get("preflight_sequence", 0)
        folder = card.evidence(f"preflight-{number}")
        started, clock = now(), time.monotonic()
        results, prepared, restore = [], {}, []
        with ExitStack() as resources:
            base_dir = resources.enter_context(gitio.checkout(workspace, state["base"]))
            base_tree = gitio.snapshot(base_dir)
            try:
                for check in config["checks"]:
                    log = folder / (check["name"] + ".log")
                    timeout = check.get("timeout") or settings["check_timeout"]
                    if check.get("stand"):
                        code, problem, klass, seconds = verify._stand_check(settings, check, workspace, state["base"], b"",
                                                                            log, timeout, prepared, resources, restore)
                    else:
                        code, problem, klass, seconds = verify.run(check["argv"], base_dir, log, timeout)
                    count = count_tests(log, check["tests_pattern"]) if check.get("tests_pattern") and log.exists() else None
                    if check.get("tests_pattern") and (count is None or count <= 0):
                        klass, problem = "environment", "No positive test discovery evidence. Inspect the log and project tests_pattern, then run ai-dev-card preflight --rerun."
                    elif klass == "fail":
                        klass = "baseline_failure" if check.get("tests_pattern") else "environment"
                    results.append({"name": check["name"], "stand": check.get("stand"), "klass": klass,
                                    "exit_code": code, "seconds": seconds, "problem": problem, "test_count": count,
                                    "log": str(log), "log_sha256": verify.file_digest(log) if log.exists() else None})
            finally:
                for driver in restore:
                    try:
                        code, tail = stands.call(driver, "shutdown")
                    except (OSError, subprocess.TimeoutExpired) as error:
                        code, tail = 1, str(error)
                    if code:
                        results.append({"name": "shutdown-" + driver.name, "stand": driver.name,
                                        "klass": "environment", "problem": sanitize(tail)})
            if gitio.snapshot(base_dir) != base_tree:
                results.append({"name": "base-cleanliness", "klass": "environment",
                                "problem": "A check modified base source files. Repair the check and run ai-dev-card preflight --rerun."})
        receipt = {"id": number, "signature": sig, "base": state["base"], "observed_tree": tree,
                   "before_candidate_changes": tree == base_tree, "started": started, "at": now(),
                   "duration_seconds": time.monotonic() - clock, "checks": results,
                   "no_tests_reason": config.get("no_tests_reason")}
        refresh(receipt)
        state["preflight"], state["preflight_sequence"] = receipt, number
        state.setdefault("preflight_history", []).append(receipt)
        record(state, "preflight.ready" if receipt["ready"] else "preflight.blocked", id=number, base=state["base"])
        return receipt, 0 if receipt["ready"] else 2


def acknowledge(card, workspace, name, reason, evidence):
    if not reason or not reason.strip() or not evidence or not evidence.strip():
        raise CardError("Run ai-dev-card preflight acknowledge --check NAME --reason TEXT --evidence REFERENCE after inspecting the baseline failure.")
    with card.locked() as state:
        _, settings, _ = verify.prepare(state, workspace)
        receipt = state.get("preflight") or {}
        if receipt.get("signature") != signature(state, settings):
            raise CardError("Project settings changed. Run ai-dev-card preflight --rerun.")
        check = next((c for c in receipt.get("checks", []) if c["name"] == name), None)
        if not check or check["klass"] != "baseline_failure":
            raise CardError("Only executed baseline test failures can be assessed. Repair the environment and run ai-dev-card preflight --rerun.")
        check["assessment"] = sanitize({"at": now(), "reason": reason, "evidence": evidence})
        refresh(receipt)
        state["preflight_history"][-1] = receipt
        record(state, "preflight.assessed", id=receipt["id"], check=name, **check["assessment"])
        return receipt, 0 if receipt["ready"] else 2


def command(args, card, workspace):
    if args.action == "show":
        result = card.load().get("preflight")
        return {"preflight": result, "next": "Inspect the checks; run ai-dev-card preflight --rerun after environment changes."}, 0
    receipt, code = (acknowledge(card, workspace, args.check, args.reason, args.evidence)
                     if args.action == "acknowledge" else run(card, workspace, args.rerun))
    return {**receipt, "next": "Implement the contract, then run ai-dev-card verify." if code == 0 else
            "Inspect logs. Repair unavailable tools or missing tests and run ai-dev-card preflight --rerun. "
            "For an understood unrelated baseline defect, record preflight acknowledge --check NAME --reason TEXT --evidence REFERENCE; "
            "triage the defect under the existing finding rules."}, code
