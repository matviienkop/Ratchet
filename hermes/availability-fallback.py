#!/usr/bin/env python3
"""Restrict a dedicated Hermes 0.21.5 install's fallback chain to two cases: the provider is unavailable,
or its safety filter refused the request."""
import argparse
import ast
import hashlib
from pathlib import Path
import os
import tempfile

ANCHOR = "    from agent.fallback_cooldown import _arm_rate_limit_cooldown, switch_deferred_by_reset\n"
AVAILABILITY_ONLY = """    # ai-dev: switch subscriptions only on classified provider availability failures.
    if getattr(reason, "value", None) not in (
        "billing", "rate_limit", "upstream_rate_limit", "overloaded", "server_error",
        "timeout", "model_not_found", "model_entitlement", "auth", "auth_permanent",
    ):
        return False
"""
# A refusal is the provider's decision about this request, so the same request goes to the peer unchanged.
GUARD = """    # ai-dev: switch subscriptions when the provider is unavailable or its safety filter refuses the request.
    if getattr(reason, "value", None) not in (
        "billing", "rate_limit", "upstream_rate_limit", "overloaded", "server_error",
        "timeout", "model_not_found", "model_entitlement", "auth", "auth_permanent",
        "content_policy_blocked",
    ):
        return False
"""
# Guards written by earlier versions of this script; an upgrade replaces them.
PREVIOUS = (AVAILABILITY_ONLY,)
EXPECTED = "c83ecb529990e837f0c29afdf1abc00a5eebd9c8b532c45409734010c68561d2"


def install(root):
    path = root / "agent" / "chat_completion_helpers.py"
    original = path.read_bytes()
    data = original.decode()
    clean = data
    for guard in (GUARD, *PREVIOUS):
        clean = clean.replace(guard + ANCHOR, ANCHOR, 1)
    if hashlib.sha256(clean.encode()).hexdigest() != EXPECTED or clean.count(ANCHOR) != 1:
        raise ValueError("Unknown Hermes fallback implementation. Keep fallback chains disabled until verified.")
    patched = clean.replace(ANCHOR, GUARD + ANCHOR)
    ast.parse(patched)
    if data == patched:
        return
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
        try:
            tmp.write(patched.encode())
            tmp.flush()
            os.fchmod(tmp.fileno(), path.stat().st_mode & 0o777)
            os.replace(tmp.name, path)
        finally:
            Path(tmp.name).unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    install(parser.parse_args().root)
