# Operations

Installation is in [INSTALL.md](../INSTALL.md). This page covers the policy file, running a card, the owner's decisions and the first live run.

## Policy file

`ai-dev-card doctor` prints which file is in effect: `/etc/ai-dev/policy.json` when it exists, otherwise `~/.ai-dev/policy.json`. See [examples/policy.json](../examples/policy.json).

`defaults` apply to every repository. An entry under `repos` is keyed by the repository's origin URL or by the absolute path of its main checkout and overrides the defaults.

| Setting | Default | Meaning |
| --- | --- | --- |
| `checks` | none | List of `{name, argv, timeout, stage, stand}`. `stage` is `pre_review` (default) or `post_review`. |
| `check_timeout` | 900 | Seconds per check without its own `timeout`. |
| `base_probe` | true | Rerun a failing check on the base to tell a defect from a pre-existing failure. |
| `max_attempts` | 5 | Total implementation attempts, including the initial attempt. Verification failures, review changes and late returns share this budget. |
| `max_defect_failures` | 5 | Legacy ceiling, retained as an additional ceiling on the shared attempt limit. |
| `target_branch` | detected | Branch the base is computed against. Set it when `origin/HEAD` is missing. |
| `project` | none | Stand registration name: drivers live in `/etc/ai-dev/stands/<project>/`. |
| `delivery` | `local` | `pr` tells the coder to push and open a pull request after verification. |
| `pr_ci` | none | For PR-only delivery, `{repository, branch, ci_workflow}` pins the GitHub repository, target branch and Actions workflow checked by `release check-pr`. |
| `review.max_rounds` | 5 | Legacy ceiling, retained as an additional ceiling on the shared attempt limit. |
| `review.reviewer` | `reviewer` | Profile the coder hands the card to. |
| `review.panel` | OpenAI Astra through Hermes and Anthropic Opus through Claude Code | Two subprocess reviewers. Each entry supplies name, argv, output type, model and optional focus. |
| `review.panel_required` | true | Wait for every panel reviewer before settling. |
| `review.confirm_timeout` | 600 | Seconds per reproduction run. |

A card can add checks in its contract. It cannot remove policy checks.

## A card from request to done

1. **Owner to planner, in chat.** The planner investigates, writes the contract and creates the card held until routing and the contract are ready; an existing implementation instruction authorizes dispatch (skill `ai-dev-planner`).
2. **Owner approves:** `/kanban unblock <id>`. The dispatcher starts the coder in a worktree of the board's repository.
3. **Coder** implements, runs `ai-dev-card verify`, and calls `kanban_request_review`. The gate lets it through only with a passing receipt for the tree as it is.
4. **Review coordinator** runs `review brief` and `review panel`, confirms each reported blocker candidate once, runs `review settle` and makes the board call it names.
5. **Changes** send the card back to the coder with the fix brief. **Accept** completes it. For a card with a pull request, Kanban then requires green required checks on its head.

What the owner sees in chat comes from Kanban's notifier: completed, blocked, gave up, crashed, timed out.

## Owner decisions

After authorized automatic recovery has been attempted, a card may need the following owner decisions. In each the block reason says what is needed.

| Case | What the owner does (through the planner) |
| --- | --- |
| Plan still needs implementation authorization | `/kanban unblock <id>` |
| Required reviewer unavailable or model identities overlap | Review the PR and available reports directly, or restore the independent review route and rerun the panel. |
| Confirmed blockers persist after independent diagnosis and corrective review | `finding waive KEY --owner-ref MSG --reason TEXT`, or `review extend --rounds N --owner-ref MSG`, then unblock; or archive the card |
| Verification still fails after the diagnosed correction | `review extend --failures N --owner-ref MSG`, then unblock |
| A frozen contract needs weakening | `contract set --file FILE --owner-ref MSG`, then unblock |

All commands take `--board <board> --card <id>`.

## Inspecting a card

