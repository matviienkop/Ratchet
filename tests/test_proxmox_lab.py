import base64
import json
from pathlib import Path
import runpy
import unittest
from unittest.mock import Mock


DRIVER = Path(__file__).resolve().parents[1] / "stands/proxmox-lab"
Lab = runpy.run_path(str(DRIVER))["Lab"]


class ProxmoxLabTests(unittest.TestCase):
    def setUp(self):
        self.lab = Lab({"api": "https://pve:8006", "node": "local",
                        "guest_user": "lab-qa", "state_directory": "lab-work",
                        "guests": {"200": {"name": "demo", "uuid": "known"}}}, "200")

    def test_identity_requires_registered_name_and_uuid(self):
        for name, uuid in [("other", "known"), ("demo", "other")]:
            self.lab.api = Mock(return_value={"name": name, "smbios1": "uuid=" + uuid})
            with self.assertRaisesRegex(ValueError, "name or UUID"):
                self.lab.verify()

    def test_argv_stays_inside_guest_test_service(self):
        argv = ["printf", "$HOME %s", "single quote '", "--property=User=root"]
        responses = [{"status": "running"}, {"pid": 123}, {"exited": 1, "exitcode": 17}]
        self.lab.api = Mock(side_effect=responses)
        self.assertEqual(self.lab.test(argv), 17)
        args = self.lab.api.call_args_list[1].args
        self.assertEqual(args[0], "agent/exec")
        command = args[1]["command"]
        self.assertEqual(command[0], "/usr/bin/systemd-run")
        self.assertIn("--uid=lab-qa", command)
        self.assertIn("--property=NoNewPrivileges=yes", command)
        self.assertIn("--property=IPAddressDeny=any", command)
        self.assertNotIn("--property=User=root", command)
        self.assertEqual(json.loads(base64.b64decode(command[-1])), argv)
        self.assertEqual(self.lab.api.call_args_list[-1].args, ("agent/exec-status?pid=123",))

    def test_test_requires_running_vm(self):
        self.lab.api = Mock(return_value={"status": "stopped"})
        with self.assertRaisesRegex(ValueError, "stopped"):
            self.lab.test(["true"])
        self.lab.api.assert_called_once_with("status/current")
