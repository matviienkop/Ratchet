---
name: ai-dev-card
description: Work a coding card on the Kanban board as its implementer or its reviewer. Verify the exact tree before review, review in bounded rounds, and follow the ai-dev gate when it refuses a board tool.
---

# Work a coding card

You hold one card. Its acceptance boundary is the contract: criteria, scenarios and checks. `ai-dev-card` keeps the card's evidence and tells you the next step. Start every run with:

```sh
cd "$HERMES_KANBAN_WORKSPACE" && ai-dev-card status
```

Follow the `next` line. The board tools `kanban_request_review`, `kanban_complete` and `kanban_request_changes` are gated: when one is refused, the message names the command to run. Do what it says. Do not look for another way around it.

## Implementer

1. Read the card (`kanban_show`) and `ai-dev-card contract show`. If the card has no contract, write `contract.json` from the card body and run `ai-dev-card contract set --file contract.json`.
2. If `status` says the reviewer asked for changes, read `ai-dev-card review fix-brief`. Fix every item under "Must fix". Add each listed reproduction test to the patch at the given path.
3. Work only inside the workspace. Keep the change inside the contract. Something worth doing outside it goes into a comment or a new card.
4. Run `ai-dev-card verify`. Exit codes:
   - `0` passed. Go to step 5.
   - `1` a check failed on your change. Read the log path in the output, fix the code, verify again.
   - `2` the checks could not judge the change: a command could not start or timed out, or the same check fails on the base revision. Use the recovery procedure below.
   - `3` the budget of failed verifications is spent. Run `ai-dev-card recover verification` and follow the recovery decision below.
5. When `status` shows `delivery: pr` or `delivery: release`, commit on the card's branch, push it and open the pull request now, so CI runs while the review does. Committing does not change the tree, so the receipt stays valid. Later fixes go to the same branch.
6. Call `kanban_request_review(summary=..., reviewer=<reviewer from status>)`. In the summary say what changed, which verification passed and the pull request URL if there is one. Stop after this call.

Editing a file after a passing verification makes the receipt stale. Verify again before requesting review.

A criterion or check that turns out wrong is the owner's call: `kanban_block(kind="needs_input")` with the proposed change. After the owner agrees, `ai-dev-card contract set --file FILE --owner-ref <their message>`.

## Review coordinator

Keep the candidate worktree unchanged. You own dispatch, reproduction and settlement. Each verdict belongs to a separate subprocess reviewer.

1. Run `ai-dev-card review brief` on the verified tree. A full round covers the whole patch. Later rounds cover the interdiff, open blockers and affected callers/tests.
2. Run `ai-dev-card review panel` once. It starts the two subagents concurrently in separate candidate checkouts. Let the command finish. On restart, repeat it to collect any missing report; completed reviewers keep their receipts. The runner tries approved independent cross-provider peers on an external outage, records every attempt and reports reduced diversity. Follow the returned recovery action if a slot remains unavailable.
3. Read the reports, coverage and runtime identities for this round. The OpenAI reviewer traces contracts and callers. The Anthropic reviewer traces failure paths and recovery. Compare the findings, confirm blockers with evidence, and send nonblocking discoveries to follow-ups.
4. A missing required provider or an unverified identity leaves automatic acceptance closed. Pass the candidate, available reports, missing reviewer and reason to a human. For an explicitly configured legacy manual workflow, follow the brief's submit step.
5. Resolve each key in `to_assess` yourself with `ai-dev-card finding assess KEY --severity LEVEL --impact TEXT --evidence FILE`. Inspect a tracked source file or verification artifact to explain actual user impact. Keep each reviewer's confidence and severity visible. A low impact stays advisory; a demonstrably material violation of a named requirement still needs a reproduction before it can block. List the current candidates again with `ai-dev-card finding list`.
   For each key under `to_confirm`, try once to reproduce the blocker with a test that fails when the defect is present:
   `ai-dev-card confirm --finding KEY --file <test file>:<repo path> -- <command that runs it>`
   The command runs on the candidate and on the base. If you cannot write such a test, record it: `ai-dev-card confirm --finding KEY --cannot-reproduce "<why>"`. An unreproduced claim becomes a note and does not block.
