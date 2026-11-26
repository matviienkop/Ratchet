"""Carry acceptance obligations into a fresh card without carrying acceptance verdicts."""

import copy
import hashlib
from pathlib import Path

from . import review, verify
from .store import CardError, record


def inherit(card, source_card):
    if card.dir.resolve() == source_card.dir.resolve():
        raise CardError("Choose a different source with ai-dev-card contract inherit --from-card ID.")
    source = source_card.load()
    if not source.get("contract"):
        raise CardError("The source has no contract. Run ai-dev-card contract show on the source card.")
    contract = copy.deepcopy(verify.validate_contract(source["contract"]))
    provenance = {"board": source_card.board, "card": source_card.id, "state_sha256": verify.digest(source)}
    stored_files = {}
    ledger = copy.deepcopy(source.get("ledger", {}))
    source_root = source_card.dir / "evidence"
    for key, entry in ledger.items():
        old_status = entry["status"]
        proof = entry.get("confirm") or {}
        executable = bool(proof.get("argv"))
        if executable:
            argv = proof["argv"]
            if not isinstance(argv, list) or not all(isinstance(arg, str) and arg for arg in argv):
                raise CardError("The source reproduction command is invalid. Run ai-dev-card finding list on the source.")
            copied = {}
            hashes = proof.get("files_sha256", {})
            if not isinstance(proof.get("files", {}), dict) or not isinstance(hashes, dict):
                raise CardError("The source reproduction files are invalid. Inspect ai-dev-card finding list on the source.")
            for destination, filename in proof.get("files", {}).items():
                relative = Path(destination)
                path = Path(filename)
                if (relative.is_absolute() or ".." in relative.parts or not relative.parts
                        or relative.parts[0] == ".git" or not path.is_file()
                        or not path.resolve().is_relative_to(source_root.resolve())):
                    raise CardError("A source reproduction file is outside its evidence directory or has an unsafe destination. Inspect ai-dev-card finding list on the source.")
                data = path.read_bytes()
                digest = hashlib.sha256(data).hexdigest()
                if destination in hashes and hashes[destination] != digest:
                    raise CardError("A source reproduction file changed after confirmation. Reconfirm it with ai-dev-card confirm before inheriting.")
                token = hashlib.sha256(key.encode()).hexdigest()[:16]
                target = card.dir / "evidence" / "inherited" / token / relative
                stored_files[target] = data
                copied[destination] = str(target)
            entry["confirm"] = {"argv": argv, "files": copied,
                                "files_sha256": {name: hashlib.sha256(stored_files[Path(path)]).hexdigest()
                                                 for name, path in copied.items()},
                                "inherited_from": {**provenance, "key": key, "tree": proof.get("tree")}}
            entry["status"] = "confirmed"
        else:
            entry.pop("confirm", None)
            if entry.get("kind") == "gap":
                entry["status"] = "gap"
            elif old_status not in ("note", "discovery"):
                entry["status"] = "candidate" if review._valid_violation(entry.get("violates"), contract) else "note"
        entry.pop("disposition", None)
        entry.pop("assessment", None)
        entry.pop("attempts", None)
        entry.pop("sources", None)
        entry.pop("require_reproduction", None)
        entry["ratings"] = {}
        entry["rating_round"] = 0
        entry["first_round"] = 1
        entry["raised_round"] = 1
        entry["reporters"] = []
        entry["needs_assessment"] = False
        entry["inherited_from"] = {**provenance, "key": key, "status": old_status}
        entry["history"] = [{"round": 0, "status": entry["status"], "by": "inherit", "source": provenance,
                             "reason": "The new implementation must prove this obligation again."}]
    for scenario in contract.get("scenarios", []):
        if "manual" in scenario:
            scenario["manual"] = str(card.dir / "evidence" / "manual" / (scenario["id"] + ".json"))
    with card.locked() as state:
        if (state.get("contract") or state.get("rounds") or state.get("ledger")
                or state.get("base") or state.get("verifications") or state.get("inherited_from")):
            raise CardError("Inheritance requires a fresh card. Create a new card and run ai-dev-card contract inherit --from-card ID.")
        for path, data in stored_files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        state["contract"] = contract
        state["ledger"] = ledger
        state["inherited_from"] = provenance
        record(state, "contract.inherited", source=provenance, findings=sorted(ledger))
    return {"source": provenance, "findings": sorted(ledger), "contract": contract,
            "next": "Implement and run ai-dev-card verify. The first review brief reruns inherited reproductions; collect new manual evidence for this tree."}
