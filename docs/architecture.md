# Architecture

## Division of work

Hermes references are to `NousResearch/hermes-agent` at v0.23.4 (`0bf290fd4a`). The same functions exist in 0.21.5 (`749220ef`).

| Concern | Owner | How |
| --- | --- | --- |
| Pick and start the next step | Hermes | Dispatcher loop inside the gateway promotes, claims and spawns the assignee profile (`website/docs/user-guide/features/kanban.md`, "Dispatcher") |
| Worker liveness | Hermes | Claim TTL 15 min extended by tool activity, heartbeat, crash detection by PID (`hermes_cli/kanban_db.py:280-306`) |
| A worker that stops without finishing | Hermes | Two in-agent nudges, then bounded retry, limit 3, then the card is blocked (`hermes_cli/kanban_db_dispatch.py:938`) |
| Quota and provider errors | Hermes | Exit 75 requeues with a 5 min cooldown and no failure count; exit 78 blocks at once |
| Waiting | Hermes | `kanban_block` kinds; `dependency` resumes when the parent card finishes; repeated blocks escalate to `triage` |
| Owner notices | Hermes | Gateway notifier delivers terminal events to the subscribed chat |
| Required CI on a pull request | Hermes | Completion contract reads required checks on the exact head |
| Checks bound to the exact tree | **ai-dev** | `ai-dev-card verify`, receipts in the card store |
| Review that converges | **ai-dev** | Findings ledger, confirmation, round cap, code-derived outcome |
| Board moves only with evidence | **ai-dev** | `ai-dev-gate` plugin on three board tools, and on terminal calls that run the same three transitions through `hermes kanban` |
| Verification stands | **ai-dev** | Root-registered drivers under `/etc/ai-dev/stands` |

Kanban's own review loop has no round limit (`request_changes`, `hermes_cli/kanban_db.py:3408`), and its bundled `sdlc-review` skill changes its lens every round. That is why review is the part ai-dev replaces rather than configures.

## Components

```
ai_dev/store.py     one directory per card, card.json replaced atomically under a lock
ai_dev/policy.py    operator settings: checks, budgets, review rules per repository
ai_dev/gitio.py     tree snapshot, anchored commits, diffs, scratch checkouts
ai_dev/verify.py    contract, check runner, base probe, receipts, failure budget
ai_dev/review.py    brief, reports, ledger, confirmation, settle, panel
ai_dev/findings.py  hosted finding intake, explicit dispositions, bounded late-return reservations
ai_dev/continuity.py carry obligations and reproductions into a fresh implementation card
ai_dev/gate.py      allow or block a board tool for the coder or reviewer lane
ai_dev/stands.py    trusted stand drivers
ai_dev/cli.py       the ai-dev-card command
```

The Hermes surface the code depends on is small and documented: `ctx.register_hook("pre_tool_call", ...)`, its `{"action": "block", "message": ...}` return, the worker variables `HERMES_KANBAN_TASK`, `HERMES_KANBAN_BOARD`, `HERMES_KANBAN_WORKSPACE`, and the names of three board tools. Nothing imports Hermes modules.

## Card state

`$AI_DEV_HOME` (default `~/.ai-dev`) holds one directory per card, shared by every profile that works it:

```
cards/<board>/<card>/card.json        contract, base, verification receipts, review rounds, findings ledger, log
cards/<board>/<card>/evidence/
    verify-N/candidate.patch, check-K-<name>.log
    base-probes/<digest>.log
    round-N/brief.json, patch.diff, interdiff.diff, report-<reviewer>.json, fix-brief.md
    confirm/<finding>/<attempt>/files/..., candidate.log, base.log
```

Kanban stays the source of truth for the card's status, assignee and history. `card.json` holds only what Kanban does not have.

**Tree identity.** A snapshot is `git write-tree` over a private index that takes in tracked, staged and untracked files and leaves ignored ones out. Each verified snapshot is committed under `refs/ai-dev/<board>/<card>/vN`, so it can be diffed and checked out later. A commit by the coder does not change the tree, so a receipt survives it. An edit does change it, so the receipt goes stale.

