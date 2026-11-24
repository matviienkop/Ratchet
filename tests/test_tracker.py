from contextlib import closing
import argparse
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))
loader = importlib.machinery.SourceFileLoader("tracker", str(BIN / "ai-dev-tracker"))
spec = importlib.util.spec_from_loader(loader.name, loader)
tracker = importlib.util.module_from_spec(spec)
loader.exec_module(tracker)


class TrackerTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.project = Path(root.name) / "example"
        self.project.mkdir()

    def config(self, provider, scope):
        manifest = {"project": "example", "tasks": {"provider": provider, "scope": scope,
                    "status_mapping": {"running": "Working"}}}
        (self.project / "project.yaml").write_text(json.dumps(manifest))
        return tracker.Tracker(self.project)

    def test_scope(self):
        adapter = self.config("github-issues", {"repository": "Acme/product"})
        self.assertEqual(adapter.key("acme/product#7"), "Acme/product#7")
        with self.assertRaises(ValueError):
            adapter.key("other/product#7")

    def test_bind_conflicts_and_retries(self):
        self.config("linear", {"team_key": "DEV"})
        args = argparse.Namespace(action="bind", board="one", card="id", ticket="DEV-1")
        with patch.object(tracker.Tracker, "read", return_value={"key": "DEV-1", "id": "123", "url": "link"}):
            for _ in range(2):
                self.assertEqual(tracker.run(self.project, args)["status"], "bound")
            args.card = "duplicate"
            with self.assertRaisesRegex(ValueError, "already bound"):
                tracker.run(self.project, args)

    def test_linear_stage(self):
        adapter = self.config("linear", {"team_key": "DEV"})
        target = {"id": "state2", "name": "Working"}
        current = {"id": "123", "identifier": "DEV-1", "state": {"id": "state1", "name": "Open"},
                   "team": {"states": {"nodes": [target]}}}
        confirmed = {"issueUpdate": {"success": True, "issue": {"id": "123", "state": {"id": "state2"}}}}
        with patch.object(adapter, "linear", side_effect=[{"issue": current}, confirmed]) as api:
            self.assertTrue(adapter.stage("DEV-1", "running", "123")["changed"])
            self.assertEqual(api.call_count, 2)
        current["state"] = target
        with patch.object(adapter, "linear", return_value={"issue": current}) as api:
            self.assertFalse(adapter.stage("DEV-1", "running", "123")["changed"])
            self.assertEqual(api.call_count, 1)

    def test_jira_confirmed_transition(self):
        adapter = self.config("jira", {"project_key": "OPS", "site": "https://example.atlassian.net"})
        def issue(state):
            return {"id": "101", "key": "OPS-9", "fields": {"status": {"name": state}}}
        transition = {"id": "21", "to": {"name": "Working"}}
        with patch.object(adapter, "jira", side_effect=[issue("Open"), {"transitions": [transition]}, {},
                                                       issue("Working")]) as api:
            self.assertTrue(adapter.stage("OPS-9", "running", "101")["changed"])
            self.assertEqual(api.call_args_list[2].args, ("101/transitions", "POST", {"transition": {"id": "21"}}))

    def test_moved_issue_cannot_write(self):
        adapter = self.config("jira", {"project_key": "DEV"})
        with patch.object(adapter, "jira", return_value={"id": "other", "key": "DEV-1"}) as api:
            with self.assertRaisesRegex(ValueError, "identity"):
                adapter.stage("DEV-1", "running", "123")
            self.assertEqual(api.call_count, 1)

    def test_native_bridge_claimed_reviewer_and_retry(self):
        import sqlite3
        from ai_dev_tracker_sync import tick

        root = self.project.parent
        self.config("linear", {"team_key": "DEV"})
        home = root / "hermes"
        native = home / "kanban/boards/main"
        native.mkdir(parents=True)
        with closing(sqlite3.connect(native / "kanban.db")) as db, db:
            db.execute("CREATE TABLE tasks(id TEXT,status TEXT,current_run_id INTEGER)")
            db.execute("CREATE TABLE task_events(id INTEGER,task_id TEXT,run_id INTEGER,kind TEXT,payload TEXT)")
            db.execute("INSERT INTO tasks VALUES ('x','running',3)")
            db.execute("""INSERT INTO task_events VALUES (1,'x',3,'claimed','{"source_status":"review"}')""")
        args = argparse.Namespace(action="bind", board="main", card="x", ticket="DEV-1")
        with patch.object(tracker.Tracker, "read", return_value={"key": "DEV-1", "id": "123", "url": "link"}):
            tracker.run(self.project, args)
        with patch("ai_dev_tracker_sync.time.time", side_effect=[100, 110, 131, 140, 150]), patch.object(
                tracker.Tracker, "stage", side_effect=[OSError("outage"), {"status": "confirmed"},
                                                       {"status": "confirmed"}]) as stage:
            first = tick(root, home, tracker.Tracker)
            self.assertEqual((first[0]["phase"], first[0]["receipt"]["status"]), ("review", "error"))
            self.assertEqual(tick(root, home, tracker.Tracker), [])
            self.assertEqual(tick(root, home, tracker.Tracker)[0]["phase"], "review")
            self.assertEqual(tick(root, home, tracker.Tracker), [])
            with closing(sqlite3.connect(native / "kanban.db")) as db, db:
                db.execute("UPDATE tasks SET status='done'")
            self.assertEqual(tick(root, home, tracker.Tracker)[0]["phase"], "done")
            self.assertEqual(stage.call_count, 3)
        with closing(sqlite3.connect(self.project / ".tracker-bindings.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM mirror_history").fetchone()[0], 3)

    def test_jira_candidates_use_configured_project(self):
        adapter = self.config("jira", {"project_key": "DEV", "site": "https://example.atlassian.net"})
        candidate = {"key": "DEV-8", "self": "https://example.atlassian.net/rest/api/3/issue/17",
                     "fields": {"summary": "Repair"}}
        with patch.object(adapter, "jira", return_value={"issues": [candidate], "nextPageToken": "cursor"}) as api:
            result = adapter.candidates(5)
            self.assertTrue(result["more_available"])
            self.assertEqual(result["tickets"][0]["url"], "https://example.atlassian.net/browse/DEV-8")
            self.assertIn('project = "DEV"', api.call_args.args[2]["jql"])
        with patch.object(adapter, "jira", return_value={"issues": [dict(candidate, key="OTHER-8")]}):
            with self.assertRaises(ValueError):
                adapter.candidates(5)
