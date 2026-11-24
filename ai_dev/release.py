"""Verify delivery of a reviewed tree through a merged GitHub PR and a published package channel."""

import json
import re
import subprocess
from urllib.parse import quote, urlsplit
from urllib.request import urlopen

from .store import CardError, now, record

REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]*")
CHECK = "ai-dev-card release verify --pr https://github.com/OWNER/REPO/pull/NUMBER"


def validate(rule):
    keys = {"channel", "repository", "branch", "catalog_url", "publisher", "package", "architectures"}
    if not isinstance(rule, dict) or set(rule) - {"ci_workflow"} != keys:
        raise CardError("release needs channel, repository, branch, catalog_url, publisher, package and architectures.")
    if not all(isinstance(rule[k], str) and rule[k] for k in keys - {"architectures"}):
        raise CardError("Invalid release configuration: string fields must be nonempty.")
    site = urlsplit(rule["catalog_url"])
    if site.scheme != "https" or not site.hostname or site.username or site.password or site.fragment or site.query:
        raise CardError("release.catalog_url must be a public HTTPS URL without credentials, query or fragment.")
    if any(not REPO.fullmatch(rule[k]) for k in ("repository", "publisher")):
        raise CardError("release.repository and release.publisher must be GitHub OWNER/REPO names.")
    if not all(NAME.fullmatch(rule[k]) for k in ("package", "channel")):
        raise CardError("Invalid package or channel name in release configuration.")
    if "ci_workflow" in rule and (not isinstance(rule["ci_workflow"], str)
                                  or not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*\.ya?ml", rule["ci_workflow"])):
        raise CardError("release.ci_workflow must name one GitHub Actions workflow file.")
    arches = rule["architectures"]
    if not isinstance(arches, list) or not arches or not all(isinstance(a, str) and NAME.fullmatch(a) for a in arches):
        raise CardError("release.architectures needs at least one named package architecture.")
    return rule


def selected(contract, settings):
    project = settings.get("release")
    card = (contract or {}).get("release")
    if project and card and project != card:
        raise CardError("The card's release contract differs from the repository policy. Reconcile the delivery target.")
    return project or card


def api(path):
    response = subprocess.run(["gh", "api", "--hostname", "github.com", path], capture_output=True, timeout=45)
    if response.returncode:
        raise CardError("GitHub could not verify publication. Check the account's read access and retry " + CHECK, code=2)
    return json.loads(response.stdout)


def catalog(url):
    with urlopen(url, timeout=30) as response:
        if urlsplit(response.geturl()).scheme != "https":
            raise ValueError("Catalog redirected away from HTTPS")
        body = response.read(4_000_001)
    if len(body) > 4_000_000:
        raise ValueError("Channel catalog exceeds four megabytes")
    return json.loads(body)


def published_head(rule, pr_url, tree):
    repository = rule["repository"]
    match = re.fullmatch(r"https://github\.com/" + re.escape(repository) + r"/pull/([1-9][0-9]*)", pr_url, re.I)
    if not match:
        raise CardError("Supply the PR URL for the contracted repository to " + CHECK)
    pr = api(f"repos/{repository}/pulls/{match[1]}")
    if pr["base"]["ref"] != rule["branch"]:
        raise CardError("PR targets a different branch. Check the delivery contract and the PR.")
    head = pr["head"]["sha"]
    if not isinstance(head, str) or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", head):
        raise ValueError("PR omitted its head commit")
    if api(f"repos/{repository}/git/commits/{head}")["tree"]["sha"] != tree:
        raise CardError("PR head differs from the reviewed candidate. Verify and review the published tree.")
    return pr, head, int(match[1])


def action_run(rule, head, number, pr):
    repo, workflow = rule["repository"], rule["ci_workflow"]
    runs = api(f"repos/{repo}/actions/workflows/{workflow}/runs?event=pull_request&head_sha={head}&per_page=1")["workflow_runs"]
    run = runs[0] if runs else {}
    links = run.get("pull_requests") or []
    # GitHub may omit pull_requests for forks or after branch deletion. In that case match
    # the PR's head repository and ref; a same-named branch in another fork cannot qualify.
    linked = (any(link.get("number") == number for link in links) if links else
              bool(pr["head"].get("repo") and (run.get("head_repository") or {}).get("full_name", "").lower()
                   == pr["head"]["repo"]["full_name"].lower() and run.get("head_branch") == pr["head"]["ref"]))
    # The newest matching run must itself pass. An older green run cannot cover a queued or failed rerun.
    if (run.get("head_sha") != head or run.get("event") != "pull_request"
            or run.get("repository", {}).get("full_name", "").lower() != repo.lower()
            or run.get("path") != ".github/workflows/" + workflow
            or not linked
            or run.get("status") != "completed" or run.get("conclusion") != "success"):
        raise CardError("The latest exact-head pull_request run for the contracted workflow has not passed. "
                        "Inspect GitHub Actions and retry ai-dev-card release check-pr --pr URL.", code=2)
    return {"id": run["id"], "attempt": run["run_attempt"], "workflow": workflow, "head": head,
            "url": run["html_url"], "conclusion": "success"}


def evidence(rule, pr_url, tree, before_merge=False, premerge=None):
    pr, head, number = published_head(rule, pr_url, tree)
    repository = rule["repository"]
    if before_merge:
        if not rule.get("ci_workflow"):
            raise CardError("check-pr requires an explicit release.ci_workflow.")
        if pr["merged"] or pr.get("state") != "open":
            raise CardError("Pre-merge CI verification requires the reviewed PR to be open.")
        ci = action_run(rule, head, number, pr)
        fresh, latest, _ = published_head(rule, pr_url, tree)
        if fresh["merged"] or fresh.get("state") != "open" or latest != head:
            raise CardError("The PR changed while collecting CI evidence. Recheck the current PR.")
        return {"pr": pr_url, "tree": tree, "head": head, "ci": ci, "verified_at": now()}
    if pr["merged"] is not True:
        raise CardError("Await merge of the approved PR into the contracted branch, then retry " + CHECK, code=2)
    merged = pr["merge_commit_sha"]
    if not isinstance(merged, str) or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", merged):
        raise ValueError("PR omitted its merge commit")
    extra = {}
    if rule.get("ci_workflow"):
        if (not premerge or premerge.get("pr") != pr_url or premerge.get("head") != head
                or premerge.get("tree") != tree or premerge.get("rule") != rule):
            raise CardError("No matching pre-merge CI receipt. Hand the merged PR to the owner with this gap.")
        extra = {"premerge_ci": premerge["ci"], "ci": action_run(rule, head, number, pr)}
    feed = catalog(rule["catalog_url"])
    if feed["channel"] != rule["channel"]:
        raise CardError("Published catalog has the wrong channel. Check the configured catalog URL.")
    for entry in feed["releases"]:
        sources = entry["sources"].values()
        if (entry["channel"] == rule["channel"] and any(s["repository"].lower() == repository.lower()
                                                        and s["commit"] == merged for s in sources)):
            break
    else:
        raise CardError("The channel has no published package for the merged commit. Watch the publisher and retry " + CHECK, code=2)
    publisher, version, tag = rule["publisher"], entry["version"], entry["tag"]
    if not isinstance(version, str) or not version.strip() or not isinstance(tag, str) or not tag:
        raise ValueError("Publication needs a version and release tag")
    remote = api(f"repos/{publisher}/releases/tags/{quote(tag, safe='')}")
    if remote["draft"] or remote["tag_name"] != tag:
        raise ValueError("The release tag has no published release")
    assets = {asset["browser_download_url"]: asset for asset in remote["assets"] if asset["state"] == "uploaded"}
    packages = []
    for arch in rule["architectures"]:
        rows = [r for r in entry["packages"].get(arch, []) if r["name"] == rule["package"] and r["version"] == version
                and r["architecture"] in (arch, "all")]
        if len(rows) != 1:
            raise ValueError(f"Expected one published {arch} package at version {version}")
        row = rows[0]
        asset = assets.get(row["url"], {})
        if (not re.fullmatch(r"[0-9a-f]{64}", row["sha256"]) or asset.get("digest") != "sha256:" + row["sha256"]
                or type(row["size"]) is not int or row["size"] <= 0 or asset.get("size") != row["size"]):
            raise ValueError(f"The published {arch} asset does not match the catalog checksum and size")
        packages.append({"architecture": arch, "url": row["url"], "sha256": row["sha256"], "bytes": row["size"]})
    return {"pr": pr_url, "tree": tree, "head": head, "merged": merged, "branch": rule["branch"],
            "channel": rule["channel"], "version": version, "catalog_url": rule["catalog_url"], "package": rule["package"],
            "release_url": remote["html_url"], "packages": packages, "verified_at": now(), **extra}


def verify_release(card, workspace, pr_url, before_merge=False):
    from . import review, verify
    state = card.load()
    _, settings, tree = verify.prepare(state, workspace)
    rule = selected(state["contract"], settings)
    if not rule:
        raise CardError("No package delivery contract is configured for this card.")
    def ready(snapshot):
        rounds = snapshot["rounds"]
        return (rounds and rounds[-1]["tree"] == tree and rounds[-1]["outcome"] == "accept"
                and not review.independent_evidence(snapshot, settings["review"], rounds[-1])
                and verify.passing_receipt(snapshot, settings, tree, "pre_review")
                and (not verify.checks_for(settings, snapshot["contract"], "post_review")
                     or verify.passing_receipt(snapshot, settings, tree, "post_review")))
    if not ready(state):
        raise CardError("Complete independent review and the contracted checks before verifying publication.")
    try:
        receipt = evidence(rule, pr_url, tree, before_merge, state.get("premerge_receipt"))
    except (KeyError, TypeError, AttributeError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
        raise CardError(f"Publication could not be confirmed ({exc}). Inspect the publisher and retry {CHECK}.", code=2) from exc
    with card.locked() as state:
        _, current_settings, current_tree = verify.prepare(state, workspace)
        if (current_tree != tree or current_settings != settings or not ready(state)
                or selected(state["contract"], current_settings) != rule):
            raise CardError("The card changed while publication was checked. Retry " + CHECK)
        state["premerge_receipt" if before_merge else "release_receipt"] = {**receipt, "rule": rule}
        record(state, "release.ci-passed" if before_merge else "release.verified", **receipt)
    return receipt
