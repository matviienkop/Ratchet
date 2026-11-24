"""Pinned review peers, independent model identities and trusted runner errors."""
import json
import re

from .store import CardError

PROVIDERS = {"hermes-usage": "openai-codex", "claude-json": "anthropic"}
EXTERNAL = {"quota", "auth", "provider_unavailable"}
TIERS = ("standard", "advanced", "frontier")


class AttestationError(ValueError):
    """The runner could not prove which model reviewed the candidate."""


def identity(model):
    name = re.sub(r"-\d+[km]$", "", (model or "").strip().lower())
    name = name.removesuffix("[1m]").rsplit("/", 1)[-1]
    gpt = re.fullmatch(r"gpt-[0-9]+(?:\.[0-9]+)*-(astra|sol|terra|luna)(?:-.*)?", name)
    claude = re.fullmatch(r"claude-(opus|fable|sonnet|haiku)(?:-.*)?", name)
    return (gpt or claude).group(1) if gpt or claude else name


def authors(state, config):
    result = {identity(e["model"]) for e in state["log"]
              if e.get("event") in {"worker.model", "model.attempt"} and e.get("lane") == "coder" and e.get("model")}
    if config.get("author_model"):
        result.add(identity(config["author_model"]))
    return result


def routes(member):
    return [member, *[{**member, **peer} for peer in member.get("fallbacks", [])]]


def validate(member):
    tier = member.get("tier", "frontier")
    peers = member.get("fallbacks", [])
    if tier not in TIERS or not isinstance(peers, list):
        raise CardError("Review tier must be standard, advanced or frontier, and fallbacks must be a list.")
    seen = {identity(member.get("model"))}
    for index, peer in enumerate(peers):
        if (not isinstance(peer, dict) or set(peer) != {"model", "output", "tier", "argv"}
                or not isinstance(peer["model"], str) or not peer["model"].strip()
                or identity(peer["model"]) in seen or peer["output"] not in PROVIDERS
                or (index == 0 and peer["output"] == member.get("output")) or peer["tier"] not in TIERS
                or TIERS.index(peer["tier"]) < TIERS.index(tier)
                or not isinstance(peer["argv"], list) or not peer["argv"]
                or not all(isinstance(a, str) and a for a in peer["argv"])):
            raise CardError("A fallback must pin a distinct cross-provider peer of equal or higher tier and its command.")
        seen.add(identity(peer["model"]))


def reserve(state, config, rnd, member, reserved):
    entry = rnd["panel"].get(member["name"], {})
    if entry.get("error_class") not in EXTERNAL | {"identity_conflict"}:
        return None
    used = reserved | authors(state, config) | {identity(m.get("model")) for m in config["panel"]}
    previous_provider = entry.get("provider") if entry.get("error_class") in EXTERNAL else None
    for history in rnd.get("panel_attempts", {}).values():
        used.update(identity(a["model"]) for a in history)
    for lane in rnd["panel"].values():
        used.add(identity(lane.get("runtime", {}).get("model")))
    return next((r for r in routes(member)[1:] if identity(r["model"]) not in used
                 and PROVIDERS.get(r["output"]) != previous_provider), None)


def runner_failure(done, member, usage):
    """Only runner stderr or an explicitly unsuccessful runtime envelope can authorize fallback."""
    body = done.stderr.decode(errors="replace") if done.returncode else ""
    failed = bool(done.returncode)
    try:
        envelope = (json.loads(usage.read_bytes()) if member.get("output") == "hermes-usage"
                    else json.loads(done.stdout) if member.get("output") == "claude-json" else {})
        if isinstance(envelope, dict) and (envelope.get("is_error") is True or envelope.get("failed") is True):
            failed = True
            body += " " + str(envelope.get("error") or envelope.get("failure") or envelope.get("error_message") or "")
            # Claude may put the CLI's own API error in the result field. Ordinary model
            # prose, including an unsuccessful max-turns result, is never an outage receipt.
            driver_error = envelope.get("result")
            if (member.get("output") == "claude-json" and isinstance(driver_error, str)
                    and re.match(r"^(?:API Error:|API Error [45]|You've hit your limit|Not logged in)", driver_error, re.I)):
                body += " " + driver_error
    except (ValueError, OSError):
        pass
    if not failed:
        return None
    for kind, pattern in (
        ("quota", r"\b429\b|rate.limit|quota|usage.limit|credit balance|limit reached|hit your limit"),
        ("auth", r"\b(?:401|403)\b|unauthenticated|invalid.api.key|not logged in|authentication.error|token.expired|expired.token|invalid.bearer.token"),
        ("provider_unavailable", r"\b(?:500|502|503|504)\b|overloaded|model.not.found|service.unavailable|model.{0,60}(?:unavailable|does not exist)|connection refused"),
    ):
        if re.search(pattern, body, re.I):
            return kind, f"{kind} (exit {done.returncode}): {body[-350:].strip()}"
    return "process_failure", f"exit {done.returncode}: {body[-350:].strip()}"
