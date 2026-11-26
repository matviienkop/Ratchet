"""Stand grants, guest isolation and preserving power across card runs."""
import argparse
from pathlib import Path
import runpy
import unittest
from unittest.mock import Mock, patch

from test_card import CardCase
from ai_dev import stands, stand_cli, verify

DRIVERS = Path(__file__).resolve().parents[1] / "stands"


class StandControl(CardCase):
    def test_busy_lease_denies_second_card_until_release(self):
        with stands.exclusive("project", "demo"):
            with self.assertRaisesRegex(stands.CardError, "busy"):
                with stands.exclusive("project", "demo"):
                    self.fail("same stand acquired twice")
        with stands.exclusive("project", "demo"):
            pass

    def test_preparation_failure_restores_only_an_initially_stopped_guest(self):
        self.set_policy(project="project", checks=[{"name":"unit","argv":["true"],"stand":"guest"}])
        self.contract()
        self.implement()
        driver = Path("/guest")
        for start in ("running", "stopped"):
            with patch.object(stands, "registered", return_value=driver), \
                    patch.object(stands, "call", side_effect=lambda _, a: (0, start if a == "status" else "stopped")) as power, \
                    patch.object(stands, "prepare", return_value="base probe failed"):
                receipt, code = verify.verify(self.card, self.repo)
                self.assertEqual(code, 2)
                self.assertEqual(receipt["checks"][0]["klass"], "environment")
                self.assertEqual([c.args[1] for c in power.call_args_list],
                                 ["status", "shutdown"] if start == "stopped" else ["status"])

    def test_verify_on_busy_stand_does_not_call_its_driver(self):
        self.set_policy(project="project", checks=[{"name":"unit","argv":["true"],"stand":"guest"}])
        self.contract()
        self.implement()
        with stands.exclusive("project", "guest"), patch.object(stands, "registered", return_value=Path("/guest")), \
                patch.object(stands, "call") as power, patch.object(stands, "prepare") as prepare:
            receipt, code = verify.verify(self.card, self.repo)
            self.assertEqual(code, 2)
            self.assertIn("busy", receipt["checks"][0]["problem"])
            power.assert_not_called()
            prepare.assert_not_called()

    def test_shutdown_failure_is_recorded_and_voids_a_passing_receipt(self):
        self.set_policy(project="project", checks=[{"name":"unit","argv":["true"],"stand":"guest"}])
        self.contract()
        self.implement()
        driver = self.write("guest", "#!/bin/sh\nexit 0\n")
        driver.chmod(0o755)
        with patch.object(stands, "registered", return_value=driver), patch.object(stands, "prepare", return_value=None), \
                patch.object(stands, "call", side_effect=[(0,"stopped"),(2,"failure")]):
            receipt, code = verify.verify(self.card, self.repo)
            self.assertEqual(code, 2)
            self.assertEqual(receipt["checks"][0]["klass"], "pass")
            self.assertEqual(receipt["checks"][1]["name"], "shutdown-guest")
            self.assertIn("stand.shutdown-failed", [e["event"] for e in self.card.load()["log"]])

    def test_prepare_driver_receives_its_own_sealed_fd(self):
        driver = self.write("probe-driver", """#!/bin/sh
            case "$1" in
              start|status) exit 0;;
              prepare) case "$4" in /proc/self/fd/*) test "$(cat "$4")" = sample;; *) exit 1;; esac;;
            esac
            """)
        driver.chmod(0o755)
        self.assertIsNone(stands.prepare(driver, self.repo, "base", b"sample"))

    def test_lab_restricts_verbs_and_forwards_literal_guest_argv(self):
        main = runpy.run_path(str(DRIVERS / "lab-guest"))["main"]
        cfg = {"vmid":200, "broker":"/usr/local/bin/lab","operations":["status","start","shutdown","test"]}
        with patch("subprocess.run", return_value=Mock(returncode=3)) as call:
            self.assertEqual(main(["test","--","printf","a; b"], cfg), 3)
            self.assertEqual(call.call_args.args[0], [cfg["broker"], "200", "test", "--", "printf", "a; b"])
            for op in ("repair","prepare","reboot","delete","install"):
                with self.assertRaises(ValueError):
                    main([op], cfg)
            self.assertEqual(call.call_count, 1)
            granted = {**cfg, "operations": ["status", "install"]}
            package = ["https://example.org/demo.deb", "ab" * 32]
            self.assertEqual(main(["install", "--", *package], granted), 3)
            self.assertEqual(call.call_args.args[0], [cfg["broker"], "200", "install", "--", *package])
            for bad in (["install"], ["install", "--", package[0]], ["install", *package]):
                with self.assertRaises(ValueError):
                    main(bad, granted)
            self.assertEqual(call.call_count, 2)

    def test_management_operations_record_outcomes_without_granting_a_verification_receipt(self):
        driver = self.write("fake", "#!/bin/sh\nprintf 'done\\n'\n")
        driver.chmod(0o755)
        args = argparse.Namespace(action="test",name="demo",arguments=["--","true"])
        with patch.object(stands, "registered", return_value=driver), \
                patch.object(stand_cli, "operations", return_value=["test"]):
            result, code = stand_cli.run(args, self.card, self.repo)
        self.assertEqual((code, result["output"]), (0, "done\n"))
        self.assertEqual(self.card.load()["verifications"], [])
        self.assertEqual([e["event"] for e in self.card.load()["log"]], ["stand.requested", "stand.finished"])
