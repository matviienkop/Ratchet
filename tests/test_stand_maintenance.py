import hashlib
import importlib.machinery
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch


loader = importlib.machinery.SourceFileLoader("maintenance", str(Path(__file__).resolve().parents[1] / "stands/proxmox-maintenance"))
spec = importlib.util.spec_from_loader(loader.name, loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)


class StandMaintenance(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        guard = patch.object(mod, "STATE", self.dir)
        guard.start()
        self.addCleanup(guard.stop)
        self.cfg = dict(vmid=204, node="lab", name="project", uuid="pinned", isolation={},
                        operations=["reset", "reinstall", "restore-previous"])
        self.b = mod.Maintenance(self.cfg)
        self.live = {"name": "project", "smbios1": "uuid=pinned", "boot": "order=scsi0", "scsi0": "lab:vm-204-orig"}
        self.snapshot = dict(self.live, snaptime=17)
        self.cfg["baseline"] = {"type": "snapshot", "name": "clean-os", "config_sha256": mod.fingerprint(self.snapshot)}
        self.calls = []
        def get(resource, *opts):
            if resource == "pending":
                return []
            if resource == "config":
                return self.live
            if resource == "snapshot":
                return [{"name": "clean-os"}]
            if resource == "status/current":
                return {"status": "stopped"}
            if resource.startswith("snapshot/"):
                return self.snapshot
            raise AssertionError(resource)
        self.b.get = Mock(side_effect=get)
        self.b.qm = Mock(side_effect=lambda *args: self.calls.append(args))

    def test_scoped_snapshot_and_recovery(self):
        result = self.b.run("reset")
        self.assertEqual(result["phase"], "complete")
        self.assertEqual(self.calls, [("snapshot", result["checkpoint"], "--vmstate", "0"), ("rollback", "clean-os")])
        restored = self.b.run("restore-previous")
        self.assertEqual(restored["phase"], "restored")
        self.assertEqual(self.calls[-1], ("rollback", result["checkpoint"]))
        self.b.run("restore-previous")
        self.assertEqual(len(self.calls), 3)

    def test_refuses_wrong_vm_network_or_replaced_snapshot_before_writes(self):
        for edit in ({"smbios1": "uuid=other"}, {"net0": "virtio=00:11,bridge=vmbr0"}, {"snaptime": 18}):
            self.snapshot = dict(self.live, **edit)
            with self.assertRaises(ValueError):
                self.b.run("reset")
            self.assertFalse(self.calls)
        self.assertFalse(self.b.journal.exists())

    def test_failed_reset_retains_authenticated_recovery(self):
        self.b.qm = Mock(side_effect=[None, ValueError("storage rejected rollback")])
        with self.assertRaisesRegex(ValueError, "storage"):
            self.b.run("reset")
        with self.assertRaisesRegex(ValueError, "interrupted"):
            self.b.run("reset")
        self.assertIn('"phase": "applying"', self.b.journal.read_text())

    def test_refuses_pending_device_or_ungranted_action(self):
        self.cfg["operations"] = []
        with self.assertRaisesRegex(ValueError, "grant"):
            self.b.run("reinstall")
        self.b.get = Mock(return_value=[{"key": "net0", "pending": "new NIC"}])
        with self.assertRaisesRegex(ValueError, "Pending"):
            self.b.run("reset")
        self.assertFalse(self.calls)

    def test_image_digest_and_data_disk_guard(self):
        image = self.dir / "plain.img"
        image.write_bytes(b"baseline")
        self.cfg["baseline"] = dict(type="image", path=str(image), disk="scsi0", storage="lab", format="raw",
                                    sha256=hashlib.sha256(image.read_bytes()).hexdigest())
        with patch.object(mod, "trusted", return_value=image), patch.object(mod, "invoke", return_value=b'{"format":"raw"}'):
            self.b.baseline(self.live)
            self.cfg["baseline"]["sha256"] = "0" * 64
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                self.b.run("reset")
            self.live["scsi1"] = "lab:other"
            with self.assertRaisesRegex(ValueError, "one mutable boot disk"):
                self.b.run("reset")
            self.assertFalse(self.calls)

    def test_image_import_confirms_retained_old_disk(self):
        self.cfg["baseline"] = dict(type="image", disk="scsi0", storage="lab", path="/fixed/clean.img")
        self.b.baseline = Mock()
        before = self.live.copy()
        def qm(*args):
            if args[0] == "set":
                self.assertEqual(args, ("set", "--scsi0", "lab:0,import-from=/fixed/clean.img"))
                self.live = dict(before, scsi0="lab:vm-204-new", unused0=before["scsi0"])
        self.b.qm = Mock(side_effect=qm)
        self.assertEqual(self.b.run("reset")["phase"], "complete")
        self.assertNotIn("delete", [c.args[0] for c in self.b.qm.call_args_list])

    def test_reinstall_uses_fixed_operator_program_only(self):
        script = Path("/usr/local/libexec/ai-dev/tools-204")
        self.cfg["bootstrap"] = str(script)
        with patch.object(mod, "trusted", return_value=script) as trusted, patch.object(mod, "invoke") as run:
            self.assertEqual(self.b.run("reinstall")["phase"], "complete")
            trusted.assert_called_once_with(str(script), executable=True)
            run.assert_called_once_with([str(script)])
        with self.assertRaisesRegex(ValueError, "one maintenance operation"):
            mod.main(["reinstall", "/tmp/user-supplied"])

    def test_foreign_volume_and_untrusted_operator_path(self):
        self.cfg["baseline"] = dict(type="image", disk="scsi0", storage="lab", path="/fixed/image")
        self.live["scsi0"] = "lab:vm-200-disk-1"
        with self.assertRaisesRegex(ValueError, "not owned"):
            self.b.run("reset")
        self.assertFalse(self.calls)
        with self.assertRaises(ValueError):
            mod.trusted("/tmp/../bin/sh")

    def test_registered_cdrom_replacement_requires_matching_slot_and_hash(self):
        image = self.dir / "disk.iso"
        image.write_bytes(b"fixed")
        destination = "local:iso/fixed.iso"
        self.cfg["media"] = {"ide2": {"volume": destination, "replaces": ["local:iso/old.iso"],
                                      "sha256": hashlib.sha256(image.read_bytes()).hexdigest()}}
        live = dict(self.live, ide2="local:iso/old.iso,media=cdrom")
        with patch.object(mod, "invoke", return_value=str(image).encode()), patch.object(mod, "trusted", return_value=image):
            self.b.media(live)
            with self.assertRaisesRegex(ValueError, "unexpected device"):
                self.b.media(dict(live, ide2="lab:vm-204-data"))
            image.write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "checksum"):
                self.b.media(live)
        self.assertEqual(self.calls, [])
