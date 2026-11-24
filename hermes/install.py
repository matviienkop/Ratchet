#!/usr/bin/env python3
"""Install ai-dev next to Hermes: programs under a prefix, then the gate plugin and skills into each profile.

    python3 hermes/install.py --profile default:planner --profile coder:coder --profile reviewer:reviewer

Run it again after every update. It needs no sudo and changes no Hermes configuration by itself;
`--enable` asks Hermes to enable the plugin, `--remove-legacy` deletes the files of ai-dev 1.x.
"""

import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile

SOURCE = Path(__file__).resolve().parent.parent
MARKER, OWNER = ".ai-dev-managed", "ai-dev\n"
ROLES = ("planner", "coder", "reviewer")
TOOLS = ("ai-dev-card", "ai-dev", "ai-dev-discover", "ai-dev-analyze", "ai-dev-style-default", "ai-dev-incident", "ai-dev-tracker")
SKILLS = {"planner": ("ai-dev-planner", "project-onboarding", "ai-dev-repair"), "coder": ("ai-dev-card",), "reviewer": ("ai-dev-card",)}
LEGACY = ("plugins/ai-dev-journal", "skills/software-development/development-run",
          "skills/software-development/code-review")


class InstallError(Exception):
    pass


def managed(path):
    marker = path / MARKER
    return marker.is_file() and marker.read_text() == OWNER


def owned(destination):
    if destination.is_symlink() or (destination.exists() and not managed(destination)):
        raise InstallError(f"{destination} exists and was not installed by ai-dev. Move it away first.")


def launcher_owned(launcher):
    if launcher.is_symlink() or (launcher.exists() and "ai-dev launcher" not in launcher.read_text(errors="replace")):
        raise InstallError(f"{launcher} exists and was not installed by ai-dev.")


def preflight(prefix, bin_dir, profiles):
    """Refuse before the first write, so a conflict in one profile cannot leave the others half updated."""
    for name in ("ai_dev", "bin", "stands"):
        owned(prefix / name)
    for tool in TOOLS:
        launcher_owned(bin_dir / tool)
    for _, role, home in profiles:
        owned(home / "plugins" / "ai-dev-gate")
        for skill in SKILLS[role]:
            owned(home / "skills" / "software-development" / skill)