```sh
ai-dev-card --board panasms --card t_abcd status
ai-dev-card --board panasms --card t_abcd finding list
hermes kanban show t_abcd
hermes kanban runs t_abcd
```

Evidence is under `~/.ai-dev/cards/<board>/<card>/evidence/`.

## Stands

A stand is a root-owned executable at `/etc/ai-dev/stands/<project>/<name>` whose whole directory chain is root-owned and not writable by others. See [stands/README.md](../stands/README.md) for the SSH guest driver. A check with `"stand": "<name>"` goes through it: `start`, `status`, `prepare WORKTREE BASE PATCH_FILE`, `check WORKTREE BASE PATCH_SHA -- COMMAND`, then `shutdown`. An unregistered or failing stand makes the verification an environment failure, which is not charged to the budget.

## First live run

Nothing here has run inside a live Hermes worker. Do this once on a disposable repository before real work, and stop at the first step that fails.

1. Install with three profiles, enable the plugin, restart the gateway. `hermes -p coder plugins list` shows `ai-dev-gate` enabled.
2. Create a board with `--default-workdir` pointing at a throwaway repository that has one trivial test. Put that test into the policy as a check.
3. Ask the planner in chat for a one-line change. Confirm it created a blocked card and that `ai-dev-card --board B --card ID contract show` returns the contract.
4. Unblock. Watch `hermes kanban tail ID`. Check, in this order:
   - the coder's run has `HERMES_KANBAN_WORKSPACE` set to a worktree and loaded the `ai-dev-card` skill;
   - a `kanban_request_review` before `ai-dev-card verify` is refused with the gate's message (the hook loads in dispatcher-spawned workers);
   - after `verify` passes the call goes through and the card moves to `review`;
   - the reviewer run starts in the same worktree and also has the skill;
   - `kanban_complete` before `review settle` is refused;
   - after `settle` with `accept` the card completes.
5. Repeat with a seeded defect and a reviewer instructed to find it: confirm `changes`, the return to the coder, the automatic `fixed` on the next round.
6. Kill the coder's process mid-run. Kanban should reclaim the card and start a new run; `ai-dev-card status` in the new run should report the same state.

If a hook does not load in workers, or a pinned skill does not reach the reviewer run, record what Hermes did instead; the fallback is to load the skill from the profile's own instructions and, for the gate, to open an issue against this repository before going further.


Native recovery uses `kanban_create`, `kanban_show`, `kanban_link`, and `kanban_block` from Hermes `tools/kanban_tools.py` and `tools/kanban_tools_schemas.py` (0.21.5, 749220ef). The repair worker is dispatched under the default planner profile with the pinned ai-dev-repair skill; its scratch workspace prevents writes to the candidate. The parent may be linked by its own active worker. An unsatisfied dependency releases the claim through a dependency block, and Hermes schedules the waiting worker again when the repair completes.


## Exact-head CI for PR-only delivery

A project ending at a reviewable PR can configure CI without any package publication contract:

```json
{"delivery":"pr","pr_ci":{"repository":"example/backend","branch":"main","ci_workflow":"build.yml"}}
```

After independent review and the contracted checks pass, run `ai-dev-card release check-pr --pr URL`.
It reads the open PR, matches its source tree to the reviewed candidate, checks the newest successful
`pull_request` run of that workflow on the exact head SHA, and stores a pre-merge receipt. Complete the
card with `metadata.published_pr` set to the same URL. The gate rereads the current head and newest CI run;
a new commit, a rerun, a closed PR or a policy change requires fresh evidence. A pending or failed rerun
cannot reuse an earlier passing receipt. No package catalog, installation or merge is required for this
boundary.

Whether the PR is merged before or after completion is the project's decision. Where the worker is
authorized to merge, it merges the head the receipt names (`gh pr merge --match-head-commit SHA`) and then
completes the card: the gate accepts a PR merged from that head while its newest exact-head run still passes.
A PR merged from any other head has no evidence and goes to the owner. A PR closed without a merge cannot
complete the card.

