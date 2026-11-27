"""A card from contract to accepted review, on a real git repository and without any model."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai_dev import findings, gate, gitio, notifications, policy, recovery, release, review, severity, verify
from ai_dev.store import Card, CardError

PY = sys.executable
SUITE = [PY, "-m", "unittest", "-q"]
CONTRACT = {"criteria": ["add keeps working", "div(a, 0) raises ValueError"],
            "scenarios": [{"id": "div-zero", "operation": "div(1, 0)", "expected": "ValueError"}]}


class CardCase(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.policy = self.root / "policy.json"
        self.set_policy()
        env = patch.dict(os.environ, {
            "AI_DEV_HOME": str(self.root / "home"), "AI_DEV_POLICY": str(self.policy), "HERMES_KANBAN_RUN_ID": "test-run",
            "HERMES_KANBAN_TASK": "t_1", "HERMES_KANBAN_BOARD": "test", "HERMES_KANBAN_WORKSPACE": str(self.repo)})
        env.start()
        self.addCleanup(env.stop)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.invalid")
        self.git("config", "user.name", "t")
        self.write("calc.py", "def add(a, b):\n    return a + b\n")
        self.write("test_calc.py", """
            import unittest, calc
            class T(unittest.TestCase):
                def test_add(self):
                    self.assertEqual(calc.add(2, 2), 4)
            """)
        self.write(".gitignore", "__pycache__/\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")
        self.git("switch", "-q", "-c", "feature")
        self.card = Card("test", "t_1")

    def git(self, *args):
        subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True)

    def write(self, name, content):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content).lstrip("\n"))
        return path

    def set_policy(self, **overrides):
        settings = {"require_scenario_evidence": False, "target_branch": "main", "checks": [{"name": "unit", "argv": SUITE}], **overrides}
        settings["review"] = {"panel": [], "panel_required": False, **settings.get("review", {})}
        self.policy.write_text(json.dumps({"defaults": settings}))

    def contract(self, data=CONTRACT, **kwargs):
        with self.card.locked() as state:
            return verify.set_contract(state, data, **kwargs)

    def implement(self, body="def div(a, b):\n    return a / b\n"):
        self.write("calc.py", "def add(a, b):\n    return a + b\n\n\n" + body)

    def verified(self):
        receipt, code = verify.verify(self.card, self.repo)
        self.assertEqual(code, 0, receipt)
        return receipt

    def report(self, findings, reviewer="reviewer"):
        path = self.root / f"report-{reviewer}.json"
        path.write_text(json.dumps({"summary": "s", "findings": findings}))
        return review.submit(self.card, path, reviewer)

    def blocker(self, **fields):
        return {"class": "blocker", "severity": "medium", "confidence": "high", "impact": "Zero-divisor callers receive the wrong error.",
                "title": "div by zero is not handled", "violates": "criterion:2",
                "location": "calc.py:6", "trigger": "div(1, 0)", "expected": "ValueError",
                "actual": "ZeroDivisionError", "mechanism": "missing zero guard", **fields}

    def repro(self, name="test_repro.py", body=None):
        return self.root / name, body or """
            import unittest, calc
            class R(unittest.TestCase):
                def test_zero(self):
                    with self.assertRaises(ValueError):
                        calc.div(1, 0)
            """

    def confirm(self, key, body=None, name="test_repro.py"):
        path, text = self.repro(name, body)
        path.write_text(textwrap.dedent(text).lstrip("\n"))
        return review.confirm(self.card, self.repo, key, files=[(str(path), name)], argv=[PY, "-m", "unittest", "-q", name[:-3]])

    def diagnose(self, kind="review"):
        request, _ = recovery.recover(self.card, self.repo, kind)
        if request["action"] != "dispatch":
            return request
        marker = request["repair_request"]["idempotency_key"]
        child = "diag_" + marker[-16:]
        detail = {"task": {"id": child, "status": "done", "body": marker,
                           "skills": ["ai-dev-repair"]}, "children": [self.card.id],
                  "latest_summary": "The reproduction isolates the missing branch; correct that branch and rerun it."}
        with patch.object(recovery, "native_show", return_value=detail):
            return recovery.recover(self.card, self.repo, kind, child)[0]

    def decide(self, tool, lane):
        return gate.decide(tool, {}, lane=lane)


class Verification(CardCase):
    def test_review_waits_for_commits_under_the_configured_identity(self):
        self.contract()
        self.implement()
        self.verified()
        self.assertIsNone(self.decide("kanban_request_review", "coder"))
        self.git("add", "-A")
        self.git("-c", "user.name=Hermes Agent", "-c", "user.email=hermes@local", "commit", "-qm", "Add div")
        refusal = self.decide("kanban_request_review", "coder")
        self.assertIn("authored as Hermes Agent <hermes@local>", refusal)
        self.assertIn("--reset-author", refusal)
        self.git("commit", "-q", "--amend", "--no-edit", "--reset-author")
        self.assertIsNone(self.decide("kanban_request_review", "coder"))
        self.git("commit", "-q", "--amend", "-m", "Add div\n\nCo-authored-by: Hermes Agent <hermes@local>")
        self.assertIn("has the trailer", self.decide("kanban_request_review", "coder"))
        # A person who already has commits on the base branch may be credited.
        self.git("commit", "-q", "--amend", "-m", "Add div\n\nCo-authored-by: T <t@example.invalid>")
        self.assertIsNone(self.decide("kanban_request_review", "coder"))

    def test_passing_tree_gets_one_receipt_and_freezes_the_contract(self):
        self.contract()
        self.implement()
        first = self.verified()
        again, code = verify.verify(self.card, self.repo)
        self.assertEqual((code, again["id"]), (0, first["id"]))
        state = self.card.load()
        self.assertTrue(state["frozen"])
        self.assertEqual(state["counters"], {"defect": 0, "environment": 0})
        self.assertEqual(gitio.text(self.repo, "rev-parse", f"refs/ai-dev/test/t_1/v1^{{tree}}"), first["tree"])

    def test_needs_a_contract_and_a_change(self):
        with self.assertRaises(CardError):
            verify.verify(self.card, self.repo)
        self.contract()
        with self.assertRaisesRegex(CardError, "nothing to verify"):
            verify.verify(self.card, self.repo)

    def test_defect_is_charged_and_the_budget_ends_in_a_refusal(self):
        self.set_policy(max_defect_failures=2)
        self.contract()
        for attempt in (1, 2):
            self.write("calc.py", f"def add(a, b):\n    return a - b + {attempt}\n")
            receipt, code = verify.verify(self.card, self.repo)
            self.assertEqual((code, receipt["result"], receipt["checks"][0]["klass"]), (1, "defect", "fail"))
            self.diagnose("verification")
        self.implement()
        with self.assertRaises(CardError) as refused:
            verify.verify(self.card, self.repo)
        self.assertEqual(refused.exception.code, 3)
        review.extend(self.card, "msg-1", failures=1)
        self.diagnose("verification")
        self.assertEqual(verify.verify(self.card, self.repo)[1], 0)

    def test_environment_and_pre_existing_failures_are_not_charged(self):
        self.set_policy(checks=[{"name": "missing", "argv": ["/nonexistent/tool"]}])
        self.contract()
        self.implement()
        receipt, code = verify.verify(self.card, self.repo)
        self.assertEqual((code, receipt["checks"][0]["klass"]), (2, "environment"))
        repair = receipt["repair_requests"][0]
        self.assertEqual((repair["assignee"], repair["workspace_kind"], repair["skills"]),
                         ("default", "scratch", ["ai-dev-repair"]))
        self.assertNotIn("parents", repair)
        retried, _ = verify.verify(self.card, self.repo)
        self.assertEqual(retried["repair_requests"][0]["idempotency_key"], repair["idempotency_key"])
        self.set_policy(checks=[{"name": "broken", "argv": [PY, "-c", "import sys; sys.exit(3)"]}])
        receipt, code = verify.verify(self.card, self.repo)
        self.assertEqual((code, receipt["checks"][0]["klass"]), (2, "pre_existing"))
        self.assertEqual(self.card.load()["counters"], {"defect": 0, "environment": 3})

    def test_a_check_on_an_unregistered_stand_is_an_environment_failure(self):
        self.set_policy(project="demo", checks=[{"name": "vm", "argv": ["make", "check"], "stand": "ubuntu"}])
        self.contract()
        self.implement()
        receipt, code = verify.verify(self.card, self.repo)
        self.assertEqual((code, receipt["checks"][0]["klass"]), (2, "environment"))
        self.assertIn("stand", receipt["checks"][0]["problem"].lower())

    def test_a_check_added_after_a_pass_voids_the_receipt(self):
        self.contract()
        self.implement()
        self.verified()
        self.assertIsNone(self.decide("kanban_request_review", "coder"))
        failing = {"name": "strict", "argv": [PY, "-c", "import sys; sys.exit(1)"]}
        self.contract({**CONTRACT, "checks": [failing]})
        self.assertIn("no passing verification", self.decide("kanban_request_review", "coder"))
        receipt, code = verify.verify(self.card, self.repo)
        self.assertEqual([c["name"] for c in receipt["checks"]], ["unit", "strict"])
        self.assertNotEqual(code, 0)

    def test_a_check_that_litters_the_tree_is_named(self):
        self.set_policy(checks=[{"name": "messy", "argv": [PY, "-c", "open('out.bin', 'w').write('x')"]}])
        self.contract()
        self.implement()
        receipt, code = verify.verify(self.card, self.repo)
        self.assertEqual((code, receipt["result"]), (2, "environment"))
        self.assertIn("out.bin", receipt["problem"])

    def test_timeout_is_an_environment_failure(self):
        self.set_policy(checks=[{"name": "slow", "argv": [PY, "-c", "import time; time.sleep(30)"], "timeout": 1}])
        self.contract()
        self.implement()
        receipt, code = verify.verify(self.card, self.repo)
        self.assertEqual((code, receipt["checks"][0]["klass"]), (2, "environment"))

    def test_frozen_contract_grows_freely_and_shrinks_only_with_the_owner(self):
        self.contract()
        self.implement()
        self.verified()
        grown = {**CONTRACT, "criteria": [*CONTRACT["criteria"], "div rounds nothing"]}
        self.contract(grown)
        with self.assertRaisesRegex(CardError, "frozen"):
            self.contract({"criteria": ["add keeps working"]})
        self.contract({"criteria": ["add keeps working"]}, owner_ref="msg-7")
        self.assertEqual(self.card.load()["log"][-1]["owner_ref"], "msg-7")

    def test_untracked_files_are_part_of_the_tree_and_ignored_ones_are_not(self):
        self.contract()
        self.implement()
        first = gitio.snapshot(self.repo)
        self.write("notes.txt", "x\n")
        second = gitio.snapshot(self.repo)
        self.assertNotEqual(first, second)
        self.write(".gitignore", "__pycache__/\n*.log\n")
        third = gitio.snapshot(self.repo)
        self.write("run.log", "noise\n")
        self.assertEqual(gitio.snapshot(self.repo), third)


class CoderGate(CardCase):
    def test_review_request_needs_a_passing_receipt_for_the_current_tree(self):
        self.assertIn("no contract", self.decide("kanban_request_review", "coder"))
        self.contract()
        self.implement()
        self.assertIn("no passing verification", self.decide("kanban_request_review", "coder"))
        self.verified()
        self.assertIsNone(self.decide("kanban_request_review", "coder"))
        self.write("calc.py", (self.repo / "calc.py").read_text() + "# touched\n")
        self.assertIn("no passing verification", self.decide("kanban_request_review", "coder"))
        self.assertIn("through review", self.decide("kanban_complete", "coder"))

    def test_other_sessions_and_other_cards_pass_untouched(self):
        self.assertIsNone(gate.decide("kanban_complete", {}, lane="planner"))
        self.assertIsNone(gate.decide("terminal", {}, lane="coder"))
        self.assertIsNone(gate.decide("kanban_complete", {"task_id": "t_other"}, lane="coder"))
        with patch.dict(os.environ):
            del os.environ["HERMES_KANBAN_TASK"]
            self.assertIsNone(gate.decide("kanban_complete", {}, lane="coder"))

    def test_the_installed_role_file_beats_an_inherited_variable(self):
        (self.root / "role").write_text("coder\n")
        self.assertEqual(gate.role(env={"AI_DEV_ROLE": "planner"}, plugin_dir=self.root), "coder")
        self.assertEqual(gate.role(env={"AI_DEV_ROLE": "reviewer"}, plugin_dir=self.root / "none"), "reviewer")
        self.assertIsNone(gate.role(env={"AI_DEV_ROLE": "root"}))

    def test_an_unreadable_workspace_blocks_instead_of_passing(self):
        with patch.dict(os.environ, {"HERMES_KANBAN_WORKSPACE": str(self.root / "missing")}):
            self.assertIn("ai-dev gate", self.decide("kanban_request_review", "coder"))


class ReviewRounds(CardCase):
    def setUp(self):
        super().setUp()
        self.contract()
        self.implement()
        self.verified()

    def test_review_needs_verification_of_the_exact_tree(self):
        self.write("calc.py", (self.repo / "calc.py").read_text() + "# drift\n")
        with self.assertRaisesRegex(CardError, "passing verification"):
            review.brief(self.card, self.repo)

    def test_confirmed_blocker_returns_the_card_and_its_fix_closes_it(self):
        brief = review.brief(self.card, self.repo)
        self.assertEqual((brief["round"], brief["mode"]), (1, "full"))
        self.assertIn("settled review round", self.decide("kanban_complete", "reviewer"))
        key = self.report([self.blocker()])["to_confirm"][0]
        with self.assertRaises(CardError) as pending:
            review.settle(self.card, self.repo)
        self.assertEqual(pending.exception.code, 2)
        self.assertEqual(self.confirm(key)["status"], "confirmed")
        settled = review.settle(self.card, self.repo)
        self.assertEqual((settled["outcome"], settled["blocking"]), ("changes", [key]))
        self.assertIn("test_repro.py", Path(settled["fix_brief"]).read_text())
        self.diagnose("verification")
        self.diagnose("review")
        self.assertIsNone(self.decide("kanban_request_changes", "reviewer"))
        self.assertIn("not accept", self.decide("kanban_complete", "reviewer"))

        self.implement("def div(a, b):\n    if b == 0:\n        raise ValueError('zero')\n    return a / b\n")
        self.verified()
        self.assertIsNone(self.decide("kanban_request_review", "coder"))
        second = review.brief(self.card, self.repo)
        self.assertEqual((second["round"], second["mode"], second["delta_files"]), (2, "delta", ["calc.py"]))
        self.assertEqual([(r["key"], r["fixed"]) for r in second["rechecks"]], [(key, True)])
        self.assertEqual(self.card.load()["ledger"][key]["status"], "fixed")
        self.report([])
        self.assertEqual(review.settle(self.card, self.repo)["outcome"], "accept")
        self.assertIsNone(self.decide("kanban_complete", "reviewer"))
        self.assertIn("accept", self.decide("kanban_request_changes", "reviewer"))

    def _recheck_committed_python_reproduction(self, identical):
        review.brief(self.card, self.repo)
        key = self.report([self.blocker()])["to_confirm"][0]
        self.assertEqual(self.confirm(key)["status"], "confirmed")
        settled = review.settle(self.card, self.repo)
        self.assertEqual((settled["outcome"], settled["blocking"]), ("changes", [key]))
        self.diagnose("review")

        proof = self.card.load()["ledger"][key]["confirm"]
        fixture = Path(proof["files"]["test_repro.py"])
        data = fixture.read_bytes()
        tracked = self.repo / "test_repro.py"
        tracked.write_bytes(data if identical else data + b"# independent edit\n")
        self.implement("def div(a, b):\n    if b == 0:\n        raise ValueError('zero')\n    return a / b\n")
        self.git("add", "calc.py", "test_repro.py")
        self.git("commit", "-qm", "Fix zero divisor and retain its regression test")
        receipt = self.verified()
        second = review.brief(self.card, self.repo)
        self.assertEqual((second["round"], second["tree"]), (2, receipt["tree"]))
        self.assertEqual(tracked.read_bytes(), data if identical else data + b"# independent edit\n")
        self.assertEqual(fixture.read_bytes(), data)
        recheck, = second["rechecks"]
        self.assertEqual(recheck["key"], key)
        return key, recheck

    def test_recheck_runs_byte_identical_committed_python_reproduction(self):
        key, recheck = self._recheck_committed_python_reproduction(identical=True)
        self.assertEqual((recheck["fixed"], recheck["exit_code"]), (True, 0), recheck)
        self.assertIn("Ran 1 test", Path(recheck["log"]).read_text())
        self.assertEqual(self.card.load()["ledger"][key]["status"], "fixed")

    def test_changed_reproduction_goes_back_to_the_reviewer(self):
        key, recheck = self._recheck_committed_python_reproduction(identical=False)
        self.assertEqual((recheck["fixed"], recheck["exit_code"], recheck["reconfirm"]), (False, None, True))
        self.assertEqual(recheck["collision"], "test_repro.py")
        self.assertEqual(self.card.load()["ledger"][key]["status"], "candidate")
        brief = json.loads((self.card.evidence("round-2") / "brief.json").read_text())
        self.assertEqual(brief["reconfirm"], [{"key": key, "changed_file": "test_repro.py"}])
        self.report([])
        with self.assertRaisesRegex(CardError, "Confirm each blocker candidate"):
            review.settle(self.card, self.repo)
        with self.assertRaisesRegex(CardError, "confirmed by a reproduction before"):
            review.confirm(self.card, self.repo, key, cannot="it looks fixed")
        # The reviewer runs the candidate's own test: no stored file is written over it.
        result = review.confirm(self.card, self.repo, key, argv=[PY, "-m", "unittest", "-q", "test_repro"])
        self.assertEqual(result["status"], "fixed")
        entry = self.card.load()["ledger"][key]
        self.assertEqual((entry["status"], entry["disposition"]["outcome"]), ("fixed", "fixed"))
        self.assertEqual(review.settle(self.card, self.repo)["outcome"], "accept")

    def test_changed_reproduction_that_still_fails_blocks_again(self):
        key, _ = self._recheck_committed_python_reproduction(identical=False)
        failing = self.root / "still.py"
        failing.write_text("import unittest\nclass R(unittest.TestCase):\n    def test_no(self):\n        self.fail('still broken')\n")
        result = review.confirm(self.card, self.repo, key, files=[(str(failing), "test_still.py")],
                                argv=[PY, "-m", "unittest", "-q", "test_still"])
        self.assertEqual(result["status"], "confirmed")

    def test_inconclusive_attempt_can_end_as_unconfirmed(self):
        review.brief(self.card, self.repo)
        key = self.report([self.blocker()])["to_confirm"][0]
        self.assertEqual(review.confirm(self.card, self.repo, key, argv=["/nonexistent/reproduction-tool"])["status"], "candidate")
        with self.assertRaisesRegex(CardError, "Confirm each blocker candidate"):
            review.settle(self.card, self.repo)
        self.assertEqual(review.confirm(self.card, self.repo, key, cannot="No tool here can run it")["status"], "unconfirmed")
        self.assertEqual(review.settle(self.card, self.repo)["outcome"], "accept")

    def test_project_adapter_controls_confirmation_and_keeps_actual_command(self):
        adapter = self.root / "adapter.py"
        adapter.write_text("import sys\nfrom pathlib import Path\nprint(sys.argv[1:])\n"
                           "assert Path('calc.py').is_file()\nraise SystemExit(2)\n")
        self.set_policy(review={"reproduction_adapter": [PY, str(adapter)]})
        review.brief(self.card, self.repo)
        key = self.report([self.blocker()])["to_confirm"][0]
        result = review.confirm(self.card, self.repo, key, argv=["project-runner", "with spaces"])
        self.assertEqual(result["status"], "candidate")
        evidence = result["proof"]["candidate"]
        self.assertEqual(evidence["klass"], "environment")
        self.assertEqual(evidence["command"], [PY, str(adapter), "project-runner", "with spaces"])
        self.assertIn("with spaces", Path(evidence["log"]).read_text())
        adapter.write_text("raise SystemExit(1)\n")
        self.assertEqual(review.confirm(self.card, self.repo, key, argv=["project-runner"])["status"], "confirmed")
        self.assertEqual(len(self.card.load()["ledger"][key]["confirmations"]), 1)

    def test_adapter_nonprotocol_exit_or_missing_tool_does_not_prove_a_defect(self):
        review.brief(self.card, self.repo)
        key = self.report([self.blocker()])["to_confirm"][0]
        for adapter in (["/missing/adapter"], [PY, "-c", "raise SystemExit(7)"]):
            with self.subTest(adapter=adapter):
                self.set_policy(review={"reproduction_adapter": adapter})
                result = review.confirm(self.card, self.repo, key, argv=[PY, "-c", "pass"])
                self.assertEqual(result["status"], "candidate")
                self.assertEqual(result["proof"]["candidate"]["klass"], "environment")

    def test_recheck_uses_current_project_adapter(self):
        review.brief(self.card, self.repo)
        key = self.report([self.blocker()])["to_confirm"][0]
        self.confirm(key)
        review.settle(self.card, self.repo)
        self.diagnose("review")
        self.implement("def div(a, b):\n    if b == 0: raise ValueError('zero')\n    return a / b\n")
        self.verified()
        self.set_policy(review={"reproduction_adapter": [PY, "-c", "raise SystemExit(2)"]})
        packet = review.brief(self.card, self.repo)
        self.assertEqual(packet["rechecks"][0]["klass"], "environment")
        self.assertEqual(self.card.load()["ledger"][key]["status"], "confirmed")
        self.report([])
        self.assertIn(key, review.settle(self.card, self.repo)["blocking"])

    def test_adapter_policy_rejects_malformed_prefix(self):
        for adapter in ("runner", ["relative-runner"], [PY, ""], [None]):
            with self.subTest(adapter=adapter):
                self.set_policy(review={"reproduction_adapter": adapter})
                with self.assertRaisesRegex(CardError, "reproduction_adapter"):
                    policy.load(repo=self.repo)

    def _recorded_supersession(self):
        review.brief(self.card, self.repo)
        key = self.report([self.blocker()])["to_confirm"][0]
        self.confirm(key)
        self.assertEqual(review.settle(self.card, self.repo)["outcome"], "changes")
        folder = self.card.evidence("supersessions", key)
        log = folder / "replacement.log"
        log.write_text("ok\n")
        with self.card.locked() as state:
            entry, rnd = state["ledger"][key], state["rounds"][-1]
            replacement = {"klass": "pass", "exit_code": 0, "log": str(log), "sha256": verify.file_digest(log)}
            state["evidence_supersessions"] = [{
                "key": key, "round": rnd["n"], "tree": rnd["tree"], "commit": rnd["commit"], "owner_ref": "owner message",
                "reason": "stale copy", "actor": "maintainer", "argv": [PY, "-c", "pass"], "replacement": replacement,
                "old": {"fixture": {"destination": "test_repro.py"}, "confirmation_sha256": verify.digest(entry["confirm"])},
                "receipt_floor": max(v["id"] for v in state["verifications"])}]
            entry["status"] = "fixed"
            entry["disposition"] = {"outcome": "fixed", "tree": rnd["tree"], "evidence": replacement}
        return key, log

    def test_recorded_evidence_supersession_still_requires_a_fresh_round(self):
        key, log = self._recorded_supersession()
        self.assertIn("not actionable after evidence supersession", self.decide("kanban_request_changes", "reviewer"))
        with self.assertRaisesRegex(CardError, "passing verification of this exact tree"):
            review.brief(self.card, self.repo)
        receipt, code = verify.verify(self.card, self.repo, rerun=True)
        self.assertEqual(code, 0, receipt)
        fresh = review.brief(self.card, self.repo)
        self.assertEqual((fresh["round"], fresh["supersedes_round"], fresh["mode"], fresh["rechecks"]), (2, 1, "full", []))
        self.report([])
        self.assertEqual(review.settle(self.card, self.repo)["outcome"], "accept")
        log.write_text("edited\n")
        self.assertIn("replacement log no longer matches", self.decide("kanban_complete", "reviewer"))

    def test_recorded_repair_keeps_exact_tree_at_handoff(self):
        self._recorded_supersession()
        self.implement("def div(a, b):\n    return 42\n")
        self.verified()
        self.assertIn("exact tree", self.decide("kanban_request_review", "coder"))
        with self.assertRaisesRegex(CardError, "exact tree"):
            review.brief(self.card, self.repo)

    def test_recorded_repair_panel_refresh_keeps_log_integrity(self):
        key, log = self._recorded_supersession()
        verify.verify(self.card, self.repo, rerun=True)
        review.brief(self.card, self.repo)
        self.report([])
        self.assertEqual(review.settle(self.card, self.repo)["outcome"], "accept")
        self.set_policy(review={"panel_required": True, "panel": policy.DEFAULTS["review"]["panel"]})
        packet = review.brief(self.card, self.repo)
        self.assertEqual(packet["supersedes_round"], 1)
        self.assertEqual(packet["mode"], "full")
        log.write_text("tampered\n")
        with self.assertRaisesRegex(CardError, "replacement log"):
            review.brief(self.card, self.repo)

    def test_recorded_repair_recheck_uses_adapter_and_stays_blocking(self):
        key, log = self._recorded_supersession()
        verify.verify(self.card, self.repo, rerun=True)
        review.brief(self.card, self.repo)
        self.report([])
        review.settle(self.card, self.repo)
        self.implement("def div(a, b):\n    return 42\n")
        self.verified()
        self.set_policy(review={"reproduction_adapter": [PY, "-c", "raise SystemExit(2)"]})
        packet = review.brief(self.card, self.repo)
        self.assertEqual(packet["rechecks"][0]["klass"], "environment")
        self.assertEqual(self.card.load()["ledger"][key]["status"], "confirmed")
        self.report([])
        self.assertIn(key, review.settle(self.card, self.repo)["blocking"])

    def test_claims_outside_the_contract_and_unreproduced_ones_do_not_block(self):
        review.brief(self.card, self.repo)
        result = self.report([
            self.blocker(title="naming is odd", violates="taste", mechanism="style"),
            self.blocker(title="float drift", mechanism="float rounding"),
            {"class": "discovery", "title": "add has no type hints", "severity": "low", "confidence": "high", "impact": "Readers infer argument types."}])
        statuses = {f["title"]: f["status"] for f in result["findings"]}
        self.assertEqual(statuses, {"naming is odd": "note", "float drift": "candidate", "add has no type hints": "discovery"})
        key = result["to_confirm"][0]
        passing = "import unittest\nclass R(unittest.TestCase):\n    def test_ok(self):\n        pass\n"
        self.assertEqual(self.confirm(key, passing)["status"], "disproved")
        settled = review.settle(self.card, self.repo)
        self.assertEqual((settled["outcome"], settled["blocking"]), ("accept", []))

    def test_low_blocker_is_advisory_and_critical_claim_alone_does_not_block(self):
        review.brief(self.card, self.repo)
        result = self.report([self.blocker(severity="low"), self.blocker(severity="critical", mechanism="other")])
        self.assertEqual([f["status"] for f in result["findings"]], ["note", "candidate"])
        review.confirm(self.card, self.repo, result["to_confirm"][0], cannot="No supported failing path.")
        self.assertEqual(review.settle(self.card, self.repo)["outcome"], "accept")

    def test_promoting_low_requires_a_contract_and_reproduction(self):
        review.brief(self.card, self.repo)
        notes = self.report([self.blocker(severity="low", kind="gap"),
                             self.blocker(severity="low", violates="regression", mechanism="another cause")])
        key, regression = [row["key"] for row in notes["findings"]]
        with self.assertRaisesRegex(CardError, "named contract"):
            review.assess(self.card, self.repo, regression, "medium", "Material consequence.", "calc.py")
        self.assertEqual(review.assess(self.card, self.repo, key, "medium", "Divisor-zero callers fail.", "calc.py")["status"], "candidate")
        with self.assertRaisesRegex(CardError, "Confirm each"):
            review.settle(self.card, self.repo)
        self.confirm(key)
        self.assertEqual(review.settle(self.card, self.repo)["blocking"], [key])

    def test_empty_round_summary_is_queued_and_acknowledged_once(self):
        with self.card.locked() as state:
            state["conversation"] = {"platform":"telegram","chat":"123","thread":"5"}
        review.brief(self.card, self.repo)
        self.report([])
        for _ in range(2):
            review.settle(self.card, self.repo)
        summaries = [e for e in self.card.load()["log"] if e["event"] == "review.round-summary"]
        self.assertEqual([e["findings"] for e in summaries], [[]])
        acknowledged = subprocess.CompletedProcess([], 0, '{"success":true,"message_id":"42"}', "")
        with patch("ai_dev.notifications.subprocess.run", return_value=acknowledged) as sender:
            self.assertEqual(notifications.deliver(self.card), "sent")
            self.assertEqual(notifications.deliver(self.card), "idle")
            self.assertEqual(sender.call_count, 1)
            self.assertIn("telegram:123:5", sender.call_args.args[0])
            self.assertIn("Замечаний в отчётах этого раунда: 0", sender.call_args.kwargs["input"])

    def test_legacy_findings_keep_unknown_impact(self):
        self.assertEqual(severity.describe({"key":"old","title":"historical","status":"note"})["severity"], "unknown")

    def test_new_report_requires_severity_confidence_and_impact(self):
        review.brief(self.card, self.repo)
        for field in ("severity", "confidence", "impact"):
            item = self.blocker()
            del item[field]
            with self.assertRaises(CardError):
                self.report([item])

    def test_a_confirmed_blocker_cannot_be_talked_away(self):
        self.set_policy(review={"max_rounds": 1})
        review.brief(self.card, self.repo)
        key = self.report([self.blocker()])["to_confirm"][0]
        self.confirm(key)
        with self.assertRaisesRegex(CardError, "not waiting for confirmation"):
            review.confirm(self.card, self.repo, key, cannot="changed my mind")
        self.assertEqual(review.settle(self.card, self.repo)["outcome"], "owner")
        with self.assertRaisesRegex(CardError, "at the round cap"):
            review.confirm(self.card, self.repo, key, cannot="changed my mind")
        self.assertEqual(review.settle(self.card, self.repo)["outcome"], "owner")
        self.assertIsNotNone(self.decide("kanban_complete", "reviewer"))

    def test_a_retried_reviewer_gets_the_settled_round_back(self):
        self.set_policy(review={"max_rounds": 2})
        review.brief(self.card, self.repo)
        key = self.report([self.blocker()])["to_confirm"][0]
        self.confirm(key)
        self.assertEqual(review.settle(self.card, self.repo)["outcome"], "changes")
        again = review.brief(self.card, self.repo)
        self.assertEqual((again["round"], again["settled"]), (1, "changes"))
        self.assertEqual(len(self.card.load()["rounds"]), 1)
        self.assertEqual(review.settle(self.card, self.repo)["outcome"], "changes")
        self.diagnose("verification")
        self.diagnose("review")
        self.assertIsNone(self.decide("kanban_request_changes", "reviewer"))

    def test_a_claim_raised_again_is_judged_as_stated_now(self):
        review.brief(self.card, self.repo)
        first = self.report([self.blocker(violates="taste"),
                             self.blocker(title="no proof", kind="gap", violates="nothing", mechanism="unproved")])
        self.assertEqual([f["status"] for f in first["findings"]], ["note", "note"])
        taste, gap = (f["key"] for f in first["findings"])
        second = self.report([self.blocker(key=taste), self.blocker(key=gap, title="no proof", mechanism="unproved")], "second")
        ledger = self.card.load()["ledger"]
        self.assertEqual((ledger[taste]["status"], ledger[taste]["violates"]), ("candidate", "criterion:2"))
        self.assertEqual((ledger[gap]["status"], ledger[gap]["kind"]), ("candidate", "defect"))
        self.assertEqual(sorted(second["to_confirm"]), sorted([taste, gap]))
        self.assertEqual(self.confirm(taste)["status"], "confirmed")
        self.assertEqual(review.confirm(self.card, self.repo, gap, cannot="no failing path")["status"], "unconfirmed")

    def test_malformed_reports_are_refused_with_a_reason(self):
        review.brief(self.card, self.repo)
        with self.assertRaisesRegex(CardError, "key is a string"):
            self.report([self.blocker(key=["x"])])
        odd = self.report([self.blocker(violates="criterion:\u00b2")])
        self.assertEqual(odd["findings"][0]["status"], "note")
        key = self.report([self.blocker(title="real", mechanism="real one")])["to_confirm"][0]
        with self.assertRaisesRegex(CardError, "Cannot read the reproduction file"):
            review.confirm(self.card, self.repo, key, files=[(str(self.root / "absent.py"), "t.py")], argv=["true"])

    def test_a_regression_is_confirmed_when_the_base_passes(self):
        self.write("calc.py", "def add(a, b):\n    return a + b if a >= 0 else a - b\n\n\ndef div(a, b):\n    return a / b\n")
        self.verified()
        review.brief(self.card, self.repo)
        key = self.report([self.blocker(title="add broke for negatives", violates="regression",
                                        mechanism="sign branch in add")])["to_confirm"][0]
        test = "import unittest, calc\nclass R(unittest.TestCase):\n    def test_neg(self):\n        self.assertEqual(calc.add(-1, 1), 0)\n"
        result = self.confirm(key, test, "test_neg.py")
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual((result["proof"]["candidate"]["klass"], result["proof"]["base"]["klass"]), ("fail", "pass"))

    def test_regression_needs_a_passing_base_and_a_base_failure_is_a_discovery(self):
        review.brief(self.card, self.repo)
        result = self.report([
            self.blocker(title="add broke", violates="regression", mechanism="add changed"),
            self.blocker(title="mul missing", violates="regression", mechanism="no mul")])
        by_title = {f["title"]: f["key"] for f in result["findings"]}
        missing = "import unittest, calc\nclass R(unittest.TestCase):\n    def test_mul(self):\n        calc.mul(1, 2)\n"
        self.assertEqual(self.confirm(by_title["mul missing"], missing, "test_mul.py")["status"], "discovery")
        given_up = review.confirm(self.card, self.repo, by_title["add broke"], cannot="add behaves as on base")
        self.assertEqual(given_up["status"], "unconfirmed")
        self.assertEqual(review.settle(self.card, self.repo)["outcome"], "accept")

    def test_settled_finding_reopens_only_with_a_reproduction(self):
        review.brief(self.card, self.repo)
        key = self.report([self.blocker()])["to_confirm"][0]
        review.confirm(self.card, self.repo, key, cannot="could not trigger it")
        self.assertEqual(review.settle(self.card, self.repo)["outcome"], "accept")
        # A later card state: the same claim comes back in a new round.
        self.write("calc.py", (self.repo / "calc.py").read_text() + "\n\ndef neg(a):\n    return -a\n")
        self.verified()
        review.brief(self.card, self.repo)
        again = self.report([self.blocker(key=key)])
        self.assertEqual(again["to_confirm"], [key])
        self.assertEqual(self.confirm(key)["status"], "confirmed")
        self.assertEqual(review.settle(self.card, self.repo)["outcome"], "changes")

    def test_gaps_survive_silent_round_and_require_current_evidence(self):
        review.brief(self.card, self.repo)
        gap = self.blocker(title="no test for div", kind="gap", mechanism="div untested")
        key = self.report([gap])["findings"][0]["key"]
        review.assess(self.card, self.repo, key, "medium", "Current suite covers addition only.", "test_calc.py")
        self.assertEqual(review.settle(self.card, self.repo)["outcome"], "changes")
        self.diagnose()
        self.write("test_div.py", "import unittest, calc\nclass D(unittest.TestCase):\n    def test_div(self):\n        self.assertEqual(calc.div(4, 2), 2)\n")
        self.verified()
        review.brief(self.card, self.repo)
        late = self.report([self.blocker(title="no test for negatives", kind="gap", mechanism="negatives untested")])
        self.assertEqual(late["findings"][0]["status"], "note")
        self.assertEqual(self.card.load()["ledger"][key]["status"], "gap")
        findings.resolve(self.card, self.repo, key, "resolved", "A test now proves the scenario", "test_div.py")
        self.assertEqual(review.settle(self.card, self.repo)["outcome"], "accept")
        self.assertEqual(self.card.load()["ledger"][key]["status"], "resolved")

    def test_round_cap_routes_to_diagnosis_before_owner(self):
        self.set_policy(review={"max_rounds": 2})
        review.brief(self.card, self.repo)
        key = self.report([self.blocker()])["to_confirm"][0]
        self.confirm(key)
        self.assertEqual(review.settle(self.card, self.repo)["outcome"], "changes")
        self.diagnose()
        self.write("calc.py", (self.repo / "calc.py").read_text() + "# attempt 2, still wrong\n")
        self.verified()
        second = review.brief(self.card, self.repo)
        self.assertEqual([r["fixed"] for r in second["rechecks"]], [False])
        self.report([])
        settled = review.settle(self.card, self.repo)
        self.assertEqual(settled["outcome"], "owner")
        self.assertIn("Recovery decision", Path(settled["fix_brief"]).read_text())
        self.assertIn("recover review", self.decide("kanban_request_changes", "reviewer"))
        self.assertIn("recover review", self.decide("kanban_complete", "reviewer"))
        review.extend(self.card, "msg-3", rounds=1)
        self.assertEqual(review.settle(self.card, self.repo)["outcome"], "changes")

    def test_owner_can_waive_at_the_cap(self):
        self.set_policy(review={"max_rounds": 1})
        review.brief(self.card, self.repo)
        key = self.report([self.blocker()])["to_confirm"][0]
        self.confirm(key)
        self.assertEqual(review.settle(self.card, self.repo)["outcome"], "owner")
        review.waive(self.card, key, "msg-9", "accepted for the pilot")
        self.assertEqual(review.settle(self.card, self.repo)["outcome"], "accept")
        self.assertIsNone(self.decide("kanban_complete", "reviewer"))

    def test_editing_the_tree_during_review_voids_the_round(self):
        review.brief(self.card, self.repo)
        self.report([])
        self.write("calc.py", (self.repo / "calc.py").read_text() + "# reviewer edit\n")
        with self.assertRaisesRegex(CardError, "changed during review"):
            review.settle(self.card, self.repo)
        self.assertIn("settled review round", self.decide("kanban_complete", "reviewer"))

    def test_post_review_checks_gate_completion(self):
        self.set_policy(checks=[{"name": "unit", "argv": SUITE},
                                {"name": "late", "argv": [PY, "-c", "pass"], "stage": "post_review"}])
        review.brief(self.card, self.repo)
        self.report([])
        settled = review.settle(self.card, self.repo)
        self.assertIn("post_review", settled["next"])
        self.assertIn("post-review", self.decide("kanban_complete", "reviewer"))
        self.assertEqual(verify.verify(self.card, self.repo, stage="post_review")[1], 0)
        self.assertIsNone(self.decide("kanban_complete", "reviewer"))

    def test_a_post_review_defect_returns_the_card(self):
        self.set_policy(base_probe=False, checks=[
            {"name": "unit", "argv": SUITE},
            {"name": "late", "argv": [PY, "-c", "import sys; sys.exit(1)"], "stage": "post_review"}])
        review.brief(self.card, self.repo)
        self.report([])
        self.assertEqual(review.settle(self.card, self.repo)["outcome"], "accept")
        self.assertIn("accept", self.decide("kanban_request_changes", "reviewer"))
        self.assertEqual(verify.verify(self.card, self.repo, stage="post_review")[1], 1)
        self.assertIn("kanban_request_changes", self.decide("kanban_complete", "reviewer"))
        self.diagnose("verification")
        self.diagnose("review")
        self.assertIsNone(self.decide("kanban_request_changes", "reviewer"))

    def test_two_reviewers_share_one_finding_by_mechanism(self):
        review.brief(self.card, self.repo)
        first = self.report([self.blocker()], "reviewer")["findings"][0]["key"]
        second = self.report([self.blocker(title="zero divisor crashes")], "second")["findings"][0]["key"]
        self.assertEqual(first, second)
        self.assertEqual(self.card.load()["ledger"][first]["reporters"], ["reviewer", "second"])


class PackageDelivery(CardCase):
    def test_only_published_package_for_reviewed_and_merged_change_allows_completion(self):
        rule = {"channel": "testing", "repository": "demo/core", "branch": "main",
                "publisher": "demo/packages", "catalog_url": "https://demo.example/testing.json",
                "package": "core", "architectures": ["amd64"]}
        self.contract({**CONTRACT, "release": rule})
        self.implement()
        tree = self.verified()["tree"]
        with self.assertRaisesRegex(CardError, "frozen"):
            self.contract(CONTRACT)
        review.brief(self.card, self.repo)
        self.report([])
        settled = review.settle(self.card, self.repo)
        self.assertIn("release verify", settled["next"])
        self.assertIn("release verify", self.decide("kanban_complete", "reviewer"))
        url = "https://github.com/demo/core/pull/4"
        head, merged = "1" * 40, "2" * 40
        pr = {"merged": False, "state": "open", "base": {"ref": "main"}, "head": {"sha": head}, "merge_commit_sha": merged}
        commit = {"tree": {"sha": "3" * 40}}
        package = {"name": "core", "architecture": "amd64", "size": 12, "version": "v3", "sha256": "a"*64,
                   "url": "https://github.com/demo/packages/releases/download/test/core.deb"}
        entry = {"channel": "testing", "version": "v3", "tag": "test",
                 "sources": {"app": {"repository": "demo/core", "commit": merged}}, "packages": {"amd64": [package]}}
        feed = {"channel": "testing", "releases": []}
        asset = {"browser_download_url": package["url"], "state": "uploaded", "digest": "sha256:wrong", "size": 12}
        remote = {"tag_name": "test", "draft": False, "assets": [asset],
                  "html_url": "https://github.com/demo/packages/releases/tag/test"}
        responses = {"repos/demo/core/pulls/4": pr, f"repos/demo/core/git/commits/{head}": commit,
                     "repos/demo/packages/releases/tags/test": remote}
        with patch.object(release, "api", side_effect=responses.__getitem__), patch.object(release, "catalog", return_value=feed):
            commit["tree"]["sha"] = tree
            with self.assertRaisesRegex(CardError, "Await merge"):
                release.verify_release(self.card, self.repo, url)
            pr["merged"] = True
            commit["tree"]["sha"] = "3" * 40
            with self.assertRaisesRegex(CardError, "PR head differs"):
                release.verify_release(self.card, self.repo, url)
            commit["tree"]["sha"] = tree
            with self.assertRaisesRegex(CardError, "no published package"):
                release.verify_release(self.card, self.repo, url)
            feed["releases"] = [entry]
            with self.assertRaisesRegex(CardError, "checksum"):
                release.verify_release(self.card, self.repo, url)
            asset["digest"] = "sha256:" + package["sha256"]
            receipt = release.verify_release(self.card, self.repo, url)
            with_ci = {**rule, "ci_workflow": "build.yml"}
            with self.assertRaisesRegex(CardError, "No matching pre-merge"):
                release.evidence(with_ci, url, tree)
            pr["merged"] = False
            key = f"repos/demo/core/actions/workflows/build.yml/runs?event=pull_request&head_sha={head}&per_page=1"
            run = {"id": 7, "run_attempt": 1, "html_url": "https://github.com/demo/core/actions/runs/7",
                   "repository": {"full_name": "demo/core"}, "path": ".github/workflows/build.yml",
                   "event": "pull_request", "head_sha": head, "pull_requests": [{"number": 4}],
                   "status": "completed", "conclusion": "success"}
            responses[key] = {"workflow_runs": [run]}
            before = release.evidence(with_ci, url, tree, before_merge=True)
            pr["merged"] = True
            stored = {**before, "rule": with_ci}
            run["status"] = "queued"
            with self.assertRaisesRegex(CardError, "latest exact-head"):
                release.evidence(with_ci, url, tree, premerge=stored)
            run["status"] = "completed"
            self.assertEqual(release.evidence(with_ci, url, tree, premerge=stored)["ci"]["id"], 7)
        self.assertEqual(receipt["version"], "v3")
        self.assertEqual(receipt["packages"][0]["url"], package["url"])
        self.assertIn("metadata.published_pr", self.decide("kanban_complete", "reviewer"))
        self.assertIsNone(gate.decide("kanban_complete", {"metadata": {"published_pr": url}}, lane="reviewer"))


class Panel(CardCase):
    def setUp(self):
        super().setUp()
        self.contract()
        self.implement()
        self.verified()

    def reviewer(self, name, body):
        script = self.root / f"{name}.py"
        script.write_text(textwrap.dedent(body).lstrip("\n"))
        return {"name": name, "argv": [PY, str(script)]}

    def test_second_opinion_lands_in_the_ledger_and_an_outage_is_recorded(self):
        answer = {"summary": "s", "findings": [self.blocker()]}
        good = self.reviewer("second", f"""
            import json, os, sys
            packet = sys.stdin.read()
            assert "ValueError" in packet and os.path.exists("calc.py")
            print("Here is my report:\\n```json\\n" + json.dumps({answer!r}) + "\\n```")
            """)
        bad = self.reviewer("third", "import sys\nsys.stderr.write('quota exhausted')\nsys.exit(4)\n")
        self.set_policy(review={"panel": [good, bad]})
        review.brief(self.card, self.repo)
        self.report([])
        result = review.panel(self.card, self.repo)
        self.assertEqual([p["status"] for p in result["panel"]], ["ok", "unavailable"])
        self.assertIn("quota exhausted", result["panel"][1]["reason"])
        self.assertEqual(len(result["to_confirm"]), 1)
        self.assertEqual(subprocess.run(["git", "worktree", "list"], cwd=self.repo, capture_output=True,
                                        text=True).stdout.count("\n"), 1)

    def test_default_panel_cannot_be_impersonated(self):
        self.policy.write_text(json.dumps({"defaults": {"require_scenario_evidence": False, "target_branch": "main", "checks": [{"name":"unit","argv": SUITE}]}}))
        packet = review.brief(self.card, self.repo)
        self.assertEqual(packet["panel"], ["openai-astra", "anthropic-opus"])
        for name in ["reviewer", *packet["panel"]]:
            with self.assertRaisesRegex(CardError, "coordinator cannot submit"):
                self.report([], name)
        with self.assertRaisesRegex(CardError, "panel is required"):
            review.settle(self.card, self.repo)

    def strict_panel(self, author="writer", fail=False, findings=None, usage=None):
        # Two actual processes rendezvous across checkouts; serial dispatch times out here.
        members = []
        for name, other, output in [("astra", "opus", "hermes-usage"), ("opus", "astra", "claude-json")]:
            program = f"""
                import json, os, sys, time
                from pathlib import Path
                assert "HERMES_KANBAN_TASK" not in os.environ
                root = Path({str(self.root)!r})
                (root / "{name}.started").write_text(str(Path.cwd()))
                deadline = time.monotonic() + 3
                while not (root / "{other}.started").exists():
                    assert time.monotonic() < deadline
                    time.sleep(0.02)
                report = {{"summary":"inspected", "model":"self-report-ignored", "findings":{(findings or {}).get(name, [])!r},
                           "coverage":[{{"path":"calc.py","observation":"checked the conditional return"}}]}}
                if "{output}" == "hermes-usage":
                    dest = Path(sys.argv[sys.argv.index("--usage-file") + 1])
                    brief = dest.parent / "review-task.txt"
                    assert str(brief) in sys.argv[-1] and brief.is_file() and Path.cwd() not in brief.parents
                    assert '{{"path": ' in brief.read_text() and '"observation": ' in brief.read_text()
                    dest.write_text(json.dumps({{"model":"astra","provider":"openai-codex","failed":False,
                        "completed":True,"session_id":"a1","api_calls":1, **{usage or {}!r}}}))
                    print(json.dumps(report))
                else:
                    if {fail!r}: sys.exit(1)
                    print(json.dumps({{"subtype":"success","is_error":False,"session_id":"o1",
                        "modelUsage":{{"opus":{{"inputTokens":42}}}},"result":json.dumps(report)}}))
            """
            members.append({**self.reviewer(name, program), "output": output, "model": name})
        self.set_policy(review={"panel": members, "panel_required": True, "author_model": author})
        return members

    def test_two_subagents_run_in_parallel_and_coordinator_settles_from_runtime_receipts(self):
        members = self.strict_panel(author="writer")
        review.brief(self.card, self.repo)
        result = review.panel(self.card, self.repo)
        self.assertEqual([p["status"] for p in result["panel"]], ["ok", "ok"], result)
        self.assertNotEqual((self.root/"astra.started").read_text(), (self.root/"opus.started").read_text())
        self.set_policy(review={"panel": members, "panel_required": True, "author_model": "astra"})
        with self.assertRaisesRegex(CardError, "human review"):
            review.settle(self.card, self.repo)
        self.assertIsNotNone(self.decide("kanban_complete", "reviewer"))
        self.set_policy(review={"panel": members, "panel_required": True, "author_model": "writer"})
        self.assertEqual(review.panel(self.card, self.repo)["panel"], [])
        self.assertEqual(review.settle(self.card, self.repo)["outcome"], "accept")
        rnd = self.card.load()["rounds"][0]
        self.assertEqual(set(rnd["reports"]), {"astra", "opus"})
        self.assertEqual(rnd["panel"]["astra"]["runtime"]["session_id"], "a1")

    def test_one_report_settles_when_the_other_provider_is_down(self):
        # Hermes answers through its fallback provider when the pinned one is rate-limited.
        self.strict_panel(usage={"provider": "other-subscription", "model": "sonnet"})
        review.brief(self.card, self.repo)
        result = review.panel(self.card, self.repo)
        self.assertEqual([p["status"] for p in result["panel"]], ["unavailable", "ok"], result)
        self.assertEqual(result["panel"][0]["error_class"], "provider_unavailable")
        self.assertEqual(result["provider_diversity"], "single-reviewer")
        self.assertIsNone(recovery.trigger(self.card.load(), policy.load(repo=self.repo), "provider"))
        self.assertEqual(review.settle(self.card, self.repo)["outcome"], "accept")
        messages = [e["text"] for e in self.card.load()["log"] if e["event"] == "review.summary-message"]
        self.assertIn("Ревью одним ревьюером", "".join(messages))

    def test_an_unattested_model_on_the_pinned_provider_still_blocks(self):
        self.strict_panel(usage={"model": "someone-else"})
        review.brief(self.card, self.repo)
        result = review.panel(self.card, self.repo)
        self.assertEqual(result["panel"][0]["error_class"], "attestation")
        with self.assertRaisesRegex(CardError, "panel is required"):
            review.settle(self.card, self.repo)

    def test_two_ratings_reconciled_once_with_evidence_and_retained_in_handoff(self):
        self.strict_panel(findings={"astra":[self.blocker(severity="high")], "opus":[self.blocker(severity="low")]})
        review.brief(self.card, self.repo)
        result = review.panel(self.card, self.repo)
        self.assertEqual([p["status"] for p in result["panel"]], ["ok","ok"], result)
        key = result["panel"][0]["findings"][0]["key"]
        with self.assertRaisesRegex(CardError, "Resolve severity"):
            review.settle(self.card, self.repo)
        view = review.assess(self.card, self.repo, key, "medium", "Only zero-divisor calls raise the wrong error.", "calc.py")
        self.assertEqual((view["severity"], view["status"]), ("medium", "candidate"))
        self.assertEqual(self.confirm(key)["status"], "confirmed")
        settled = review.settle(self.card, self.repo)
        self.assertEqual(settled["outcome"], "changes")
        self.assertEqual(settled["findings"][0]["ratings"]["opus"]["severity"], "low")
        self.assertIn("astra: high", Path(settled["fix_brief"]).read_text())
        self.assertIn("Coordinator: Only zero-divisor", Path(settled["fix_brief"]).read_text())
        events = self.card.load()["log"]
        self.assertTrue(any(e["event"] == "finding.severity-assessed" for e in events))
        snapshot = next(e for e in events if e["event"] == "review.round-summary")
        self.assertEqual(snapshot["findings"], settled["findings"])
        messages = [notifications.message(self.card, e) for e in events if e["event"] == "review.summary-message"]
        self.assertIn("astra: blocker, важность high", "".join(messages))
        self.assertIn("[medium]", "".join(messages))

    def panel_with_fallback(self, failure="quota", peer="fable"):
        members = self.strict_panel()
        # Leave the rendezvous in place and substitute only the primary's response.
        primary = Path(members[0]["argv"][1])
        text = primary.read_text()
        if failure == "quota":
            text = text.replace("print(json.dumps(report))", "print('HTTP 429 subscription quota', file=sys.stderr); sys.exit(3)")
        elif failure == "malformed":
            text = text.replace("print(json.dumps(report))", "print('quota in untrusted prose')")
        primary.write_text(text)
        program = Path(members[1]["argv"][1]).read_text().replace('"opus":{', f'"{peer}":{{').replace('"o1"', '"fable-session"')
        members[0]["fallbacks"] = [{"model":peer, "tier":"frontier", "output":"claude-json",
                                    "argv":self.reviewer("peer", program)["argv"]}]
        self.set_policy(review={"panel":members, "panel_required":True, "author_model":"writer"})
        return members

    def test_quota_starts_peer_before_other_reviewer_finishes(self):
        members = self.panel_with_fallback()
        other = Path(members[1]["argv"][1])
        other.write_text(other.read_text().replace('root / "astra.started"', 'root / "peer.started"'))
        peer = Path(members[0]["fallbacks"][0]["argv"][1])
        peer.write_text(peer.read_text().replace('root / "opus.started"', 'root / "peer.started"'))
        review.brief(self.card, self.repo)
        self.assertEqual([r["status"] for r in review.panel(self.card, self.repo)["panel"]], ["ok", "ok"])

    def test_external_failure_dispatches_attested_peer_and_records_degraded_diversity(self):
        self.panel_with_fallback()
        review.brief(self.card, self.repo)
        result = review.panel(self.card, self.repo)
        self.assertEqual([r["status"] for r in result["panel"]], ["ok","ok"], result)
        self.assertEqual(result["provider_diversity"], "degraded")
        self.assertEqual(review.panel(self.card, self.repo)["panel"], [])
        self.assertEqual(review.settle(self.card, self.repo)["outcome"], "accept")
        state = self.card.load()
        attempts = state["rounds"][0]["panel_attempts"]["astra"]
        self.assertEqual([(a["model"], a.get("error_class")) for a in attempts], [("astra","quota"),("fable",None)])
        self.assertEqual(attempts[-1]["runtime"]["session_id"], "fable-session")
        sent = [e["text"] for e in state["log"] if e["event"] == "review.summary-message"]
        self.assertEqual(sum("astra → fable" in m and "quota" in m for m in sent), 1)
        self.assertTrue(any("одного провайдера" in m for m in sent))

    def test_author_from_intra_turn_attempt_skips_primary_and_uses_peer(self):
        members = self.panel_with_fallback()
        # Permit the other process to complete without the excluded primary rendezvous.
        (self.root / "astra.started").write_text("never ran")
        with self.card.locked() as state:
            state["log"].append({"event":"model.attempt", "lane":"coder", "model":"gpt-6.2-astra"})
        review.brief(self.card, self.repo)
        result = review.panel(self.card, self.repo)
        self.assertEqual([r["status"] for r in result["panel"]], ["ok", "ok"], result)
        self.assertEqual((self.root / "astra.started").read_text(), "never ran")
        attempts = self.card.load()["rounds"][0]["panel_attempts"]["astra"]
        self.assertEqual([a.get("error_class") for a in attempts], ["identity_conflict", None])
        self.assertEqual(review.settle(self.card, self.repo)["provider_diversity"], "degraded")

    def test_production_astra_family_uses_fable_then_sol(self):
        from ai_dev import policy, review_routes
        config = policy.DEFAULTS["review"]
        slot = config["panel"][0]
        state = {"log": [{"event": "model.attempt", "lane": "coder", "model": "gpt-6.2-astra"}]}
        self.assertEqual(review_routes.identity("gpt-6-astra-900k"), "astra")
        rnd = {"panel": {slot["name"]: {"error_class": "identity_conflict"}}}
        peer = review_routes.reserve(state, config, rnd, slot, set())
        self.assertEqual(peer["model"], "claude-fable-5-2")
        rnd["panel"][slot["name"]] = {"error_class": "quota", "provider": "anthropic"}
        rnd["panel_attempts"] = {slot["name"]: [{"model": peer["model"]}]}
        self.assertEqual(review_routes.reserve(state, config, rnd, slot, set())["model"], "gpt-6.2-sol")
        state["log"].append({"event": "model.attempt", "lane": "coder", "model": "gpt-6-sol"})
        self.assertIsNone(review_routes.reserve(state, config, rnd, slot, set()))

    def test_terminal_cannot_move_the_card_around_the_gate(self):
        env = {"HERMES_KANBAN_TASK": "t_1"}
        for command, verb in (("hermes kanban --board demo complete t_1", "complete"),
                              ("cd /w && hermes kanban request-review t_1 --summary done", "request_review"),
                              ("hermes -p reviewer kanban --board=demo request-changes t_1", "request_changes")):
            for lane in ("coder", "reviewer"):
                self.assertIn("kanban_" + verb, gate.decide("terminal", {"command": command}, env=env, lane=lane))
        for command in ("hermes kanban --board demo schedule t_1 ai-dev-recovery-abc-wait-1",
                        "hermes kanban show t_1 --json", "hermes kanban comment t_1 complete soon",
                        "echo complete; hermes kanban list", "make complete"):
            self.assertIsNone(gate.decide("terminal", {"command": command}, env=env, lane="coder"))
        self.assertIsNone(gate.decide("terminal", {"command": "hermes kanban complete t_1"}, env=env, lane="planner"))
        self.assertIsNone(gate.decide("terminal", {"command": "hermes kanban complete t_1"}, env={}, lane="coder"))

    def test_safety_filter_refusal_moves_the_review_to_another_provider(self):
        from ai_dev import policy, review_routes
        member = {"output": "hermes-usage", "model": "gpt-6-astra"}
        refusal = b"This content was flagged for possible cybersecurity risk. Try rephrasing your request."
        done = subprocess.CompletedProcess([], 1, b"", refusal)
        self.assertEqual(review_routes.runner_failure(done, member, Path("missing"))[0], "provider_refused")
        crash = subprocess.CompletedProcess([], 1, b"", b"Traceback: the reviewer discussed a content policy")
        self.assertEqual(review_routes.runner_failure(crash, member, Path("missing"))[0], "process_failure")
        config = policy.DEFAULTS["review"]
        slot = config["panel"][0]
        state = {"log": [{"event": "model.attempt", "lane": "coder", "model": "gpt-6.2-astra"}]}
        rnd = {"panel": {slot["name"]: {"error_class": "provider_refused", "status": "unavailable",
                                         "provider": "openai-codex", "model": "gpt-6-astra"}}}
        peer = review_routes.reserve(state, config, rnd, slot, set())
        self.assertEqual(review_routes.PROVIDERS[peer["output"]], "anthropic")

    def test_sdk_rejected_model_is_a_provider_outage(self):
        from ai_dev import policy, review_routes
        member = {"output": "claude-json", "model": "claude-fable-5-2"}
        line = '[claude-code:unrecognized_model] {"model":"claude-fable-5-2","query_source":"sdk"}'
        done = subprocess.CompletedProcess([], 1, b"", line.encode())
        self.assertEqual(review_routes.runner_failure(done, member, Path("missing"))[0], "provider_unavailable")
        other = subprocess.CompletedProcess([], 1, b"", line.replace("fable-5-2", "opus-5-5").encode())
        self.assertEqual(review_routes.runner_failure(other, member, Path("missing"))[0], "process_failure")
        config = policy.DEFAULTS["review"]
        slot = config["panel"][0]
        state = {"log": [{"event": "model.attempt", "lane": "coder", "model": "gpt-6.2-astra"}]}
        saved = {"error_class": "process_failure", "status": "unavailable", "provider": "anthropic",
                 "model": "claude-fable-5-2", "reason": "exit 1: " + line}
        rnd = {"panel": {slot["name"]: saved}, "panel_attempts": {slot["name"]: [{"model": "claude-fable-5-2"}]}}
        self.assertEqual(review_routes.reserve(state, config, rnd, slot, set())["model"], "gpt-6.2-sol")
        saved["reason"] = "exit 1: segmentation fault"
        self.assertIsNone(review_routes.reserve(state, config, rnd, slot, set()))

    def test_malformed_successful_model_output_cannot_trigger_fallback(self):
        self.panel_with_fallback(failure="malformed")
        review.brief(self.card, self.repo)
        result = review.panel(self.card, self.repo)
        self.assertEqual(result["panel"][0]["error_class"], "invalid_report")
        self.assertFalse(any(e["event"] == "panel.fallback" for e in self.card.load()["log"]))

    def test_history_and_other_lanes_exclude_model_aliases(self):
        self.panel_with_fallback(peer="fable-900k")
        with self.card.locked() as state:
            state["log"].append({"event":"worker.model", "lane":"coder", "model":"fable"})
        review.brief(self.card, self.repo)
        self.assertEqual(review.panel(self.card, self.repo)["panel"][0]["status"], "unavailable")
        self.assertFalse(any(e["event"] == "panel.fallback" for e in self.card.load()["log"]))

    def test_duplicate_model_and_lower_tier_cannot_substitute(self):
        members = self.panel_with_fallback(peer="opus")
        review.brief(self.card, self.repo)
        self.assertEqual(review.panel(self.card, self.repo)["panel"][0]["status"], "unavailable")
        members[0]["fallbacks"][0]["tier"] = "standard"
        self.set_policy(review={"panel":members, "panel_required":True, "author_model":"writer"})
        with self.assertRaisesRegex(CardError, "higher tier"):
            review.panel(self.card, self.repo)

    def test_claude_model_must_match_its_cli_usage(self):
        result = json.dumps({"summary": "done", "findings": []})
        envelope = json.dumps({"result": result, "modelUsage": {"sonnet": {}}})
        with self.assertRaisesRegex(ValueError, "expected model"):
            review._extract(envelope, "claude-json", "opus")

    def test_answers_about_a_replaced_round_are_dropped(self):
        self.set_policy(review={"panel": [{"name": "second", "argv": ["slow-reviewer"]}]})
        review.brief(self.card, self.repo)
        real = subprocess.run

        def run(argv, *args, **kwargs):
            if argv[0] != "slow-reviewer":
                return real(argv, *args, **kwargs)
            self.write("calc.py", (self.repo / "calc.py").read_text() + "\n\ndef neg(a):\n    return -a\n")
            self.verified()
            review.brief(self.card, self.repo)
            return subprocess.CompletedProcess(argv, 0, json.dumps({"summary": "s", "findings": []}).encode(), b"")

        with patch.object(subprocess, "run", side_effect=run):
            with self.assertRaisesRegex(CardError, "round changed"):
                review.panel(self.card, self.repo)
        self.assertEqual(self.card.load()["rounds"][-1]["reports"], {})

    def test_required_panel_holds_the_round_until_it_answers(self):
        self.strict_panel(fail=True)
        review.brief(self.card, self.repo)
        result = review.panel(self.card, self.repo)
        self.assertEqual([r["status"] for r in result["panel"]], ["ok", "unavailable"])
        with self.assertRaisesRegex(CardError, "panel is required"):
            review.settle(self.card, self.repo)


class CommandLine(CardCase):
    def run_cli(self, *args):
        tool = Path(__file__).resolve().parents[1] / "bin" / "ai-dev-card"
        done = subprocess.run([PY, str(tool), *args], capture_output=True, text=True, cwd=self.repo)
        return done.returncode, done.stdout, done.stderr

    def test_a_worker_can_follow_the_next_step_from_status(self):
        code, out, _ = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("contract set", json.loads(out)["next"])
        contract = self.root / "contract.json"
        contract.write_text(json.dumps(CONTRACT))
        self.assertEqual(self.run_cli("contract", "set", "--file", str(contract))[0], 0)
        self.implement()
        code, out, _ = self.run_cli("verify")
        self.assertEqual((code, json.loads(out)["result"]), (0, "passed"))
        self.assertIn("kanban_request_review", json.loads(self.run_cli("status")[1])["next"])
        with patch.dict(os.environ, {"AI_DEV_ROLE": "reviewer"}):
            self.assertIn("review brief", json.loads(self.run_cli("status")[1])["next"])
        code, _, err = self.run_cli("review", "settle")
        self.assertEqual(code, 1)
        self.assertIn("review brief", err)

    def test_a_review_round_through_the_command_line(self):
        contract = self.root / "contract.json"
        contract.write_text(json.dumps(CONTRACT))
        self.run_cli("contract", "set", "--file", str(contract))
        self.implement()
        self.assertEqual(self.run_cli("verify")[0], 0)
        brief = json.loads(self.run_cli("review", "brief")[1])
        Path(brief["files"]["report"]).write_text(json.dumps({"summary": "s", "findings": [self.blocker()]}))
        code, out, err = self.run_cli("review", "submit", "--report", brief["files"]["report"], "--reviewer", "reviewer")
        self.assertEqual(code, 0, err)
        key = json.loads(out)["to_confirm"][0]
        code, _, err = self.run_cli("review", "settle")
        self.assertEqual(code, 2)
        self.assertIn(key, err)
        test = self.root / "test_repro.py"
        test.write_text(textwrap.dedent(self.repro()[1]).lstrip("\n"))
        code, out, err = self.run_cli("confirm", "--finding", key, "--file", f"{test}:test_repro.py",
                                      "--", PY, "-m", "unittest", "-q", "test_repro")
        self.assertEqual((code, json.loads(out)["status"]), (0, "confirmed"), err)
        settled = json.loads(self.run_cli("review", "settle")[1])
        self.assertEqual(settled["outcome"], "changes")
        self.assertIn("kanban_request_changes", settled["next"])
        self.assertIn("Must fix", json.loads(self.run_cli("review", "fix-brief")[1])["text"])
        listed = json.loads(self.run_cli("finding", "list")[1])["findings"]
        self.assertEqual([(f["key"], f["status"]) for f in listed], [(key, "confirmed")])
        self.assertEqual(self.run_cli("finding", "waive", key)[0], 1)




class AcceptanceEvidence(CardCase):
    def accepted(self):
        self.contract()
        self.implement()
        self.verified()
        review.brief(self.card, self.repo)
        self.report([])
        review.settle(self.card, self.repo)

    def imported(self, finding=None, **overrides):
        payload = {"source": "https://github.com/example/repo/pull/1#discussion_1",
                   "tree": gitio.snapshot(self.repo), "finding": finding or self.blocker(), **overrides}
        path = self.root / "hosted.json"
        path.write_text(json.dumps(payload))
        return findings.import_finding(self.card, self.repo, path)

    def test_hosted_import_is_idempotent_and_gates_settled_acceptance(self):
        self.accepted()
        old = self.card.load()["rounds"]
        key = self.imported()["key"]
        self.assertTrue(self.imported()["duplicate"])
        self.assertEqual(self.card.load()["rounds"], old)
        self.assertIn(key, self.decide("kanban_complete", "reviewer"))
        self.assertEqual(self.confirm(key)["status"], "confirmed")
        with self.assertRaisesRegex(CardError, "blocks acceptance"):
            findings.resolve(self.card, self.repo, key, "deferred", "old defect", ticket="https://example.com/issues/2")
        self.diagnose()
        self.assertIn("finding return", self.decide("kanban_request_changes", "reviewer"))
        self.assertEqual(findings.late_return(self.card, self.repo, "Confirmed contract violation")["number"], 1)
        self.assertEqual(findings.late_return(self.card, self.repo, "Retry")["number"], 1)
        self.diagnose("verification")
        self.diagnose("review")
        self.assertIsNone(self.decide("kanban_request_changes", "reviewer"))

    def test_hosted_comment_for_another_tree_is_refused(self):
        self.accepted()
        with self.assertRaisesRegex(CardError, "current reviewed tree"):
            self.imported(tree="0" * 40)
        self.assertEqual(self.card.load()["ledger"], {})

    def test_baseline_followup_is_distinct_from_fix_and_requires_link(self):
        self.accepted()
        key = self.imported(self.blocker(**{"class": "discovery", "violates": "regression"}))["key"]
        self.assertIsNotNone(self.decide("kanban_complete", "reviewer"))
        with self.assertRaises(CardError):
            findings.resolve(self.card, self.repo, key, "deferred", "Exists on base")
        findings.triage(self.card, key, "M", "followup", "Requires an independent subsystem change")
        findings.resolve(self.card, self.repo, key, "deferred", "Outside this contract; exists on base", ticket="https://example.com/issues/2")
        self.assertEqual(self.card.load()["ledger"][key]["status"], "deferred")
        self.assertIsNone(self.decide("kanban_complete", "reviewer"))

    def test_low_contract_note_still_needs_a_disposition(self):
        self.accepted()
        key = self.imported(self.blocker(severity="low"))["key"]
        self.assertIn(key, findings.pending(self.card.load()))
        with self.assertRaisesRegex(CardError, "Only low non-contract"):
            findings.resolve(self.card, self.repo, key, "not-planned", "It was low")
        findings.resolve(self.card, self.repo, key, "resolved", "Source proves this was mistaken", "calc.py")
        self.assertIsNone(self.decide("kanban_complete", "reviewer"))

    def test_confirmed_defect_cannot_be_closed_by_a_source_citation(self):
        self.accepted()
        key = self.imported()["key"]
        self.confirm(key)
        with self.assertRaisesRegex(CardError, "reproduction"):
            findings.resolve(self.card, self.repo, key, "resolved", "Looks fixed", "calc.py")

    def test_two_late_returns_do_not_authorize_contract_deferral(self):
        self.accepted()
        key = self.imported()["key"]
        self.confirm(key)
        with self.card.locked() as state:
            state["late_returns"] = [{"tree": "a"}, {"tree": "b"}]
        with self.assertRaisesRegex(CardError, "recover review"):
            findings.late_return(self.card, self.repo, "third")
        with self.assertRaisesRegex(CardError, "blocks acceptance"):
            findings.resolve(self.card, self.repo, key, "deferred", "cap", ticket="https://example.com/issues/2")
        self.assertIsNotNone(self.decide("kanban_complete", "reviewer"))

    def test_unknown_check_is_rejected_even_in_legacy_mode(self):
        self.contract({"criteria": ["works"], "scenarios": [{"id": "case", "operation": "run", "expected": "pass", "check": "typo"}]})
        self.implement()
        with self.assertRaisesRegex(CardError, "unknown check"):
            self.verified()

    def test_strict_coverage_requires_scenarios_for_each_criterion(self):
        self.set_policy(require_scenario_evidence=True)
        self.contract()
        self.implement()
        with self.assertRaisesRegex(CardError, "needs check or manual"):
            self.verified()
        self.contract({"criteria": ["add works", "div works"], "scenarios": [
            {"id": "suite", "operation": "run suite", "expected": "pass", "check": "unit", "criteria": [1]}]})
        with self.assertRaisesRegex(CardError, "Criteria \\[2\\]"):
            self.verified()
        self.contract({"criteria": ["add works", "div works"], "scenarios": [
            {"id": "suite", "operation": "run suite", "expected": "pass", "check": "unit", "criteria": [1, 2]}]})
        self.verified()

    def test_manual_evidence_is_bound_to_candidate_at_completion(self):
        self.set_policy(require_scenario_evidence=True)
        proof = self.card.evidence("manual") / "result.json"
        self.contract({"criteria": ["works"], "scenarios": [
            {"id": "manual", "operation": "inspect", "expected": "works", "manual": str(proof), "criteria": [1]}]})
        self.implement()
        self.verified()
        review.brief(self.card, self.repo)
        self.report([])
        review.settle(self.card, self.repo)
        proof.write_text(json.dumps({"tree": "old", "scenario": "manual", "result": "pass", "observed": "worked"}))
        self.assertIn("Manual proof", self.decide("kanban_complete", "reviewer"))
        proof.write_text(json.dumps({"tree": gitio.snapshot(self.repo), "scenario": "manual", "result": "pass", "observed": "worked"}))
        self.assertIsNone(self.decide("kanban_complete", "reviewer"))

    def test_pr_only_gate_requires_exact_ci_receipt(self):
        self.accepted()
        self.set_policy(delivery="pr", pr_ci={"repository": "example/repo", "branch": "main", "ci_workflow": "ci.yml"})
        self.assertIn("check-pr", self.decide("kanban_complete", "reviewer"))
        with patch.object(release, "require_pr_ci", return_value={} ) as check:
            self.assertIsNone(gate.decide("kanban_complete", {"metadata": {"published_pr": "https://github.com/example/repo/pull/1"}}, lane="reviewer"))
            self.assertEqual(check.call_args.args[-1], "https://github.com/example/repo/pull/1")

    def test_cli_import_and_resolve(self):
        self.accepted()
        payload = {"source": "https://example.com/comment/1", "tree": gitio.snapshot(self.repo),
                   "finding": self.blocker(**{"class": "discovery", "violates": "regression"})}
        path = self.root / "comment.json"
        path.write_text(json.dumps(payload))
        from ai_dev import cli
        with patch("sys.stdout"):
            self.assertEqual(cli.main(["finding", "import", "--file", str(path)]), 0)
            key = next(iter(self.card.load()["ledger"]))
            self.assertEqual(cli.main(["finding", "triage", key, "--size", "M", "--reason", "Separate subsystem"]), 0)
            self.assertEqual(cli.main(["finding", "resolve", key, "--outcome", "deferred", "--ticket", "https://example.com/2", "--reason", "baseline"]), 0)

    def test_hosted_gap_can_be_assessed_after_acceptance(self):
        self.accepted()
        key = self.imported(self.blocker(kind="gap"))["key"]
        review.assess(self.card, self.repo, key, "medium", "Criterion lacks a test", "test_calc.py")
        self.assertIsNotNone(self.decide("kanban_complete", "reviewer"))
        findings.resolve(self.card, self.repo, key, "resolved", "Reviewed evidence", "test_calc.py")
        self.assertIsNone(self.decide("kanban_complete", "reviewer"))

    def test_unconfirmed_contract_findings_have_an_owner_exit(self):
        self.accepted()
        key = self.imported()["key"]
        review.confirm(self.card, self.repo, key, cannot="No representative stand available")
        self.assertIn(key, findings.pending(self.card.load()))
        review.waive(self.card, key, "owner-message-1", "Explicitly accepted limitation")
        self.assertIsNone(self.decide("kanban_complete", "reviewer"))

    def test_new_evidence_reopens_a_waived_mechanism(self):
        self.accepted()
        key = self.imported()["key"]
        review.waive(self.card, key, "owner-message", "Accepted previous evidence")
        self.imported(self.blocker(**{"class": "note", "actual": "New evidence"}))
        self.assertEqual(self.card.load()["ledger"][key]["status"], "note")
        self.assertIn(key, findings.pending(self.card.load()))

    def test_old_verification_log_cannot_resolve_current_gap(self):
        self.accepted()
        old = self.card.load()["verifications"][-1]["checks"][0]["log"]
        self.write("calc.py", (self.repo / "calc.py").read_text() + "\n# new candidate\n")
        self.verified()
        review.brief(self.card, self.repo)
        self.report([])
        key = self.imported(self.blocker(**{"class": "note"}))["key"]
        with self.assertRaisesRegex(CardError, "not tied to this candidate"):
            findings.resolve(self.card, self.repo, key, "resolved", "Claiming old proof", old)
        current = self.card.load()["verifications"][-1]["checks"][0]["log"]
        findings.resolve(self.card, self.repo, key, "resolved", "Current proof", current)

    def test_late_return_authority_is_scoped_to_native_run(self):
        self.accepted()
        key = self.imported()["key"]
        self.confirm(key)
        self.diagnose()
        findings.late_return(self.card, self.repo, "first")
        for number in (2, 3):
            with patch.dict(os.environ, {"HERMES_KANBAN_RUN_ID": f"run-{number}"}):
                self.assertIn("finding return", self.decide("kanban_request_changes", "reviewer"))
                self.assertEqual(findings.late_return(self.card, self.repo, "resumed session")["number"], number)
                self.assertEqual(self.card.load()["attempt"], 2)

    def test_new_pre_review_check_invalidates_completion(self):
        self.accepted()
        self.set_policy(checks=[{"name": "unit", "argv": SUITE}, {"name": "new", "argv": [PY, "-c", "pass"]}])
        self.assertIn("current pre-review", self.decide("kanban_complete", "reviewer"))
        self.verified()
        self.assertIsNone(self.decide("kanban_complete", "reviewer"))

    def test_demoted_late_gap_never_becomes_an_unsupported_status(self):
        self.accepted()
        key = self.imported(self.blocker(**{"class": "note"}))["key"]
        findings.resolve(self.card, self.repo, key, "resolved", "Source evidence", "calc.py")
        self.write("calc.py", (self.repo / "calc.py").read_text() + "\n# next candidate\n")
        self.verified()
        review.brief(self.card, self.repo)
        self.report([])
        self.imported(self.blocker(kind="gap"))
        self.assertEqual(self.card.load()["ledger"][key]["status"], "note")
        self.assertIn(key, findings.pending(self.card.load()))

    def test_small_baseline_defect_is_fixed_in_the_current_card(self):
        self.accepted()
        key = self.imported(self.blocker(violates="regression", mechanism="baseline zero addition"))["key"]
        self.confirm(key, body="""
            import unittest, calc
            class R(unittest.TestCase):
                def test_zero_add(self):
                    with self.assertRaises(ValueError): calc.add(0, 0)
            """)
        self.assertEqual(self.card.load()["ledger"][key]["status"], "discovery")
        decision = findings.triage(self.card, key, "S", None, "One local guard, compatible with the current scope")
        self.assertEqual((decision["decision"], decision["priority"]), ("inline", "P2"))
        with self.assertRaisesRegex(CardError, "test to pass"):
            findings.resolve(self.card, self.repo, key, "resolved", "Not actually fixed", "calc.py")
        self.diagnose()
        findings.late_return(self.card, self.repo, "Small baseline correction selected inline")
        self.diagnose("verification")
        self.diagnose("review")
        self.assertIsNone(self.decide("kanban_request_changes", "reviewer"))
        self.write("calc.py", "def add(a,b):\n    if a == b == 0: raise ValueError('zero')\n    return a+b\n\ndef div(a,b):\n    return a/b\n")
        self.verified()
        packet = review.brief(self.card, self.repo)
        self.assertTrue(packet["rechecks"][0]["fixed"])
        self.report([])
        self.assertEqual(review.settle(self.card, self.repo)["outcome"], "accept")
        self.assertIsNone(self.decide("kanban_complete", "reviewer"))
        self.assertEqual(self.card.load()["ledger"][key]["triage"]["decision"], "inline")

    def test_baseline_followup_requires_importance_and_complexity_analysis(self):
        self.accepted()
        key = self.imported(self.blocker(**{"class":"discovery", "violates":"regression"}))["key"]
        with self.assertRaisesRegex(CardError, "Analyze importance"):
            findings.resolve(self.card, self.repo, key, "deferred", "Baseline", ticket="https://example.com/2")
        with self.assertRaises(CardError):
            findings.triage(self.card, key, "M", "not-planned", "Material but inconvenient")
        decision = findings.triage(self.card, key, "M", None, "Needs a separate migration and rollback plan")
        self.assertEqual(decision["decision"], "followup")
        findings.resolve(self.card, self.repo, key, "deferred", "Separate migration", ticket="https://example.com/2")
        self.assertEqual(self.card.load()["ledger"][key]["disposition"]["triage"]["size"], "M")

    def test_small_but_risky_baseline_fix_can_have_an_explicit_followup(self):
        self.accepted()
        key = self.imported(self.blocker(**{"class":"discovery", "violates":"regression"}))["key"]
        findings.triage(self.card, key, "S", "followup", "Few lines but requires an incompatible schema decision")
        findings.resolve(self.card, self.repo, key, "deferred", "Compatibility decision", ticket="https://example.com/2")
        self.assertIsNone(self.decide("kanban_complete", "reviewer"))

    def test_baseline_triage_cannot_defer_a_promised_contract_fix(self):
        self.accepted()
        key = self.imported(self.blocker(**{"class":"discovery"}))["key"]
        with self.assertRaisesRegex(CardError, "contract already promises"):
            findings.triage(self.card, key, "M", "followup", "Old but promised")

    def test_low_baseline_finding_still_needs_size_and_scope_triage(self):
        self.accepted()
        key = self.imported(self.blocker(**{"class":"discovery", "violates":"regression", "severity":"low"}))["key"]
        self.assertIn(key, findings.pending(self.card.load()))
        decision = findings.triage(self.card, key, "M", None, "Low impact and unrelated cleanup")
        self.assertEqual(decision["decision"], "not-planned")
        findings.resolve(self.card, self.repo, key, "not-planned", "Cost exceeds benefit for this scope")
        self.assertIsNone(self.decide("kanban_complete", "reviewer"))

    def test_panel_discovery_can_choose_inline_after_acceptance(self):
        self.accepted()
        self.write("calc.py", (self.repo / "calc.py").read_text() + "\n# next round\n")
        self.verified()
        review.brief(self.card, self.repo)
        key = self.report([self.blocker(**{"class":"discovery", "violates":"regression"})])["findings"][0]["key"]
        review.settle(self.card, self.repo)
        findings.triage(self.card, key, "S", None, "Small local correction")
        self.diagnose()
        self.assertEqual(findings.late_return(self.card, self.repo, "Selected inline")["number"], 1)
        self.diagnose("verification")
        self.diagnose("review")
        self.assertIsNone(self.decide("kanban_request_changes", "reviewer"))

    def test_mixed_panel_note_retains_baseline_triage_obligation(self):
        self.accepted()
        self.write("calc.py", (self.repo / "calc.py").read_text() + "\n# new round\n")
        self.verified()
        review.brief(self.card, self.repo)
        key = self.report([self.blocker(**{"class":"note", "violates":"regression"})])["findings"][0]["key"]
        self.report([self.blocker(**{"class":"discovery", "violates":"regression"})], "second")
        self.assertTrue(findings.baseline(self.card.load()["ledger"][key]))
        findings.triage(self.card, key, "S", None, "Small local correction")
        self.assertEqual(review.settle(self.card, self.repo)["outcome"], "changes")


if __name__ == "__main__":
    unittest.main()


class DiagnosedAttempts(CardCase):
    def test_five_review_attempts_require_diagnosis_and_then_stop(self):
        self.contract()
        for attempt in range(1, 6):
            self.implement(f"def div(a, b):\n    return a / b  # attempt {attempt}\n")
            self.verified()
            review.brief(self.card, self.repo)
            if attempt == 1:
                key = self.report([self.blocker()])["to_confirm"][0]
                self.confirm(key)
            else:
                self.report([])
            result = review.settle(self.card, self.repo)
            self.assertEqual(result["outcome"], "changes" if attempt < 5 else "owner")
            self.assertIn("recover review", self.decide("kanban_request_changes", "reviewer"))
            self.assertIn("recover review", self.decide("kanban_request_review", "coder"))
            diagnosis = self.diagnose()
            self.assertEqual(diagnosis["action"], "resume" if attempt < 5 else "handoff")
            if attempt < 5:
                self.assertIsNone(self.decide("kanban_request_changes", "reviewer"))
                self.assertIn("already failed review", self.decide("kanban_request_review", "coder"))
        self.assertEqual(self.card.load()["attempt"], 5)
        self.assertIn("exhausted", self.decide("kanban_request_changes", "reviewer"))
        self.implement("def div(a, b):\n    if not b: raise ValueError('zero')\n    return a / b\n")
        with self.assertRaisesRegex(CardError, "exhausted"):
            self.verified()

    def test_owner_extension_allows_exactly_one_more_review_attempt(self):
        for extension in ("rounds", "failures"):
            with self.subTest(extension=extension):
                self.card = Card("test", extension)
                os.environ["HERMES_KANBAN_TASK"] = extension
                self.contract()
                self.implement()
                self.verified()
                review.brief(self.card, self.repo)
                key = self.report([self.blocker()])["to_confirm"][0]
                self.confirm(key)
                with self.card.locked() as state:
                    state["attempt"] = 5
                self.assertEqual(review.settle(self.card, self.repo)["outcome"], "owner")
                self.assertEqual(self.diagnose()["action"], "handoff")
                review.extend(self.card, "owner/message", **{extension: 1})
                self.assertEqual(self.diagnose()["action"], "resume")
                self.assertEqual(self.card.load()["attempt"], 6)
                self.assertEqual(review.settle(self.card, self.repo)["outcome"], "changes")
                self.assertIsNone(self.decide("kanban_request_changes", "reviewer"))
                self.assertEqual(self.diagnose()["attempt"], 6)
                self.implement("def div(a, b):\n    return a / b  # correction six\n")
                self.verified()
                review.brief(self.card, self.repo)
                self.report([])
                self.assertEqual(review.settle(self.card, self.repo)["outcome"], "owner")
                self.assertEqual(self.diagnose()["action"], "handoff")
                self.assertIn("exhausted", self.decide("kanban_request_changes", "reviewer"))

    def test_inheritance_cannot_reset_an_exhausted_attempt_budget(self):
        from ai_dev import continuity
        self.contract()
        with self.card.locked() as state:
            state["attempt"] = 5
        target = Card("test", "rewrite")
        continuity.inherit(target, self.card)
        self.implement()
        with self.assertRaisesRegex(CardError, "inherited attempt budget"):
            verify.verify(target, self.repo)
        self.assertEqual(target.load()["attempt"], 6)
        self.assertEqual(self.card.load()["attempt"], 5)

    def test_confirmed_critical_regression_cannot_be_deferred_at_limit(self):
        self.contract()
        self.implement()
        self.verified()
        review.brief(self.card, self.repo)
        key = self.report([self.blocker(violates="regression", severity="critical")])["to_confirm"][0]
        with self.card.locked() as state:
            entry = state["ledger"][key]
            entry.update(status="confirmed", sources=[{"url": "https://example.org/review/1"}])
            state["attempt"] = 5
            state["late_returns"] = [{"tree": "a"}, {"tree": "b"}]
        with self.assertRaisesRegex(CardError, "blocks acceptance"):
            findings.resolve(self.card, self.repo, key, "deferred", "attempt limit", ticket="https://example.org/issues/1")
        with self.card.locked() as state:
            state["ledger"][key]["severity"] = "medium"
            state["rounds"][-1].update(outcome="accept", fix_brief="brief")
        with self.assertRaisesRegex(CardError, "blocks acceptance"):
            findings.resolve(self.card, self.repo, key, "deferred", "no final diagnosis", ticket="https://example.org/issues/1")
        self.assertEqual(self.diagnose()["action"], "handoff")
        findings.resolve(self.card, self.repo, key, "deferred", "Diagnosed non-contract work", ticket="https://example.org/issues/1")
        self.assertEqual(self.card.load()["ledger"][key]["status"], "deferred")


class ProjectPreflight(CardCase):
    def configured(self, **options):
        self.set_policy(preflight={"checks": [{"name": "unit", "argv": SUITE,
                                              "tests_pattern": r"Ran (?P<count>\d+) tests?"}], **options})
        self.contract()

    def test_base_receipt_precedes_changes_and_gates_verification(self):
        from ai_dev import preflight
        self.configured()
        self.assertIn('preflight', self.decide('kanban_request_review', 'coder'))
        receipt, code = preflight.run(self.card, self.repo)
        self.assertEqual(code, 0)
        self.assertTrue(receipt['before_candidate_changes'])
        self.assertEqual(receipt['checks'][0]['test_count'], 1)
        self.implement()
        self.verified()
        self.assertIsNone(self.decide('kanban_request_review', 'coder'))
        self.assertEqual(preflight.run(self.card, self.repo)[0]['id'], receipt['id'])

    def test_missing_receipt_blocks_even_existing_verification(self):
        self.contract()
        self.implement()
        self.verified()
        self.set_policy(preflight={"checks": [{"name": "unit", "argv": SUITE,
                                              "tests_pattern": r"Ran (?P<count>\d+) tests?"}]})
        self.assertIn('preflight', self.decide('kanban_request_review', 'coder'))
        with self.assertRaisesRegex(CardError, 'preflight'):
            verify.verify(self.card, self.repo)

    def test_candidate_changes_do_not_contaminate_baseline(self):
        from ai_dev import preflight
        self.configured()
        self.write('calc.py', 'def add(a, b): return 0\n')
        candidate = gitio.snapshot(self.repo)
        receipt, code = preflight.run(self.card, self.repo)
        self.assertEqual(code, 0)
        self.assertFalse(receipt['before_candidate_changes'])
        self.assertEqual(gitio.snapshot(self.repo), candidate)
        self.assertEqual(self.card.load()['counters'], {'defect': 0, 'environment': 0})

    def test_zero_tests_cannot_be_acknowledged(self):
        from ai_dev import preflight
        self.set_policy(preflight={'checks': [{'name': 'empty', 'argv': [PY, '-c', "print('Ran 0 tests')"],
                                             'tests_pattern': r'Ran (?P<count>\d+) tests?'}]})
        self.contract()
        receipt, code = preflight.run(self.card, self.repo)
        self.assertEqual(code, 2)
        self.assertEqual(receipt['checks'][0]['klass'], 'environment')
        with self.assertRaisesRegex(CardError, 'Only executed'):
            preflight.acknowledge(self.card, self.repo, 'empty', 'irrelevant', 'reference')

    def test_baseline_defect_needs_assessment_and_does_not_spend_attempt(self):
        from ai_dev import preflight
        self.write('calc.py', 'def add(a, b): return 0\n')
        self.git('add', 'calc.py')
        self.git('commit', '-qm', 'Existing defect')
        self.git('branch', '-f', 'main', 'HEAD')
        self.configured()
        receipt, code = preflight.run(self.card, self.repo)
        self.assertEqual(code, 2)
        self.assertEqual(receipt['checks'][0]['klass'], 'baseline_failure')
        receipt, code = preflight.acknowledge(self.card, self.repo, 'unit', 'Existing add defect; scoped separately', 'base log')
        self.assertEqual(code, 0)
        self.assertEqual(receipt['checks'][0]['klass'], 'baseline_failure')
        self.assertEqual(self.card.load().get('attempt', 1), 1)
        self.assertEqual(self.card.load()['counters']['defect'], 0)
        receipt, code = preflight.run(self.card, self.repo, rerun=True)
        self.assertEqual(code, 2)
        self.assertNotIn('assessment', receipt['checks'][0])

    def test_changed_policy_invalidates_receipt(self):
        from ai_dev import preflight
        self.configured()
        preflight.run(self.card, self.repo)
        settings = json.loads(self.policy.read_text())
        settings['defaults']['preflight']['checks'][0]['timeout'] = 5
        self.policy.write_text(json.dumps(settings))
        self.assertIn('stale', self.decide('kanban_request_review', 'coder'))

    def test_missing_executable_and_modified_source_are_environment_failures(self):
        from ai_dev import preflight
        for argv in (['/nonexistent/ratchet-tool'], [PY, '-c', "open('calc.py','w').write('changed')"]):
            self.set_policy(preflight={'checks': [{'name':'readiness', 'argv':argv}], 'no_tests_reason':'Documentation-only fixture'})
            self.contract()
            receipt, code = preflight.run(self.card, self.repo, rerun=True)
            self.assertEqual(code, 2)
            self.assertTrue(any(c['klass']=='environment' for c in receipt['checks']))
            self.assertIn('def add', (self.repo/'calc.py').read_text())

    def test_project_without_tests_needs_explicit_operator_reason(self):
        from ai_dev import preflight
        with self.assertRaisesRegex(CardError, 'tests_pattern'):
            preflight.validate({'checks':[{'name':'syntax','argv':[PY,'--version']}]})
        self.set_policy(preflight={'checks':[{'name':'syntax','argv':[PY,'--version']}],
                                   'no_tests_reason':'Documentation project; syntax check is the applicable validation'})
        self.contract()
        self.assertEqual(preflight.run(self.card, self.repo)[1], 0)

    def test_stand_receives_base_without_candidate_patch_and_restores_power(self):
        from ai_dev import preflight, stands
        self.configured()
        self.set_policy(project='test', preflight={'checks':[{'name':'unit','argv':SUITE,'stand':'vm',
                                                            'tests_pattern':r'Ran (?P<count>\d+) tests?'}]})
        self.implement()
        driver=self.root/'vm'
        def execute(argv, cwd, log, timeout):
            log.write_text('Ran 1 test\n')
            return 0, None, 'pass', 1
        from contextlib import nullcontext
        with patch.object(stands,'registered',return_value=driver), patch.object(stands,'exclusive',return_value=nullcontext()), \
             patch.object(stands,'call',side_effect=[(0,'stopped'),(0,'')]) as call, \
             patch.object(stands,'prepare',return_value=None) as prepare, patch.object(verify,'run',side_effect=execute):
            self.assertEqual(preflight.run(self.card,self.repo)[1],0)
        self.assertEqual(prepare.call_args.args[3],b'')
        self.assertEqual(call.call_args.args,(driver,'shutdown'))


class TaskAnalysis(CardCase):
    def analysis(self, kind='chat', **changes):
        return {'source': {'kind':kind,'reference':'thread/message-1','text':'Fix division'},
                'title':'Handle zero','problem':'Unclear zero behavior','outcome':'Predictable error',
                'repository':str(self.repo),'scope':['division'], 'non_goals':[],
                'criteria':CONTRACT['criteria'], 'constraints':[], 'facts':['Read calc.py'], 'hypotheses':[],
                'evidence':[{'source':'code','question':'Current behavior?','status':'checked','reference':'base:calc.py','summary':'No zero guard'}],
                'unknowns':[], 'risks':[{'area':'compatibility','assessment':'Preserve ordinary addition'}],
                'decomposition':{'decision':'single','reason':'One behavior','children':[]},
                'budget':{'minutes':15,'used_minutes':3}, 'readiness':'ready', **changes}

    def plan(self, revision=1):
        from ai_dev import intake
        return intake.save_plan(self.card, {'analysis_revision':revision,'steps':['Add zero guard'],
            'validation':['Run regression test'], 'rollback':'Revert candidate before release', 'authority':'Owner message-1'})

    def test_chat_and_ticket_use_same_gate(self):
        from ai_dev import intake
        for kind in ('chat','ticket'):
            self.card=Card('test',kind)
            os.environ['HERMES_KANBAN_TASK']=kind
            self.set_policy(require_task_analysis=True)
            self.contract()
            self.assertIn('not ready',self.decide('kanban_request_review','coder'))
            intake.save_analysis(self.card,self.analysis(kind))
            self.assertIn('plan is missing',intake.issue(self.card.load(),policy.load(),self.repo))
            self.plan()
            self.implement()
            self.verified()
            self.assertIsNone(intake.issue(self.card.load(),policy.load(),self.repo))

    def test_blocking_unknown_prevents_ready_and_plan(self):
        from ai_dev import intake
        data=self.analysis(unknowns=[{'question':'Required error type?','blocking':True,'next_step':'Ask owner'}])
        with self.assertRaisesRegex(CardError,'Resolve blocking'):
            intake.save_analysis(self.card,data)
        data['readiness']='needs_input'
        intake.save_analysis(self.card,data)
        with self.assertRaisesRegex(CardError,'before planning'):
            self.plan()

    def test_reanalysis_invalidates_plan_and_repeated_save_is_idempotent(self):
        from ai_dev import intake
        self.set_policy(require_task_analysis=True)
        self.contract()
        data=self.analysis()
        intake.save_analysis(self.card,data)
        self.plan()
        self.assertEqual(intake.save_analysis(self.card,data)['revision'],1)
        data['facts'].append('New relevant owner clarification')
        self.assertEqual(intake.save_analysis(self.card,data)['revision'],2)
        self.assertIn('stale',intake.issue(self.card.load(),policy.load(),self.repo))
        with self.assertRaisesRegex(CardError,'before planning'):
            self.plan(1)
        self.plan(2)
        self.assertIsNone(intake.issue(self.card.load(),policy.load(),self.repo))

    def test_original_chat_source_cannot_be_silently_rewritten(self):
        from ai_dev import intake
        intake.save_analysis(self.card,self.analysis())
        data=self.analysis()
        data['source']['text']='A different scope'
        with self.assertRaisesRegex(CardError,'Preserve the original'):
            intake.save_analysis(self.card,data)

    def test_contract_and_repository_must_match_analysis(self):
        from ai_dev import intake
        self.set_policy(require_task_analysis=True)
        self.contract()
        intake.save_analysis(self.card,self.analysis(repository='/different/repo'))
        self.plan()
        self.assertIn('another repository',intake.issue(self.card.load(),policy.load(),self.repo))
        intake.save_analysis(self.card,self.analysis(criteria=['Different promise']))
        self.plan(2)
        self.assertIn('disagree',intake.issue(self.card.load(),policy.load(),self.repo))
        self.implement()
        with self.assertRaisesRegex(CardError,'disagree'):
            verify.verify(self.card,self.repo)

    def test_redacted_requirements_compare_original_content(self):
        from ai_dev import intake
        self.set_policy(require_task_analysis=True)
        criteria = ["Request with an expired token: rejected with 401", "rejected is an outcome"]
        non_goals = ["Do not change password: example-value"]
        data = self.analysis(criteria=criteria, non_goals=non_goals)
        data["source"]["text"] = "Investigate secret=rejected"
        row = intake.save_analysis(self.card, data)
        self.assertNotIn("rejected", json.dumps(row))
        self.assertNotIn("example-value", json.dumps(row))
        self.assertEqual(intake.save_analysis(self.card, data)["revision"], 1)
        self.plan()
        contract = {"criteria": criteria, "non_goals": non_goals}
        self.contract(contract)
        self.assertIsNone(intake.issue(self.card.load(), policy.load(), self.repo))
        # Different raw requirements must not become equal through redaction.
        self.contract({**contract, "criteria": [criteria[0].replace("rejected", "accepted"), criteria[1]]})
        self.assertIn("disagree", intake.issue(self.card.load(), policy.load(), self.repo))
        self.contract(contract)
        self.contract({**contract, "non_goals": ["Do not change password: another-value"]})
        self.assertIn("disagree", intake.issue(self.card.load(), policy.load(), self.repo))

    def test_legacy_analysis_without_fingerprint_still_matches(self):
        from ai_dev import intake
        self.set_policy(require_task_analysis=True)
        self.contract()
        intake.save_analysis(self.card, self.analysis())
        with self.card.locked() as state:
            state["analyses"][-1].pop("contract_fingerprint")
        self.plan()
        self.assertIsNone(intake.issue(self.card.load(), policy.load(), self.repo))

    def test_split_root_cannot_get_implementation_plan(self):
        from ai_dev import intake
        data=self.analysis(readiness='split',decomposition={'decision':'split','reason':'Two repositories','children':['test/a','test/b']})
        intake.save_analysis(self.card,data)
        with self.assertRaisesRegex(CardError,'before planning'):
            self.plan()

    def test_unavailable_optional_source_does_not_require_fabricated_evidence(self):
        from ai_dev import intake
        data=self.analysis()
        data['evidence'].append({'source':'grafana','question':'Any impact spike?','status':'unavailable',
                                 'summary':'No project connector; reproduction is sufficient for this scope'})
        intake.save_analysis(self.card,data)
        self.plan()
        self.assertEqual(len(self.card.load()['analyses']),1)

    def test_checked_sources_require_reference_and_redact_credentials(self):
        from ai_dev import intake
        data=self.analysis()
        data['evidence'][0].pop('reference')
        with self.assertRaisesRegex(CardError,'reference'):
            intake.save_analysis(self.card,data)
        data=self.analysis()
        data['source']['text']='Fix failure password=secret-value'
        row=intake.save_analysis(self.card,data)
        self.assertNotIn('secret-value',json.dumps(row))
