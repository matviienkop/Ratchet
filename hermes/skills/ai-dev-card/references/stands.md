# Stand operations

Run `ai-dev-card stand list` inside the task. Each registered guest advertises the operations actually enabled on this host. Scratch repair cards use the project board as the catalog name. Keep the same registered name for a guest across all lanes.

- `ai-dev-card stand status NAME` reads the initial power state.
- `ai-dev-card stand start NAME` starts the guest and waits for the registered driver's readiness check.
- `ai-dev-card stand test NAME -- COMMAND ARG...` runs a diagnostic with the registered guest test identity. Start the guest first if it is stopped. Keep existing fixtures and their data intact.
- `ai-dev-card stand repair NAME < repair.sh` runs the script under the guest test identity when the operator explicitly enabled fixture repair. The driver can bypass a broken bootstrap. Keep repair scope on the named guest.
- When a disposable guest advertises `reset`, `reinstall` or `restore-previous`, those commands invoke its operator-provisioned maintenance procedure with no arguments. Use the documented restore point for that guest.
- `ai-dev-card stand shutdown NAME` requests orderly shutdown. Restore the initial state after diagnostics.

A busy result identifies another run using the same stand. Allow that run to finish before repeating the operation. The journal records requested operations, results, bounded logs and repair-script digests. Only `ai-dev-card verify` issues a verification receipt bound to the candidate tree.
