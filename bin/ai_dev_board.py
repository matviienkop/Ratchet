"""Synchronize a registered GitHub issue with its Projects v2 status field."""

import http.client
import json
from pathlib import Path
import re


DEFAULT_STATUSES = {
    "backlog": "Backlog",
    "ready": "Ready",
    "running": "In progress",
    "review": "In review",
    "blocked": "Blocked",
    "done": "Done",
}
REPOSITORY = r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+"
BOARD_URL = r"https://github\.com/orgs/([A-Za-z0-9-]+)/projects/([1-9][0-9]*)/?"

BOARD = """
query Board($owner: String!, $board: Int!, $field: String!) {
  organization(login: $owner) {
    projectV2(number: $board) {
      id viewerCanUpdate
      field(name: $field) {
        ... on ProjectV2SingleSelectField { id options { id name } }
      }
    }
  }
}
"""

ITEM_FIELDS = """
  id isArchived project { id }
  status: fieldValueByName(name: $field) {
    ... on ProjectV2ItemFieldSingleSelectValue { optionId }
  }
"""

ISSUE = """
query IssueBoardItems($owner: String!, $repo: String!, $number: Int!, $field: String!, $after: String) {
  repository(owner: $owner, name: $repo) {
    nameWithOwner
    issue(number: $number) {
      id state stateReason
      projectItems(first: 50, after: $after, includeArchived: true) {
        pageInfo { hasNextPage endCursor }
        nodes { %s }
      }
    }
  }
}
""" % ITEM_FIELDS

ADD = """
mutation AddIssueToBoard($input: AddProjectV2ItemByIdInput!, $field: String!) {
  addProjectV2ItemById(input: $input) { item { %s } }
}
""" % ITEM_FIELDS

UPDATE = """
mutation UpdateBoardStatus($input: UpdateProjectV2ItemFieldValueInput!, $field: String!) {
  updateProjectV2ItemFieldValue(input: $input) { projectV2Item { %s } }
}
""" % ITEM_FIELDS


class GitHub:
    def __init__(self, token):
        self.headers = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                        "Content-Type": "application/json", "User-Agent": "ai-dev-board"}

    def query(self, query, **variables):
        request = json.dumps({"query": query, "variables": variables}).encode()
        connection = http.client.HTTPSConnection("api.github.com", timeout=30)
        try:
            connection.request("POST", "/graphql", headers=self.headers, body=request)
            response = connection.getresponse()
            if response.status != 200:
                raise ValueError(f"GitHub Projects returned HTTP {response.status}.")
            document = json.loads(response.read())
            if document.get("errors") or not isinstance(document.get("data"), dict):
                raise ValueError("GitHub Projects rejected the GraphQL operation.")
            return document["data"]
        except http.client.HTTPException as error:
            raise ValueError("GitHub Projects connection failed.") from error
        finally:
            connection.close()


def github_for(project):
    credential = Path.home() / ".config/ai-dev/credentials" / project / "github-token"
    if credential.resolve() != credential or not credential.is_file() or credential.stat().st_mode & 0o077:
        raise ValueError("Use a private token file in this project's configured credentials directory.")
    token = credential.read_text().strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", token):
        raise ValueError("Invalid project GitHub credential.")
    return GitHub(token)


def locate_issue_item(api, repo, number, board_id, field):
    owner, name = repo.split("/")
    after = None
    while True:
        result = api.query(ISSUE, owner=owner, repo=name, number=number, field=field, after=after)
        repository = result.get("repository")
        if not repository or repository["nameWithOwner"].lower() != repo.lower() or not repository.get("issue"):
            raise ValueError("GitHub could not verify the issue in the registered tracker.")
        issue = repository["issue"]
        page = issue["projectItems"]
        for item in page["nodes"]:
            if item and item["project"]["id"] == board_id:
                return issue, item
        if not page["pageInfo"]["hasNextPage"]:
            return issue, None
        after = page["pageInfo"]["endCursor"]


def item_option(item):
    return (item["status"] or {}).get("optionId")


