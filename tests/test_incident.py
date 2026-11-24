import argparse
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


CLI = Path(__file__).resolve().parents[1] / "bin" / "ai-dev-incident"


class IncidentTests(unittest.TestCase):
    def invoke(self, *args, pages=None, success=True):
        result = subprocess.run([CLI, *args], input=json.dumps(pages), capture_output=True, text=True)
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def markers(self, parent="org/tracker#42", symptom="missing-review-route"):
        return self.invoke("markers", "--project", "example", "--provider", "github",
                           "--task", parent, "--step", "review", "--symptom", symptom)

    def test_identity_survives_retries_and_is_scoped_to_parent_and_fault(self):
        markers = self.markers()
        self.assertEqual(self.markers(), markers)
        self.assertEqual(set(markers), {"comment", "follow-up", "resolved", "blocked"})
        self.assertEqual(len(set(markers.values())), len(markers))
        self.assertNotEqual(self.markers(parent="org/other#42"), markers)
        self.assertNotEqual(self.markers(symptom="review-timeout"), markers)

    def test_lost_follow_up_receipt_and_incomplete_link(self):
        markers = self.markers()
        comments = [[{"id": 5, "body": "Blocked on review.\n" + markers["comment"]}]]
        issues = [[{"id": 6, "body": None}], [{"id": 7, "body": "Parent #42\r\n" + markers["follow-up"]}]]
        self.assertEqual(self.invoke("locate", markers["comment"], pages=comments), {"status": "found", "id": "5"})
        self.assertEqual(self.invoke("locate", markers["follow-up"], pages=issues), {"status": "found", "id": "7"})
        self.assertEqual(self.invoke("locate", markers["resolved"], pages=comments), {"status": "absent"})

    def test_ambiguous_and_malformed_reads_cannot_signal_absence(self):
        marker = self.markers()["comment"]
        records = [{"id": 1, "body": marker}, {"id": 2, "body": marker}]
        for bad in ([], [records], [[{"id": 1}]], [records[0]], [[{"id": "", "body": marker}]]):
            with self.subTest(pages=bad):
                self.invoke("locate", marker, pages=bad, success=False)
        self.assertEqual(self.invoke("locate", marker, pages=[[]]), {"status": "absent"})
        self.assertEqual(self.invoke("locate", marker, pages=[[{"id": 3, "body": "Quoted " + marker}]]),
                         {"status": "absent"})


loader = importlib.machinery.SourceFileLoader("incident_test_module", str(CLI))
incident = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
loader.exec_module(incident)


class FakeGitHub:
    root = "/repos/Org/backlog/issues"
    url = "https://github.com/Org/backlog/issues"
    blocked_by_path = root + "/42/dependencies/blocked_by"

    def __init__(self):
        self.writes = []
        self.blocked_by = []
        self.fail_before = self.fail_after = None
        self.records = {self.root + "/42": {"html_url": self.url + "/42", "number": 42, "state": "open", "body": None}}

    def request(self, method, path, body=None):
        if method == "GET":
            if path == "/user":
                return {"id": 100}
            if path.startswith(self.blocked_by_path + "?"):
                page = int(path.rsplit("=", 1)[1])
                return self.blocked_by[(page - 1) * 100:page * 100]
            if path.startswith(self.root + "/42/comments?"):
                return [r.copy() for r in self.records.values() if "#issuecomment-" in r["html_url"]]
            if path.startswith(self.root + "?"):
                return [r.copy() for r in self.records.values() if "#issuecomment-" not in r["html_url"]]
            return self.records[path].copy()
        self.writes.append((method, path))
        if self.fail_before == (method, path):
            self.fail_before = None
            raise TimeoutError("timeout")
        if path == self.blocked_by_path:
            record = next(r for r in self.records.values() if r.get("id") == body["issue_id"])
            self.blocked_by.append(record)
        elif method == "POST":
            if path == self.root:
                key = self.root + "/43"
                record = {"id": 333, "number": 43, "html_url": self.url + "/43", "state": "open"}
            else:
                key = self.root + "/comments/88"
                record = {"id": 88, "html_url": self.url + "/42#issuecomment-88"}
            record.update(body, user={"id": 100})
            self.records[key] = record
        else:
            record = self.records[path]
            record.update(body)
        if self.fail_after == (method, path):
            self.fail_after = None
            raise TimeoutError("timeout")
        return record.copy()


class GitHubSyncTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        workspace = self.root / "workspace"
        self.project = workspace / "demo"
        self.project.mkdir(parents=True)
        self.manifest = {"project": "demo", "tasks": {"provider": "github-issues",
                         "scope": {"repository": "Org/backlog", "writes": "explicit_authorization_only"}}}
        (self.project / "project.yaml").write_text(json.dumps(self.manifest))
        self.priv = self.root / ".config/ai-dev/credentials/demo/github-token"
        self.priv.parent.mkdir(parents=True)
        self.priv.write_text("private-test-value")
        self.priv.chmod(0o600)
        self.comment = self.root / "text"
        self.comment.write_text("Required review could not start. Verify the authorized reviewer route.")
        self.child = self.root / "child"
        self.child.write_text("Restore reviewer access and repeat the original verification.")
        self.args = argparse.Namespace(project="demo", task="Org/backlog#42", step="review", symptom="auth-missing",
                                       token_file=None, comment_file=self.comment, follow_up_file=self.child,
                                       follow_up_title="Restore reviewer access")
        self.fake = FakeGitHub()
        environment = patch.dict(os.environ, {"AI_DEV_WORKSPACE": str(workspace), "HOME": str(self.root)})
        environment.start()
        self.addCleanup(environment.stop)
        client = patch.object(incident, "GitHub", return_value=self.fake)
        self.client = client.start()
        self.addCleanup(client.stop)

    def test_parent_and_follow_up_confirmed_updates_and_retry(self):
        receipt = incident.github_sync(self.args)
        self.assertEqual(receipt["comment"], self.fake.url + "/42#issuecomment-88")
        self.assertEqual(receipt["follow_up"], self.fake.url + "/43")
        self.assertEqual([w[0] for w in self.fake.writes], ["POST", "POST", "PATCH"])
        self.assertEqual(incident.github_sync(self.args), receipt)
        self.assertEqual(len(self.fake.writes), 3)
        self.comment.write_text("The route was repaired and the approved check passed.")
        self.child.write_text("The approved route passed verification.")
        self.assertEqual(incident.github_sync(self.args), receipt)
        self.assertEqual([w[0] for w in self.fake.writes], ["POST", "POST", "PATCH", "PATCH", "PATCH"])
        self.assertEqual(self.fake.records[self.fake.root + "/42"]["state"], "open")

    def test_lost_child_creation_receipt_completes_the_link(self):
        self.fake.fail_after = ("POST", self.fake.root)
        with self.assertRaises(TimeoutError):
            incident.github_sync(self.args)
        self.assertEqual(incident.github_sync(self.args)["follow_up"], self.fake.url + "/43")
        self.assertEqual([w[0] for w in self.fake.writes], ["POST", "POST", "PATCH"])

    def test_existing_follow_up_survives_lost_link_receipt_without_editing_child(self):
        child = {"id": 555, "number": 138, "html_url": self.fake.url + "/138", "state": "open",
                 "body": f"Parent: {self.fake.url}/42\n\nExisting prerequisite.", "user": {"id": 102}}
        self.fake.records[self.fake.root + "/138"] = child.copy()
        self.args.follow_up_title = self.args.follow_up_file = None
        self.args.existing_follow_up = 138
        self.args.impact = "blocking"
        self.fake.fail_after = ("POST", self.fake.root + "/42/comments")
        with self.assertRaises(TimeoutError):
            incident.github_sync(self.args)
        self.args.existing_follow_up = None
        receipt = incident.github_sync(self.args)
        self.assertEqual(receipt["follow_up"], child["html_url"])
        self.assertEqual(receipt["impact"], "blocking")
        self.assertIs(receipt["blocked_by"], True)
        self.assertEqual(self.fake.records[self.fake.root + "/138"], child)
        comment = self.fake.records[self.fake.root + "/comments/88"]["body"]
        self.assertIn("Impact: Blocks parent acceptance.", comment)
        self.assertIn(f"Follow-up: {child['html_url']}", comment)
        self.args.follow_up = 138
        self.assertEqual(incident.github_status(self.args)["state"], "open")
        self.assertEqual(self.fake.writes,
                         [("POST", self.fake.root + "/42/comments"), ("POST", self.fake.blocked_by_path)])

    def test_blocking_relation_reconciles_lost_receipt_and_reads_past_first_page(self):
        self.args.impact = "blocking"
        self.fake.blocked_by = [{"id": 1000 + i, "html_url": self.fake.url + f"/{1000 + i}"} for i in range(100)]
        self.fake.fail_after = ("POST", self.fake.blocked_by_path)
        receipt = incident.github_sync(self.args)
        self.assertIs(receipt["blocked_by"], True)
        self.assertEqual(self.fake.blocked_by[-1]["id"], 333)
        self.assertEqual(incident.github_sync(self.args), receipt)
        self.assertEqual(self.fake.writes.count(("POST", self.fake.blocked_by_path)), 1)
        self.args.follow_up = 43
        self.args.blocked_by = True
        self.assertIs(incident.github_status(self.args)["blocked_by"], True)
        self.fake.blocked_by.pop()
        self.assertIs(incident.github_status(self.args)["blocked_by"], False)

    def test_dependency_retry_reuses_confirmed_issue(self):
        self.args.impact = "blocking"
        self.fake.fail_before = ("POST", self.fake.blocked_by_path)
        with self.assertRaises(TimeoutError):
            incident.github_sync(self.args)
        self.assertIs(incident.github_sync(self.args)["blocked_by"], True)
        self.assertEqual(self.fake.writes.count(("POST", self.fake.root)), 1)

    def test_closed_duplicate_follow_up_does_not_create_a_native_dependency(self):
        self.fake.records[self.fake.root + "/138"] = {
            "id": 555, "number": 138, "html_url": self.fake.url + "/138", "state": "closed",
            "state_reason": "duplicate", "body": f"Parent: {self.fake.url}/42"}
        self.args.follow_up_title = self.args.follow_up_file = None
        self.args.existing_follow_up = 138
        self.args.impact = "blocking"
        with self.assertRaisesRegex(ValueError, "follow-up is closed"):
            incident.github_sync(self.args)
        self.assertNotIn(("POST", self.fake.root), self.fake.writes)
        self.assertNotIn(("POST", self.fake.blocked_by_path), self.fake.writes)

    def test_follow_up_impact_is_recorded_without_changing_parent_state(self):
        self.args.impact = "nonblocking"
        self.assertEqual(incident.github_sync(self.args)["impact"], "nonblocking")
        self.assertEqual(self.fake.records[self.fake.root + "/42"]["state"], "open")
        comment = self.fake.records[self.fake.root + "/comments/88"]["body"]
        self.assertIn("Impact: Parent work can continue.", comment)
        self.assertNotIn(("POST", self.fake.blocked_by_path), self.fake.writes)

    def test_existing_follow_up_requires_explicit_parent_link(self):
        self.fake.records[self.fake.root + "/138"] = {
            "number": 138, "html_url": self.fake.url + "/138", "state": "open",
            "body": f"Mentioning {self.fake.url}/42 in a note."}
        self.args.follow_up_title = self.args.follow_up_file = None
        self.args.existing_follow_up = 138
        with self.assertRaisesRegex(ValueError, "explicit parent link"):
            incident.github_sync(self.args)
        self.assertEqual(self.fake.writes, [])

    def test_uncertain_absent_request_is_never_repeated(self):
        self.fake.fail_before = ("POST", self.fake.root + "/42/comments")
        with self.assertRaises(TimeoutError):
            incident.github_sync(self.args)
        with self.assertRaisesRegex(ValueError, "uncertain"):
            incident.github_sync(self.args)
        self.assertEqual(len(self.fake.writes), 1)

    def test_manual_edit_and_foreign_author_are_preserved(self):
        incident.github_sync(self.args)
        record = self.fake.records[self.fake.root + "/comments/88"]
        record["body"] += "\nMaintainer's edit."
        with self.assertRaisesRegex(ValueError, "changed outside"):
            incident.github_sync(self.args)
        record["user"]["id"] = 999
        with self.assertRaisesRegex(ValueError, "unexpected owner"):
            incident.github_sync(self.args)
        self.assertEqual(len(self.fake.writes), 3)

    def test_authority_and_publication_boundaries_precede_writes(self):
        self.args.task = "Org/code#42"
        with self.assertRaisesRegex(ValueError, "canonical"):
            incident.github_sync(self.args)
        self.args.task = "Org/backlog#42"
        for value in ("Bearer some-secret", "/home/someone/file", "private-test-value", "https://example.org/?sig=test"):
            self.comment.write_text(value)
            with self.assertRaises(ValueError):
                incident.github_sync(self.args)
        self.client.assert_not_called()
        self.assertEqual(self.fake.writes, [])

    def test_unrelated_repo_url_and_readonly_provider_are_rejected(self):
        scope = self.manifest["tasks"]["scope"]
        for key, value in (("issues_url", "https://github.com/Org/code/issues"), ("writes", "read_only")):
            original = dict(scope)
            scope[key] = value
            (self.project / "project.yaml").write_text(json.dumps(self.manifest))
            with self.assertRaises(ValueError):
                incident.github_sync(self.args)
            scope.clear()
            scope.update(original)
        self.client.assert_not_called()

    def test_status_reports_only_verified_state_without_writes(self):
        incident.github_sync(self.args)
        self.args.follow_up = 43
        record = self.fake.records[self.fake.root + "/43"]
        receipt_file, = self.project.rglob("records.json")
        before = receipt_file.stat().st_mtime_ns
        self.manifest["tasks"]["scope"]["writes"] = "read_only"
        (self.project / "project.yaml").write_text(json.dumps(self.manifest))
        for state, reason in (("open", None), ("closed", "completed"), ("closed", "not_planned")):
            record.update(state=state, state_reason=reason)
            self.assertEqual(incident.github_status(self.args),
                             {"state": state, "state_reason": reason, "url": self.fake.url + "/43"})
        self.assertEqual(len(self.fake.writes), 3)
        self.assertEqual(receipt_file.stat().st_mtime_ns, before)

    def test_status_rejects_unknown_issue_or_changed_identity(self):
        self.args.follow_up = 43
        with self.assertRaisesRegex(ValueError, "No confirmed"):
            incident.github_status(self.args)
        self.assertFalse((self.project / "runs").exists())
        incident.github_sync(self.args)
        self.args.task = "Org/code#42"
        with self.assertRaisesRegex(ValueError, "canonical"):
            incident.github_status(self.args)
        self.args.task = "Org/backlog#42"
        self.args.follow_up = 44
        with self.assertRaisesRegex(ValueError, "No unique confirmed"):
            incident.github_status(self.args)
        self.args.follow_up = 43
        issue = self.fake.records[self.fake.root + "/43"]
        original = dict(issue)
        for delta in ({"html_url": self.fake.url.replace("backlog", "code") + "/43"},
                      {"body": issue["body"].replace("/42", "/123")},
                      {"body": issue["body"].split("ai-dev-incident:")[0]},
                      {"pull_request": {}}, {"user": {"id": 999}}):
            with self.subTest(fields=list(delta)):
                issue.update(delta)
                with self.assertRaises(ValueError):
                    incident.github_status(self.args)
                issue.clear()
                issue.update(original)
        self.assertEqual(len(self.fake.writes), 3)


class GitHubTransportTests(unittest.TestCase):
    def test_cross_host_redirect_never_receives_credential(self):
        with patch.object(incident.http.client, "HTTPSConnection") as connection:
            stream = connection.return_value
            response = stream.getresponse.return_value
            response.status = 307
            with self.assertRaisesRegex(ValueError, "HTTP 307"):
                incident.GitHub("private-test-value").request("POST", "/repos/org/tracker/issues", {"body": "Test"})
            connection.assert_called_once_with("api.github.com", timeout=30)
            self.assertEqual(stream.request.call_count, 1)
            response.read.assert_not_called()
            stream.close.assert_called_once()
