"""Installing into Hermes profiles, and the plugin as Hermes would load it."""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ai_dev import stands
from ai_dev.redact import sanitize
from ai_dev.store import CardError

spec = importlib.util.spec_from_file_location("ai_dev_install", ROOT / "hermes" / "install.py")
install = importlib.util.module_from_spec(spec)
spec.loader.exec_module(install)


class Context:
    def __init__(self):
        self.hooks = {}

    def register_hook(self, name, callback):
        self.hooks[name] = callback


class Install(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.hermes = self.root / "hermes"
        for name in ("coder", "reviewer"):
            (self.hermes / "profiles" / name).mkdir(parents=True)
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for name in ("AI_DEV_ROLE", "AI_DEV_PLUGIN_DIR", "HERMES_KANBAN_TASK"):
            os.environ.pop(name, None)

    def run_install(self, *extra):
        argv = ["--prefix", str(self.root / "prefix"), "--bin", str(self.root / "bin"),
                "--hermes-root", str(self.hermes), "--profile", "default:planner",
                "--profile", "coder:coder", "--profile", "reviewer:reviewer", *extra]
        with patch("builtins.print"), patch("sys.stderr"):
            return install.main(argv)

    def plugin(self, profile):
        path = self.hermes / "profiles" / profile / "plugins" / "ai-dev-gate" / "__init__.py"
        spec = importlib.util.spec_from_file_location("installed_gate_" + profile, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_profiles_get_their_lane_and_the_programs_run_from_the_prefix(self):
        self.assertEqual(self.run_install(), 0)
        self.assertEqual(self.run_install(), 0)
        coder = self.hermes / "profiles" / "coder"
        self.assertEqual((coder / "plugins/ai-dev-gate/role").read_text(), "coder\n")
        self.assertEqual((coder / "plugins/ai-dev-gate/prefix").read_text().strip(), str((self.root / "prefix").resolve()))
        self.assertTrue((coder / "skills/software-development/ai-dev-card/SKILL.md").is_file())
        self.assertNotIn("ai-dev commands", (coder / "skills/software-development/unslop/SKILL.md").read_text())
        self.assertFalse((coder / "skills/software-development/ai-dev-planner").exists())
        planner = self.hermes / "skills/software-development"
        self.assertTrue((planner / "ai-dev-planner/SKILL.md").is_file())
        self.assertTrue((planner / "ai-dev-planner/references/task-analysis.md").is_file())
        template = json.loads((planner / "ai-dev-planner/references/analysis-template.json").read_text())
        self.assertEqual(template["readiness"], "needs_input")
        self.assertTrue((planner / "project-onboarding/references/research.md").is_file())
        self.assertIn(str((self.root / "bin").resolve()), (planner / "ai-dev-planner/SKILL.md").read_text())
        done = subprocess.run([str(self.root / "bin" / "ai-dev-card"), "doctor"], capture_output=True, text=True,
                              env={**os.environ, "AI_DEV_HOME": str(self.root / "home")})
        self.assertEqual(json.loads(done.stdout)["version"], "2.0.0")
        from ai_dev.analytics import workflow_fingerprint
        self.assertEqual((self.root / "prefix/ai_dev/workflow.sha256").read_text().strip(), workflow_fingerprint())

    def test_onboarding_tools_get_the_workspace_default(self):
        self.assertEqual(self.run_install("--workspace", str(self.root / "projects")), 0)
        launcher = (self.root / "bin" / "ai-dev").read_text()
        self.assertIn("AI_DEV_WORKSPACE", launcher)
        (self.root / "projects").mkdir()
        done = subprocess.run([str(self.root / "bin" / "ai-dev"), "project", "status", "--project", "ghost"],
                              capture_output=True, text=True, env={k: v for k, v in os.environ.items() if k != "AI_DEV_WORKSPACE"})
        self.assertNotIn("AI_DEV_WORKSPACE", done.stderr)

    def test_installed_plugin_blocks_board_tools_and_ignores_everything_else(self):
        self.run_install()
        module, context = self.plugin("coder"), Context()
        module.register(context)
        self.assertEqual(os.environ["AI_DEV_ROLE"], "coder")
        check = context.hooks["pre_tool_call"]
        self.assertIsNone(check(tool_name="kanban_complete", args={}, task_id=""))
        os.environ.update(HERMES_KANBAN_TASK="t_1", HERMES_KANBAN_WORKSPACE=str(self.root / "missing"),
                          AI_DEV_HOME=str(self.root / "home"))
        self.assertIsNone(check(tool_name="terminal", args={"command": "ls"}, task_id="s"))
        notice = context.hooks["pre_llm_call"]
        notice(model="writer", session_id="session", user_message="secret prompt")
        notice(model="writer", session_id="session")
        from ai_dev.store import Card
        state = Card("default", "t_1").load()
        self.assertEqual(state["routes"]["coder"]["model"], "writer")
        self.assertEqual(len(state["log"]), 1)
        self.assertNotIn("secret prompt", json.dumps(state))
        refused = check(tool_name="kanban_complete", args={}, task_id="s")
        self.assertEqual(refused["action"], "block")
        self.assertIn("through review", refused["message"])
        self.assertEqual(check(tool_name="kanban_request_review", args={})["action"], "block")

    def test_a_broken_install_fails_closed(self):
        self.run_install()
        module = self.plugin("reviewer")
        (self.hermes / "profiles/reviewer/plugins/ai-dev-gate/prefix").write_text(str(self.root / "nowhere") + "\n")
        os.environ["HERMES_KANBAN_TASK"] = "t_1"
        with patch.dict(sys.modules):
            for name in [n for n in sys.modules if n == "ai_dev" or n.startswith("ai_dev.")]:
                del sys.modules[name]
            with patch.object(sys, "path", [p for p in sys.path if Path(p).resolve() != ROOT]):
                refused = module.check(tool_name="kanban_complete", args={})
        self.assertEqual(refused["action"], "block")
        self.assertIn("not usable", refused["message"])

    def test_foreign_files_are_kept_and_legacy_ones_removed_on_request(self):
        foreign = self.hermes / "profiles/coder/skills/software-development/ai-dev-card"
        foreign.mkdir(parents=True)
        (foreign / "SKILL.md").write_text("someone else's")
        self.assertEqual(self.run_install(), 1)
        self.assertEqual((foreign / "SKILL.md").read_text(), "someone else's")
        self.assertFalse((self.hermes / "plugins" / "ai-dev-gate").exists())
        self.assertFalse((self.root / "prefix").exists())
        (foreign / "SKILL.md").unlink()
        foreign.rmdir()
        old = self.hermes / "profiles/coder/plugins/ai-dev-journal"
        old.mkdir(parents=True)
        (old / install.MARKER).write_text(install.OWNER)
        kept = self.hermes / "profiles/reviewer/skills/software-development/code-review"
        kept.mkdir(parents=True)
        self.assertEqual(self.run_install("--remove-legacy"), 0)
        self.assertFalse(old.exists())
        self.assertTrue(kept.exists())

    def test_missing_profile_is_named(self):
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            code = install.main(["--hermes-root", str(self.hermes), "--profile", "ghost:coder",
                                 "--prefix", str(self.root / "p"), "--bin", str(self.root / "b")])
        self.assertEqual(code, 1)
        self.assertIn("hermes profile create ghost", errors.getvalue())


class Helpers(unittest.TestCase):
    def test_a_stand_must_be_registered_by_root(self):
        with tempfile.TemporaryDirectory() as scratch, patch.object(stands, "ROOT", Path(scratch)):
            driver = Path(scratch) / "project" / "vm"
            driver.parent.mkdir()
            driver.write_text("#!/bin/sh\n")
            driver.chmod(0o755)
            if os.geteuid() != 0:
                with self.assertRaisesRegex(CardError, "Untrusted"):
                    stands.registered("project", "vm")
                self.assertEqual(stands.available("project"), [])
            with self.assertRaises(CardError):
                stands.registered("project", "../vm")

    def test_credentials_are_redacted(self):
        self.assertEqual(sanitize("api_key=abcd1234efgh"), "api_key=[redacted]")
        self.assertEqual(sanitize({"token": "abcd1234"}), {"token": "[redacted]"})


if __name__ == "__main__":
    unittest.main()
