# Independent model review

## Evidence behind the workflow

- Song compared review strategies on 30 mixed artifacts with 150 planted errors. At a budget of two calls, combining a fresh same-model pass with a strong cross-model pass recovered 56.7% of planted errors, versus 42.7% for two fresh same-model passes. Two passes by the strong cross-model reviewer were statistically indistinguishable from the mixed pair on this comparison. The experiment leaves reviewer strength and diversity confounded. It supports testing combinations on representative work. [Preprint, version 2](https://arxiv.org/abs/2610.01471v2)
- Anthropic's production PR reviewer searches in parallel, verifies findings against code, then deduplicates and ranks them. It takes review instructions from the base or default branch. These patterns also apply to a local coordinator. Its implementation uses Claude models and offers no evidence about cross-provider gains. [Claude Code Review](https://code.claude.com/docs/en/code-review)
- Archon routes review aspects per change, fixes the reviewed commit before dispatch, and on subsequent rounds checks open findings and the correction diff. Its evaluation workflow runs on a sealed checkout. For ai-dev a reproducible test or binding contract can settle a disagreement even when only one reviewer identified the defect. [Pinned review workflow](https://github.com/coleam00/Archon/blob/f9d1d2b2b9a930ca87108ec787ec223da7cbf294/.archon/workflows/sdlc/review/archon-review.yaml) · [Evaluation workflow](https://github.com/coleam00/Archon/blob/f9d1d2b2b9a930ca87108ec787ec223da7cbf294/.archon/workflows/sdlc/review-eval/archon-review-eval.yaml)
- CRBugBench found that bugs requiring reasoning across code locations were harder, particularly in proprietary repositories. SWE-PRBench found that indiscriminately expanding the supplied context lowered performance in its tested configurations. Together these suggest starting with criteria and the patch, with targeted access to related callers, contracts and tests. [CRBugBench](https://arxiv.org/abs/2610.13668) · [SWE-PRBench](https://arxiv.org/abs/2603.26130)

## How to direct the reviewers

Google also explicitly calls for considering the surrounding file and system when the diff hides design or behavior. Our minimum for a code change is to read the entire affected function and trace its interactions, including paths whose lines did not change. Reviewers can fetch those units on demand and report the path they inspected. [Google on review context](https://google.github.io/eng-practices/review/reviewer/looking-for.html#context)

- Google's engineering guide asks reviewers to examine design in the surrounding system, user-visible behavior, complexity and the quality of test assertions. Its feedback guidance distinguishes required changes from optional improvements and asks the reviewer to explain the reason. Our common brief uses a counterexample and a consequence for a real caller to turn those principles into reviewable claims. [What to look for](https://google.github.io/eng-practices/review/reviewer/looking-for.html) · [Writing review comments](https://google.github.io/eng-practices/review/reviewer/comments.html) · [Review standard](https://google.github.io/eng-practices/review/reviewer/standard.html)
- GitHub's guidance for AI reviewers favors specific instructions, short files, examples and rules scoped to the paths where they apply. For ai-dev we also select by behavior across paths. A change to a worker's acknowledgement timing can need a data-integrity review even when it touches no database file. [GitHub Copilot review instructions](https://docs.github.com/en/copilot/tutorials/customize-code-review)
- The specialist questions draw on concrete engineering practices. Amazon describes the ambiguity when work succeeds and its response is lost, and W3C's introductory checks cover controls, labels and keyboard focus. We use these as prompts for relevant changes. The accessibility checks provide initial evidence and cover only part of a full accessibility evaluation. [AWS Builders' Library on retries](https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/) · [W3C WAI Easy Checks](https://www.w3.org/WAI/test-evaluate/easy-checks/)

## Local pilot on 2026-11-20

Two fresh reviewers, OpenAI `gpt-6.2-astra` and Anthropic `claude-opus-5-5`, independently received the same synthetic Python contract, base function and two alternative replacements. Each reviewed both candidates before seeing the other's verdict, test results or defect labels. The contract required summing posted signed units per team in arbitrary input order, retaining zero balances, and accepting a single-use iterable.

| Candidate | Astra | Opus | Local Python verification |
| --- | --- | --- | --- |
| K, accumulate posted units with a default integer per team | Clear | Clear | Passed all six cases |
| M, group consecutive rows and filter the resulting totals | Repeated teams lose earlier units. Zero balances disappear. | Same two root causes | Failed three cases corresponding to those two defects |

The six cases covered empty input, only unposted rows, interleaved teams, a posted zero, cancellation to zero, and a negative posting alongside an unposted row. All ran with single-use iterators, and the original function passed all six. For interleaved postings A +1, B +2, A +3, candidate M returned A +3 instead of +4. For A +3 followed by A −3, it omitted A.

Both reviewers found both defects and raised no false alarms on the control. The second reviewer added zero unique confirmed defects in this run. This exercises dispatch, independent reporting and verification on a small fixture. It does not measure expected gains on project PRs or exercise a Hermes integration.

## How to evaluate routing on project changes

Replay historical changes at their original base and candidate revisions. Include confirmed regressions and clean controls, grouped by review aspect and change complexity. Give a reviewer only the evidence available before merge. Keep later fixes and incident notes for adjudication. Validate new findings even when they are absent from the historical review.

Compare a single eligible reviewer, two independent calls to that reviewer and two distinct eligible reviewers. Hold the patch, brief, accessible context and per-call limits constant. Rotate which reviewer supplies the single-model baseline to separate model strength from diversity. Record known defects recovered, unique verified defects added by each reviewer, false alarms on controls, unresolved findings, elapsed time and token or quota usage when available. Select pairs by confirmed additional coverage at the project's budget. Keep a held-out set for checking routing changes.
