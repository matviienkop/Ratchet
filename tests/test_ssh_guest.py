import hashlib
import os
from pathlib import Path
import pwd
import runpy
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch


DRIVER = Path(__file__).resolve().parents[1] / "stands/ssh-guest"
MAIN = runpy.run_path(str(DRIVER))["main"]


class SSHGuestTests(unittest.TestCase):
    def test_repair_boots_without_bootstrap_and_streams_stdin_to_guest(self):
        cfg = {"fixture_repair": True}
        start, ssh = Mock(), Mock(return_value=Mock(returncode=9))
        with patch.dict(MAIN.__globals__, settings=lambda: cfg, start=start, ssh=ssh):
            self.assertEqual(MAIN(["repair"]), 9)
            start.assert_called_once_with(cfg, bootstrap=False)
            argv = ssh.call_args.args[1]
            self.assertEqual(argv[:3], ["python3", "-I", "-c"])
            self.assertIn("os.getuid()==0", argv[3])
            self.assertEqual(argv[4:], ["/bin/sh", "-s"])
            cfg.clear()
            with self.assertRaises(ValueError):
                MAIN(["repair"])
            self.assertEqual(start.call_count, 1)

    def test_start_retries_and_waits_for_cloud_init_before_bootstrap(self):
        cfg = {"power": "/power", "bootstrap": "/bootstrap"}
        calls = []
        attempts = [subprocess.TimeoutExpired("ssh", 10), Mock(returncode=0)]

        def ssh(_cfg, argv, **_kwargs):
            calls.append(argv)
            if argv == ["true"]:
                result = attempts.pop(0)
                if isinstance(result, Exception):
                    raise result
                return result

        with patch.dict(MAIN.__globals__, ssh=ssh, settings=lambda: cfg), \
                patch("subprocess.run", side_effect=lambda argv, **kw: calls.append(argv)), \
                patch("time.sleep"):
            self.assertEqual(MAIN(["start"]), 0)
        self.assertEqual(calls[:3], [["/power", "start"], ["true"], ["true"]])
        self.assertIn("cloud-init status --wait", calls[3][-1])
        self.assertEqual(calls[4:], [["/bootstrap"]])

    def test_binary_patch_clean_probe_fresh_checks_and_drift(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / "guest"
            home.mkdir()
            guest_identity = {}
            if os.getuid() == 0:
                nobody = pwd.getpwnam("nobody")
                root.chmod(0o711)
                os.chown(home, nobody.pw_uid, nobody.pw_gid)
                guest_identity = dict(user=nobody.pw_uid, group=nobody.pw_gid, extra_groups=[])
            repo = root / "run with spaces"
            repo.mkdir()

            def git(*args):
                return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.PIPE)

            git("init")
            git("config", "user.name", "Test")
            git("config", "user.email", "test@example.org")
            git("config", "remote.origin.url", "host-unique-private-remote")
            git("commit", "-q", "--allow-empty", "-m", "Ancestor")
            previous = git("rev-parse", "HEAD").decode().strip()
            (repo / ".gitignore").write_text("local-only\n")
            (repo / ".gitattributes").write_text("data export-ignore\n")
            (repo / "data").write_bytes(b"\x00base")
            git("add", ".")
            git("commit", "-qm", "Base")
            base = git("rev-parse", "HEAD").decode().strip()
            (repo / "local-only").write_text("private")
            (repo / "data").write_bytes(b"\x00patched")
            payload = root / "changes.patch"
            payload.write_bytes(git("diff", "--binary", "--full-index", "HEAD"))
            digest = hashlib.sha256(payload.read_bytes()).hexdigest()

            # The coder publishes the candidate on its branch before post-review stand checks.
            git("commit", "-qam", "Candidate")
            self.assertNotEqual(git("rev-parse", "HEAD").decode().strip(), base)
            cfg = {"base_probe": ["python3", "-c", "assert open('data','rb').read() == b'\\x00base'"]}
            started = Mock()
            results = []

            def ssh(_cfg, argv, **kwargs):
                result = subprocess.run(argv, env=dict(os.environ, HOME=str(home)), capture_output=True,
                                        **guest_identity, **kwargs)
                results.append(result)
                return result

            def check(*args, expected=0):
                result = MAIN(["check", str(repo), base, digest, "--", *args])
                self.assertEqual(result, expected, results[-1].stderr.decode())

            with patch.dict(MAIN.__globals__, settings=lambda: cfg, start=started, ssh=ssh):
                tree = git("rev-parse", base + "^{tree}").decode().strip()
                with self.assertRaisesRegex(ValueError, "Git commit"):
                    MAIN(["prepare", str(repo), tree, str(payload)])
                self.assertEqual(MAIN(["prepare", str(repo), base, str(payload)]), 0, results[-1].stderr)
                started.assert_called_once_with(cfg)
                (repo / "data").write_bytes(b"local edit after preparation")
                git("commit", "-qam", "Later local revision")
                check("python3", "-c", "import sys; assert sys.argv[1] == '$(false) two words'; "
                      "assert open('data','rb').read() == b'\\x00patched'", "$(false) two words")
                check("sh", "-ec", 'test "$(git rev-parse HEAD)" = "$1" && ! git cat-file -e "$2" && '
                      'test ! -e local-only && test -z "$(git remote)"', "sh", base, previous)
                check("sh", "-ec", 'touch local-only; test "$(stat -c %a local-only)" = 644; '
                      'test "$(stat -c %a ..)" = 700; test "$(stat -c %a ../..)" = 700')
                check("touch", "new-file", expected=1)
                check("test", "!", "-e", "new-file")
                check("sh", "-c", "echo drift > data", expected=1)
                check("sh", "-ec", "echo staged > data; git add data; printf '\\0patched' > data", expected=1)
                check("sh", "-c", "exit 7", expected=7)
                other = root / "other run"
                git("worktree", "add", "-q", "--detach", str(other), base)
                self.assertNotEqual(MAIN(["check", str(other), base, digest, "--", "true"]), 0)
                cached = list(home.rglob("changes.patch"))
                self.assertEqual(len(cached), 1)
                cached[0].write_bytes(b"altered")
                check("true", expected=1)
                self.assertIn(b"digest mismatch", results[-1].stderr)