The setting is optional for projects whose native Hermes PR acceptance already supplies the required
checks. With a package release contract, its `release.ci_workflow` continues to govern pre-merge CI.
Neither mode weakens native required checks or grants merge authority.

## Optional GitHub package-channel delivery

Set `release` in the card contract during planning for an owner-authorized publication. The same object in repository policy applies to all of that repository's coding cards. Its absence selects the existing local or PR workflow. An example for a project using a GitHub release publisher and an HTTPS package catalog:

```json
{"release":{"channel":"testing","repository":"example/backend","branch":"main",
 "catalog_url":"https://example.github.io/packages/channels/testing.json",
 "publisher":"example/packages","package":"example-backend","architectures":["amd64","arm64"]}}
```

Use the GitHub repository as the native Kanban completion contract. Hermes 0.21.5 `hermes_cli/kanban_pr_acceptance.py` checks required CI against the PR head. A repository with no required status contexts can explicitly set `release.ci_workflow` to an exact GitHub Actions workflow filename, such as `build.yml`. Use native `local-only` for that transport, while keeping the ai-dev release contract. Before merging the open PR run `ai-dev-card release check-pr --pr URL` and pin the authorized merge to the returned head SHA. This verifies the newest pull-request run for the repository, workflow, PR and exact head. A missing or pending run blocks progress. Publication verification requires the saved pre-merge receipt, checks the latest run again and includes the run URL. For a release card the ai-dev gate additionally requires a receipt from `ai-dev-card release verify --pr URL`. This command only reads publication data. It verifies the PR's merged state and target branch, the reviewed tree at its source head, the merge SHA in the HTTPS channel catalog, and the published assets in the specified GitHub release. The supported catalog has a top-level `channel` and `releases` list. Each entry supplies `channel`, `version`, `tag`, `sources.*.{repository,commit}`, and `packages.<architecture>[]` entries with name, architecture, version, size, URL and SHA-256. GitHub release asset size and digest must match. Other catalog formats need an adapter.

The native contract has two preconditions on the worker host and the repository. It calls `gh api --paginate --slurp`, so it needs GitHub CLI 2.48 or newer; with an older `gh` every acceptance ends as `infra` ("evidence unavailable"). The target branch must have required status checks, from branch protection or a ruleset; without them Hermes answers `missing` and asks for a `local-only` contract. Choose the contract when the card is created: Hermes has no command to change it later. For a `delivery: pr` repository without required checks, use native `local-only` together with the `pr_ci` policy below. Do not treat a manual glance at an older CI run as completion evidence.

The receipt contains the PR, head and merge SHAs, release URL, package versions, architectures and asset URLs. Include them in the handoff with the outcomes of project-specific checks. Projects requiring a signed catalog, installation smoke or deployed-service checks must collect those results separately. Publication verification uses the catalog over HTTPS and GitHub asset metadata.


### Bounded autonomous recovery

After every failed implementation attempt, including the first and the final one, run `ai-dev-card recover verification|review`. Before another correction, a separate scratch diagnosis must inspect the failed checks or findings, previous diagnoses and attempted fixes, then return evidence and a concrete next step. Create or reuse its stable native repair request and link it as a prerequisite. `recover KIND --resolved CHILD_ID` verifies its native identity, dependency, completion and marker. Repeated calls reuse the diagnosis and never consume another attempt.

The default shared budget is five attempts total, including the initial implementation. The effective limit is the minimum of `max_attempts`, `max_defect_failures` and `review.max_rounds`; set all three to five when upgrading an older policy. Only an explicit owner `review extend --rounds N --owner-ref MESSAGE` or `--failures N` adds attempts. Diagnosis does not add budget. The final diagnosis produces a handoff and the worker calls `kanban_block(kind='needs_input')`; it must not merge, schedule another correction or repeatedly notify the owner. A new finding, reviewer or session does not reset the card budget. Provider outages use `recover provider` and their existing bounded waiting policy; they do not spend implementation attempts.

