import os
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
DOCKER = ROOT / "docker"
SERVICES = DOCKER / "rootfs/etc/s6-overlay/s6-rc.d"


class Container(unittest.TestCase):
    def test_scripts_are_executable(self):
        scripts = [DOCKER / "build.sh", DOCKER / "smoke.sh", DOCKER / "rootfs/etc/cont-init.d/03-ratchet",
                   *SERVICES.glob("ratchet-*/run")]
        self.assertGreaterEqual(len(scripts), 6)
        for script in scripts:
            self.assertTrue(os.access(script, os.X_OK), script)

    def test_every_service_is_complete_and_enabled(self):
        names = sorted(p.name for p in SERVICES.glob("ratchet-*"))
        self.assertEqual(names, ["ratchet-notifications", "ratchet-recovery", "ratchet-tracker"])
        for name in names:
            self.assertEqual((SERVICES / name / "type").read_text().strip(), "longrun")
            self.assertTrue((SERVICES / name / "dependencies.d/base").is_file())
            self.assertTrue((SERVICES / "user/contents.d" / name).is_file(), name)
            # A service must drop to the hermes user and keep its state in the volume.
            run = (SERVICES / name / "run").read_text()
            self.assertIn("s6-setuidgid hermes", run)
            self.assertIn("export HOME=/opt/data", run)

    def test_image_copies_what_the_installer_reads(self):
        dockerfile = (DOCKER / "Dockerfile").read_text()
        copied = set(re.findall(r"^COPY (\w+)/ /opt/ratchet/", dockerfile, re.M))
        self.assertLessEqual({"ai_dev", "bin", "stands", "hermes"}, copied)
        for name in copied:
            self.assertTrue((ROOT / name).is_dir(), name)
        hook = (DOCKER / "rootfs/etc/cont-init.d/03-ratchet").read_text()
        self.assertIn("/opt/ratchet/hermes/install.py", hook)
        self.assertTrue((ROOT / "hermes/install.py").is_file())

    def test_build_context_leaves_out_local_state(self):
        ignored = (ROOT / ".dockerignore").read_text().split()
        for entry in (".git", ".env", "codex-session"):
            self.assertIn(entry, ignored)


if __name__ == "__main__":
    unittest.main()
