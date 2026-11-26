"""PR-only delivery uses the same exact-head CI evidence as package delivery."""

import copy
from unittest.mock import patch

from test_card import CardCase
from ai_dev import gitio, policy, release, review
from ai_dev.store import CardError


class PRDelivery(CardCase):
    def setUp(self):
        super().setUp()
        self.rule = {"repository": "demo/core", "branch": "main", "ci_workflow": "test.yml"}
        self.url = "https://github.com/demo/core/pull/7"
        self.set_policy(delivery="pr", pr_ci=self.rule)
        self.contract()
        self.implement()
        self.verified()
        review.brief(self.card, self.repo)
        self.report([])
        review.settle(self.card, self.repo)
        self.tree = gitio.snapshot(self.repo)
        self.head = "a" * 40
        self.pr = {"base": {"ref": "main"}, "head": {"sha": self.head}, "merged": False, "state": "open"}
        self.run = {"id": 8, "run_attempt": 1, "head_sha": self.head, "event": "pull_request",
                    "repository": {"full_name": "demo/core"}, "path": ".github/workflows/test.yml",
                    "pull_requests": [{"number": 7}], "status": "completed", "conclusion": "success",
                    "html_url": "https://github.com/demo/core/actions/runs/8"}
        self.mock_api = patch.object(release, "api", side_effect=self.api).start()
        self.addCleanup(patch.stopall)
        self.no_catalog = patch.object(release, "catalog", side_effect=AssertionError("PR-only must not fetch packages")).start()

    def api(self, path):
        if path == "repos/demo/core/pulls/7":
            return copy.deepcopy(self.pr)
        if path.startswith("repos/demo/core/git/commits/"):
            return {"tree": {"sha": self.tree}}
        if path.startswith("repos/demo/core/actions/workflows/test.yml/runs?"):
            return {"workflow_runs": [copy.deepcopy(self.run)]}
        raise AssertionError(path)

    def check(self):
        return release.verify_release(self.card, self.repo, self.url, before_merge=True)

    def ready(self):
        return release.require_pr_ci(self.card.load(), policy.load(), self.tree, self.url)

    def test_pr_only_receipt_requires_no_release_or_package(self):
        with self.assertRaisesRegex(CardError, "current CI evidence"):
            self.ready()
        receipt = self.check()
        self.assertEqual(receipt["head"], self.head)
        self.assertEqual(self.ready()["ci"]["id"], 8)
        self.assertEqual(self.card.load()["premerge_receipt"]["rule"], self.rule)
        self.no_catalog.assert_not_called()
        with self.assertRaisesRegex(CardError, "Configure the repository delivery policy"):
            release.verify_release(self.card, self.repo, self.url)

    def test_rerun_must_pass_and_have_a_fresh_receipt(self):
        self.check()
        self.run.update(status="in_progress", conclusion=None, run_attempt=2)
        with self.assertRaisesRegex(CardError, "has not passed"):
            self.ready()
        self.run.update(status="completed", conclusion="failure")
        with self.assertRaisesRegex(CardError, "has not passed"):
            self.check()
        self.run["conclusion"] = "success"
        with self.assertRaisesRegex(CardError, "changed after verification"):
            self.ready()
        self.check()
        self.assertEqual(self.ready()["ci"]["attempt"], 2)

    def test_same_tree_new_head_invalidates_receipt(self):
        self.check()
        self.pr["head"]["sha"] = self.run["head_sha"] = "b" * 40
        with self.assertRaisesRegex(CardError, "changed after verification"):
            self.ready()
        self.check()
        self.assertEqual(self.ready()["head"], "b" * 40)

    def test_changed_tree_closed_pr_and_wrong_metadata_fail(self):
        self.check()
        with self.assertRaisesRegex(CardError, "metadata.published_pr"):
            release.require_pr_ci(self.card.load(), policy.load(), self.tree, "https://github.com/demo/core/pull/9")
        self.pr["state"] = "closed"
        with self.assertRaisesRegex(CardError, "requires the reviewed PR to be open"):
            self.ready()
        self.pr["state"] = "open"
        self.tree = "c" * 40
        with self.assertRaisesRegex(CardError, "differs from the reviewed candidate"):
            release.require_pr_ci(self.card.load(), policy.load(), self.card.load()["premerge_receipt"]["tree"], self.url)

    def test_unconfigured_local_delivery_does_not_make_network_calls(self):
        self.assertIsNone(release.require_pr_ci({}, {"delivery": "local", "pr_ci": self.rule}, "tree", None))
        self.assertIsNone(release.require_pr_ci({}, {"delivery": "pr", "pr_ci": None}, "tree", None))
        self.mock_api.assert_not_called()

    def test_pr_policy_is_validated(self):
        for field, value in (("repository", "other"), ("branch", ""), ("ci_workflow", "../test.yml")):
            with self.subTest(field=field):
                self.set_policy(delivery="pr", pr_ci={**self.rule, field: value})
                with self.assertRaisesRegex(CardError, "Configure pr_ci"):
                    policy.load()
