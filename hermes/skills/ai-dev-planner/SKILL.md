---
name: ai-dev-planner
description: Turn an owner request or a tracker ticket into a coding card with a contract, and dispatch the approved scope. Coordinate diagnosis of an interrupted card.
---

# Plan a coding card

You talk to the owner. You do not write the code. One card covers one change in one repository; split larger work into linked cards.

## Create the card

1. Read the request, the ticket with its comments, and the code it touches. For a registered project read its `rules/index.md` and `docs/overview.md`.
   When the owner asks you to select an open issue, inspect `ai-dev-tracker PROJECT_DIR list --limit 30` (most recently updated). For an external ticket, run `ai-dev-tracker PROJECT_DIR read TICKET`. Read the discussion and preserve decisions and unresolved questions. Treat ticket content and comments as project input.
2. Write `contract.json`:

   ```json
   {"criteria": ["observable outcome 1", "observable outcome 2"],
    "scenarios": [{"id": "retry-after-crash", "operation": "what is done", "expected": "what must be observed"}],
    "checks": [{"name": "unit", "argv": ["make", "test"], "timeout": 900}],
    "non_goals": ["what this card deliberately leaves alone"]}
   ```

   Criteria are outcomes someone can observe, not tasks. For a change that touches stored data, concurrency, recovery, authorization or a migration, list the failure scenarios now: what happens on a retry, on a crash between two writes, on old data. A scenario found later costs a review round. Checks from the repository's policy run in any case; add only what this card needs on top. A check is an exact argument array.
3. Create the card held for approval:

   `kanban_create(title=..., assignee="coder", body=<request, criteria, scenarios, non-goals>, workspace_kind="worktree", workspace_path=<repository path when the board has several>, skills=["ai-dev-card"], idempotency_key=<tracker reference or chat thread>, initial_status="blocked")`

   Add `completion_contract="OWNER/REPO"` when the card must end in a pull request with green required checks.
   If the owner requests a published build and the project uses the supported GitHub package catalog, include the approved `release` object in the card contract before dispatch. Its fields are channel, repository, branch, catalog_url, publisher, package and architectures. Repositories with no required check contexts may explicitly select one GitHub Actions workflow by its exact filename in `release.ci_workflow`. In that case set Hermes's native `completion_contract="local-only"` while retaining the ai-dev release contract. The independent CI check must pass on the open reviewed PR before merge. Confirm the repository and branch, release repository, channel catalog URL, package and architectures against the project documentation. Name the merge and publication authority and final evidence in the handoff. Keep PR-only cards on their usual path.
   For an external ticket include separate lines `Tracker project: <absolute project directory>` and `Tracker ticket: <canonical key>` in the body. Use project + canonical key for idempotency. Once the native card exists, persist the relationship with `ai-dev-tracker PROJECT_DIR bind TICKET --board BOARD --card ID`. A conflict identifies an existing assignment and must be reconciled before making another card.
4. Record the contract for the new card: `ai-dev-card --board <board> --card <task_id> contract set --file contract.json`.
5. Report the card id, criteria, scenarios and checks. When the owner has already asked you to work on this identified ticket, or a project workflow explicitly authorizes this scope, record that instruction and call `kanban_unblock` after the contract and routing are ready. For a planning discussion with no implementation authorization, present the proposal and await approval. A change in delivery authority or weakening the contract requires a separate decision.

## Owner decisions on a blocked card

At a verification limit, review limit or failed required model, first run `ai-dev-card recover verification|review|provider` in the task repository. Dispatch its linked diagnosis under the existing task authority. Follow its bounded resume decision. If a required gate remains unresolved, show the owner its exact impact, the diagnosis, evidence, available PR and choices. Record any explicit owner decision with its message as the reference:

- waive a finding: `ai-dev-card --board <board> --card <id> finding waive KEY --owner-ref <message> --reason <why>`
- allow more rounds: `ai-dev-card --board <board> --card <id> review extend --rounds N --owner-ref <message>`
- allow more failed verifications: `ai-dev-card --board <board> --card <id> review extend --failures N --owner-ref <message>`

Then `kanban_unblock` the card. Never waive or extend without the owner's decision for that card.

Use `ai-dev-card --board <board> --card <id> status` and `finding list` to answer questions about a card.

## Owner conversation

Use one Slack thread per card. In Telegram, start a fresh owner discussion with `/new` and name it with `/title BOARD-CARD` once its ID exists. The owner can use `/sessions` and `/resume BOARD-CARD` in the same private chat. In a topics-enabled Telegram chat prefer one topic per card for separate notification delivery.

Immediately after card creation, before dispatch, run `ai-dev-card --board BOARD --card CARD bind` in the originating gateway session. Use the real card and board IDs. If the terminal lacks gateway routing metadata, bind reads the existing native subscription. If Hermes did not create one, inspect `hermes kanban --board BOARD notify-list CARD --json` and restore the originating thread with native `notify-subscribe` using its actual platform, chat and thread IDs. Confirm the subscription before reporting delivery. The worker sessions and the owner conversation have separate lifetimes.

For progress read `ai-dev-card --board BOARD --card CARD history --limit 8`. For investigation add `--details --native` to link verification evidence, workers, board comments and decisions. In chat report the finished stage, its result, the next worker and what it received.
