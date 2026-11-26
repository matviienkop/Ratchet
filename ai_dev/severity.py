"""Rank consequences separately from review class, confidence and the proof of a defect."""

LEVELS = ("low", "medium", "high", "critical")
CONFIDENCE = ("low", "medium", "high")
RUBRIC = """Every finding, including notes and pre-existing discoveries, needs severity, confidence and impact.
Severity measures the consequence on a supported path:
- critical: irreversible data loss, unauthorized access to sensitive data, or service-wide outage.
- high: a core operation fails for many users or breaks upgrades, with no practical workaround.
- medium: a supported case produces incorrect results or needs a meaningful manual workaround.
- low: minor inconvenience or maintainability issue, such as misleading wording with no change in behavior.
Test-only findings use the consequence of the criterion left unproven, not the file type.
Confidence (low, medium, high) measures how well the evidence supports the claim. Impact describes who or what
is affected and what happens. A high-confidence typo is still low severity. A suspected critical outcome needs proof.
"""


def describe(entry):
    return {"key": entry["key"], "title": entry["title"], "status": entry["status"],
            "severity": entry.get("severity", "unknown"), "location": entry.get("location"), "ratings": entry.get("ratings", {}),
            "assessment": entry.get("assessment"), "needs_assessment": entry.get("needs_assessment", False)}


def apply_grade(entry):
    """Low stays advisory. Raising an eligible defect still requires normal reproduction."""
    eligible = any(r["eligible"] for r in entry["ratings"].values())
    if entry["severity"] == "low" and entry["status"] in ("gap", "candidate"):
        entry["status"] = "note"
    elif entry["severity"] != "low" and eligible and entry["status"] == "note":
        entry["status"] = "gap" if entry["kind"] == "gap" and not entry.get("require_reproduction") else "candidate"


def rate(entry, number, reviewer, finding, eligible):
    if entry.get("rating_round") != number:
        entry["ratings"] = {}
        entry["rating_round"] = number
    rating = {"severity": finding["severity"], "confidence": finding["confidence"], "impact": finding["impact"],
              "class": finding["class"], "eligible": eligible}
    if entry["ratings"].get(reviewer) == rating:
        apply_grade(entry)
        return
    entry["ratings"][reviewer] = rating
    scores = {r["severity"] for r in entry["ratings"].values()}
    entry["severity"] = min(scores, key=LEVELS.index)
    entry["assessment"] = None
    apply_grade(entry)
    # A contract evidence gap is a coordinator judgment. The reviewer cannot make it blocking by labeling it.
    entry["needs_assessment"] = len(scores) > 1 or entry["status"] == "gap"
    entry["history"].append({"round": number, "by": reviewer, "event": "severity.proposed", **rating})
