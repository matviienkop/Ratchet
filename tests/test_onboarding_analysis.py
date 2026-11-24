import fcntl
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


BIN = Path(__file__).resolve().parents[1] / "bin/ai-dev-analyze"


class OnboardingTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = self.root / "repos/main"
        self.repo.mkdir(parents=True)
        self.git("init", "-q")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "user.name", "Fixture")
        self.git("remote", "add", "origin", "git@example.invalid:fixture/main.git")
        (self.repo / "README.md").write_text("# Fixture\nA test project.\nRun make check.\n[ ] TEST-1 Add a test.\n")
        (self.repo / "secret.pem").write_text("PRIVATE KEY")
        self.git("add", "README.md", "secret.pem")
        self.git("commit", "-qm", "fixture")
        self.manifest = {"project": "demo", "repositories": [{"path": "main", "url": "git@example.invalid:fixture/main.git"}]}
        self.manifest_path = self.root / "project.yaml"
        self.manifest_path.write_text(json.dumps(self.manifest))
        self.report = {"revision": self.git("rev-parse", "HEAD").strip(), "findings": [
            {"kind": "component", "text": "Fixture library", "file": "README.md", "line": 2},
            {"kind": "check", "text": "Run make check in main", "file": "README.md", "line": 3},
            {"kind": "task", "text": "Add a test", "file": "README.md", "line": 4, "id": "TEST-1", "status": "open"}]}

    def git(self, *args):
        return subprocess.check_output(["git", "-C", self.repo, *args], text=True)

    def cli(self, *args, stdin=None, status=0):
        run = subprocess.run([BIN, self.root, *args], input=stdin, capture_output=True, text=True)
        self.assertEqual(run.returncode, status, run.stderr + run.stdout)
        return json.loads(run.stdout) if run.stdout else {}

    def record(self, status=0):
        return self.cli("record", "main", stdin=json.dumps(self.report), status=status)

    def test_scoped_style_evidence_and_refresh(self):
        backend = self.repo / "backend"
        backend.mkdir()
        (backend / ".editorconfig").write_text("[*.java]\nindent_size = 4\n")
        (backend / ".prettierignore").write_text("generated/\n")
        for file in ("FirstTest.java", "SecondTest.java"):
            (backend / file).write_text(f"class {file[:-5]} {{}}\n")
        self.git("add", "backend")
        self.git("commit", "-qm", "Java component")
        self.assertEqual(self.cli("read", "main", "backend/.prettierignore")["excerpt"][0]["text"], "generated/")
        self.report["revision"] = self.git("rev-parse", "HEAD").strip()
        self.record(status=1)
        self.report["style_limitation"] = "External formatting guide is unavailable."
        self.record()
        self.cli("finalize")
        self.assertEqual(self.cli("style", "main", "backend/A.java", "--language", "java")["limitation"],
                         self.report.pop("style_limitation"))
        rules = [
            {"kind": "style", "scope": "backend", "language": "java", "basis": "configured",
             "file": "backend/.editorconfig", "line": 2, "text": "Indent *.java by four spaces."},
            {"kind": "style", "scope": "backend", "language": "java", "basis": "observed",
             "file": "backend/FirstTest.java", "line": 1, "text": "Suffix test classes with Test.",
             "corroboration": {"file": "backend/SecondTest.java", "line": 1}},
        ]
        self.report["findings"].extend(rules)
        self.record()
        self.cli("finalize")

        def style(file, language):
            return self.cli("style", "main", file, "--language", language)["style"]

        self.assertEqual([r["text"] for r in style("backend/src/A.java", "java")], [r["text"] for r in rules])
        self.assertEqual(style("backend/a.ts", "typescript"), [])
        self.assertEqual(style("other/A.java", "java"), [])
        self.manifest["repositories"].append({"path": "missing", "url": "git@example.invalid:fixture/missing.git"})
        self.manifest_path.write_text(json.dumps(self.manifest))
        rules[0]["text"] = "Indent Java source by four spaces, except generated code."
        self.record()
        self.cli("finalize", "--partial", status=2)
        self.assertEqual(style("backend/A.java", "java")[0]["text"], rules[0]["text"])
        self.cli("finalize", status=2)
        self.assertEqual(style("backend/A.java", "java")[0]["text"], rules[0]["text"])
        rules[1]["corroboration"]["file"] = rules[1]["file"]
        self.record(status=1)
        old, = (self.root / ".onboarding/reports").glob("*.json")
        saved = json.loads(old.read_text())
        del saved["style_profile_version"]
        old.write_text(json.dumps(saved))
        self.assertEqual(self.cli("inventory")[0]["status"], "pending-analysis")
        self.git("commit", "-qm", "changed", "--allow-empty")
        self.cli("style", "main", "backend/A.java", "--language", "java", status=1)

    def test_unborn_checkout_before_and_after_first_commit(self):
        target = self.root / "repos/new"
        target.mkdir()
        origin = "git@example.invalid:fixture/new.git"
        for args in (["init", "-q"], ["remote", "add", "origin", origin]):
            subprocess.run(["git", *args], cwd=target, check=True)
        self.manifest["repositories"].append({"url": origin, "path": "new"})
        self.manifest_path.write_text(json.dumps(self.manifest))
        self.record()
        self.assertEqual(self.cli("brief", "new")["files"], [])
        self.assertEqual(self.cli("style", "new", "a.py", "--language", "python")["status"], "empty")
        result = self.cli("finalize")
        self.assertEqual((result["analyzed"], result["empty"], result["total"]), (1, ["new"], 2))
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                        "commit", "--allow-empty", "-qm", "initial"], cwd=target, check=True)
        self.assertEqual(self.cli("inventory")[1]["status"], "pending-analysis")

    def test_discover_record_rerun_and_user_edit(self):
        self.assertEqual(self.cli("finalize", status=2)["status"], "incomplete")
        self.assertEqual(self.cli("brief", "main")["files"], ["README.md"])
        self.assertEqual(self.cli("read", "main", "README.md", "--start", "2", "--limit", "1")["excerpt"][0]["text"], "A test project.")
        self.record()
        self.assertEqual(self.cli("finalize")["local_task_cards"], 1)
        self.assertEqual(self.cli("finalize")["updated"], 0)
        card, = (self.root / "tasks").glob("*.md")
        card.write_text("Owner's edits\n")
        self.assertIn(str(card.relative_to(self.root)), self.cli("finalize")["manual_conflicts"])
        self.assertEqual(card.read_text(), "Owner's edits\n")
        self.assertIn("make check", (self.root / "config/discovered.json").read_text())
        self.git("commit", "-qm", "new revision", "--allow-empty")
        self.record(status=1)
        self.assertEqual(self.cli("finalize", status=2)["outstanding"][0]["status"], "pending-analysis")

    def test_manually_imported_card_keeps_its_identity(self):
        (self.root / "tasks").mkdir()
        manual = self.root / "tasks/original-card.md"
        manual.write_text("---\nid: TEST-1\n---\n# Owner's card\n")
        self.record()
        self.cli("finalize")
        self.assertEqual(list(manual.parent.glob("*.md")), [manual])
        self.assertIn("original-card.md", (self.root / "docs/recovered-tasks.md").read_text())

    def test_partial_results_preserve_complete_outputs_and_user_edits(self):
        self.record()
        full = self.cli("finalize")
        old_config = (self.root / full["configuration"]).read_text()
        self.manifest["repositories"].append({"path": "private", "url": "git@example.invalid:fixture/private.git"})
        self.manifest_path.write_text(json.dumps(self.manifest))
        pending = self.cli("finalize", status=2)
        self.assertEqual((pending["analyzed"], pending["total"]), (1, 2))
        self.assertFalse((self.root / "provisional").exists())
        self.report["findings"][1]["text"] = "A revised finding"
        self.record()
        partial = self.cli("finalize", "--partial", status=2)
        self.assertEqual(partial["status"], "incomplete")
        self.assertEqual(partial["outstanding"][0]["checkout"], "private")
        self.assertEqual((self.root / ".onboarding/last-full-summary.json").read_text(),
                         json.dumps(full, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
        self.assertIn("1 of 2", Path(partial["overview"]).read_text())
        self.assertIn("A revised finding", (self.root / partial["configuration"]).read_text())
        self.assertEqual((self.root / full["configuration"]).read_text(), old_config)
        preview_card, = (self.root / "provisional/tasks").glob("*.md")
        self.assertIn("../../repos/main/README.md#L4", preview_card.read_text())
        self.assertEqual(self.cli("finalize", "--partial", status=2)["updated"], 0)
        preview_card.write_text("Owner annotations\n")
        retry = self.cli("finalize", "--partial", status=2)
        self.assertIn(str(preview_card.relative_to(partial["output_root"])), retry["manual_conflicts"])
        self.assertEqual(preview_card.read_text(), "Owner annotations\n")
        self.manifest["repositories"].pop()
        self.manifest_path.write_text(json.dumps(self.manifest))
        self.assertEqual(self.cli("finalize", "--partial")["status"], "complete")
        self.assertIn("A revised finding", (self.root / full["configuration"]).read_text())

    def test_removed_member_regenerates_only_bot_owned_documents(self):
        self.record()
        self.cli("finalize")
        component, = (self.root / "docs/components").glob("*.md")
        manual, = (self.root / "docs/verification").glob("*.md")
        original_card, = (self.root / "tasks").glob("*.md")
        card_text = original_card.read_text()
        manual.write_text("Owner's notes about this component\n")
        self.manifest["repositories"] = []
        self.manifest_path.write_text(json.dumps(self.manifest))
        self.assertEqual(self.cli("inventory"), [])
        self.cli("record", "main", stdin=json.dumps(self.report), status=1)
        removed = self.cli("finalize")
        self.assertEqual((removed["members"], removed["analyzed"], removed["total"]), ({}, 0, 0))
        self.assertEqual(json.loads((self.root / removed["configuration"]).read_text())["repositories"], {})
        self.assertFalse(component.exists())
        self.assertIn(str(component.relative_to(self.root)), removed["retired"])
        self.assertTrue(list((self.root / ".onboarding/retired/full").rglob(component.name)))
        self.assertEqual(manual.read_text(), "Owner's notes about this component\n")
        self.assertIn(str(manual.relative_to(self.root)), removed["manual_conflicts"])
        self.assertEqual(original_card.read_text(), card_text)
        self.assertIn(str(original_card.relative_to(self.root)), removed["previous_outputs_to_review"])
        self.assertTrue(self.repo.is_dir())
        self.assertEqual(self.cli("finalize")["retired"], [])

    def test_origin_bound_findings_and_legacy_upgrade(self):
        self.record()
        saved, = (self.root / ".onboarding/reports").glob("*.json")
        old = json.loads(saved.read_text())
        del old["origin"]
        saved.write_text(json.dumps(old))
        self.assertEqual(self.cli("inventory")[0]["status"], "analyzed")
        self.cli("finalize")
        self.assertEqual(json.loads(saved.read_text())["origin"], self.manifest["repositories"][0]["url"])
        self.manifest["repositories"][0]["url"] = "git@example.invalid:fixture/fork.git"
        self.manifest_path.write_text(json.dumps(self.manifest))
        self.git("remote", "set-url", "origin", self.manifest["repositories"][0]["url"])
        self.assertEqual(self.cli("inventory")[0]["status"], "pending-analysis")
        self.report["origin"] = "git@example.invalid:fixture/main.git"
        self.record(status=1)
        partial = self.cli("finalize", status=2)
        self.assertEqual(json.loads((self.root / ".onboarding/summary.json").read_text()), partial)

    def test_manifest_read_waits_for_project_directory_writer(self):
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self.manifest_path.write_text("{}")
            with subprocess.Popen([BIN, self.root, "inventory"], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True) as child:
                try:
                    with self.assertRaises(subprocess.TimeoutExpired):
                        child.communicate(timeout=0.5)
                finally:
                    self.manifest["repositories"] = []
                    self.manifest_path.write_text(json.dumps(self.manifest))
                    fcntl.flock(fd, fcntl.LOCK_UN)
                stdout, stderr = child.communicate(timeout=5)
                self.assertEqual(child.returncode, 0, stderr)
                self.assertEqual(json.loads(stdout), [])
        finally:
            os.close(fd)

    def test_external_tracker_and_provenance_validation(self):
        self.manifest["tasks"] = {"provider": "linear"}
        self.manifest_path.write_text(json.dumps(self.manifest))
        self.record()
        self.assertEqual(self.cli("finalize")["local_task_cards"], 0)
        self.assertFalse((self.root / "tasks").exists())
        self.report["findings"][0]["line"] = 500
        self.record(status=1)
        self.report["findings"][0]["file"] = "../README.md"
        self.record(status=1)
        self.git("remote", "set-url", "origin", "git@example.invalid:fixture/other.git")
        self.assertEqual(self.cli("inventory")[0]["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