## Verification

`ai-dev-card verify` runs the `pre_review` checks from the policy and the contract, in order, in the workspace.

| Result | Meaning | Charged to the budget |
| --- | --- | --- |
| `passed` (exit 0) | every check passed and the tree did not change meanwhile | no |
| `defect` (exit 1) | a check failed and passes on the base | yes |
| `environment` (exit 2) | a command could not start, timed out, the tree changed while checks ran, or the failing check fails on the base too | no |
| refused (exit 3) | a diagnosis is required or the shared five-attempt budget is exhausted | |

A receipt is bound to the tree and to the set of checks that ran. A check added later, in the policy or the contract, voids older receipts for that stage. A check that leaves untracked files in the workspace changes the tree; the receipt then names the files, and the fix is `.gitignore` or a check that cleans up.

A failing check is rerun once on the base revision, cached per base and command. A check with `stand` runs through the registered driver (`start`, `status`, `prepare`, `check`, `shutdown`). `post_review` checks run after the review accepts and before completion.

The contract freezes at the first verification. Later additions pass. Removing or changing a criterion, scenario or check needs `--owner-ref`.

## Gate

The plugin is installed with a lane per profile, written to a `role` file next to it. That file decides the lane; the `AI_DEV_ROLE` variable is only a fallback for the command line, because a worker inherits its parent's environment. In other profiles and outside Kanban workers the plugin does nothing.

| Lane | Tool | Allowed when |
| --- | --- | --- |
| coder | `kanban_request_review` | the card has a contract and a passing `pre_review` receipt for the workspace tree as it is at this moment |
| coder | `kanban_complete`, `kanban_request_changes` | never; a coding card finishes through review |
| reviewer | `kanban_request_changes` | the last round is settled for this tree with outcome `changes`, or with `accept` and a `post_review` check failed or a confirmed late defect has a bounded return reservation |
| reviewer | `kanban_complete` | the last round is settled for this tree with outcome `accept`, current checks and manual evidence passed, required findings have dispositions, and configured delivery evidence is current |
| reviewer | `kanban_request_review` | never |

A refusal carries the command to run next. An error inside the gate blocks the call.

## Trust boundaries and known limits

- **Same account.** Workers run as the Hermes service account. A worker that edits `card.json` or `~/.ai-dev/policy.json` by hand defeats the gate. A root-owned `/etc/ai-dev/policy.json` protects the policy; the card store has no such protection. The gate guards against a model that stops early or takes a shortcut, not against one that attacks its host.
- **Owner references are recorded, not verified.** `--owner-ref` on a waiver, an extension or a weakened contract is a string. The planner skill requires the owner's explicit word; the audit trail is in `card.json` and in Kanban comments.
- **A reproduction can be wrong.** For a claimed regression the test must pass on the base, which rules out a test that always fails. For a criterion or scenario violation only the candidate is decisive. The same test must pass after the fix, so a bogus test reaches independent diagnosis at the round cap. A remaining mandatory gate gets an evidence-backed handoff.
- **One repository per card.** A change across repositories is several linked cards.
- **Live qualification is version-specific.** The Telegram/GitHub project flow has run on Hermes 0.21.5. Container startup and persistence smoke tests cover the newer image; they do not establish a complete model-driven card cycle on Hermes 0.23.4. Qualify that cycle before relying on changed dispatcher or provider behavior.

## Remaining integration work

Hosted findings have an explicit import/disposition command, but no automatic comment collector. Package release receipts are implemented; installed-product behavior checks remain project-specific. Watched Slack-channel triage, complete token accounting, and a full live qualification of all tracker adapters remain separate work. Keep publication, installation and product-behavior evidence distinct.

## Shared correction budget

Five implementation attempts, including the first, are shared across verification, review and late findings. Every failed attempt requires an independently completed native diagnosis before the next correction. Recovery never extends this budget. At exhaustion the final diagnosis supports one owner handoff; only an explicit owner extension permits further attempts. Native Hermes owns diagnostic scheduling and dependency wakeup.
