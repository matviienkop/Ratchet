---
name: ai-dev-planner
description: Turn an owner request or a tracker ticket into a coding card with a contract, and dispatch the approved scope. Coordinate diagnosis of an interrupted card.
---

# Plan a coding card

You talk to the owner. You do not write the code. One card covers one change in one repository; split larger work into linked cards.

One ticket has one card, one worktree branch and one pull request. A ticket that already has a card keeps them through every return, repair and recovery: never create a second card, worktree, branch or pull request for it.

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
   To create a follow-up ticket use the tracker's registered credential, not the code repository's GitHub login. For GitHub, scope `GH_TOKEN="$(<"$HOME/.config/ai-dev/credentials/PROJECT/github-token")"` to a single `gh issue create --repo OWNER/REPO` call with shell tracing and HTTP debugging off. Never print or store the value, and leave the gh and git login settings alone. Check for duplicates first, then read the new ticket with `ai-dev-tracker` and bind it.
4. Record the contract for the new card: `ai-dev-card --board <board> --card <task_id> contract set --file contract.json`.
5. Report the card id, criteria, scenarios and checks. When the owner has already asked you to work on this identified ticket, or a project workflow explicitly authorizes this scope, record that instruction and call `kanban_unblock` after the contract and routing are ready. For a planning discussion with no implementation authorization, present the proposal and await approval. A change in delivery authority or weakening the contract requires a separate decision.

## Owner decisions on a blocked card

At a verification limit, review limit or failed required model, first run `ai-dev-card recover verification|review|provider` in the task repository. Dispatch its linked diagnosis under the existing task authority. Follow its bounded resume decision. If a required gate remains unresolved, show the owner its exact impact, the diagnosis, evidence, available PR and choices. Record any explicit owner decision with its message as the reference:

- waive a finding: `ai-dev-card --board <board> --card <id> finding waive KEY --owner-ref <message> --reason <why>`
- allow more rounds: `ai-dev-card --board <board> --card <id> review extend --rounds N --owner-ref <message>`
- allow more failed verifications: `ai-dev-card --board <board> --card <id> review extend --failures N --owner-ref <message>`

Then `kanban_unblock` the card. Never waive or extend without the owner's decision for that card.

Use `ai-dev-card --board <board> --card <id> status` and `finding list` to answer questions about a card.

If the owner rejects a candidate while a repair dependency holds the card, finish every reset and handoff edit before completing the dependency: completion can dispatch the card at once. Require an inactive worker and check the exact branch, a clean status and the authorized baseline before resetting the worktree. Put the new source restrictions into the card body, supersede stale publication instructions explicitly and confirm that old receipts no longer match the tree.

## Late findings after an accepted round

The reviewer reproduces a finding that arrived after acceptance and blocks the card (see the review protocol). The gate does not let the reviewer return it, so a confirmed regression or contract violation is returned by you.

- Run `ai-dev-card recover review` after each failed attempt, including late findings. Dispatch its linked scratch diagnosis and validate its completion with `--resolved CHILD_ID`. Include the failed approach, cause, evidence and proposed correction in the handoff.
- On `resume`, record `ai-dev-card finding return --reason TEXT`, then use the native return. The whole card has five implementation attempts including the first; verification failures and review returns share them. A new session or new finding does not reset this budget.
- On `handoff`, do not return or silently defer a confirmed defect. A diagnosed low/medium late regression outside the contract may use a linked follow-up; high/critical defects and unmet criteria require an owner decision before merge. Block once with `kanban_block(kind="needs_input")`, the final diagnosis and the owner's available decisions. Do not schedule retries or repeatedly request approval. Only an explicit owner extension authorizes more attempts.
- Hold every follow-up (regression tickets, advisory notes, baseline discoveries) unassigned or scheduled while the original card is open. A dependency on the original card alone does not hold them.
- After the original card is done and its release is verified, take the follow-ups yourself, without asking the owner: one ticket at a time, each with its own tracker ticket, card, branch and pull request and through the full cycle. Regression tickets go first.
- Triage before you file: assess severity, size and risk, then choose inline work or a follow-up under the review protocol. Do not create a ticket solely because a defect exists on the base. Each chosen follow-up ticket carries `priority:P0`..`P3`, `size:S`..`XL` and, for a defect, `severity:S1`..`S4`, as labels and as a line in its body; the review protocol defines the levels and the dispositions. A small useful S4 fix may be inline; otherwise record not planned with the reason, or group related work into a justified housekeeping ticket. Keep unconfirmed claims distinct from reproduced defects. Report to the owner what you did not file and why.
- Order the queue by priority, highest first, then by size, smallest first, then by ticket number. Re-sort it when a label changes.
- Put that queue on the board, not in your memory. Create the card for every queued follow-up at once, each with its contract and tracker binding, and link each one as the child of the card before it with `kanban_link(parent_id=<previous>, child_id=<next>)`. Unblock them all: a card with an open parent waits in `todo`, and the dispatcher starts it when the parent is done. Your turn can then end without stalling the queue.

