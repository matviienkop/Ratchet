# Project preflight

Run `ai-dev-card preflight` after setting the task contract and before editing source. The command pins the base and executes project readiness and baseline checks in a detached checkout of that base. A check with `stand` uses the existing registered stand with an empty candidate patch, holding its normal lease and restoring its initial stopped state. It never probes the implementation as if it were the baseline.

Preflight is project-specific. Configure `preflight` in the repository's policy entry:

```json
{
  "preflight": {
    "checks": [
      {"name": "runtime", "argv": ["python3", "--version"]},
      {"name": "baseline-unit", "argv": ["python3", "-m", "unittest", "discover", "-s", "tests"],
       "timeout": 600, "tests_pattern": "Ran (?P<count>\\d+) tests?"}
    ]
  }
}
```

Checks accept the existing `name`, `argv`, `timeout`, `stand` fields plus `tests_pattern`; they have no review stage. Use separate readiness checks for dependency installation, services, compilers or other prerequisites. Each local check runs in the same disposable base checkout. Ignored dependency/build artifacts may remain there until cleanup; modifying tracked source or creating nonignored files fails the cleanliness check.

Test discovery uses operator-configured output patterns, not a claim from the model. With a named `count` group, matched nonnegative totals are added. Without it, each match represents an individual test record. Examples are `^# tests (?P<count>\d+)$` for Node TAP totals and `^.*"Action":"run".*"Test":` for Go test JSON test records. Verify patterns against the project's actual output; the mechanism cannot prove a badly configured regex measures real tests. ANSI color escapes are removed before matching. Exit zero without positive test evidence is not a passing test probe. Multiple suites should have separate checks so a working suite cannot hide an empty one.

A project with no applicable automated tests must give `no_tests_reason` and still configure at least one meaningful readiness check. Missing `preflight`, represented by null, preserves legacy behavior and is exposed as `preflight_configured=false` in status. Configure it during onboarding before assigning implementation; once configured, both verification and board acceptance require its evidence. There is no worker CLI flag to disable the policy.

## Failure handling

- Missing executables, timeouts, missing test evidence, failed readiness commands and cleanup failures block readiness. Repair the environment and run `ai-dev-card preflight --rerun`.
- A failed test command with positive discovery evidence is a baseline failure requiring investigation. Read the log to distinguish a real old defect from environment/configuration problems. For an understood unrelated defect, run `ai-dev-card preflight acknowledge --check NAME --reason TEXT --evidence REFERENCE`. Explain the cause, relevance and evidence. Apply existing severity/size triage to decide inline work or a follow-up; do not create tickets automatically for every failure.
- Assessment is an explicit worker attestation. It does not mechanically prove root cause or make that test pass. Candidate verification and review requirements remain unchanged, including handling failures that also occur on the base. Environment failures cannot use this acknowledgement route.

Preflight does not spend an implementation attempt, increment defect counters, relax a contract or mark acceptance checks passed. It can run before any candidate exists. If implementation already exists, it still examines the base and records `before_candidate_changes=false`. Skills require running before edits; the tool gate enforces evidence at verification and board transitions, not on arbitrary filesystem writes.

The receipt records base, observed candidate tree, project configuration signature, start/end times, duration, each test count/result, log hashes and assessments. Repeated calls reuse a matching receipt; `--rerun` produces a fresh one and clears old acknowledgements. Base, project identity, commands, patterns or timeout changes invalidate it. Rerun after changing the environment, since external machine state is not represented by a Git tree. `ai-dev-card preflight show` displays the current receipt. Analytics imports these receipts into its ten-day projection; authoritative evidence follows the normal card lifecycle.

VM/product scenarios remain project contracts. A baseline preflight does not prove that the changed product works after installation or on physical hardware.
