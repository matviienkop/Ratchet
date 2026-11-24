import concurrent.futures
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


CLI = Path(__file__).resolve().parents[1] / "bin" / "ai-dev-style-default"


class StyleDefaultTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.project = Path(scratch.name) / "sample"
        self.project.mkdir()
        (self.project / "project.yaml").write_text('{"project":"sample","repositories":[]}')
        (self.project / "repos").mkdir()
        self.config = self.project / "config"

    def invoke(self, *options, checkout="future", stack="python", scope=".", code=0):
        result = subprocess.run([CLI, self.project, checkout, "--stack", stack, "--scope", scope, *options],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_greenfield_selections_and_lookup(self):
        none = {"path": None, "selection": None}
        self.assertEqual(self.invoke("--lookup"), none)
        self.assertFalse(self.config.exists())
        root = self.invoke()
        self.assertEqual(root["selection"]["source"], "https://peps.python.org/pep-0008/")
        self.assertEqual(root["selection"]["precedence"], "fallback")
        scoped = self.invoke("--guide", "Service style.", "--source", "https://example.org/guide",
                             scope="services/web")
        go = self.invoke(stack="go", scope="services/web")
        ui = self.invoke(stack="TS")
        self.assertEqual(ui["selection"]["stack"], "typescript")
        self.assertEqual(self.invoke("--lookup", scope="scripts/demo"), root)
        self.assertEqual(self.invoke("--lookup", scope="services/web/api"), scoped)
        self.assertEqual(self.invoke("--lookup", scope="services/website"), root)
        self.assertEqual(self.invoke("--lookup", scope="services/web/api", stack="go"), go)
        self.assertEqual(self.invoke("--lookup", stack="typescript"), ui)
        self.assertEqual(self.invoke("--lookup", scope="src/(app)/[name]", stack="ts"), ui)
        self.assertEqual(self.invoke("--lookup", checkout="other"), none)
        self.assertEqual(self.invoke("--lookup", stack="java"), none)
        self.assertEqual(list((self.project / "repos").iterdir()), [])

    def test_manual_convention_survives_retries_and_bad_records(self):
        self.invoke(stack="elixir", code=2)
        args = ("--guide", "Use standard formatting.", "--source", "https://example.org/style")
        first = self.invoke(*args, stack="elixir")
        path = self.project / first["path"]
        self.assertEqual(path.parent, self.config / "style-defaults")
        self.assertEqual(self.invoke(*args, stack="elixir"), first)
        saved = first["selection"]
        saved.update(guide="Use the team's format.", owner="team")
        path.write_text(json.dumps(saved))
        self.assertEqual(self.invoke(stack="elixir")["selection"], saved)
        self.invoke(*args, stack="elixir", code=2)
        self.assertEqual(json.loads(path.read_text()), saved)
        path.write_text("owner's unfinished edit")
        self.invoke(*args, stack="elixir", code=2)
        self.assertEqual(path.read_text(), "owner's unfinished edit")

    def test_concurrent_first_writes_and_competing_selections(self):
        def call(_):
            return self.invoke()
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(call, range(12)))
        self.assertTrue(all(result == results[0] for result in results))
        def compete(guide):
            result = subprocess.run([CLI, self.project, "future", "--stack", "zig", "--guide", guide,
                                     "--source", "https://example.org/style"], capture_output=True)
            return result.returncode, guide
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            runs = sorted(pool.map(compete, ["Choose A.", "Choose B."]))
        self.assertEqual([run[0] for run in runs], [0, 2])
        self.assertEqual(self.invoke("--lookup", stack="zig")["selection"]["guide"], runs[0][1])
        self.assertEqual(len(list((self.config / "style-defaults").iterdir())), 2)

    def test_rejects_unsafe_paths_and_invalid_options(self):
        for checkout, scope in [("../outside", "."), ("app", "/tmp"), ("app", "a/../b"),
                                ("app", ".git"), ("app//web", ".")]:
            self.invoke(checkout=checkout, scope=scope, code=2)
        self.invoke("--guide", "missing source", code=2)
        self.invoke("--source", "https://example.org/style", code=2)
        for url in ("http://example.org", "https:///missing", "https://user:p@example.org"):
            self.invoke("--guide", "Style", "--source", url, code=2)
        self.invoke("--lookup", "--guide", "Style", "--source", "https://example.org/style", code=2)
        self.assertFalse(self.config.exists())

    def test_symlinks_cannot_redirect_inputs_or_writes(self):
        outside = self.project.parent / "outside"
        outside.mkdir()
        repos = self.project / "repos"
        (repos / "foreign").symlink_to(outside, target_is_directory=True)
        self.invoke(checkout="foreign/nested", code=2)
        self.config.symlink_to(outside, target_is_directory=True)
        self.invoke(code=2)
        self.config.unlink()
        self.config.mkdir()
        defaults = self.config / "style-defaults"
        defaults.symlink_to(outside, target_is_directory=True)
        self.invoke(code=2)
        defaults.unlink()
        record = self.project / self.invoke()["path"]
        record.unlink()
        record.symlink_to(outside / "secret")
        self.invoke(code=2)
        self.invoke("--lookup", code=2)
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
