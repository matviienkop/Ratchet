"""Native Hermes card inspection backed by the same CLI used by operators."""

import json
from functools import partial
import os
from pathlib import Path
import subprocess
import sys

from .redact import sanitize

DESCRIPTION = "Inspect a Ratchet card, its history, or the installation. Uses the current worker card by default."
SCHEMA = {
    "name": "ratchet_inspect", "description": DESCRIPTION,
    "parameters": {
        "type": "object",
        "properties": {
            "operation": {"type": "string", "enum": ["status", "history", "doctor"]},
            "card": {"type": "string", "description": "Card ID; defaults to the worker card."},
            "board": {"type": "string", "description": "Board slug; defaults to the worker board."},
            "workspace": {"type": "string", "description": "Git working tree; use when inspecting another card from chat."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        "required": ["operation"], "additionalProperties": False,
    },
}


def inspect_card(args, *, plugin_dir=None, lane=None, **_):
    operation = args.get("operation")
    if operation not in ("status", "history", "doctor"):
        return json.dumps({"error": "Choose status, history or doctor."})
    argv = [sys.executable, str(Path(__file__).resolve().parents[1] / "bin" / "ai-dev-card")]
    for key in ("board", "card", "workspace"):
        value = args.get(key)
        if value is not None:
            if not isinstance(value, str) or not value or value.startswith("-"):
                return json.dumps({"error": f"Invalid {key}. Run ai-dev-card doctor."})
            argv.extend(["--" + key, value])
    argv.append(operation)
    if "limit" in args:
        limit = args["limit"]
        if operation != "history" or type(limit) is not int or not 1 <= limit <= 100:
            return json.dumps({"error": "Use limit 1 through 100 with history only."})
        argv.extend(["--limit", str(limit)])
    env = os.environ.copy()
    if plugin_dir is not None:
        env["AI_DEV_PLUGIN_DIR"] = str(plugin_dir)
        if lane:
            env["AI_DEV_ROLE"] = lane
        else:
            env.pop("AI_DEV_ROLE", None)
    try:
        # A subprocess isolates argparse and stdout from concurrent Hermes tool calls.
        result = subprocess.run(argv, capture_output=True, text=True, timeout=60,
                                env=env)
    except (OSError, subprocess.TimeoutExpired) as error:
        return json.dumps(sanitize({"error": str(error), "next": "Run ai-dev-card doctor."}))
    try:
        output = json.loads(result.stdout)
    except ValueError:
        output = result.stdout
    response = {"exit_code": result.returncode, "result": output}
    if result.returncode:
        response["error"] = result.stderr.strip() or "Card inspection failed. Run ai-dev-card doctor."
    elif result.stderr.strip():
        response["warnings"] = result.stderr.strip()
    return json.dumps(sanitize(response), ensure_ascii=False)


def register(ctx, *, plugin_dir=None, lane=None):
    ctx.register_tool(name="ratchet_inspect", toolset="ratchet", schema=SCHEMA,
                      handler=partial(inspect_card, plugin_dir=plugin_dir, lane=lane), description=DESCRIPTION)
