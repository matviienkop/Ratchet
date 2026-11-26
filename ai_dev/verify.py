"""Run the approved checks on the exact working tree and keep a receipt the gate can read."""

from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import time

from . import gitio, policy as policies, release, stands
from .redact import sanitize
from .store import CardError, now, record


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def validate_contract(contract):
    if not isinstance(contract, dict) or set(contract) - {"criteria", "scenarios", "checks", "non_goals", "release"}:
        raise CardError("A contract accepts criteria, scenarios, checks, non_goals and optional release.")
    criteria = contract.get("criteria")
    if not isinstance(criteria, list) or not criteria or not all(isinstance(c, str) and c.strip() for c in criteria):
        raise CardError("A contract needs criteria: a nonempty list of observable outcomes.")
    if not isinstance(contract.get("scenarios", []), list):
        raise CardError("Scenarios must be a list. Run ai-dev-card contract set --file FILE.")
    seen = set()
    for scenario in contract.get("scenarios", []):
        if (not isinstance(scenario, dict) or set(scenario) - {"id", "operation", "expected", "check", "manual", "criteria"}
                or not all(isinstance(scenario.get(k), str) and scenario[k].strip() for k in ("id", "operation", "expected"))
                or not policies.NAME.fullmatch(scenario["id"]) or scenario["id"] in seen):
            raise CardError("Each scenario needs a unique lowercase id, an operation and the expected result.")
        links = scenario.get("criteria", [])
        if (not isinstance(links, list) or any(type(n) is not int or not 1 <= n <= len(criteria) for n in links)):
            raise CardError("Scenario criteria must list 1-based criterion numbers. Run ai-dev-card contract set --file FILE.")
        if "manual" in scenario and (not isinstance(scenario["manual"], str) or not scenario["manual"].strip()):
            raise CardError("Scenario manual must name an evidence file. Run ai-dev-card contract set --file FILE.")
        if "check" in scenario and "manual" in scenario:
            raise CardError("Choose check or manual evidence for a scenario. Run ai-dev-card contract set --file FILE.")
        seen.add(scenario["id"])
    policies.validate_checks(contract.get("checks", []), "Contract")
    for scenario in contract.get("scenarios", []):
        if "check" in scenario and not isinstance(scenario["check"], str):
            raise CardError("A scenario's check is the name of the check that proves it.")
    if not all(isinstance(g, str) and g.strip() for g in contract.get("non_goals", [])):
        raise CardError("non_goals is a list of strings.")
    return {"criteria": criteria, "scenarios": contract.get("scenarios", []),
            "checks": contract.get("checks", []), "non_goals": contract.get("non_goals", []),
            **({"release": release.validate(contract["release"])} if "release" in contract else {})}


def set_contract(state, contract, owner_ref=None):
    """Free to change until the first verification. Afterwards only additions pass without an owner reference."""
    new = validate_contract(contract)
    old = state["contract"]
    if old and state["frozen"] and new != old:
        keeps = (new["criteria"][:len(old["criteria"])] == old["criteria"]
                 and all(any(all(candidate.get(k) == v for k, v in row.items()) for candidate in new["scenarios"])
                         for row in old["scenarios"])
                 and all(row in new["checks"] for row in old["checks"])
                 and ("release" not in old or old["release"] == new.get("release")))
        if not keeps and not owner_ref:
            raise CardError(
                "The contract is frozen. Adding criteria, scenarios or checks is allowed; removing or changing one "
                "needs the owner: call kanban_block(kind='needs_input') with the proposed change, and after the "
                "owner agrees repeat this command with --owner-ref naming their message.")
        record(state, "contract.amended", owner_ref=owner_ref, weakened=not keeps, digest=digest(new))
    elif new != old:
        record(state, "contract.set", digest=digest(new))
    state["contract"] = new
    return {"digest": digest(new), "frozen": state["frozen"], "contract": new}


