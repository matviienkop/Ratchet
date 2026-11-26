# Container image

The image is an official Hermes Agent release with Ratchet on top. It runs the same parts as a host install: the Hermes gateway with its Kanban dispatcher, the gate plugin and skills in every lane, and the three Ratchet services.

## Build

```sh
docker/build.sh
```

The script asks GitHub for the newest Hermes release, checks that `nousresearch/hermes-agent` has an image for it, builds, and runs the smoke test. The tag names both versions, for example `ratchet-bot:hermes-0.23.4-ratchet-245d13d46971`. `ratchet-bot:latest` points to the same build.

| Variable | Default | Meaning |
| --- | --- | --- |
| `HERMES_VERSION` | `latest` | A Hermes release tag such as `v0.23.4`. `latest` resolves to the newest release. |
| `HERMES_IMAGE` | `nousresearch/hermes-agent` | Repository of the base image. |
| `IMAGE` | `ratchet-bot` | Name of the image to build, with its registry. |
| `CLAUDE_CODE_VERSION` | `latest` | npm version of Claude Code. Empty leaves it out. |

`--push` pushes both tags after the smoke test. `--if-missing` does nothing when the registry already has the tag. `--no-smoke` skips the test.

## Published image

The workflow `.github/workflows/image.yml` builds on GitHub and publishes to the GitHub Container Registry as `ghcr.io/OWNER/ratchet-bot`.

| Event | What happens |
| --- | --- |
| Push to `main` that touches the code or the image files | Unit tests, build, smoke test, publish. |
| Daily schedule | The same, with `--if-missing`: a new Hermes release produces a new image, otherwise nothing is built. |
| Manual run | The same, with the Hermes release you enter. |
| Pull request | Unit tests, build and smoke test. Nothing is published. |

Each publication pushes the full tag and moves `latest`. Deploy by the full tag.

Pin `HERMES_VERSION` for a build you need to repeat. Ratchet hooks into Hermes plugin hooks and board tools, so a new Hermes release can break it. The smoke test checks that the plugin loads and the board works. It does not run a model, so it cannot show that a card passes the gates.

## What the image adds

- The Ratchet source in `/opt/ratchet`, root-owned and read-only.
- GitHub CLI from its own apt repository. Hermes pull request acceptance needs 2.48 or newer.
- Claude Code, which runs the Anthropic reviewer of the default review panel.
- `jq`.
- A boot hook, `/etc/cont-init.d/03-ratchet`.
- Three supervised services: `ratchet-notifications`, `ratchet-tracker` and `ratchet-recovery`.

Project toolchains are not in the image. Build a project image from this one and add what the project's checks need:

```dockerfile
FROM ratchet-bot:hermes-0.23.4-ratchet-245d13d46971
USER root
RUN apt-get update && apt-get install -y --no-install-recommends golang && rm -rf /var/lib/apt/lists/*
```

## State and the boot hook

Hermes keeps everything mutable in `/opt/data`, which is its home directory and a volume. Ratchet's state is there too:

| Path | Content |
| --- | --- |
| `/opt/data/kanban`, `/opt/data/state.db` | Boards and conversations. |
| `/opt/data/.ai-dev` | Card store with contracts, receipts and evidence, and `policy.json`. |
| `/opt/data/ai-dev/workspace` | Registered projects, their clones and worktrees. |
| `/opt/data/auth.json` | Provider logins. |
| `/opt/data/profiles/NAME` | One Hermes profile per lane. |

The plugin, the skills and the Ratchet programs also live under `/opt/data`, because Hermes loads them from the profile directories. A volume would therefore keep an old copy after an image update. The boot hook prevents that. On every start it creates the profiles that are missing, runs `hermes/install.py` from `/opt/ratchet`, enables the plugin in each profile and turns off the Kanban auto-decomposer. The image decides which Ratchet version runs. A failed hook stops the container.

| Variable | Default | Meaning |
| --- | --- | --- |
| `RATCHET_PROFILES` | `default:planner coder:coder reviewer:reviewer` | Profiles and their lanes, as `NAME:ROLE` pairs. |
| `AI_DEV_WORKSPACE` | `/opt/data/ai-dev/workspace` | Root of registered projects. |
| `RATCHET_INSTALL` | `1` | `0` skips the hook and leaves the volume as it is. |

The Hermes variables still apply, for example `HERMES_UID`, `HERMES_GID` and `HERMES_DASHBOARD`.

## Run

```sh
docker run -d --name ratchet -v ratchet-data:/opt/data ratchet-bot:latest
```

The default command is `gateway run`. Start the container as root, the image default: Hermes drops to its own user after the boot hooks.

Configuration comes from outside the image:

- **Policy.** Mount it read-only at `/etc/ai-dev/policy.json`. A root-owned file there wins over the one in the volume, so a worker cannot relax it.
- **Secrets.** Provide tokens for the code host and the chat gateway as files or environment variables. Subscription logins for model providers refresh their tokens and need a writable place, so they stay in the volume.
- **Git identity.** Put one `user.name` and `user.email` into `/opt/data/.gitconfig` before the first card: the owner's, or the bot's own name. See `INSTALL.md` for the difference.
- **Models.** Each profile's `config.yaml` is in the volume. Set the models once with `hermes -p NAME setup` or write the files before the first start.

## Kubernetes

- One replica. The boards are SQLite files with file locks, and a chat gateway reads its updates in one place. Use a StatefulSet with `replicas: 1` or a Deployment with the `Recreate` strategy.
- A ReadWriteOnce block volume at `/opt/data`. Do not put it on a network file system.
- The container must be PID 1. With an injected init process Hermes skips its supervisor, and the boot hook and the Ratchet services do not run.
- Workers run as processes in the same container. Size CPU and memory for the project's builds and tests.
- Stands are reached through drivers under `/etc/ai-dev/stands`. Mount them read-only with their keys, or run checks that need isolation another way.

## Smoke test

```sh
docker/smoke.sh ratchet-bot:latest v0.23.4
```

It starts the image on an empty volume and checks the Hermes version, GitHub CLI, Claude Code, the plugin and skills in three profiles, `ai-dev-card doctor`, and the two always-on services. Then it creates a board, removes the container, starts a new one on the same volume and checks that the board is still there.