An independent diagnosis can establish that no supported correction is available. In that case block for the owner immediately instead of spending the next authorization on an unchanged approach. Credentials, merge scope, acceptance criteria and independent-review requirements retain their own authorization boundaries.

For a recognizable quota or temporary transport outage, `recover provider` first returns bounded delays (15 minutes, one hour, four hours). The worker parks the card with the returned command, `hermes kanban --board BOARD schedule CARD REASON` (Hermes 0.21.5 `hermes_cli/kanban.py`, `_cmd_schedule`; it has no board tool for this), and that reason identifies the particular wait. No model runs while parked. Install `hermes/ai-dev-recovery.service` and `hermes/ai-dev-recovery.timer` in `/etc/systemd/system/`, then `systemctl daemon-reload` and `systemctl enable --now ai-dev-recovery.timer`. Other platforms can call `ai-dev-card recovery-wake` once per minute as the planner. The wake command checks the board's scheduled event and exact reason, skips other states and owner pauses, then uses native `kanban unblock`. After the delayed retries, investigate remaining unavailability via the bounded repair path.

## Scenario evidence and upgrades

`require_scenario_evidence` defaults to `true`. Each criterion needs at least one linked scenario. A scenario has a `criteria` list of 1-based criterion numbers and either `check`, the name of a check in the effective project/contract policy, or `manual`, an absolute JSON evidence path inside the card. For example:

```json
{"criteria": ["Reject an invalid signature"], "scenarios": [
  {"id": "invalid-signature", "criteria": [1], "operation": "Install an archive with an invalid signature",
   "expected": "Rejected before installation", "check": "signature-rejection"}
], "checks": [{"name": "signature-rejection", "argv": ["python3", "-m", "unittest", "tests.test_signature"]}]}
```

Unknown check names are rejected even when the operator temporarily opts out of strict coverage. The validator checks links; the reviewer still judges whether a test proves the behavior. Run project-specific test discovery before freezing the contract. A generic zero exit status does not prove that tests were selected.

Manual proof contains `tree`, `scenario`, `result: "pass"` and nonempty `observed` behavior. Use `ai-dev-card status` to obtain the candidate tree. Completion and release verification reject missing or stale manual proof. These files are attestations by the operator/reviewer, not automated execution evidence.

On an active legacy card, add evidence links with `contract set`. Adding fields to an otherwise unchanged scenario does not weaken the frozen contract. Do not invent passing evidence for historical cards. Completed cards are not migrated or reopened by installation. Projects can set `require_scenario_evidence: false` during an explicit migration; malformed references remain errors.

When the owner chooses a fresh implementation card, run `ai-dev-card contract inherit --from-card ID --from-board BOARD` on the fresh destination. It copies the contract and obligations with provenance, copies stored reproductions, and reruns them on the first new review candidate. It does not carry over acceptance, owner waivers, CI or manual proof. The source card stays unchanged. Inheritance refuses to overwrite an existing destination contract. The rewrite consumes the next shared implementation attempt; an exhausted source needs an owner extension before verification. Diagnosis permissions are not copied. The inherited contract is frozen from the start: `contract set` can add criteria, scenarios, checks and evidence links, and removing or changing an inherited one needs `--owner-ref`.

## Before implementation

Configure and run [project preflight](preflight.md) to establish environment readiness and baseline test discovery before source edits. Its receipts and failure assessments are separate from candidate acceptance and do not consume correction attempts.

## Before implementation planning

Use [task analysis](task-analysis.md) for tickets and requests received directly in chat. Enable `require_task_analysis: true` in defaults or repository policy to require ready analysis and a current implementation plan at verification and board handoff. Configure source access per project; no database or observability connector is assumed to exist.
