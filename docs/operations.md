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
| `max_defect_failures` | 6 | Failed verifications a card may spend before the owner is asked. |
| `target_branch` | detected | Branch the base is computed against. Set it when `origin/HEAD` is missing. |
| `project` | none | Stand registration name: drivers live in `/etc/ai-dev/stands/<project>/`. |
| `delivery` | `local` | `pr` tells the coder to push and open a pull request after verification. |
| `review.max_rounds` | 3 | Review rounds before the owner decides. |
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


## Optional GitHub package-channel delivery

Set `release` in the card contract during planning for an owner-authorized publication. The same object in repository policy applies to all of that repository's coding cards. Its absence selects the existing local or PR workflow. An example for a project using a GitHub release publisher and an HTTPS package catalog:

```json
{"release":{"channel":"testing","repository":"example/backend","branch":"main",
 "catalog_url":"https://example.github.io/packages/channels/testing.json",
 "publisher":"example/packages","package":"example-backend","architectures":["amd64","arm64"]}}
```

Use the GitHub repository as the native Kanban completion contract. Hermes 0.21.5 `hermes_cli/kanban_pr_acceptance.py` checks required CI against the PR head. A repository with no required status contexts can explicitly set `release.ci_workflow` to an exact GitHub Actions workflow filename, such as `build.yml`. Use native `local-only` for that transport, while keeping the ai-dev release contract. Before merging the open PR run `ai-dev-card release check-pr --pr URL` and pin the authorized merge to the returned head SHA. This verifies the newest pull-request run for the repository, workflow, PR and exact head. A missing or pending run blocks progress. Publication verification requires the saved pre-merge receipt, checks the latest run again and includes the run URL. For a release card the ai-dev gate additionally requires a receipt from `ai-dev-card release verify --pr URL`. This command only reads publication data. It verifies the PR's merged state and target branch, the reviewed tree at its source head, the merge SHA in the HTTPS channel catalog, and the published assets in the specified GitHub release. The supported catalog has a top-level `channel` and `releases` list. Each entry supplies `channel`, `version`, `tag`, `sources.*.{repository,commit}`, and `packages.<architecture>[]` entries with name, architecture, version, size, URL and SHA-256. GitHub release asset size and digest must match. Other catalog formats need an adapter.

The receipt contains the PR, head and merge SHAs, release URL, package versions, architectures and asset URLs. Include them in the handoff with the outcomes of project-specific checks. Projects requiring a signed catalog, installation smoke or deployed-service checks must collect those results separately. Publication verification uses the catalog over HTTPS and GitHub asset metadata.


### Bounded autonomous recovery

For exhausted verification attempts, a capped review with open blockers, or an unavailable required provider, the worker runs `ai-dev-card recover verification|review|provider`. The command produces one stable native repair request. The parent links that scratch diagnosis as a dependency. When the child finishes, `recover KIND --resolved CHILD_ID` checks the native board, parent link, task identity, diagnosis marker and completion summary. The journal records the child and decision. Verification and review budgets receive one additional attempt each, and only a missing reviewer can rerun in its existing round. A successful check, resolved old findings, or a confirmed report from the previously failed reviewer distinguishes a new incident from retries of the same one. An unresolved mandatory gate after diagnosis and the corrective pass leads to a PR and evidence handoff. Credentials, merge scope, acceptance criteria and independent-review requirements retain their own authorization boundaries.

For a recognizable quota or temporary transport outage, `recover provider` first returns bounded delays (15 minutes, one hour, four hours). Its native `kanban_schedule` reason identifies that particular wait. No model runs while parked. Install `hermes/ai-dev-recovery.service` and `hermes/ai-dev-recovery.timer` in `/etc/systemd/system/`, then `systemctl daemon-reload` and `systemctl enable --now ai-dev-recovery.timer`. Other platforms can call `ai-dev-card recovery-wake` once per minute as the planner. The wake command checks the board's scheduled event and exact reason, skips other states and owner pauses, then uses native `kanban unblock`. After the delayed retries, investigate remaining unavailability via the bounded repair path.
