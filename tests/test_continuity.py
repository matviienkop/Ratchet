"""Implementation rewrites retain acceptance obligations, not prior acceptance."""

import hashlib
from pathlib import Path

from test_card import CardCase
from ai_dev import continuity, findings, review, verify
from ai_dev.store import Card, CardError


class Continuity(CardCase):
    def setUp(self):
        super().setUp()
        self.contract()
        self.implement()
        self.verified()
        review.brief(self.card, self.repo)
        self.key = self.report([self.blocker()])["to_confirm"][0]
        self.confirm(self.key)
        self.target = Card("test", "rewrite")

    def test_reproduction_files_and_contract_survive_fresh_card(self):
        before = self.card.file.read_bytes()
        result = continuity.inherit(self.target, self.card)
        state = self.target.load()
        self.assertEqual(state["contract"], self.card.load()["contract"])
        self.assertEqual(state["rounds"], [])
        self.assertIsNone(state["base"])
        copied = state["ledger"][self.key]["confirm"]
        name, destination = next(iter(copied["files"].items()))
        original = self.card.load()["ledger"][self.key]["confirm"]["files"][name]
        self.assertNotEqual(destination, original)
        self.assertEqual(Path(destination).read_bytes(), Path(original).read_bytes())
        self.assertEqual(copied["files_sha256"][name], hashlib.sha256(Path(destination).read_bytes()).hexdigest())
        self.assertEqual(state["ledger"][self.key]["status"], "confirmed")
        self.assertEqual(result["source"]["card"], self.card.id)
        self.assertEqual(self.card.file.read_bytes(), before)

    def test_first_brief_rechecks_inherited_reproduction(self):
        continuity.inherit(self.target, self.card)
        verify.verify(self.target, self.repo)
        brief = review.brief(self.target, self.repo)
        self.assertEqual(brief["rechecks"][0]["key"], self.key)
        self.assertFalse(brief["rechecks"][0]["fixed"])

    def test_repaired_reimplementation_clears_the_stored_reproduction(self):
        continuity.inherit(self.target, self.card)
        self.implement("def div(a, b):\n    if b == 0:\n        raise ValueError('zero')\n    return a / b\n")
        verify.verify(self.target, self.repo)
        brief = review.brief(self.target, self.repo)
        self.assertTrue(brief["rechecks"][0]["fixed"])
        self.assertEqual(self.target.load()["ledger"][self.key]["status"], "fixed")

    def test_old_fixed_result_is_not_inherited_as_acceptance(self):
        with self.card.locked() as state:
            state["ledger"][self.key]["status"] = "fixed"
            state["ledger"][self.key]["disposition"] = {"outcome": "fixed"}
        continuity.inherit(self.target, self.card)
        entry = self.target.load()["ledger"][self.key]
        self.assertEqual(entry["status"], "confirmed")
        self.assertEqual(entry["inherited_from"]["status"], "fixed")
        self.assertNotIn("disposition", entry)

    def test_waiver_and_manual_evidence_do_not_transfer(self):
        with self.card.locked() as state:
            entry = state["ledger"][self.key]
            entry.pop("confirm")
            entry["status"] = "waived"
            entry["disposition"] = {"outcome": "waived", "owner_ref": "old-owner-message"}
            state["contract"]["scenarios"][0]["manual"] = str(self.card.dir / "evidence" / "manual.json")
        continuity.inherit(self.target, self.card)
        state = self.target.load()
        self.assertEqual(state["ledger"][self.key]["status"], "candidate")
        self.assertNotIn("disposition", state["ledger"][self.key])
        evidence = Path(state["contract"]["scenarios"][0]["manual"])
        self.assertTrue(evidence.is_relative_to(self.target.dir))
        self.assertFalse(evidence.exists())

    def test_baseline_discovery_and_followup_need_fresh_confirmation(self):
        self.report([self.blocker(violates="regression", mechanism="baseline sum mismatch")])
        key = next(k for k, entry in self.card.load()["ledger"].items()
                   if entry["mechanism"] == "baseline sum mismatch")
        proof = self.confirm(key, body="""
            import unittest, calc
            class R(unittest.TestCase):
                def test_sum(self):
                    self.assertEqual(calc.add(2, 2), 5)
            """)
        self.assertEqual(proof["status"], "discovery")
        ticket = "https://example.org/issues/baseline-sum"
        for old_status in ("discovery", "deferred"):
            with self.subTest(old_status=old_status):
                if old_status == "deferred":
                    findings.triage(self.card, key, "M", "followup", "Separate behavior change")
                    findings.resolve(self.card, self.repo, key, "deferred", "Existing baseline behavior", ticket=ticket)
                target = Card("test", "rewrite-" + old_status)
                continuity.inherit(target, self.card)
                entry = target.load()["ledger"][key]
                self.assertEqual(entry["status"], "candidate")
                self.assertNotIn("disposition", entry)
                if old_status == "deferred":
                    self.assertEqual(entry["inherited_from"]["ticket"], ticket)
                verify.verify(target, self.repo)
                brief = review.brief(target, self.repo)
                self.assertNotIn(key, [check["key"] for check in brief["rechecks"]])
                copied = entry["confirm"]
                fresh = review.confirm(target, self.repo, key,
                                       files=[(path, name) for name, path in copied["files"].items()],
                                       argv=copied["argv"])
                self.assertEqual(fresh["status"], "discovery")
                findings.triage(target, key, "M", "followup", "Still requires separate work")
                result = findings.resolve(target, self.repo, key, "deferred", "Still existing on the new base", ticket=ticket)
                self.assertEqual(result["status"], "deferred")

    def test_existing_target_and_same_card_are_rejected(self):
        with self.assertRaisesRegex(CardError, "different source"):
            continuity.inherit(self.card, self.card)
        continuity.inherit(self.target, self.card)
        before = self.target.file.read_bytes()
        with self.assertRaisesRegex(CardError, "fresh card"):
            continuity.inherit(self.target, self.card)
        self.assertEqual(self.target.file.read_bytes(), before)

    def test_tampered_or_outside_reproduction_is_rejected(self):
        with self.card.locked() as state:
            state["ledger"][self.key]["confirm"]["files_sha256"] = {"test_repro.py": "0" * 64}
        with self.assertRaisesRegex(CardError, "changed after confirmation"):
            continuity.inherit(self.target, self.card)
        self.assertFalse(self.target.exists)
        with self.card.locked() as state:
            proof = state["ledger"][self.key]["confirm"]
            proof.pop("files_sha256")
            proof["files"]["test_repro.py"] = str(self.repo / "calc.py")
        with self.assertRaisesRegex(CardError, "outside its evidence"):
            continuity.inherit(self.target, self.card)
        self.assertFalse(self.target.exists)
