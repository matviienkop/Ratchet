# Clean-OS reset and project tool installation

This broker runs on the VM's Proxmox node. It uses the operator's pinned VM identity, clean OS source and bootstrap. The caller supplies a verb. Each destructive operation first shuts the guest down cleanly, creates a disk-only recovery snapshot and records its identity in `/var/lib/ai-dev-stand-maintenance/VMID.json`. The previous power state is restored on success. Approved project tests and maintenance on the same canonical stand use the same CT-side lock. The node broker also serializes maintenance calls by VMID.

## Register one guest

Use a separate root-owned executable and JSON file for each authorized target, for example `/usr/local/libexec/ai-dev/stand-204` (a copy of `proxmox-maintenance`) and `/usr/local/libexec/ai-dev/stand-204.json`. Its directory chain, registered image and bootstrap executable must be root-owned and writable only by root. Python 3.11+, `qm`, `pvesh`, `qemu-img`, snapshot-capable storage and sufficient capacity for a snapshot and a replacement disk are required. Keep VM HA/autostart and concurrent administrative jobs out of the maintenance window. Register one canonical ai-dev stand per VM so repair and acceptance share one lock.

```json
{
  "node": "REPLACE_WITH_LOCAL_PVE_NODE",
  "vmid": 204,
  "name": "REPLACE_WITH_VM_NAME",
  "uuid": "REPLACE_WITH_SMBIOS_UUID",
  "isolation": {},
  "max_checkpoints": 4,
  "baseline": {
    "type": "image",
    "disk": "scsi0",
    "storage": "REPLACE_WITH_VM_DISK_STORAGE",
    "path": "/var/lib/vz/template/iso/panasms-lab/ubuntu-24.04-server-cloudimg-amd64.img",
    "format": "qcow2",
    "sha256": "REPLACE_WITH_VALIDATED_IMAGE_SHA256"
  },
  "bootstrap": "/usr/local/libexec/ai-dev/tools-204",
  "operations": ["reset", "reinstall", "restore-previous"]
}
```

Verify the registered image name, SHA-256, format and distro against the operator's image inventory, including the adjacent `.verified.json` and the provenance recorded there. The same mechanism accepts the registered `debian-13-generic-amd64.qcow2` for a Debian guest. Compare `pvesh get /nodes/NODE/qemu/VMID/config --current 1 --output-format json` and the snapshot/disk inventory with the project grant. `isolation` is the exact live mapping for network and guest integration options checked by the broker, including NICs, IP configuration, serial/parallel ports, device passthrough, special filesystem devices and startup hooks. Set this from the approved isolated configuration. For example, a disallowed historical NIC must never appear in the approved mapping. Resolve pending PVE settings first.

**An existing clean snapshot** can be registered with `"baseline":{"type":"snapshot","name":"clean-os","config_sha256":"..."}`. With an empty digest, run the node broker's read-only `inspect` to obtain the config fingerprint, compare the snapshot inventory with the approved source, and pin the result. Inspect and reset validate the snapshot's own UUID, name and isolation. The fingerprint covers returned snapshot configuration, including its timestamp when exposed by PVE. It is distinct from a disk-content checksum. A snapshot with saved RAM is ineligible. Some backends cannot roll back an older snapshot while retaining a newer one. In that case use the pinned image route on suitable storage or have the operator qualify an alternate storage backend. The broker leaves the recovery checkpoint in place.

**An existing VM with no clean snapshot** uses the image mode above. This preserves the existing VMID, name, SMBIOS UUID, network isolation and other VM settings. It makes an independent pre-reset snapshot, imports the verified local cloud image onto a new boot volume, and confirms that the old boot volume is retained as an unused disk. The image must be self-contained and exactly match the configured SHA-256 and detected format. The selected disk must be first in the configured boot order, on the registered storage, belong to the VM, and be its sole writable OS/data disk. Cloud-init and read-only CD-ROM devices can stay attached. An optional `baseline.size_gib` grows the replacement disk to the registered capacity. An optional `media` mapping pins setup or cloud-init CDs by slot. Each entry has a PVE `volume` (for example `local:iso/stand-seed.iso`), a required `sha256` and an optional `replaces` list of preexisting CD-ROM volume IDs. The broker verifies each file against its hash before shutdown, then selects the fixed media after checkpointing. Use a guest-specific NoCloud seed with a pinned guest host key and offline provisioning when replacing its OS. The operator should validate the base image's boot mode, guest OS, disk layout, tool access and cloud-init configuration on an isolated disposable guest of the same type first. Never borrow another VM's snapshot identity for a wipe. Neither available image nor an example VM number grants authorization for another guest.

**Project bootstrap** is a root-owned executable on the PVE node, runs without arguments in a clean environment and starts with the target powered off. Pin its target VMID and all tool artifacts, versions and hashes. The operator supplies the installation method appropriate to the clean image, for example a fixed guest-agent command with an approved offline artifact volume, or a tested offline installer. It must preserve the transport used to run acceptance, pin guest trust after an OS replacement, verify installed versions and return success only when the guest is usable. A generic cloud image may need QGA and an unprivileged acceptance account provisioned before tests. Record project-specific private credentials outside the broker and guest image.

## Expose bounded operations

Connect a root-owned CT executable to this per-VM broker through a pinned host key and dedicated SSH forced command (or an equivalently scoped local transport). It accepts exactly one of `reset`, `reinstall`, `restore-previous` and passes that verb literally to the fixed broker. A forced command must match the entire `SSH_ORIGINAL_COMMAND` to one of these constants and reject all other input. Disable agent, PTY, X11 and port forwarding on the transport key. Add `maintenance` (absolute CT transport path) to the canonical stand driver's JSON and explicitly list the granted verbs in `operations`. Keep the registration, transport key and destination pinned by the operator.

`reset` installs the clean base, `reinstall` runs the registered project tool installation, and `restore-previous` reverts the latest authenticated checkpoint. Each reset or reinstall retains its own checkpoint. For example, after a reset and a subsequent reinstall, the latest checkpoint is the clean OS immediately before the tool install. Earlier snapshots remain on the VM for operator recovery. No automatic snapshot deletion occurs. At the configured capacity the broker requires operator retention. A failed/interrupted operation leaves a durable journal and checkpoint. Inspect before retrying. If failure occurred before the checkpoint fingerprint was persisted, the operator must reconcile the journal with the VM; the worker cannot accept an unverified recovery point.

## Acceptance on MSA2

1. Inventory the current configuration and snapshots. The reported starting inventory has `clean-os` on VM200/201 and no baseline snapshots on VM202/203/204. Choose only guests granted by the owner. Verify the live inventory and maintenance grant independently.
2. Install the broker, config, bootstrap and transport. Run the node's `inspect` and the CT's stand capability listing. Check identity, disk ownership, image hash, snapshot support, isolation, available disk space and ability to retain the old volume. A VM200 clean snapshot containing a NIC removed from the isolated VM will be refused before shutdown.
3. On a disposable equivalent guest, exercise reset → reinstall → project acceptance → restore previous. Verify UUID, VMID, isolation, disk inventory, guest access, tool versions, initial power state, and preservation of every checkpoint. Exercise lost transport and a bootstrap failure. Retry recovery only after confirming the prior operation ended.
4. For VM204, importing the approved image with an independently retained pre-reset snapshot is the path to a clean OS on the same existing VM. Provisioning, inspection and credential setup are independent of actually executing a wipe. Record the first real operation and receipts in the owning card's history.
