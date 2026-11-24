# Install ai-dev

Run everything as the account that runs Hermes, on the host of its local terminal backend. Requirements: Python 3.11 or newer, Git, a Hermes build with Kanban (0.21.5 or newer), and one Hermes profile per lane.

## 1. Profiles

A lane is a role on the board. Each lane is a Hermes profile with its own model.

| Lane | Does | Model |
| --- | --- | --- |
| planner | Talks to the owner, writes the contract, creates cards | the profile the owner chats with, usually `default` |
| coder | Implements and verifies | the coding model |
| reviewer | Reviews, confirms blockers, settles the round | a model from another family than the coder's |

```sh
hermes profile create coder --clone
hermes profile create reviewer --clone
hermes -p default tools enable kanban --platform telegram
```

Set each profile's model with `hermes -p NAME setup` or its `config.yaml`.

## 2. Programs, plugin and skills

```sh
git clone git@github.com:matviienkop/Ratchet.git ~/ai-dev-source
python3 ~/ai-dev-source/hermes/install.py \
    --profile default:planner --profile coder:coder --profile reviewer:reviewer --enable
```

Add `--workspace DIR` when the profile uses the onboarding tools: it is the root where `ai-dev project create` keeps registered projects, and the launchers export it as `AI_DEV_WORKSPACE`.

The installer copies the programs to `~/.local/share/ai-dev`, puts launchers into `~/.local/bin`, and installs into each profile the `ai-dev-gate` plugin with its lane and the skills for that lane. It never overwrites a file it did not create. `--enable` runs `hermes plugins enable ai-dev-gate` per profile; without it, do that yourself.

Upgrading from ai-dev 1.x: add `--remove-legacy` to delete the old `ai-dev-journal` plugin and the `development-run` and `code-review` skills, then remove the old recovery cron jobs (`hermes cron list`, names start with `ai-dev-recover-`).

To update later: `git -C ~/ai-dev-source pull` and run the installer again.

## 3. Policy

Copy [examples/policy.json](examples/policy.json) to `~/.ai-dev/policy.json` and edit it: the checks per repository, the review settings. A root-owned `/etc/ai-dev/policy.json` takes precedence, and a worker cannot edit it.

```sh
ai-dev-card doctor
```

## 4. Board

```sh
hermes kanban boards create panasms --name "PaNasMs" --default-workdir /path/to/main/repository
```

`--default-workdir` is the repository that worktree cards branch from. A card for another repository passes its path at creation.

## 5. Restart

Restart the Hermes gateway. The dispatcher runs inside it, and workers load plugins and skills when they start.

Then follow [the first live run](docs/operations.md#first-live-run).

## External tracker synchronization

For projects using GitHub Projects, Linear or Jira Cloud, run the bridge continuously under the account that owns the Hermes board and project credentials. The [systemd service](hermes/ai-dev-tracker.service) uses CT105's account, default installer prefix and workspace. Create the workspace root with that account if this is a fresh installation. For other installations, adjust `User`, `Group`, `HOME` and the three absolute paths in `ExecStart` to match the launcher's location, registered workspace and shared Kanban home.

From the source checkout, as an operator with systemd administration access:

```sh
install -m 0644 hermes/ai-dev-tracker.service /etc/systemd/system/ai-dev-tracker.service
systemctl daemon-reload
systemctl enable --now ai-dev-tracker.service
systemctl is-active ai-dev-tracker.service
journalctl -u ai-dev-tracker.service -n 20
```

The bridge discovers durable bindings created by the planner. The [tracker registration reference](hermes/skills/project-onboarding/references/trackers.md) covers credentials and provider status mappings. To inspect a single reconciliation, run `ai-dev-tracker WORKSPACE_ROOT watch --kanban-home HERMES_ROOT --once` under the same account. A one-shot pass also applies outstanding tracker updates.


## Stage messages and reviewer capacity waits

For the standard Linux service account, install the stage delivery service and the reviewer-capacity wake timer alongside the gateway. Adjust the account and executable paths for a custom deployment.

```sh
install -m 0644 hermes/ai-dev-notifications.service hermes/ai-dev-recovery.service hermes/ai-dev-recovery.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now ai-dev-notifications.service ai-dev-recovery.timer
```

Container deployments can run `ai-dev-card notifications --watch` with the owner profile and call `ai-dev-card recovery-wake` once a minute under the same account. Both use the shared card store.

### Subscription peers on a dedicated Hermes 0.21.5 host

Verify the account's model catalog and test a short turn for each peer. Install the official `claude-subscription-directsdk` provider into each participating Hermes profile. Keep Claude Code authenticated as the service user. On the dedicated, pinned Hermes checkout run `hermes/enable-subscription-fallback.sh /path/to/hermes-agent` as that user. This replaces the fallback list for default, coder and reviewer. It retains the primary models and effort. Planner uses Sonnet 5.5 as its peer and the coding and review coordinators use Opus 5.5. These are role assignments, not measured equivalence.

This optional integration applies a checksum-checked availability-only guard to the native fallback handler. Configure gateway systemd `ExecStartPre=/usr/bin/python3 /path/to/ai-dev/hermes/availability-fallback.py /path/to/hermes-agent` to check the guard at startup. Requalify the handler when upgrading Hermes before enabling fallback on another revision. Card workers record API route attempts and classified failures from the Hermes API request hooks. The review panel has separate reservations and runtime model checks.