6. `ai-dev-card review settle`. It prints the outcome and the one board call to make:
   - `accept`: `kanban_complete(summary=...)`, after `ai-dev-card verify --stage post_review` when the output asks for it. For a card with a pull request pass `metadata={"published_pr": "<URL>"}`; Kanban then requires green required checks on its head.
     For `delivery: release`, the approved contract names the target branch, channel and package. Resolve hosted feedback. When `release.ci_workflow` is set, run `ai-dev-card release check-pr --pr <URL>` on the open reviewed PR. On success, merge with its returned head as an atomic precondition (GitHub CLI `gh pr merge --match-head-commit SHA` with the project's approved merge method). Otherwise verify repository-required CI before the authorized merge. Let the project publisher finish, then run `ai-dev-card release verify --pr <URL>`. On a pending release, inspect the publishing workflow and resume when the channel updates. Include the verified version, channel, PR and release URL in the final handoff. Preserve any separate signature validation or installation smoke receipts required by the project.
   - `changes`: `kanban_request_changes(reason=<contents of the fix brief>)`.
   - `owner`: the nominal round limit is reached. Run `ai-dev-card recover review` and follow the recovery decision.

Settlement queues the full round summary, including an empty list, for the bound conversation. Let the notifier deliver it.

Report few, high-confidence findings. An empty list is a valid result. A blocker names the criterion or scenario it violates, or is a regression against the base. Everything else is a note. A defect that already exists on the base is a discovery: it goes to a follow-up, it does not block this card.

## Handoffs and progress

At the beginning of each run read `ai-dev-card history --limit 6`. At a change of stage (including verification, incident investigation, handback for fixes, and delivery), write one short handoff in the owner's language:

`ai-dev-card handoff --finished "<stage>" --result "<observable result and evidence reference>" --stage "<next stage>" --actor "<next worker/profile>" --inputs "<revision, criteria, open findings>" --reason "<decision and why>"`

Include the same short update in the native transition's summary or reason. If the board lane stays the same, add a `kanban_comment` with the update. Use the card id in chat-facing summaries. On resume read the latest handoff before repeating work. Keep logs and patches in the card evidence directory.


## Recovery

For a failed required reviewer run `ai-dev-card recover provider`. For exhausted correction budgets choose `recover verification` or `recover review`. On `schedule`, post the handoff and use the returned exact reason with `kanban_schedule` on your own card. A timer wakes the scheduled review when capacity is due. On `dispatch`, submit the returned repair_request unchanged with `kanban_create`, link the result as parent of the current card, save the child ID in the handoff, then block on the dependency if still outstanding. On resume, from the original card's workspace run the same recover command with `--resolved CHILD_ID`. A completed linked diagnosis grants one correction or retry. Apply its new evidence and rerun only the interrupted stage. A repeated unresolved mandatory gate produces `handoff`; preserve the PR and name the condition preventing safe completion.

Classify the evidence before dispatch. The author fixes a candidate defect. A reviewer returns its reproduction and previous handoff to that author. If the same command also fails on the base, compare the actual errors. Report an unrelated baseline defect in a follow-up in the project's tracker. Keep the candidate focused.

When an independent environment or baseline failure prevents a required check, use the corresponding `repair_requests` entry in the verification receipt as the arguments to `kanban_create`. Add the exact symptom, what you tried and the authorized stand or tool to its body. For failures outside verification construct the same request with a stable key derived from the board, card, stage and failed operation. The repair card must run immediately in its own scratch workspace with no prerequisites. After the create response supplies its ID, comment the ID and resume condition on the original card. Call `kanban_link(parent_id=<repair ID>, child_id=<original ID>)`. If the repair is already done, read its result and rerun the interrupted step immediately. Otherwise call `kanban_block(kind="dependency", reason=<repair ID and resume condition>)` on your own card if it is still running. Check the landed state. After a restart reuse the same create key and inspect the existing edge with `kanban_show`.

On resumption, read the repair worker's evidence, rerun the same check on the original candidate and stage, and record the result. If a completed repair did not work, retain its evidence; identify the residual cause and dispatch a distinct repair keyed by that cause. A failing required check still needs a passing receipt. An unrelated finding that leaves acceptance unaffected only needs a follow-up.

For a stand diagnostic or dedicated repair, load [stand operations](references/stands.md) on demand.
