"""Hermes plugin: applies the ai-dev gate to the Kanban board tools of a coder or reviewer profile.

The gate uses `pre_tool_call`; the activity journal observes `post_tool_call`. The gate uses its
`{"action": "block", "message": ...}` return, and the worker environment variables
HERMES_KANBAN_TASK, HERMES_KANBAN_BOARD and HERMES_KANBAN_WORKSPACE.
"""

import os
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
BOARD_TOOLS = ("kanban_request_review", "kanban_complete", "kanban_request_changes")


def _gate():
    """The installer writes `prefix` next to this file. In a source checkout the package is two levels up."""
    prefix = HERE / "prefix"
    root = Path(prefix.read_text().strip()) if prefix.is_file() else HERE.parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from ai_dev import gate
    return gate


def check(tool_name=None, args=None, **_):
    if not os.environ.get("HERMES_KANBAN_TASK"):
        return None
    if tool_name == "terminal":
        if "kanban" not in str((args or {}).get("command") or ""):
            return None
    elif tool_name not in BOARD_TOOLS:
        return None
    try:
        gate = _gate()
        message = gate.decide(tool_name, args or {}, lane=gate.role(plugin_dir=HERE))
    except Exception as error:  # A broken install must not let a card through unchecked.
        message = f"ai-dev gate is not usable ({type(error).__name__}: {error}). Run `ai-dev-card doctor`."
    if message:
        analytics_event("gate", tool=tool_name, message=message)
    return {"action": "block", "message": message} if message else None


def audit(tool_name=None, args=None, **metadata):
    if not tool_name or not os.getenv("HERMES_KANBAN_TASK"):
        return
    _gate()
    from ai_dev.history import observe
    from ai_dev.store import Card, identity
    observe(Card(*identity()), tool_name, args, **metadata)


def observe_model(model=None, session_id=None, parent_session_id=None, **_):
    if not model or parent_session_id or not os.environ.get("HERMES_KANBAN_TASK"):
        return
    gate = _gate()
    lane = gate.role(plugin_dir=HERE)
    if lane not in ("coder", "reviewer"):
        return
    from ai_dev.store import Card, identity, record
    card = Card(*identity())
    route = {"model": model, "session": session_id, "run": os.getenv("HERMES_KANBAN_RUN_ID")}
    if card.load().get("routes", {}).get(lane) == route:
        return
    with card.locked() as state:
        if state.setdefault("routes", {}).get(lane) != route:
            state["routes"][lane] = route
            record(state, "worker.model", lane=lane, **route)


def register(ctx):
    # Commands the worker starts from its terminal inherit these and report the same lane.
    try:
        lane = _gate().role(plugin_dir=HERE)
    except Exception:
        lane = None
    # Assigned, not defaulted: a worker inherits the environment of the process that spawned it,
    # and that process may have loaded another profile's copy of this plugin.
    if lane:
        os.environ["AI_DEV_ROLE"] = lane
    else:
        os.environ.pop("AI_DEV_ROLE", None)
    os.environ["AI_DEV_PLUGIN_DIR"] = str(HERE)
    from ai_dev.hermes_tools import register as register_tools
    register_tools(ctx)
    ctx.register_hook("pre_api_request", provider_attempt)
    ctx.register_hook("api_request_error", provider_error)
    ctx.register_hook("post_api_request", api_completed)
    ctx.register_hook("pre_llm_call", observe_model)
    ctx.register_hook("pre_tool_call", check)
    ctx.register_hook("post_tool_call", audit)


def provider_attempt(model=None, provider=None, session_id=None, **_):
    if not os.getenv("HERMES_KANBAN_TASK") or not provider or not model:
        return
    gate = _gate()
    from ai_dev.store import Card, identity
    from ai_dev.model_events import attempt
    attempt(Card(*identity()), gate.role(plugin_dir=HERE), model, provider, session_id)


def provider_error(model=None, provider=None, reason=None, status_code=None, session_id=None, **metadata):
    analytics_event("api", status="error", model=model, provider=provider, reason=reason,
                    status_code=status_code, session_id=session_id, **metadata)
    if not os.getenv("HERMES_KANBAN_TASK"):
        return
    gate = _gate()
    from ai_dev.store import Card, identity
    from ai_dev.model_events import failure
    failure(Card(*identity()), gate.role(plugin_dir=HERE), model, provider, session_id, reason, status_code)


def analytics_event(kind, **metadata):
    if not os.getenv("HERMES_KANBAN_TASK"):
        return
    try:
        _gate()
        from ai_dev import analytics
        from ai_dev.store import Card, identity
        card = Card(*identity())
        if kind == "gate":
            analytics.observe_gate(card, **metadata)
        else:
            analytics.observe_api(card, **metadata)
    except Exception as error:
        import logging
        logging.getLogger(__name__).warning("Analytics observation failed: %s", type(error).__name__)


def api_completed(**metadata):
    analytics_event("api", status="ok", **metadata)