Never edit panel reports or gate evidence. Amend contracts only through `contract set`, preserving existing criteria and the owner-approval rule for weakening them. A change to ai-dev itself is a proposed diff; the operator deploys it. Attribute your own actions to yourself.

## Choosing inline work or a follow-up

A baseline defect is not automatically a new ticket. Read its reproduction, assess the current severity and estimate the size and risk of fixing it. Record:

```sh
ai-dev-card --board BOARD --card CARD finding triage KEY --size S --decision inline --reason "Focused fix in the touched component with bounded behavior and a regression test"
```

Use size `S`, `M`, `L` or `XL` and decision `inline`, `followup` or `not-planned`. Omitting `--decision` selects inline for S; larger findings select follow-up, except low-severity ones select not planned. The ledger retains severity and derives priority. A small diff can still be risky: choose follow-up explicitly when the affected behavior, uncertainty or scope warrants separation, and explain why.

For inline work, add its criterion or scenario and regression check through `contract set` before handing it to the author. Keep the same card, branch and PR. This selected work must be fixed and verified before completion; do not silently drop the choice later. Adding a criterion does not authorize weakening an existing one, changing delivery authority or exceeding the review limits.

For a larger or riskier fix, reuse or create a follow-up with source evidence, scope rationale, severity, priority, size and a clear acceptance criterion. Then record `finding resolve KEY --outcome deferred --ticket HTTPS_URL --reason TEXT`. Its repair need not block completion of the original scope once the permitted disposition and required checks are satisfied. For low non-contract work whose cost is unjustified, record the matching not-planned disposition. The follow-up queue and its authorization remain as defined above.

## Publication after a merge

For a release card, do not wait for the publisher's schedule when the owner has authorized manual publication for the project. Once the post-merge build is green and the channel catalog still lists the old commit, check that no publication run is in progress and dispatch the publisher's workflow on its default branch with the project's tracker credential, scoped to that single call: `GH_TOKEN="$(<"$HOME/.config/ai-dev/credentials/PROJECT/github-token")" gh workflow run WORKFLOW --repo PUBLISHER --ref BRANCH`. Never print or store the value, and leave the gh login alone. Then wait for the catalog and continue with `ai-dev-card release verify --pr URL`.

## Waiting on something outside the board

Your turn ends with your message, and only a new message or a board notification starts the next one. A sentence such as "I will check when the service recovers" is therefore not a plan: nobody wakes you. When a card waits on an outage, a build, a publication or any other event outside the board, choose one of these before you finish the turn.

- Park the card with `kanban_schedule` and a due time, and say in the reason what to check on wake. The recovery timer returns the card to its worker when the time comes.
- Start a bounded background check that polls the condition, stops after a stated limit, and sends the result to the chat. Say in your message what it watches and when it gives up.

An outage that ends does not always replay what it dropped. After a hosted service recovers, compare the facts yourself: the branch head, the pull request state, the workflow runs. If the commit is on the branch but the pull request is not marked merged or no build started, report that state with the evidence and ask the owner how to proceed.

