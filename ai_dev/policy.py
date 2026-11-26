"""Operator-owned settings: checks, budgets and review rules per repository."""

import json
import os
from pathlib import Path
import re

from .store import CardError, home
from . import release, review_routes

NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
STAGES = ("pre_review", "post_review")
SYSTEM_FILE = Path("/etc/ai-dev/policy.json")
OPENAI_PANEL = {"name": "openai-astra", "model": "gpt-6-astra", "output": "hermes-usage",
                "focus": "Trace user-visible contracts, callers, compatibility and test assertions.",
                "argv": ["hermes", "--safe-mode", "--provider", "openai-codex", "-m", "gpt-6-astra",
                         "--reasoning", "medium", "-t", "file"]}
CLAUDE_PANEL = {"name": "anthropic-opus", "model": "claude-opus-5-5", "output": "claude-json",
                "focus": "Trace failure paths, state, concurrency, permissions and recovery.",
                "argv": ["claude", "-p", "--safe-mode", "--restricted", "--strict-mcp-config",
                         "--tools", "Read,Grep,Glob", "--no-session-persistence", "--model",
                         "claude-opus-5-5", "--effort", "medium", "--output-format", "json"]}
def peer(primary, model):
    return {"model": model, "output": primary["output"], "tier": "frontier",
            "argv": [model if arg == primary["model"] else arg for arg in primary["argv"]]}


OPENAI_PANEL["fallbacks"] = [peer(CLAUDE_PANEL, "claude-fable-5-2"), peer(OPENAI_PANEL, "gpt-6.2-sol")]
CLAUDE_PANEL["fallbacks"] = [peer(OPENAI_PANEL, "gpt-6.2-astra"), peer(OPENAI_PANEL, "gpt-6.2-sol")]
DEFAULTS = {
    "project": None,
    "target_branch": None,
    "checks": [],
    "check_timeout": 900,
    "base_probe": True,
    "max_defect_failures": 6,
    "delivery": "local",
    "release": None,
    "pr_ci": None,
    "require_scenario_evidence": True,
    "review": {"max_rounds": 3, "author_model": None, "primary_model": None, "reviewer": "reviewer", "panel": [OPENAI_PANEL, CLAUDE_PANEL], "panel_required": True,
               "panel_timeout": 1200, "max_prompt_bytes": 400_000, "confirm_timeout": 600},
}


def policy_file():
    """A root-owned file wins, so a worker running as the service account cannot relax it."""
    override = os.environ.get("AI_DEV_POLICY")
    if override:
        return Path(override)
    return SYSTEM_FILE if SYSTEM_FILE.is_file() else home() / "policy.json"


def validate_checks(checks, where):
    if not isinstance(checks, list):
        raise CardError(f"{where}: checks must be a list.")
    seen = set()
    for check in checks:
        if not isinstance(check, dict) or set(check) - {"name", "argv", "timeout", "stage", "stand"}:
            raise CardError(f"{where}: a check accepts name, argv, timeout, stage and stand.")
        name, argv = check.get("name"), check.get("argv")
        if not isinstance(name, str) or not NAME.fullmatch(name) or name in seen:
            raise CardError(f"{where}: each check needs a unique lowercase name.")
        seen.add(name)
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
            raise CardError(f"{where}: check {name} needs argv as a nonempty list of strings.")
        if check.get("stage", "pre_review") not in STAGES:
            raise CardError(f"{where}: check {name} stage must be one of {', '.join(STAGES)}.")
        timeout = check.get("timeout")
        if timeout is not None and (type(timeout) is not int or timeout < 1):
            raise CardError(f"{where}: check {name} timeout must be a positive integer.")
        stand = check.get("stand")
        if stand is not None and (not isinstance(stand, str) or not NAME.fullmatch(stand)):
            raise CardError(f"{where}: check {name} has an invalid stand name.")
    return checks


def merge(base, extra):
    result = dict(base)
    for key, value in extra.items():
        result[key] = merge(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) else value
    return result