def replace_tree(source, destination, extra=None):
    """Swap a managed directory for a fresh copy. A directory we did not create is never overwritten."""
    owned(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staged = Path(tempfile.mkdtemp(prefix=".ai-dev-stage-", dir=destination.parent))
    try:
        target = staged / "tree"
        shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for name, content in (extra or {}).items():
            (target / name).write_text(content)
        (target / MARKER).write_text(OWNER)
        if destination.exists():
            shutil.rmtree(destination)
        target.rename(destination)
    finally:
        shutil.rmtree(staged, ignore_errors=True)


def install_programs(prefix, bin_dir, workspace=None):
    prefix.mkdir(parents=True, exist_ok=True)
    for name in ("ai_dev", "bin", "stands"):
        replace_tree(SOURCE / name, prefix / name)
    bin_dir.mkdir(parents=True, exist_ok=True)
    # The onboarding tools keep registered projects under one workspace root.
    default = f": \"${{AI_DEV_WORKSPACE:={shlex.quote(str(workspace))}}}\"\nexport AI_DEV_WORKSPACE\n" if workspace else ""
    for tool in TOOLS:
        launcher = bin_dir / tool
        launcher_owned(launcher)
        launcher.write_text("#!/bin/sh\n# ai-dev launcher\n" + default +
                            f"exec {shlex.quote(sys.executable)} {shlex.quote(str(prefix / 'bin' / tool))} \"$@\"\n")
        launcher.chmod(0o755)
    subprocess.run([str(bin_dir / "ai-dev-card"), "--help"], stdout=subprocess.DEVNULL, check=True)


def profile_home(root, name):
    home = root if name == "default" else root / "profiles" / name
    if not home.is_dir():
        raise InstallError(f"Hermes profile {name} not found at {home}. Create it first: hermes profile create {name}")
    return home


def install_profile(home, role, prefix, bin_dir):
    replace_tree(SOURCE / "hermes" / "plugin" / "ai-dev-gate", home / "plugins" / "ai-dev-gate",
                 extra={"prefix": str(prefix) + "\n", "role": role + "\n"})
    note = (f"\nThe ai-dev commands are in `{bin_dir}`. If a command is not found, call it by that path or start "
            f"the terminal call with `export PATH={shlex.quote(str(bin_dir))}:\"$PATH\"`.\n")
    for skill in SKILLS[role]:
        source = SOURCE / "hermes" / "skills" / skill
        replace_tree(source, home / "skills" / "software-development" / skill,
                     extra={"SKILL.md": (source / "SKILL.md").read_text() + note})
    return [skill for skill in SKILLS[role]]


def remove_legacy(home):
    removed = []
    for relative in LEGACY:
        path = home / relative
        if path.is_dir() and not path.is_symlink() and managed(path):
            shutil.rmtree(path)
            removed.append(relative)
    return removed


def enable_plugin(name):
    hermes = shutil.which("hermes")
    if not hermes:
        return "hermes is not on PATH; enable the plugin yourself"
    argv = [hermes, *([] if name == "default" else ["-p", name]), "plugins", "enable", "ai-dev-gate"]
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"`{shlex.join(argv)}` did not finish: {error}"
    return "enabled" if done.returncode == 0 else f"`{shlex.join(argv)}` failed: {(done.stderr or done.stdout).strip()[-300:]}"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", action="append", default=[], metavar="NAME:ROLE",
                        help=f"Hermes profile and its lane ({', '.join(ROLES)}). Repeat for every profile. `default` is the root profile.")
    parser.add_argument("--prefix", type=Path, default=Path.home() / ".local/share/ai-dev")
    parser.add_argument("--bin", type=Path, default=Path.home() / ".local/bin")
    parser.add_argument("--hermes-root", type=Path, default=Path.home() / ".hermes")
    parser.add_argument("--workspace", type=Path,
                        help="Root of registered projects for the onboarding tools (sets their AI_DEV_WORKSPACE default).")
    parser.add_argument("--enable", action="store_true", help="Run `hermes plugins enable ai-dev-gate` for each profile.")
    parser.add_argument("--remove-legacy", action="store_true", help="Delete the ai-dev 1.x plugin and skills from each profile.")
    args = parser.parse_args(argv)
    try:
        profiles = []
        for item in args.profile:
            name, _, role = item.partition(":")
            if not name or role not in ROLES:
                raise InstallError(f"--profile {item}: expected NAME:ROLE with ROLE one of {', '.join(ROLES)}.")
            profiles.append((name, role, profile_home(args.hermes_root.expanduser(), name)))
        if not profiles:
            raise InstallError("Name at least one --profile NAME:ROLE.")
        prefix, bin_dir = args.prefix.expanduser().resolve(), args.bin.expanduser().resolve()
        os.umask(0o022)
        preflight(prefix, bin_dir, profiles)
        install_programs(prefix, bin_dir, args.workspace.expanduser().resolve() if args.workspace else None)
        report = {"prefix": str(prefix), "bin": str(bin_dir), "profiles": []}
        for name, role, home in profiles:
            entry = {"profile": name, "role": role, "home": str(home),
                     "skills": install_profile(home, role, prefix, bin_dir)}
            if args.remove_legacy:
                entry["removed_legacy"] = remove_legacy(home)
            if args.enable:
                entry["plugin"] = enable_plugin(name)
            report["profiles"].append(entry)
        report["next"] = [
            "Enable the plugin in each profile (unless --enable did): hermes -p NAME plugins enable ai-dev-gate",
            "Write the policy file and check it: ai-dev-card doctor",
            "Restart the Hermes gateway so the dispatcher and new workers load the plugin and skills."]
        print(json.dumps(report, indent=1))
        return 0
    except (InstallError, OSError, subprocess.CalledProcessError) as error:
        print(f"ai-dev install failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
