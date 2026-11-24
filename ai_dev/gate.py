"""Policy for the board tools: a card moves only when the evidence for its current tree exists."""

import os
from pathlib import Path

from . import gitio, policy as policies, release, review, verify
from .store import Card, CardError, identity

ROLES = ("planner", "coder", "reviewer")
BOARD_TOOLS = ("kanban_request_review", "kanban_complete", "kanban_request_changes")


def role(env=None, plugin_dir=None):
    """The lane this profile works in. The `role` file the installer wrote next to the plugin decides;
    AI_DEV_ROLE is only a fallback, because a worker inherits its parent's environment."""
    env = os.environ if env is None else env
    value = None
    if plugin_dir:
        try:
            value = (Path(plugin_dir) / "role").read_text().strip()
        except OSError:
            value = None
    value = value or env.get("AI_DEV_ROLE")
    return value if value in ROLES else None


def decide(tool_name, args, env=None, lane=None):
    """None lets the call through. A string blocks it and tells the worker what to do instead."""
    env = os.environ if env is None else env
    task = env.get("HERMES_KANBAN_TASK")
    if tool_name not in BOARD_TOOLS or not task or lane not in ("coder", "reviewer"):
        return None
    target = (args or {}).get("task_id")
    if target and target != task:
        return None
    try:
        return _coder(tool_name, env) if lane == "coder" else _reviewer(tool_name, env, args or {})
    except CardError as error:
        return f"ai-dev gate: {error}"
    except Exception as error:  # A policy that cannot be evaluated must not wave the call through.
        return f"ai-dev gate could not evaluate this call ({type(error).__name__}: {error}). Run `ai-dev-card status`."


def _card(env):
    board, card = identity(env=env)
    workspace = env.get("HERMES_KANBAN_WORKSPACE")
    if not workspace:
        raise CardError("This card has no workspace. Coding cards need a worktree workspace.")
    workspace = gitio.repo_root(workspace)
    settings = policies.load(repo=gitio.main_repo(workspace), origin=gitio.origin(workspace))
    return Card(board, card).load(), settings, gitio.snapshot(workspace)


def _coder(tool_name, env):
    if tool_name == "kanban_complete":
        return ("ai-dev gate: a coding card finishes through review. Run `ai-dev-card verify`, then call "
                "kanban_request_review.")
    if tool_name == "kanban_request_changes":
        return "ai-dev gate: only the reviewer returns a card for changes."
    state, settings, tree = _card(env)
    if not state["contract"]:
        return ("ai-dev gate: this card has no contract. Run `ai-dev-card contract set --file FILE`, then "
                "`ai-dev-card verify`.")
    if verify.passing_receipt(state, settings, tree, "pre_review"):
        return None
    last = verify.last_for(state, tree, "pre_review")
    if last and not last["passed"]:
        return (f"ai-dev gate: the last verification of this tree ended as {last['result']}. Fix it and run "
                "`ai-dev-card verify` again; for an environment failure see `ai-dev-card status`.")
    return ("ai-dev gate: no passing verification exists for the working tree as it is now. Run "
            "`ai-dev-card verify` and request review only after it passes.")


def _reviewer(tool_name, env, args):
    if tool_name == "kanban_request_review":
        return "ai-dev gate: a reviewer ends with kanban_complete, kanban_request_changes or kanban_block."
    state, settings, tree = _card(env)
    rnd = state["rounds"][-1] if state["rounds"] else None
    if not rnd or rnd["tree"] != tree or rnd["outcome"] is None:
        return ("ai-dev gate: this tree has no settled review round. Run `ai-dev-card review brief`, run "
                "`ai-dev-card review panel`, confirm blocker candidates and run `ai-dev-card review settle`.")
    issue = review.independent_evidence(state, settings["review"], rnd)
    if issue:
        return "ai-dev gate: " + issue
    outcome = rnd["outcome"]
    post_checks = verify.checks_for(settings, state["contract"], "post_review")
    post_passed = not post_checks or verify.passing_receipt(state, settings, tree, "post_review")
    post_last = verify.last_for(state, tree, "post_review") if post_checks else None
    post_defect = bool(post_last and post_last["result"] == "defect" and not post_passed)
    if tool_name == "kanban_request_changes":
        if outcome == "changes" or (outcome == "accept" and post_defect):
            return None
        if outcome == "accept":
            return "ai-dev gate: the round settled as accept, no blocker is confirmed. Call kanban_complete."
        return "ai-dev gate: the review round cap is reached. Run ai-dev-card recover review."
    if outcome != "accept":
        return (f"ai-dev gate: the round settled as {outcome}, not accept. "
                + ("Call kanban_request_changes with the fix brief." if outcome == "changes"
                   else "Run ai-dev-card recover review for the next action."))
    if post_passed:
        rule = release.selected(state["contract"], settings)
        if rule:
            receipt = state.get("release_receipt") or {}
            if receipt.get("tree") != tree or receipt.get("rule") != rule:
                return ("ai-dev gate: this card ends at verified package publication. Follow its approved merge "
                        "and publish procedure, then run " + release.CHECK)
            if (args.get("metadata") or {}).get("published_pr") != receipt["pr"]:
                return "ai-dev gate: call kanban_complete with metadata.published_pr=" + receipt["pr"]
        return None
    if post_defect:
        return ("ai-dev gate: a post-review check failed on this change. Call kanban_request_changes(reason=...) "
                "with the failing check and its log.")
    if post_last:
        return ("ai-dev gate: post-review checks could not run cleanly (environment). Repair it and run "
                "`ai-dev-card verify --stage post_review` again. Follow the recovery section of the ai-dev-card skill.")
    return "ai-dev gate: post-review checks have not passed on this tree. Run `ai-dev-card verify --stage post_review`."