def checks_for(settings, contract, stage):
    merged, names = [], set()
    for check in [*settings["checks"], *contract["checks"]]:
        if check.get("stage", "pre_review") != stage:
            continue
        if check["name"] in names:
            raise CardError(f"Check name {check['name']} is defined twice. Rename the contract check.")
        names.add(check["name"])
        merged.append(check)
    return merged


def scenario_evidence(contract, settings):
    checks = [c for stage in policies.STAGES for c in checks_for(settings, contract, stage)]
    names = {c["name"] for c in checks}
    covered = set()
    for scenario in contract.get("scenarios", []):
        name = scenario.get("check")
        if name is not None and name not in names:
            raise CardError(f"Scenario {scenario['id']} names unknown check {name!r}. Run ai-dev-card contract set --file FILE with an existing check.")
        if settings.get("require_scenario_evidence", True):
            if not name and not scenario.get("manual"):
                raise CardError(f"Scenario {scenario['id']} needs check or manual evidence. Run ai-dev-card contract set --file FILE.")
            covered.update(scenario.get("criteria", []))
    missing = set(range(1, len(contract["criteria"]) + 1)) - covered
    if settings.get("require_scenario_evidence", True) and missing:
        raise CardError(f"Criteria {sorted(missing)} have no scenario evidence. Add scenario.criteria links with ai-dev-card contract set --file FILE.")


def manual_evidence(card, state, tree):
    proofs = {}
    for scenario in state["contract"].get("scenarios", []):
        if not scenario.get("manual"):
            continue
        path = Path(scenario["manual"])
        if not path.is_absolute() or not path.resolve().is_relative_to(card.evidence().resolve()):
            raise CardError("Manual proof must be a JSON file in this card's evidence directory. Run ai-dev-card status and record the current tree, scenario and result.")
        try:
            proof = json.loads(path.read_bytes())
        except (OSError, ValueError) as error:
            raise CardError("Cannot read manual proof. Run ai-dev-card status and record evidence for this candidate.") from error
        if (not isinstance(proof, dict) or proof.get("tree") != tree or proof.get("scenario") != scenario["id"]
                or proof.get("result") != "pass" or not isinstance(proof.get("observed"), str) or not proof["observed"].strip()):
            raise CardError("Manual proof needs current tree, scenario, result=pass and observed behavior. Run ai-dev-card status and repeat the manual check.")
        proofs[scenario["id"]] = {"path": str(path), "sha256": file_digest(path), "tree": tree}
    return proofs


def run(argv, cwd, log_path, timeout):
    """Returns (exit_code, problem, klass, seconds). klass is pass, fail, or environment when the command could not run."""
    started = time.monotonic()
    with open(log_path, "wb") as log:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        try:
            process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=log,
                                       stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as error:
            return None, f"Cannot start command: {error.strerror}.", "environment", 0.0
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _stop(process)
            return None, f"Timed out after {timeout} seconds.", "environment", time.monotonic() - started
        except BaseException:
            _stop(process)
            raise
        _stop(process)
    seconds = round(time.monotonic() - started, 1)
    if code == 0:
        return 0, None, "pass", seconds
    return code, f"Exited with status {code}.", "fail", seconds