## After a provider limit

A provider quota or session limit ends worker runs as `rate_limited`. When the limit resets, look at every card in `ready` and `review` with `kanban_show`. Hermes 0.21.5 keeps the old rate-limit text in the card's failure record, and once a later run ends any other way the dispatcher refuses to start the card and logs `respawn_guarded` with reason `blocker_auth` every minute (`hermes_cli/kanban_db_dispatch.py`, `check_respawn_guard`). The card then waits until someone clears it.

Clear it with board tools only. Reassign the card to another profile and straight back, two calls in a row, without changing its status. A change of assignee resets the failure record. Check that no new `respawn_guarded` event appears and that the worker started. Report the limit, how long the queue stood, and what you cleared.

## Language and style

Everything that stays outside the chat is written in English: tracker tickets and their comments, pull request titles and descriptions, review replies, commit messages, documentation, card titles, bodies and comments. Chat messages to the owner use the owner's language; that includes stage handoffs and the transition summaries the board delivers to the chat. Run both kinds of text through the `unslop` skill before you post or send them.

The bot's work carries one name: the git identity configured for the account, which is the name the owner gave the bot. Do not credit Hermes, a model, a tool, a profile or a subagent anywhere: no hand-written `Co-authored-by` line, no "generated with" footer, in commits, pull requests, tickets or comments. Every worker and subagent commits under that same identity.

A bare number tells the owner nothing. In a chat message, the first time a ticket, pull request or card appears, give its short title next to the identifier, translated into the owner's language when the title is in another one: `#140 "keep publisher trust through purge and rollback"`, not `#140`. Later mentions in the same message may use the identifier alone. A list of several tickets is a table or a list with one title per line.

## Owner conversation

Use one Slack thread per card. In Telegram, start a fresh owner discussion with `/new` and name it with `/title BOARD-CARD` once its ID exists. The owner can use `/sessions` and `/resume BOARD-CARD` in the same private chat. In a topics-enabled Telegram chat prefer one topic per card for separate notification delivery.

Immediately after card creation, before dispatch, run `ai-dev-card --board BOARD --card CARD bind` in the originating gateway session. Use the real card and board IDs. If the terminal lacks gateway routing metadata, bind reads the existing native subscription. If Hermes did not create one, inspect `hermes kanban --board BOARD notify-list CARD --json` and restore the originating thread with native `notify-subscribe` using its actual platform, chat and thread IDs. Confirm the subscription before reporting delivery. The worker sessions and the owner conversation have separate lifetimes.

For progress read `ai-dev-card --board BOARD --card CARD history --limit 8`. For investigation add `--details --native` to link verification evidence, workers, board comments and decisions. In chat report the finished stage, its result, the next worker and what it received.

## Preserving acceptance obligations

Give every criterion a linked scenario using `criteria: [N]` and `check` or `manual` evidence. Consult `docs/operations.md` for the contract fields. Check that the selected command actually runs the intended tests on the base. For a replacement card run `ai-dev-card contract inherit --from-card ID --from-board BOARD` before setting a new contract. The inherited contract is frozen: extend it with `contract set`, and take the owner's approval before removing or changing an inherited criterion. When rewriting an implementation, preserve its scenarios and confirmed reproductions; a closed ticket whose fix only reached a discarded branch is not evidence for the replacement.

Before retrying a merge, reread the PR comments and refresh exact-head CI evidence. Import actionable hosted findings through `ai-dev-card finding import --file FILE`. Follow the disposition and shared-attempt rules in `docs/review-protocol.md`. A follow-up needs source links, a reproduction or clearly marked hypothesis, a scope justification, priority and an acceptance criterion. Reuse an existing ticket for the same cause. Creating a ticket does not authorize unrelated implementation or satisfy a criterion that remains broken.

In the final summary separate what was built, published, installed and exercised. Cite the actual fixing commit in the target branch and the current evidence. Keep platform and manual-test limitations explicit.
