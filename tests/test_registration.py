import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


CLI = Path(__file__).resolve().parents[1] / "bin" / "ai-dev"


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.workspace = self.root / "workspace"
        self.input = self.root / "manifest.json"
        self.env = {
            **os.environ,
            "GIT_CONFIG_GLOBAL": str(self.root / "gitconfig"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "AI_DEV_WORKSPACE": str(self.workspace),
        }
        self.remotes = self.root / "remotes"
        self.remotes.mkdir()
        self.git("config", "--global", f"url.{self.remotes.as_uri()}/.insteadOf", "git@host:org/")
        self.create_repo("main", "docs/policy.md")
        self.create_repo("component", "README.md")

    def git(self, *args):
        return subprocess.run(["git", *args], env=self.env, capture_output=True, text=True, check=True)

    def create_repo(self, name, tracked):
        path = self.remotes / (name + ".git")
        self.git("init", "-q", str(path))
        item = path / tracked
        item.parent.mkdir(exist_ok=True)
        item.write_text("fixture", encoding="utf-8")
        self.git("-C", str(path), "add", tracked)
        self.git("-C", str(path), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "-qm", "Initial test fixture")

    def manifest(self, entries, **kwargs):
        data = {"project": "sample", "description": "Example", "repositories": [
            {"path": path, "url": f"git@host:org/{name}.git"} for path, name in entries
        ]}
        self.input.write_text(json.dumps({**data, **kwargs}), encoding="utf-8")

    def invoke(self, action="create", *options):
        option = ["--manifest", str(self.input)] if action == "create" else ["--project", "sample"]
        result = subprocess.run([CLI, "project", action, *option, *options], env=self.env,
                                capture_output=True, text=True)
        return result.returncode, json.loads(result.stdout)

    def test_nested_retry_and_concurrent_creation(self):
        self.manifest([("core/modules/a", "component"), ("core", "main"), ("late", "late")])
        code, report = self.invoke()
        self.assertEqual((code, report["verified"], report["total"]), (1, 2, 3))
        project = self.workspace / "sample"
        user_note = project / "repos/core/draft"
        user_note.write_text("owner draft", encoding="utf-8")
        self.create_repo("late", "README.md")
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.invoke(), range(2)))
        for code, report in results:
            self.assertEqual((code, report["verified"]), (0, 3))
        self.assertEqual(user_note.read_text(), "owner draft")
        self.assertTrue((project / "tasks").is_dir())
        self.assertEqual(self.invoke("status")[1], results[0][1])

    def test_empty_checkout_survives_retry_and_first_commit(self):
        self.git("init", "-q", "--bare", str(self.remotes / "empty.git"))
        self.manifest([("core", "empty"), ("core/component", "component")])
        code, report = self.invoke()
        self.assertEqual((code, report["verified"], report["total"]), (0, 2, 2))
        url = "git@host:org/empty.git"
        self.assertEqual(report["repositories"][0], {"path": "core", "url": url, "status": "empty", "revision": None})
        core = self.workspace / "sample/repos/core"
        draft = core / "draft"
        draft.write_text("keep", encoding="utf-8")
        self.assertEqual(self.git("-C", str(core), "config", "--local", "--get", "remote.origin.url").stdout.strip(), url)
        for action in ("status", "create"):
            self.assertEqual(self.invoke(action), (0, report))
        self.git("-C", str(core), "add", "draft")
        self.git("-C", str(core), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "-qm", "First commit")
        revision = self.git("-C", str(core), "rev-parse", "HEAD").stdout.strip()
        code, status = self.invoke("status")
        self.assertEqual(code, 0)
        self.assertEqual(status["repositories"][0], {"path": "core", "url": url, "status": "ready", "revision": revision})

    def test_repo_add_empty_and_corrupted_ref(self):
        self.git("init", "-q", "--bare", str(self.remotes / "empty.git"))
        self.manifest([])
        self.invoke()
        for changed in (True, False):
            code, result = self.invoke("repo-add", "--url", "git@host:org/empty.git")
            self.assertEqual((code, result["changed"], result["status"], result["revision"]), (0, changed, "empty", None))
        core = self.workspace / "sample/repos/empty"
        branch = self.git("-C", str(core), "symbolic-ref", "HEAD").stdout.strip()
        (core / ".git" / branch).write_text("f" * 40 + "\n", encoding="ascii")
        code, report = self.invoke("status")
        self.assertEqual((code, report["verified"], report["repositories"][0]["status"]), (1, 0, "pending"))

    def test_shallow_checkout_and_unborn_head_with_existing_refs(self):
        self.manifest([])
        self.invoke()
        url = "git@host:org/main.git"
        core = self.workspace / "sample/repos/core"
        self.git("clone", "--depth=1", url, str(core))
        revision = self.git("-C", str(core), "rev-parse", "HEAD").stdout.strip()
        code, report = self.invoke("repo-add", "--url", url, "--path", "core")
        self.assertEqual((code, report["status"], report["revision"]), (0, "ready", revision))
        self.git("-C", str(core), "symbolic-ref", "HEAD", "refs/heads/unborn")
        code, report = self.invoke("status")
        self.assertEqual((code, report["verified"], report["repositories"][0]["status"]), (1, 0, "pending"))

    def test_declared_submodule_honors_the_parent_gitlink(self):
        main = self.remotes / "main.git"
        component = self.remotes / "component.git"
        pinned = self.git("-C", str(component), "rev-parse", "HEAD").stdout.strip()
        self.git("-C", str(main), "update-index", "--add", "--cacheinfo", f"160000,{pinned},modules/core")
        options = ("-c", "user.name=Test", "-c", "user.email=test@example.invalid")
        self.git("-C", str(main), *options, "commit", "-qm", "Pin component")
        self.git("-C", str(component), *options, "commit", "-qm", "New release", "--allow-empty")
        self.manifest([("kit", "main"), ("kit/modules/core", "component")])
        code, report = self.invoke()
        self.assertEqual(code, 0, report)
        self.assertEqual(report["repositories"][1]["revision"], pinned)
        self.assertEqual(self.invoke("status")[0], 0)

    def test_empty_checkout_cannot_satisfy_a_parent_gitlink(self):
        self.git("init", "-q", "--bare", str(self.remotes / "empty.git"))
        parent = self.remotes / "main.git"
        pinned = self.git("-C", str(parent), "rev-parse", "HEAD").stdout.strip()
        self.git("-C", str(parent), "update-index", "--add", "--cacheinfo", f"160000,{pinned},module")
        self.git("-C", str(parent), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "-qm", "Pin module")
        self.manifest([("main", "main"), ("main/module", "empty")])
        code, report = self.invoke()
        self.assertEqual((code, report["verified"], report["repositories"][1]["status"]), (1, 1, "pending"))
        child = self.workspace / "sample/repos/main/module"
        self.assertFalse((child / ".git").exists())
        self.git("clone", "git@host:org/empty.git", str(child))
        code, report = self.invoke("status")
        self.assertEqual((code, report["verified"]), (1, 1))
        self.assertIn("pinned Git revision", report["repositories"][1]["error"])

    def test_parent_tracked_path_blocks_nested_clone(self):
        self.manifest([("core", "main"), ("core/docs", "component")])
        code, report = self.invoke()
        self.assertEqual((code, report["verified"]), (1, 1))
        self.assertIn("tracks the destination", report["repositories"][1]["error"])
        self.assertEqual((self.workspace / "sample/repos/core/docs/policy.md").read_text(), "fixture")

    def test_invalid_manifest_and_conflicting_retry(self):
        for path in ("../escape", ".git/config", "/absolute", "core//double"):
            self.manifest([(path, "main")])
            self.assertEqual(self.invoke()[0], 2)
            self.assertFalse(self.workspace.exists())
        self.manifest([], tasks={"provider": "linear", "connection": "example"})
        self.assertEqual(self.invoke()[0], 0)
        self.assertFalse((self.workspace / "sample/tasks").exists())
        self.manifest([("core", "main")])
        self.assertIn("conflicts", self.invoke()[1]["error"])
        self.assertEqual(self.invoke("status")[1]["total"], 0)

    def test_add_and_remove_keep_owner_files_settings_and_unrelated_repos(self):
        self.manifest([("main", "main"), ("missing", "late")],
                      tasks={"provider": "external", "scope": {"team": "EXAMPLE"}},
                      extra_settings={"keep": True})
        self.assertEqual(self.invoke()[0], 1)
        self.create_repo("late", "README.md")
        project = self.workspace / "sample"
        data = lambda: json.loads((project / "project.yaml").read_text())
        before = data()
        url = "git@host:org/component.git"
        code, added = self.invoke("repo-add", "--url", url, "--path", "main/modules/service", "--role", "UI")
        self.assertEqual((code, added["changed"], added["total"]), (0, True, 3))
        self.assertFalse((project / "repos/missing").exists())
        draft = project / "repos/main/modules/service/owner-note"
        draft.write_text("keep", encoding="utf-8")
        code, rerun = self.invoke("repo-add", "--url", url)
        self.assertEqual((code, rerun["changed"], rerun["path"]), (0, False, "main/modules/service"))
        self.assertEqual(self.invoke("repo-remove", "--path", "main")[0], 2)
        code, removed = self.invoke("repo-remove", "--path", "main/modules/service")
        self.assertEqual((code, removed["changed"], removed["checkout_retained"]), (0, True, True))
        self.assertEqual(data(), before)
        self.assertEqual(draft.read_text(), "keep")
        self.assertFalse(self.invoke("repo-remove", "--path", "main/modules/service")[1]["changed"])
        self.assertEqual(self.invoke("repo-add", "--url", url, "--path", "main/modules/service")[0], 0)
        self.assertEqual(draft.read_text(), "keep")

    def test_retained_parent_and_conflicts_block_membership(self):
        self.manifest([("main", "main")])
        self.invoke()
        url = "git@host:org/component.git"
        self.assertEqual(self.invoke("repo-add", "--url", url, "--path", "main/docs")[0], 2)
        self.assertEqual(self.invoke("repo-add", "--url", url, "--path", "../escape")[0], 2)
        self.assertEqual(self.invoke("repo-add", "--url", url, "--path", "main")[0], 2)
        self.assertEqual(self.invoke("status")[1]["total"], 1)
        self.invoke("repo-remove", "--path", "main")
        code, report = self.invoke("repo-add", "--url", url, "--path", "main/nested")
        self.assertEqual(code, 2)
        self.assertIn("outside the project membership", report["error"])
        self.assertEqual(self.invoke("status")[1]["total"], 0)
        self.assertEqual((self.workspace/"sample/repos/main/docs/policy.md").read_text(), "fixture")

    def test_concurrent_add_and_pending_retry(self):
        self.manifest([])
        self.invoke()
        def add(name):
            return self.invoke("repo-add", "--url", f"git@host:org/{name}.git")
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(add, ["component", "late"]))
        self.assertEqual([r[0] for r in results], [0, 1])
        self.assertEqual(results[1][1]["status"], "pending")
        self.assertEqual(self.invoke("status")[1]["total"], 2)
        self.create_repo("late", "README.md")
        code, retried = add("late")
        self.assertEqual((code, retried["changed"], retried["total"]), (0, False, 2))

    def test_symlink_and_origin_conflicts_preserve_data(self):
        self.manifest([("core", "main"), ("component", "component")])
        self.assertEqual(self.invoke()[0], 0)
        repos = self.workspace / "sample/repos"
        self.git("-C", str(repos / "core"), "remote", "set-url", "origin", "git@host:org/other.git")
        (repos / "component").rename(self.root / "outside")
        (repos / "component").symlink_to(self.root / "outside", target_is_directory=True)
        for action in ("status", "create"):
            code, report = self.invoke(action)
            self.assertEqual((code, report["verified"]), (1, 0))
        self.assertTrue((self.root / "outside/README.md").is_file())


if __name__ == "__main__":
    unittest.main()
