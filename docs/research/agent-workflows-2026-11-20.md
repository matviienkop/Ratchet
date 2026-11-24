# Research: subagent development workflows

Reviewed: 2026-11-20. This is an evidence note, not worker instructions or an
installed dependency list. Linked upstream default branches may change; pin and
test selected releases during the prototype. No comparative benchmark was run.

## Reading the presentation

The owner's slides describe three complementary ideas:

- A product lifecycle: Identify, Discover, Design & Experiment, Build & Operate,
  Measure & Learn. Software delivery is a loop inside this wider lifecycle.
- One problem with one living record, and a shared knowledge source illustrated
  by Confluence. This is compatible with selectable task and knowledge providers.
- Evidence-based selection among Spec Kit, OpenSpec, GSD, Superpowers, and BMAD v7.

The slides are references, not authorization to install products or access the
presenter's Confluence. They do not establish which framework won the comparison.
Small screenshot text is not used as evidence.

For ai-dev, one living record means a canonical problem/task identity linking
requirements, decisions, child tasks, changes, and evidence. It does not require
one physical document or loading the whole record into every agent context.

## Framework comparison

The final column is our assessment for ai-dev, not an upstream claim.

| Candidate | Verified upstream mechanism | Proposed use or constraint |
| --- | --- | --- |
| [Superpowers](https://github.com/obra/superpowers/blob/main/skills/subagent-driven-development/SKILL.md) | Fresh implementer per task; one task reviewer checks specification and quality; a broad final review; file handoffs and a progress ledger | First implementation/review methodology to evaluate; adapt permission and budget behavior to ai-dev |
| [GSD Core](https://github.com/open-gsd/gsd-core/blob/main/README.md) | Discuss, plan, execute in waves, verify, ship; substantial work in fresh subagent contexts; durable planning artifacts | Strong reference for a thin coordinator and dependency waves; benchmark as an alternative workflow provider |
| [Spec Kit](https://github.com/github/spec-kit/blob/main/README.md) | Specification, plan, tasks, implementation and convergence; extensible workflows, presets and integrations; separate bug and idea-assessment entry points | Candidate where formal specification and reusable organizational processes are useful |
| [OpenSpec](https://github.com/Fission-AI/OpenSpec/blob/main/README.md) | Change packages with proposal, specifications, design and tasks; apply and archive; shared cross-repository stores are beta | Candidate specification provider for evolving existing systems; avoid duplicating canonical task ownership |
| [BMAD Method](https://github.com/bmad-code-org/BMAD-METHOD/blob/main/README.md) | Product, architecture, UX, development and testing perspectives; process depth varies with scope | Candidate for broader product discovery and planning; load roles only when useful |

Version and identity details matter:

- The former [GSD repository](https://github.com/gsd-build/get-shit-done/blob/main/README.md)
  explicitly redirects to `open-gsd/gsd-core`. The slide only says GSD; the exact
  edition used by the presenter is unknown.
- The BMAD README currently labels v6.12 stable and v7 preview alpha. Its stated
  release ETA is not proof that a release has occurred.
- [BMad Loop](https://github.com/bmad-code-org/bmad-loop/blob/main/README.md) is a
  separate, early-beta orchestrator. Python controls story selection, retries,
  gates and resumable state; fresh coding sessions implement and review. Its
  architecture is particularly relevant, but adopting it would also introduce
  BMAD story/board contracts and require a compatibility assessment.
- [Superpowers' Hermes integration](https://github.com/obra/superpowers#hermes-agent)
  documents installation and warns that compaction can lose its startup
  bootstrap. Installation support does not prove unattended recovery.

## Evidence and implications

### Separate production from evaluation

Anthropic's [long-running application harness experiment](https://www.anthropic.com/research/harness-design-long-running-apps)
uses planner, generator and evaluator roles, explicit completion criteria,
file-based handoffs and checks against a running application. It reports that
self-evaluation tends to be lenient; an independent evaluator still needs
concrete criteria. These experiments support the pattern, not a guarantee of
correctness or a universal cost advantage.

For ai-dev: preserve a separate reviewer context and acceptance evidence. A
reviewer's approval cannot replace behavioral checks, and passing tests cannot
prove every requirement was implemented.

### Keep work resumable outside conversation history

Anthropic's [earlier harness study](https://www.anthropic.com/research/effective-harnesses-for-long-running-agents)
separates initial setup from incremental coding sessions and uses progress
artifacts plus repository history. Compaction alone was insufficient for reliable
continuation in those experiments.

For ai-dev: checkpoint assignments and outputs, including unfinished work and
the next action. Recover ownership and artifacts before dispatching a replacement
worker; a new session must not redo accepted work.

### Bound correction and narrow repeat reviews

The current Superpowers [fix-loop definition](https://github.com/obra/superpowers/blob/main/skills/subagent-driven-development/references/fix-loop.md)
allows five rounds, reuses the implementer initially, escalates later to a fresh
stronger worker, and reviews findings plus the fix diff. Its cap behavior permits
controller rulings and deferred findings.

For ai-dev: adopt bounded, scoped correction, but choose our own measured budgets.
Do not inherit plugin permission rules or automatically waive an acceptance
blocker at the cap. Plan changes follow the project's approval policy.

### Treat context as a selected working set

[Anthropic's context guidance](https://www.anthropic.com/research/effective-context-engineering-for-ai-agents)
recommends relevant context and a clear, minimal tool set. The
[Agent Skills specification](https://agentskills.io/specification#progressive-disclosure)
formalizes progressive loading: metadata, activated instructions, then supporting
resources on demand. Splitting files alone does not ensure selective loading.

For ai-dev: assemble role-specific briefs, load required dependencies, and audit
implicit harness/plugin instructions. Keep tool output and reports as referenced
artifacts; let agents retrieve more context when the initial brief is insufficient.

### Parallelism has a cost and a dependency boundary

Anthropic's [multi-agent research report](https://www.anthropic.com/research/multi-agent-research-system)
finds benefits on independent research directions, but also substantial token
overhead and difficulty with highly coupled work. Its reported usage ratios are
for that research system, not estimates for our coding bot.

For ai-dev: parallelize independent investigations or tasks with defined
interfaces. Serialize conflicting edits and integration. Compare total cost per
accepted task, including coordination, failed attempts and rework.

### Recovery is more than saving model state

LangGraph's [fault-tolerance documentation](https://docs.langchain.com/oss/python/langgraph/fault-tolerance)
distinguishes retryable execution from exactly-once external effects. A missing
acknowledgement leaves an unknown outcome; stable operation identities and
provider reconciliation are needed. This is a useful design reference, not a
proposal to add LangGraph as a dependency.

For ai-dev: persist an operation identity before a possible external write. After
a crash, check whether the PR, tracker update or deployment already exists.
Never equate an absent local receipt with an unexecuted action.

## Recommendation and evaluation

Keep Hermes plus Superpowers as the first prototype direction. Reuse verified
harness scheduling and persistence through adapters; add only missing control
semantics. Evaluate GSD Core as an alternative execution methodology, and borrow
the deterministic control/worker separation illustrated by BMad Loop.

Choose one active owner for each phase. Do not activate all five methodologies
over the same task: overlapping planners, reviewers and state files create both
extra cost and ambiguous authority. A specification provider can feed an
execution provider through an explicit artifact contract.

Benchmark on representative bug fixes, features, cross-component changes and
restart scenarios. Hold requirements and acceptance checks constant. Record
versions, model assignments, input/output/cache usage, total cost, elapsed time,
human interventions, defects, rejected completions and repeated work. Repeat
enough runs to expose variability before making performance claims.

The concrete proposal is in the agent loop proposal of that date (since replaced by [the architecture](../architecture.md)); selective loading
is defined separately in the context loading note of that date (document removed).
