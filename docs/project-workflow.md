# Project workflow requirements

This document describes the target workflow for ai-dev 2. Hermes Kanban owns dispatch, claims, retries and dependency wakeups. The following requirements extend the product around those operations.

## Project onboarding and trackers

Register a project in chat from its description, repository URLs and choice of task tracker. Clone and analyze each selected repository, discover existing tasks, checks and conventions, and retain the same workflow for repositories added later. Removing a repository from project scope must preserve its remote contents.

Use the internal board by default. Allow the project to select GitHub Issues and Projects, Linear or Jira. Keep a stable mapping between the external issue and its execution card. The selected tracker owns the user-facing task and agreed solution. Kanban owns execution state. Map stage transitions to the project's tracker statuses. Reconcile repeated events and writes after a restart without creating another card or repeating a comment. Support discussion in ticket comments, in chat, or in chat with just the approved solution written to the ticket.

The planner uses `ai-dev-tracker` to read and bind external tickets. A separate service mirrors confirmed Kanban stages to bound issues. Its receipts and retry history survive restarts. See [tracker setup](../hermes/skills/project-onboarding/references/trackers.md). A production round-trip on Linear and Jira and automatic intake of new ticket comments remain to be validated or implemented.

## Recovery during a task

Return an implementation defect to its author with the failed check, revision and previous handoff. For an independent blocking failure, open or reuse a linked repair card and dispatch a repair worker through Kanban. Give it the observed error, affected environment, reproduction, authority and condition for resuming the parent. Keep the coordinator responsible until dispatch is acknowledged. After repair, rerun the interrupted check on the parent candidate. Keep unrelated existing defects as nonblocking follow-ups. If a required operation exceeds available authority, record the exact operation and notify the owner once.

Exercise recovery by failing an environment check, restarting the gateway after dispatch and completing the repair. Expect one repair card, one active claim and a parent that resumes with the original evidence and budget.

## Two independent reviewers

Default to two independent reviews of the same verified tree. Prefer different providers and models at least as capable as the author's model; exclude the author's model. Start both independently, with the same brief and no access to each other's verdict. Preserve both reports and actual model identities. Merge findings by cause, confirm blockers and settle one round for the pair under the existing cap.

The default panel combines the native reviewer profile and a Claude Code process. Both submit their findings against the same frozen tree. The model identity check and settlement rules are described in the [review protocol](review-protocol.md). Validate the two-provider route in a dispatched worker. Record quota or provider failure explicitly. Under the owner's previously requested fallback, send the candidate to human review and show which reports are missing. A missing report cannot count as approval for autonomous merge.

## Task conversations and stage progress

Give each card a durable logical conversation across worker sessions, corrections and gateway restarts. Record a transition after the board confirms it. Include the completed step and result, the next step, confirmed worker starts, and concise input or evidence references. Keep logs and artifacts in card storage. Deliver a transition once per destination, retry interrupted delivery and preserve ordering within the card.

For Slack, bind the conversation to the originating workspace, channel and thread. Route replies in that thread to the same card. For Telegram, let an authorized user list task conversations, select one and resume it. Label updates with the project and card so an unsolicited update from a different task cannot redirect the user's next reply. Resolve permissions for every binding.

Acceptance requires two concurrent cards: switch between them in Telegram, reply in a bound Slack thread, restart the gateway during notification delivery, and verify the transitions, replies and evidence remain attached to the correct card. The [card history guide](card-history.md) covers bindings, notification receipts and how to recover the transcript. Acknowledgement loss can cause a repeated notification with the same identifier.
