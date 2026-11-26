import base64
import json
from pathlib import Path
import runpy
import unittest
from unittest.mock import Mock, patch


DRIVER = Path(__file__).resolve().parents[1] / "stands/proxmox-lab"
Lab = runpy.run_path(str(DRIVER))["Lab"]


class ProxmoxLabTests(unittest.TestCase):
    def setUp(self):
        self.lab = Lab({"api": "https://pve:8006", "node": "local",
                        "guest_user": "lab-qa", "state_directory": "lab-work",
                        "install": {"url_prefix": "https://example.org/releases/download/"},
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

    def test_install_runs_as_root_with_the_package_as_literal_arguments(self):
        url = "https://example.org/releases/download/t-1/demo_1.0_amd64.deb"
        self.lab.api = Mock(side_effect=[{"status": "running"}, {"pid": 7}, {"exited": 1, "exitcode": 0}])
        self.assertEqual(self.lab.install(url, "ab" * 32), 0)
        command = self.lab.api.call_args_list[1].args[1]["command"]
        self.assertEqual(command[:3], ["/usr/bin/python3", "-I", "-c"])
        self.assertEqual(command[4:], [url, "ab" * 32, "[]"])
        self.assertNotIn(url, command[3])
        compile(command[3], "guest-install", "exec")

    def test_install_passes_the_registered_replacement_command(self):
        url = "https://example.org/releases/download/t-1/demo_1.0_amd64.deb"
        self.lab.cfg["install"]["replace_with"] = ["demo-maintenance"]
        self.lab.api = Mock(side_effect=[{"status": "running"}, {"pid": 7}, {"exited": 1, "exitcode": 0}])
        self.assertEqual(self.lab.install(url, "ab" * 32), 0)
        self.assertEqual(self.lab.api.call_args_list[1].args[1]["command"][-1], '["demo-maintenance"]')
        self.lab.cfg["install"]["replace_with"] = "demo-maintenance"
        with self.assertRaisesRegex(ValueError, "replace_with"):
            self.lab.install(url, "ab" * 32)

    def test_install_refuses_other_sources_and_malformed_digests(self):
        self.lab.api = Mock()
        good = "https://example.org/releases/download/t-1/demo.deb"
        for url, digest in [("https://evil.example/releases/download/t-1/demo.deb", "ab" * 32),
                            ("https://example.org/releases/download/../../x.deb", "ab" * 32),
                            ("https://example.org/releases/download/t-1/demo.deb; id", "ab" * 32),
                            ("https://example.org/releases/download/t-1/demo.rpm", "ab" * 32),
                            (good, "AB" * 32), (good, "ab" * 31)]:
            with self.assertRaises(ValueError):
                self.lab.install(url, digest)
        self.lab.api.assert_not_called()
        self.lab.cfg.pop("install")
        with self.assertRaisesRegex(ValueError, "registered release location"):
            self.lab.install(good, "ab" * 32)

    def test_install_needs_an_explicit_grant_and_two_arguments(self):
        main = runpy.run_path(str(DRIVER))["main"]
        cfg = {"api": "https://pve:8006", "node": "local",
               "guests": {"200": {"name": "demo", "uuid": "known"},
                          "201": {"name": "demo", "uuid": "known", "operations": ["status", "install"]}}}
        with self.assertRaisesRegex(ValueError, "Usage"):
            main(["201", "install", "--", "https://example.org/x.deb"])
        with patch.object(Path, "read_text", return_value=json.dumps(cfg)):
            with self.assertRaisesRegex(ValueError, "outside this guest's grant"):
                main(["200", "install", "--", "https://example.org/x.deb", "ab" * 32])
