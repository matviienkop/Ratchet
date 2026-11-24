import argparse
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


BIN = Path(__file__).resolve().parents[1] / "bin"
loader = importlib.machinery.SourceFileLoader("discover", str(BIN / "ai-dev-discover"))
spec = importlib.util.spec_from_loader(loader.name, loader)
discovery = importlib.util.module_from_spec(spec)
loader.exec_module(discovery)


def options(sources=(), primary=None, **options):
    return argparse.Namespace(project="kit", name="Example", description="A sample", source=list(sources),
        primary=primary, kind=None, checkout=options.get("checkout", []), tasks_provider="internal",
        tasks_scope_json=None)


class DiscoveryTests(unittest.TestCase):
    def test_description_only(self):
        data = discovery.discover(options())
        self.assertEqual((data["repositories"], data["tasks"], data["description"]),
                         ([], {"provider": "internal"}, "A sample"))

    def test_group_pagination_and_namespace(self):
        first = [{"http_url_to_repo": f"https://lab.example/team/sub/pkg{i}.git"} for i in range(100)]
        with mock.patch.object(discovery, "catalog_page", side_effect=[first, [
                {"http_url_to_repo": "https://lab.example/team/another.git"},
                {"http_url_to_repo": "https://lab.example/other/unrelated.git"}]]) as get:
            data = discovery.discover(options(["https://lab.example/team"]))
        self.assertEqual(len(data["repositories"]), 101)
        self.assertIn("include_subgroups=true", get.call_args_list[0].args[0])
        self.assertIn("page=2", get.call_args_list[1].args[0])
        self.assertTrue(all(item["url"].startswith("https://") for item in data["repositories"]))

    def test_github_org_and_additional_private_url(self):
        group = "https://github.com/acme"
        with mock.patch.object(discovery, "catalog_page", return_value=[
                {"clone_url": group + "/kit.git", "description": "Kit"}
        ]), mock.patch.object(discovery,"read_seed", return_value=("", "")) as read:
            data = discovery.discover(options([group, "git@github.com:acme/private.git"],group.replace("acme", "Acme")+"/Kit"))
        self.assertEqual({r["url"] for r in data["repositories"]},
                         {group+"/kit.git", "git@github.com:acme/private.git"})
        self.assertEqual(read.call_args.args, ("github.com/acme/kit", group.replace("acme", "Acme")+"/Kit"))

    def test_single_seed_to_registration(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env = {**os.environ, "GIT_CONFIG_GLOBAL": str(root / "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1"}
            def git(*args):
                subprocess.run(["git",*args],env=env,check=True,capture_output=True)
            git("config", "--global", f"url.{root.as_uri()}/.insteadOf", "https://code.example/org/")
            for name in ["kit", "api"]:
                local = root/(name+".git")
                git("init", "-q", str(local))
                content = "intro"
                if name == "kit":
                    content = ("git clone git@code.example:org/api.git components/service\n"
                        "[Extra documentation](https://code.example/external/other)\n")
                (local/"README.md").write_text(content)
                git("-C",str(local),"add","README.md")
                git("-C",str(local),"-c","user.name=Test","-c","user.email=test@example.invalid",
                    "commit","-qm","Fixture")
            output = root/"plan.json"
            subprocess.run([BIN/"ai-dev-discover","--project","sample","--source",
                "https://code.example/org/kit","--output",output], check=True, env=env, capture_output=True)
            data = json.loads(output.read_text())
            self.assertEqual({r["path"] for r in data["repositories"]}, {"kit","kit/components/service"})
            created = subprocess.run([BIN/"ai-dev","project","create","--manifest",output,
                "--workspace",root/"workspace"],env=env,check=True,capture_output=True)
            self.assertEqual(json.loads(created.stdout)["verified"],2)

    def test_conflicting_evidence_has_focused_resolution(self):
        text = "git clone https://code.example/org/api first\ngit clone https://code.example/org/api second\n"
        root = "https://code.example/org/kit"
        override = ["https://code.example/org/api=kit/canonical"]
        with mock.patch.object(discovery, "read_seed",return_value=(text,"")):
            with self.assertRaisesRegex(discovery.DiscoveryError,"Conflicting checkout paths"):
                discovery.discover(options([root]))
            data = discovery.discover(options([root],checkout=override))
        self.assertIn("kit/canonical", [r["path"] for r in data["repositories"]])

    def test_relative_submodule(self):
        modules = '[submodule "lib name"]\n path = libs/a\n url = ../common.git\n'
        ident = "code.example/org/common"
        roles, paths = discovery.seed_evidence("code.example/org/kit","",modules,set())
        self.assertEqual(paths[ident], {"libs/a"})
        self.assertEqual(roles[ident], "Git submodule")


if __name__ == "__main__":
    unittest.main()