def _stop(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def file_digest(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def prepare(state, worktree):
    """Pin repository, policy and base on first use; return (settings, tree)."""
    worktree = gitio.repo_root(worktree)
    settings = policies.load(repo=gitio.main_repo(worktree), origin=gitio.origin(worktree))
    if not state["contract"]:
        raise CardError("This card has no contract. Write criteria and checks to a JSON file and run "
                        "`ai-dev-card contract set --file FILE` before verification.")
    if not state["base"]:
        state["base"] = gitio.resolve_base(worktree, settings["target_branch"])
        state["repo"] = str(gitio.main_repo(worktree))
    scenario_evidence(state["contract"], settings)
    return worktree, settings, gitio.snapshot(worktree)


def checks_digest(checks):
    return digest([[c["name"], c["argv"], c.get("stand")] for c in checks])


def passing_receipt(state, settings, tree, stage):
    """The passing receipt for this tree under the checks that apply now. A check added later voids older receipts."""
    wanted = checks_digest(checks_for(settings, state["contract"], stage))
    for item in reversed(state["verifications"]):
        if (item["tree"] == tree and item["stage"] == stage and item["passed"]
                and item.get("checks_digest") == wanted):
            return item
    return None


def last_for(state, tree, stage):
    for item in reversed(state["verifications"]):
        if item["tree"] == tree and item["stage"] == stage:
            return item
    return None


def base_fails(card, state, worktree, check, timeout):
    """Does this check already fail on the base? Cached per base and command."""
    key = digest([state["base"], check["argv"]])
    if key not in state["probes"]:
        log = card.evidence("base-probes") / (key[:16] + ".log")
        with gitio.checkout(worktree, state["base"]) as base_dir:
            code, problem, klass, _ = run(check["argv"], base_dir, log, timeout)
        state["probes"][key] = {"check": check["name"], "exit_code": code, "klass": klass, "log": str(log)}
    return state["probes"][key]["klass"] != "pass"


def verify(card, worktree, stage="pre_review", rerun=False):
    """Returns (receipt, exit code): 0 passed, 1 defect, 2 environment, 3 budget exhausted."""
    if stage not in policies.STAGES:
        raise CardError(f"Unknown stage {stage}.")
    with card.locked() as state:
        worktree, settings, tree = prepare(state, worktree)
        from .preflight import issue
        problem = issue(state, settings)
        if problem:
            raise CardError(problem)
        contract = state["contract"]
        checks = checks_for(settings, contract, stage)
        if not checks:
            raise CardError(f"No {stage} checks are defined in the policy or the contract. Add at least one check.")
        base_tree = gitio.text(worktree, "rev-parse", state["base"] + "^{tree}")
        if tree == base_tree:
            raise CardError("The working tree equals the base revision: there is nothing to verify.")
        if not state["frozen"]:
            state["frozen"] = True
            record(state, "contract.frozen", digest=digest(contract))
        from .recovery import require_diagnosis
        require_diagnosis(state, settings)
        previous = passing_receipt(state, settings, tree, stage)
        if previous and not rerun:
            return previous, 0
        started = time.monotonic()
        started_at = now()
        number = len(state["verifications"]) + 1
        commit = gitio.anchor(worktree, f"{card.board}/{card.id}/v{number}", tree, state["base"])
        folder = card.evidence(f"verify-{number}")
        patch = gitio.diff(worktree, state["base"], commit)
        (folder / "candidate.patch").write_bytes(patch)
        results, prepared = [], {}
        with ExitStack() as resources:
            restore = []
            try:
                for index, check in enumerate(checks, 1):
                    timeout = check.get("timeout") or settings["check_timeout"]
                    log = folder / f"check-{index}-{check['name']}.log"
                    if check.get("stand"):
                        code, problem, klass, seconds = _stand_check(settings, check, worktree, state["base"], patch,
                                                                     log, timeout, prepared, resources, restore)
                    else:
                        code, problem, klass, seconds = run(check["argv"], worktree, log, timeout)
                        if klass == "fail" and settings["base_probe"] and base_fails(card, state, worktree, check, timeout):
                            klass, problem = "pre_existing", problem + " The same command fails on the base revision."
                    results.append({"name": check["name"], "argv": check["argv"], "stand": check.get("stand"),
                                    "exit_code": code, "problem": problem, "klass": klass, "seconds": seconds,
                                    "log": str(log), "log_sha256": file_digest(log) if log.exists() else None})
            finally:
                for driver in restore:
                    try:
                        code, tail = stands.call(driver, "shutdown")
                    except (OSError, subprocess.TimeoutExpired) as error:
                        code, tail = 1, str(error)
                    if code:
                        detail = sanitize(f"Could not restore stopped state of {driver.name}: {tail}")
                        record(state, "stand.shutdown-failed", stand=driver.name, problem=detail)
                        log = folder / f"shutdown-{driver.name}.log"
                        log.write_text(detail)
                        results.append({"name": "shutdown-" + driver.name, "argv": ["shutdown"],
                                        "stand": driver.name, "exit_code": code, "problem": detail,
                                        "klass": "environment", "seconds": 0.0, "log": str(log),
                                        "log_sha256": file_digest(log)})
        after = gitio.snapshot(worktree)
        changed = after != tree
        touched = gitio.changed_files(worktree, tree, after) if changed else []
        if all(r["klass"] == "pass" for r in results) and not changed:
            result = "passed"
        elif any(r["klass"] == "fail" for r in results) and not changed:
            result = "defect"
        else:
            result = "environment"
        receipt = {"id": number, "at": now(), "started": started_at,
                   "duration_seconds": time.monotonic() - started, "stage": stage, "tree": tree, "commit": commit, "base": state["base"],
                   "patch_sha256": hashlib.sha256(patch).hexdigest(), "checks": results,
                   "checks_digest": checks_digest(checks), "passed": result == "passed", "result": result,
                   "problem": ("The working tree changed while checks ran: " + ", ".join(touched[:8]) + ". If a check "
                               "writes these files, add them to .gitignore or make the check clean up; otherwise "
                               "stop editing and verify again.") if changed else None}
        repair_checks = [r for r in results if r["klass"] in ("environment", "pre_existing")]
        if repair_checks and not changed:
            receipt["repair_requests"] = []
            for failed in repair_checks:
                signature = digest([card.board, card.id, stage, failed["name"], failed["stand"], failed["argv"]])
                receipt["repair_requests"].append({
                    "title": f"Restore {failed['name']} for {card.id}",
                    "assignee": "default", "skills": ["ai-dev-repair"], "workspace_kind": "scratch",
                    "idempotency_key": f"ai-dev-repair-{signature}",
                    "body": (f"Return check {failed['name']} to service for {card.board}/{card.id}. "
                             f"Stage {stage}, receipt {number}, tree {tree}, base {state['base']}. "
                             f"Use the original card's verification receipt and log {failed['log']} for the "
                             "command, failure and environment. Stay within the registered project and its "
                             "authorized resources. Report the cause, actions and confirming evidence. "
                             "Leave the candidate untouched; its worker will rerun the original check.")})
        state["verifications"].append(receipt)
        if result != "passed":
            state["counters"][result] += 1
        record(state, "verification." + result, id=number, stage=stage, tree=tree)
        return receipt, {"passed": 0, "defect": 1, "environment": 2}[result]


def _stand_check(settings, check, worktree, base, patch, log, timeout, prepared, resources, restore):
    name = check["stand"]
    try:
        if name not in prepared:
            driver = stands.registered(settings["project"], name)
            resources.enter_context(stands.exclusive(settings["project"], name))
            code, initial = stands.call(driver, "status")
            if code or initial not in ("running", "stopped"):
                raise CardError(f"Cannot establish initial power state of stand {name}: {initial}")
            if initial == "stopped":
                restore.append(driver)
            problem = stands.prepare(driver, worktree, base, patch)
            prepared[name] = None if problem else driver
            if problem:
                Path(log).write_text(problem + "\n")
                return None, problem, "environment", 0.0
        driver = prepared[name]
        if driver is None:
            return None, f"Stand {name} is not prepared.", "environment", 0.0
    except (CardError, OSError, subprocess.TimeoutExpired) as error:
        prepared[name] = None
        Path(log).write_text(str(error) + "\n")
        return None, str(error), "environment", 0.0
    argv = [str(driver), "check", str(worktree), base, hashlib.sha256(patch).hexdigest(), "--", *check["argv"]]
    return run(argv, driver.parent, log, timeout)
