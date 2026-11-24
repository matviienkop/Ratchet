#!/usr/bin/env python3
"""Add approved subscription peers to an existing Hermes review panel."""
import argparse
import json
import os
from pathlib import Path
import tempfile


def configure(panel):
    routes = {r["model"]: r for r in panel}
    if len(panel) != 2 or set(routes) != {"gpt-6-astra", "claude-opus-5-5"}:
        raise ValueError("Expected the pinned Astra and Opus primary models in this panel")
    for primary, source, peer in (("gpt-6-astra", "claude-opus-5-5", "claude-fable-5-2"),
                                  ("claude-opus-5-5", "gpt-6-astra", "gpt-6.2-astra")):
        runner = routes[source]
        if runner["argv"].count(source) != 1:
            raise ValueError(f"Expected {source} as an explicit reviewer command argument")
        routes[primary]["fallbacks"] = [{"model": peer, "tier": "frontier", "output": runner["output"],
                                         "argv": [peer if a == source else a for a in runner["argv"]]}]
    runner = routes["gpt-6-astra"]
    for route in routes.values():
        route["fallbacks"].append({"model": "gpt-6.2-sol", "tier": "frontier", "output": runner["output"],
                                   "argv": ["gpt-6.2-sol" if a == runner["model"] else a for a in runner["argv"]]})


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("policy", type=Path)
    cli.add_argument("--repo", help="Exact repo key in the policy; defaults to the defaults section")
    args = cli.parse_args()
    path = args.policy.resolve(strict=True)
    raw = json.loads(path.read_bytes())
    section = raw["repos"][args.repo] if args.repo else raw["defaults"]
    configure(section["review"]["panel"])
    original = path.stat()
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as file:
        try:
            json.dump(raw, file, indent=2, ensure_ascii=False)
            file.write("\n")
            file.flush()
            os.fchown(file.fileno(), original.st_uid, original.st_gid)
            os.fchmod(file.fileno(), original.st_mode & 0o777)
            os.fsync(file.fileno())
            os.replace(file.name, path)
        finally:
            Path(file.name).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