def load(repo=None, origin=None):
    """Defaults, then the file's `defaults`, then the entry for this repository path or origin URL."""
    path = policy_file()
    try:
        raw = json.loads(path.read_bytes())
    except FileNotFoundError:
        raw = {}
    except ValueError as error:
        raise CardError(f"Policy file {path} is not valid JSON: {error}") from error
    if not isinstance(raw, dict) or set(raw) - {"defaults", "repos"}:
        raise CardError(f"Policy file {path} accepts only `defaults` and `repos`.")
    result = merge(DEFAULTS, raw.get("defaults") or {})
    repos = raw.get("repos") or {}
    for key in (origin, str(repo) if repo else None):
        if key and isinstance(repos.get(key), dict):
            result = merge(result, repos[key])
    unknown = set(result) - set(DEFAULTS)
    if unknown:
        raise CardError(f"Policy file {path} has unknown settings: {', '.join(sorted(unknown))}.")
    if result["release"] is not None:
        release.validate(result["release"])
    if result["pr_ci"] is not None:
        release.validate_pr_ci(result["pr_ci"])
    if type(result["require_scenario_evidence"]) is not bool:
        raise CardError("Set require_scenario_evidence to true or false in the policy, then run ai-dev-card doctor.")
    validate_checks(result["checks"], f"Policy file {path}")
    review = result["review"]
    if set(review) - set(DEFAULTS["review"]):
        raise CardError(f"Policy file {path} has unknown review settings.")
    for key in ("max_rounds", "panel_timeout", "max_prompt_bytes", "confirm_timeout"):
        if type(review[key]) is not int or review[key] < 1:
            raise CardError(f"Policy file {path}: review.{key} must be a positive integer.")
    for key in ("check_timeout", "max_defect_failures"):
        if type(result[key]) is not int or result[key] < 1:
            raise CardError(f"Policy file {path}: {key} must be a positive integer.")
    for key in ("author_model", "primary_model"):
        if review[key] is not None and (not isinstance(review[key], str) or not review[key].strip()):
            raise CardError(f"Policy file {path}: review.{key} must be a model ID.")
    if not isinstance(review["reviewer"], str) or not NAME.fullmatch(review["reviewer"]):
        raise CardError(f"Policy file {path}: review.reviewer must name a profile.")
    if not isinstance(review["panel"], list) or type(review["panel_required"]) is not bool:
        raise CardError(f"Policy file {path}: review.panel needs a list and panel_required a boolean.")
    if review["panel_required"] and len(review["panel"]) != 2:
        raise CardError(f"Policy file {path}: strict review requires two independent panel commands; remove the old single-reviewer panel override.")
    seen = {review["reviewer"]}
    for member in review["panel"]:
        if (not isinstance(member, dict) or set(member) - {"name", "argv", "output", "model", "focus", "tier", "fallbacks"}
                or not isinstance(member.get("name"), str) or not NAME.fullmatch(member["name"])
                or ("focus" in member and not isinstance(member["focus"], str))
                or ("model" in member and (not isinstance(member["model"], str) or not member["model"].strip()))
                or not isinstance(member.get("argv"), list) or not member["argv"]
                or not all(isinstance(a, str) and a for a in member["argv"])
                or member.get("output", "json") not in ("json", "claude-json", "hermes-usage")):
            raise CardError(f"Policy file {path}: a panel reviewer needs name, argv and optional output (json or claude-json).")
        if review["panel_required"] and (not member.get("model") or member.get("output") not in ("claude-json", "hermes-usage")):
            raise CardError(f"Policy file {path}: strict panel reviewers need a pinned model and a runtime usage receipt.")
        review_routes.validate(member)
        if member["name"] in seen:
            raise CardError(f"Policy file {path}: reviewer identities must be distinct.")
        seen.add(member["name"])
    if review["panel_required"] and len({review_routes.identity(m["model"]) for m in review["panel"]}) != 2:
        raise CardError("Strict review requires two distinct primary model identities.")
    result["source"] = str(path)
    return result
