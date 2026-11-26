import json
from unittest.mock import patch

from test_card import CardCase, SUITE
from ai_dev import recovery, recovery_wake, review, verify
from ai_dev.store import CardError


class Recovery(CardCase):
    def completed(self, child, marker):
        return {"task": {"id":child, "status":"done", "body":marker + "\nDiagnosis", "skills":["ai-dev-repair"]},
                "children":[self.card.id], "latest_summary":"Base probe passed after fixture repair; rerun the check."}

    def test_one_verified_diagnosis_before_another_failed_check(self):
        self.set_policy(max_defect_failures=1)
        self.contract()
        self.write("calc.py", "def add(a, b): return 0\n")
        self.assertEqual(verify.verify(self.card, self.repo)[1], 1)
        request = recovery.recover(self.card, self.repo, "verification")[0]
        self.assertEqual(request["action"], "dispatch")
        self.assertEqual(request, recovery.recover(self.card, self.repo, "verification")[0])
        child = "t_x"
        detail = self.completed(child, request["repair_request"]["idempotency_key"])
        with patch.object(recovery, "native_show", return_value={**detail,"children":[]}):
            with self.assertRaises(CardError):
                recovery.recover(self.card, self.repo, "verification", child)
        with patch.object(recovery, "native_show", return_value=detail):
            for _ in range(2):
                self.assertEqual(recovery.recover(self.card, self.repo, "verification", child)[0]["action"], "resume")
        self.assertEqual(self.card.load()["extra_failures"], 1)
        self.write("calc.py", "def add(a, b): return -1\n")
        self.assertEqual(verify.verify(self.card, self.repo)[1], 1)
        self.assertEqual(recovery.recover(self.card, self.repo, "verification")[0]["action"], "handoff")

    def test_quota_wait_retries_only_after_event_and_deadline(self):
        self.policy.write_text(json.dumps({"defaults":{"require_scenario_evidence":False,"target_branch":"main","checks":[{"name":"unit","argv":SUITE}]}}))
        self.contract()
        self.implement()
        self.verified()
        review.brief(self.card, self.repo)
        with self.card.locked() as s:
            s["rounds"][-1]["panel"]["opus"] = {"status":"unavailable","reason":"quota exceeded"}
        with patch("time.time", return_value=100):
            wait = recovery.recover(self.card,self.repo,"provider")[0]
            self.assertEqual(wait["action"], "schedule")
            self.assertEqual(wait, recovery.recover(self.card,self.repo,"provider")[0])
            with self.assertRaisesRegex(CardError, "recover provider"):
                recovery.claim_provider_retry(self.card,self.card.load()["rounds"][-1],[{"name":"opus"}])
            self.assertFalse(recovery_wake.wake(self.card))
        with patch("time.time", return_value=wait["due"]+1), \
                patch.object(recovery_wake,"native_show", return_value={"task":{"status":"scheduled"},
                    "events":[{"kind":"scheduled","payload":{"reason":"operator pause"}}]}) as show, \
                patch("subprocess.run") as invoke:
            self.assertFalse(recovery_wake.wake(self.card))
            invoke.assert_not_called()
            show.return_value["events"][0]["payload"]["reason"] = wait["reason"]
            self.assertTrue(recovery_wake.wake(self.card))
            self.assertEqual(invoke.call_args.args[0][-2:],["unblock",self.card.id])
            recovery.claim_provider_retry(self.card,self.card.load()["rounds"][-1],[{"name":"opus"}])
            with self.assertRaises(CardError):
                recovery.claim_provider_retry(self.card,self.card.load()["rounds"][-1],[{"name":"opus"}])
        with patch("time.time", return_value=wait["due"]+1):
            second = recovery.recover(self.card,self.repo,"provider")[0]
            self.assertNotEqual(second["reason"],wait["reason"])

    def test_new_round_is_insufficient_without_successful_prior_reviewer(self):
        first={"round":1,"tree":"old","failed":{"astra":"quota"}}
        second={"round":2,"tree":"new","failed":{"opus":"quota"}}
        state={"rounds":[{"n":1,"panel":{"astra":{"status":"unavailable"}}},{"n":2,"panel":{}}]}
        self.assertFalse(recovery.progressed(state,"provider",first,second))
        state["rounds"][0]["panel"]["astra"]={"status":"ok","runtime":{"session_id":"actual"}}
        self.assertTrue(recovery.progressed(state,"provider",first,second))

    def test_same_review_blocker_does_not_reset_limit_but_resolved_prior_findings_do(self):
        self.contract()
        self.implement()
        self.verified()
        self.set_policy(review={"max_rounds":1})
        with self.card.locked() as state:
            state["rounds"] = [{"n":1,"tree":"one","outcome":"owner","blocking":["k1"],"fix_brief":"brief-1"}]
            state["ledger"] = {"k1":{"status":"confirmed"}}
        req = recovery.recover(self.card,self.repo,"review")[0]["repair_request"]
        with patch.object(recovery,"native_show",return_value=self.completed("t_diag",req["idempotency_key"])):
            self.assertEqual(recovery.recover(self.card,self.repo,"review","t_diag")[0]["action"],"resume")
        self.assertEqual(recovery.recover(self.card,self.repo,"review")[0]["action"],"resume")
        with self.card.locked() as state:
            state["rounds"].append({"n":2,"tree":"two","outcome":"owner","blocking":["k1"],"fix_brief":"new"})
        self.assertEqual(recovery.recover(self.card,self.repo,"review")[0]["action"],"handoff")
        with self.card.locked() as state:
            state["ledger"]["k1"]["status"]="fixed"
            state["rounds"][-1]["blocking"]=["k2"]
        new = recovery.recover(self.card,self.repo,"review")[0]
        self.assertEqual(new["action"], "dispatch")
        self.assertNotEqual(new["repair_request"]["idempotency_key"], req["idempotency_key"])

    def test_transient_waits_exhaust_to_diagnosis_without_infinite_polling(self):
        incident={"marker":"incident"}
        with patch("time.time", return_value=10):
            for delay in recovery_wake.DELAYS:
                decision=recovery_wake.wait_decision(incident, {"opus":"HTTP 429"})
                self.assertEqual(decision["due"],10+delay)
                self.assertEqual(recovery_wake.wait_decision(incident, {"opus":"HTTP 429"}),decision)
                incident["waits"][-1]["claimed"]=True
            self.assertIsNone(recovery_wake.wait_decision(incident, {"opus":"HTTP 429"}))
            self.assertIsNone(recovery_wake.wait_decision({"marker":"i"},{"opus":"invalid runtime attestation"}))