def sync_board(project: Path, task: str, phase: str, *, api=None) -> dict:
    """Synchronize one authorized logical status using the project's scoped board."""
    manifest_path = project.absolute() / "project.yaml"
    if manifest_path.resolve() != manifest_path:
        raise ValueError("Unexpected symlink in the project manifest path.")
    manifest = json.loads(manifest_path.read_bytes())
    tasks = manifest.get("tasks", {})
    scope = tasks.get("scope", {})
    board_config = scope.get("github_project")
    if tasks.get("provider") != "github-issues" or board_config is None:
        return {"status": "unconfigured"}
    if not isinstance(board_config, dict) or not isinstance(board_config.get("url"), str):
        raise ValueError("The configured GitHub Project requires a board URL.")
    board_url = board_config["url"]

    if not re.fullmatch(REPOSITORY + r"#[1-9][0-9]*", task):
        return {"status": "skipped", "reason": "Task has no canonical GitHub issue identity."}
    repo = scope.get("repository", "")
    match = re.fullmatch(re.escape(repo) + r"#([1-9][0-9]*)", task, re.I)
    if not re.fullmatch(REPOSITORY, repo) or repo.split("/")[1] in (".", "..") or not match:
        raise ValueError("Task is outside the registered GitHub Issues tracker.")
    expected_issues = f"https://github.com/{repo}/issues"
    if scope.get("issues_url", expected_issues).rstrip("/").lower() != expected_issues.lower():
        raise ValueError("Tracker URL conflicts with its registered repository.")
    board_match = re.fullmatch(BOARD_URL, board_url)
    if not board_match or board_match[1].lower() != repo.split("/")[0].lower():
        raise ValueError("Use a GitHub organization board URL in the registered repository's organization.")
    board_number = int(board_match[2])
    if board_config.get("number", board_number) != board_number:
        raise ValueError("The configured GitHub Project number conflicts with its URL.")
    if manifest.get("project") != project.name or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", project.name):
        raise ValueError("GitHub Projects requires the registered project's workspace.")
    if scope.get("writes") not in (None, "explicit_authorization_only"):
        raise ValueError("The tracker write policy does not permit this operation.")

    mapping = tasks.get("status_mapping", {})
    if phase not in DEFAULT_STATUSES or not isinstance(mapping, dict):
        raise ValueError("Invalid GitHub Projects status mapping.")
    requested = mapping.get(phase, DEFAULT_STATUSES[phase])
    field_name = board_config.get("status_field", "Status")
    if any(not isinstance(value, str) or not value.strip() for value in (field_name, requested)):
        raise ValueError("The GitHub Projects field and option names must be nonempty strings.")
    api = github_for(project.name) if api is None else api
    result = api.query(BOARD, owner=board_match[1], board=board_number, field=field_name)
    board = (result.get("organization") or {}).get("projectV2")
    if not board:
        raise ValueError("GitHub could not find the configured organization board.")
    field = board.get("field")
    if not field or not field.get("id"):
        raise ValueError("The configured board field must be a project single-select field.")
    options = [option for option in field["options"] if option["name"].casefold() == requested.casefold()]
    fallback = phase == "blocked" and phase not in mapping and not options
    if fallback:
        requested = mapping.get("running", DEFAULT_STATUSES["running"])
        if not isinstance(requested, str) or not requested.strip():
            raise ValueError("The GitHub Projects running option must be a nonempty string.")
        options = [option for option in field["options"] if option["name"].casefold() == requested.casefold()]
    if len(options) != 1:
        raise ValueError("The configured board status does not identify a unique option.")

    board_id, option = board["id"], options[0]
    issue, item = locate_issue_item(api, repo, int(match[1]), board_id, field_name)
    if phase == "done" and (issue["state"], issue["stateReason"]) != ("CLOSED", "COMPLETED"):
        return {"status": "skipped", "reason": "The issue has not been closed as completed.",
                "open_issue": issue["state"] == "OPEN"}
    if phase != "done" and issue["state"] != "OPEN":
        return {"status": "skipped", "reason": "The issue is closed."}
    if item is not None and item["isArchived"]:
        return {"status": "skipped", "reason": "The project item is archived."}

    changed = item is None or item_option(item) != option["id"]
    if changed and not board["viewerCanUpdate"]:
        raise ValueError("The GitHub credential cannot update the configured board.")
    if item is None:
        membership = {"projectId": board_id, "contentId": issue["id"]}
        result = api.query(ADD, input=membership, field=field_name)
        item = (result.get("addProjectV2ItemById") or {}).get("item")
        if not item or item["project"]["id"] != board_id:
            raise ValueError("GitHub did not confirm adding the issue to the board.")
        if item["isArchived"]:
            return {"status": "skipped", "reason": "The project item is archived."}
    if item_option(item) != option["id"]:
        update = {"projectId": board_id, "itemId": item["id"], "fieldId": field["id"],
                  "value": {"singleSelectOptionId": option["id"]}}
        result = api.query(UPDATE, input=update, field=field_name)
        updated = (result.get("updateProjectV2ItemFieldValue") or {}).get("projectV2Item")
        if not updated or updated["id"] != item["id"] or item_option(updated) != option["id"]:
            raise ValueError("GitHub did not confirm the requested board status.")
    receipt = {"status": "confirmed", "board": board_url, "issue": f"{repo}#{match[1]}",
               "item": item["id"], "option": option["name"], "changed": changed}
    if fallback:
        receipt["fallback"] = "The board has no Blocked option. Used the running status."
    return receipt
