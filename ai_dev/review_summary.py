"""Immutable round summaries and bounded, ordered messages for the task conversation."""

import hashlib
import json

from . import severity
from .store import record
from . import review_routes


def findings(state, rnd):
    keys = {key for report in rnd["reports"].values() for key in report["findings"]}
    return [severity.describe(state["ledger"][key]) for key in sorted(keys)]


def queue(state, rnd, outcome):
    entries = findings(state, rnd)
    missing = sorted(name for name in rnd.get("panel_names", [])
                     if rnd["panel"].get(name, {}).get("status") != "ok")
    payload = {"round": rnd["n"], "tree": rnd["tree"], "outcome": outcome, "findings": entries,
               "provider_diversity": diversity(rnd), "models": {k: v.get("runtime", {}).get("model") for k, v in rnd["panel"].items()},
               "missing": missing, "blocking": sorted(rnd.get("blocking", []))}
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if fingerprint in rnd.get("summaries", []):
        return
    rnd.setdefault("summaries", []).append(fingerprint)
    record(state, "review.round-summary", summary_id=fingerprint, **payload)
    decisions = {"accept": "принято", "changes": "нужны исправления", "owner": "решение владельца",
                 "incomplete": "ревью не завершено"}
    lines = [f"Ревью · раунд {rnd['n']} · {decisions.get(outcome, outcome)}",
             f"Замечаний в отчётах этого раунда: {len(entries)}."]
    if payload["provider_diversity"] == "degraded":
        lines.append("Оба независимых ревьювера от одного провайдера: " + ", ".join(str(m) for m in payload["models"].values()) + ".")
    if payload["provider_diversity"] == "single-reviewer" and outcome != "incomplete":
        lines.append("Ревью одним ревьюером: провайдер второго недоступен. " + "; ".join(
            f"{name}: {slot.get('reason', '')}" for name, slot in sorted(rnd["panel"].items())
            if slot.get("status") != "ok")[:400])
    elif missing:
        lines.append("Нет отчёта от " + ", ".join(missing) + ".")
    for index, entry in enumerate(entries, 1):
        lines.append(f"{index}. [{entry['severity']}] {entry['title']} · {entry['status']} ({entry['key']})")
        if entry.get("location"):
            lines.append(f"Место: {entry['location']}")
        for name, rating in sorted(entry["ratings"].items()):
            lines.append(f"{name}: {rating['class']}, важность {rating['severity']}, уверенность {rating['confidence']}. {rating['impact']}")
        if decision := entry.get("assessment"):
            lines.append(f"Координатор: {decision['impact']} · {decision['evidence']['path']}")
    if payload["blocking"]:
        lines.append("Требуют исправления: " + ", ".join(payload["blocking"]))
    # At most 1,800 Unicode codepoints (3,600 UTF-16 units) plus the card header.
    # Bound the header separately so even maximal card/board IDs fit Telegram's message limit.
    chunks, text = [], "\n".join(lines)
    while text:
        end = len(text) if len(text) <= 1800 else max(text.rfind("\n", 0, 1800), 0) or 1800
        chunks.append(text[:end])
        text = text[end:].lstrip("\n")
    for number, chunk in enumerate(chunks, 1):
        record(state, "review.summary-message", round=rnd["n"], part=number, parts=len(chunks), text=chunk,
               delivery={"status": "pending", "id": f"r{rnd['n']}-{fingerprint[:16]}-{number}"})


def diversity(rnd):
    providers = [slot.get("runtime", {}).get("provider") for slot in rnd.get("panel", {}).values()
                 if slot.get("status") == "ok"]
    down = [slot for slot in rnd.get("panel", {}).values() if slot.get("status") == "unavailable"
            and slot.get("error_class") in review_routes.EXTERNAL]
    if len(providers) == 1 and providers[0] and down:
        return "single-reviewer"
    if len(providers) != 2 or not all(providers):
        return "incomplete"
    return "degraded" if len(set(providers)) == 1 else "cross-provider"


def switch(state, rnd, slot, previous, route):
    source, target = previous.get("model"), route["model"]
    fingerprint = f"route-{rnd['n']}-{slot}-{source}-{target}"
    if fingerprint in rnd.get("route_messages", []):
        return
    rnd.setdefault("route_messages", []).append(fingerprint)
    record(state, "panel.fallback", round=rnd["n"], reviewer=slot, source=source, target=target,
           reason=previous["reason"], error_class=previous["error_class"])
    record(state, "review.summary-message", round=rnd["n"], part=1, parts=1,
           text=f"Ревью, раунд {rnd['n']} · {slot}: {source} → {target}. Причина: {previous['reason']}",
           delivery={"status": "pending", "id": fingerprint})
