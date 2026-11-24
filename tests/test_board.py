import json
from pathlib import Path
import sys
import tempfile
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
import ai_dev_board as board


class BoardAPI:
    def __init__(self, *, current="ready", present=True, archived=False, issue_state="OPEN", state_reason=None):
        self.current = current
        self.present = present
        self.archived = archived
        self.issue_state = issue_state
        self.state_reason = state_reason
        self.options = {"Backlog": "backlog", "Ready": "ready", "In progress": "running",
                        "In review": "review", "Done": "done", "Deferred": "deferred"}
        self.calls = []
        self.fail_after_add = False
        self.second_page = False

    def item(self):
        return {"id": "item", "isArchived": self.archived, "project": {"id": "board"},
                "status": {"optionId": self.current} if self.current else None}

    def query(self, query, **variables):
        self.calls.append((query, variables))
        if query == board.BOARD:
            return {"organization": {"projectV2": {
                "id": "board", "viewerCanUpdate": True,
                "field": {"id": "field", "options": [{"id": i, "name": n} for n, i in self.options.items()]},
            }}}
        if query == board.ISSUE:
            next_page = self.second_page and variables["after"] is None
            nodes = [self.item()] if self.present and not next_page else []
            return {"repository": {"nameWithOwner": "Example/issues", "issue": {
                "id": "issue", "state": self.issue_state, "stateReason": self.state_reason,
                "projectItems": {"nodes": nodes, "pageInfo": {"hasNextPage": next_page, "endCursor": "next"}}}}}
        if query == board.ADD:
            self.present = True
            if self.fail_after_add:
                raise OSError("connection reset after commit")
            return {"addProjectV2ItemById": {"item": self.item()}}
        if query == board.UPDATE:
            self.current = variables["input"]["value"]["singleSelectOptionId"]
            return {"updateProjectV2ItemFieldValue": {"projectV2Item": self.item()}}
        raise AssertionError(query)

    def writes(self):
        return [(query, variables["input"]) for query, variables in self.calls if query in (board.ADD, board.UPDATE)]


class BoardTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.project = Path(temp.name) / "example"
        self.project.mkdir()
        self.config = {"project": "example", "tasks": {"provider": "github-issues", "scope": {
            "repository": "Example/issues",
            "github_project": {"url": "https://github.com/orgs/Example/projects/1", "number": 1},
            "writes": "explicit_authorization_only",
        }}}
        self.save()

    def save(self):
        (self.project / "project.yaml").write_text(json.dumps(self.config))

    def sync(self, phase, api, task="example/issues#42"):
        return board.sync_board(self.project, task, phase, api=api)

    def test_updates_existing_item_and_retry_is_no_op(self):
        api = BoardAPI()
        receipt = self.sync("review", api)
        self.assertEqual((receipt["status"], receipt["option"], receipt["changed"]), ("confirmed", "In review", True))
        self.assertEqual(api.writes(), [(board.UPDATE, {"projectId": "board", "itemId": "item",
                                                      "fieldId": "field", "value": {"singleSelectOptionId": "review"}})])
        self.assertFalse(self.sync("review", api)["changed"])
        self.assertEqual(len(api.writes()), 1)

    def test_adds_existing_issue_to_board_then_updates(self):
        api = BoardAPI(present=False, current=None)
        receipt = self.sync("backlog", api)
        self.assertEqual(receipt["option"], "Backlog")
        self.assertEqual([query for query, _ in api.writes()], [board.ADD, board.UPDATE])
        self.assertEqual(api.writes()[0][1], {"projectId": "board", "contentId": "issue"})

    def test_lost_add_response_reconciles_without_repeating_add(self):
        api = BoardAPI(present=False, current=None)
        api.fail_after_add = True
        with self.assertRaises(OSError):
            self.sync("running", api)
        self.assertEqual(self.sync("running", api)["option"], "In progress")
        self.assertEqual([query for query, _ in api.writes()], [board.ADD, board.UPDATE])

    def test_finds_item_on_later_page(self):
        api = BoardAPI()
        api.second_page = True
        self.assertEqual(self.sync("review", api)["option"], "In review")
        self.assertEqual([query for query, _ in api.writes()], [board.UPDATE])

    def test_blocked_uses_running_fallback_without_blocked_column(self):
        api = BoardAPI(current="review")
        receipt = self.sync("blocked", api)
        self.assertEqual(receipt["option"], "In progress")
        self.assertIn("fallback", receipt)
        self.assertEqual([query for query, _ in api.writes()], [board.UPDATE])

    def test_blocked_column_overrides_fallback(self):
        api = BoardAPI()
        api.options["Blocked"] = "blocked"
        self.assertEqual(self.sync("blocked", api)["option"], "Blocked")

    def test_reads_custom_field_and_status_name(self):
        api = BoardAPI()
        self.config["tasks"]["scope"]["github_project"]["status_field"] = "Execution"
        self.config["tasks"]["status_mapping"] = {"running": "Underway"}
        self.save()
        api.options["Underway"] = "custom"
        self.assertEqual(self.sync("running", api)["option"], "Underway")
        self.assertEqual(api.calls[0][1]["field"], "Execution")

    def test_only_completed_closed_issue_can_be_done(self):
        for issue_state, reason in (("OPEN", None), ("CLOSED", "NOT_PLANNED")):
            with self.subTest(reason=reason):
                api = BoardAPI(present=False, issue_state=issue_state, state_reason=reason)
                self.assertEqual(self.sync("done", api)["status"], "skipped")
                self.assertEqual(api.writes(), [])
        api = BoardAPI(issue_state="CLOSED", state_reason="COMPLETED")
        self.assertEqual(self.sync("done", api)["option"], "Done")

    def test_archived_item_is_not_changed(self):
        api = BoardAPI(archived=True)
        self.assertEqual(self.sync("review", api)["status"], "skipped")
        self.assertEqual(api.writes(), [])

    def test_local_task_skips_and_foreign_github_identity_fails(self):
        api = BoardAPI()
        self.assertEqual(self.sync("review", api, task="local-card")["status"], "skipped")
        with self.assertRaisesRegex(ValueError, "outside the registered"):
            self.sync("review", api, task="somewhere/else#10")
        self.assertEqual(api.calls, [])

    def test_invalid_scope_cannot_write_to_other_board(self):
        api = BoardAPI()
        self.config["tasks"]["scope"]["github_project"]["url"] = "https://github.com/orgs/Other/projects/1"
        self.save()
        with self.assertRaisesRegex(ValueError, "registered repository's organization"):
            self.sync("review", api)
        self.assertEqual(api.calls, [])

    def test_unconfigured_provider_performs_no_requests(self):
        api = BoardAPI()
        self.config["tasks"] = {"provider": "internal"}
        self.save()
        self.assertEqual(self.sync("running", api)["status"], "unconfigured")
        self.assertEqual(api.calls, [])


if __name__ == "__main__":
    unittest.main()
