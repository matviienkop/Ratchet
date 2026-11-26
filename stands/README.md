# Project verification stands

Install the SSH guest driver under `/etc/ai-dev/stands/PROJECT/NAME` with mode 0755. The file and its directory chain must be owned by root and writable only by root. This registers a test resource for the project. Keep the guest identity, power credential and SSH host keys outside project repositories.

The adjacent `NAME.json` selects the guest and the environment check that must pass on the approved base before a verification attempt is consumed.

```json
{
  "power": "/etc/ai-dev/stand-secrets/example/power",
  "ssh_config": "/etc/ai-dev/stand-secrets/example/ssh_config",
  "ssh_host": "example-runner",
  "base_probe": ["go", "test", "-count=1", "./internal/filesystem"]
}
```

The power program implements `start`, `status` and `shutdown`. The included `proxmox-power` helper accepts a sibling `power.json` with keys `api` (HTTPS origin), `node`, `vmid`, `name`, `ca` (trusted CA file), `resolve` (curl host:port:IP), and `authorization` (path to a protected curl header file). Scope both the Proxmox service user and its privilege-separated token to VM.Audit and VM.PowerMgmt on the designated guest. The SSH configuration pins the guest host key and selects an unprivileged account. Supply the project toolchain in the guest before checks run.

A check uses a stand when its policy entry names it: `{"name": "stand", "argv": ["make", "check"], "stage": "post_review", "stand": "NAME"}`, with `project` set on the repository entry. `ai-dev-card doctor` lists the stands registered for the project. During `ai-dev-card verify` the runner calls the driver's `start` and `status`, hands it the exact patch with `prepare`, runs each command with `check`, and restores the initial power state when the checks finish. The guest driver waits for SSH and cloud-init on startup, checks the clean base, then runs each command in a fresh checkout with the recorded patch. The worker may commit before or after preparation. The stand identifies the candidate by its workspace path, exact base commit and patch digest.

For a disposable guest that needs an installed package, configure an optional
absolute `bootstrap` program on the SSH driver. It runs after guest startup
and cloud-init. Limit installation to an operator-provisioned artifact and
make repeats safe.

Set `fixture_repair` to `true` only on a guest provisioned for autonomous
repair. The driver's `repair` operation then boots the guest without the
bootstrap hook and streams stdin to an unprivileged guest shell.

For operator-provisioned guests managed by an existing restricted broker, install `lab-guest` as the registered executable for each guest. In the adjacent `NAME.json` set the absolute broker path, the fixed VM ID, and the permitted verbs. For example:

```json
{"broker":"/usr/local/bin/panasms-lab","vmid":200,"operations":["status","start","test","shutdown"]}
```

A project that accepts its releases on such a guest can grant `install` as well. The `proxmox-lab` broker then runs one fixed action as root through the guest agent: download a `.deb`, compare its SHA-256, install it with `apt-get` and print the installed version. The worker calls `ai-dev-card stand install NAME -- PACKAGE_URL SHA256`. The operator bounds the source in the broker's root-owned configuration with `"install": {"url_prefix": "https://..."}` and lists `install` in the `operations` of each guest that may take packages, in both the broker configuration and the stand's `NAME.json`. Without both grants the verb is refused. When the product refuses a plain package replacement, name its maintenance command in `"replace_with": ["COMMAND"]`: the broker prefixes it to `apt-get install` if the package is already installed. The guest needs network access to the release host and to its package mirrors.

The existing broker receives `VMID ACTION` or `VMID test -- COMMAND ARG...`. Its identity checks, per-guest grants and unprivileged execution inside the guest remain authoritative. For an SSH fixture, `operations` can narrow its supported verbs. Fixture repair additionally requires `fixture_repair: true`. The additional CLI introduces no host sudo grant. Use `ai-dev-card stand list` to inspect the effective diagnostic verbs and the card skill's stand reference for examples.

Register one name per physical guest in the project. All workers sharing a guest must use the same `AI_DEV_HOME`. Each diagnostic takes the host lock for that name. Verification holds it through candidate preparation, its checks and power restoration, including recoverable failures. A competing operation reports busy. Verification leaves an already-running guest on and shuts down a guest that it started.

For a registered guest with a clean OS snapshot or an independently verified base image, the [maintenance broker](maintenance.md) adds scoped reset, project-tool installation and checkpoint recovery.
