# Review protocol

The protocol exists to end one failure: a green candidate that a reviewer returns round after round with a different scenario each time. Reviewers report findings. Code derives the outcome.

## Rules

1. **The contract is the boundary.** A blocker names what it violates: `criterion:N`, `scenario:ID`, or `regression`. A blocker that names nothing valid is recorded as a note.
2. **Three classes.** `blocker` blocks. `note` is a real improvement that does not block. `discovery` is a defect that exists on the base; it becomes a follow-up.
3. **A blocker is confirmed by a failing test.** `ai-dev-card confirm` places the reviewer's test files into a scratch checkout of the candidate and of the base and runs the command in both.

   | Claim | Candidate | Base | Result |
   | --- | --- | --- | --- |
   | criterion or scenario | fails | any | `confirmed` |
   | regression | fails | passes | `confirmed` |
   | regression | fails | fails | `discovery` |
   | any | passes | any | `disproved` |
   | any | could not run | | `unconfirmed` |

   A reviewer who cannot write the test records `--cannot-reproduce REASON`. The finding becomes `unconfirmed`: listed for the owner, not blocking.

   Only a `candidate` can be confirmed. A confirmed blocker leaves that state through a fix that makes its reproduction pass, or through the owner's waiver; a reviewer cannot withdraw it.
4. **Gaps.** `kind: gap` says a criterion has no proof at all. In round 1, the coordinator must inspect the contract and cite independent repository or verification evidence before a gap can block. It closes when no reviewer raises it again.
5. **Ledger.** Every finding gets a stable key derived from its mechanism, so two reviewers describing one cause share an entry. Statuses: `candidate`, `confirmed`, `gap`, `fixed`, `disproved`, `unconfirmed`, `note`, `discovery`, `waived`, `resolved`.
6. **Settled stays settled.** A finding that was fixed, disproved, unconfirmed or waived returns to `candidate` only when a reviewer raises it again. It is then judged as stated in the new report and needs a new confirmation on the current tree.
7. **Closure is checked by code.** When a new candidate arrives, every stored reproduction of a confirmed blocker is rerun on it. One that passes marks its finding `fixed`. The reviewer does not vote on it.
8. **Round 1 is full, later rounds are delta.** The brief of a later round carries the interdiff from the previous candidate and the open blockers.
9. **Cap.** With `max_rounds: 3`, rounds 1 and 2 may end in `changes`. A third round with a blocker still open ends in `owner`.
10. **No unanimity.** With a panel, a single confirmed blocker blocks and an unconfirmed objection does not. Agreement between reviewers is not required.

## Impact, confidence and disposition

A new report supplies all three dimensions for every finding. Class sets the workflow disposition (candidate blocker, advisory note or pre-existing discovery). Confidence is the reviewer's confidence in the evidence (low, medium or high). Severity describes the consequence on a supported path.

| Severity | Meaning | Example |
| --- | --- | --- |
| critical | Irreversible data loss, exposure of sensitive data, or service-wide outage | A successful migration drops persisted user records |
| high | Core functionality fails for many users without a practical workaround | Existing installations cannot upgrade |
| medium | A supported case returns incorrect results or requires material manual recovery | A supported input produces the wrong response |
| low | Limited inconvenience or maintainability cost | A misleading diagnostic with correct behavior |

Each reviewer states the affected user, operation or data in `impact`. The ledger preserves every raw rating, confidence and class. Agreement or a single rating supplies the initial severity. Conflicting ratings use the lower level provisionally, then the coordinator examines the evidence and runs `ai-dev-card finding assess KEY --severity LEVEL --impact TEXT --evidence FILE`. This same step verifies a claimed missing-proof gap. FILE must be a tracked source file of the reviewed commit or independent evidence from verification. Original ratings remain visible. A candidate blocker still requires its reproduction. A pre-existing discovery retains its follow-up disposition at any severity.

A low-rated finding is advisory. If all raw ratings are low, promotion into a blocking defect requires a named contract criterion or scenario, a material consequence documented by the coordinator, and a confirmed reproduction. Reclassifying an already confirmed blocker as low requires the usual waiver. Stored findings from older versions display `unknown` until new evidence provides a rating.

## Outcome

`ai-dev-card review settle` refuses while a candidate has no confirmation attempt. Then:

| Open confirmed blockers or gaps | Round | Outcome | Board call |
| --- | --- | --- | --- |
| none | any | `accept` | `kanban_complete` |
| some | below the cap | `changes` | `kanban_request_changes` with the fix brief |
| some | at the cap | `owner` | `ai-dev-card recover review` with the fix brief |

Settlement journals one immutable list of the findings reported in that round, including empty lists. Carried blockers appear separately. A missing provider produces an incomplete summary. The notification pump sends the full list, original ratings, final severity and coordinator decision in ordered text parts to the card's bound conversation. Replaying settlement reuses the same notification IDs. Acknowledged parts are skipped on retry; a crash after the remote send and before local acknowledgement can replay the same labeled part.

The fix brief lists each blocker with its reproduction command and the test file to add to the patch, then the notes and discoveries.

Running `review brief` or `review settle` again on a tree whose round is already settled returns that round. A reviewer run that Hermes restarts does not spend a round.

When the round settles `accept` and a `post_review` check then fails on the change, the reviewer returns the card with `kanban_request_changes` and the failing check. The next candidate opens the next round.

At `owner`, dispatch the bounded review diagnosis with `recover review`. If its correction fails and acceptance is still blocked, the owner can waive findings (`finding waive`), grants rounds (`review extend --rounds N`) or stops the card. Settling again after a waiver or an extension gives `accept` or `changes` for the same round.

## Independent review workers

The native `reviewer` profile coordinates a round. One invocation of `ai-dev-card review panel` dispatches two subprocess workers concurrently in separate checkouts of the same anchored Git candidate. Each gets the frozen brief, its own focus, relevant diff and read access to code and tests. The strict panel accepts reports only from dispatched workers. Each report lists concrete observations and repository-relative source paths. The coordinator confirms findings and settles the round after both workers return.

The default OpenAI worker runs Hermes with `--safe-mode --provider openai-codex -m gpt-6-astra --reasoning medium -t file --usage-file PATH -z PROMPT`. Its assignment is read from an ephemeral file inside its checkout. The runner instructs it to use read and search and verifies the checkout remains unchanged. The runtime ledger must identify a successful completed session and a positive API-call count for the pinned model/provider. See Hermes 0.21.5 (749220ef), `hermes_cli/_parser.py` and `hermes_cli/oneshot.py`.

The default Anthropic worker runs Claude Code with restricted read/search tools, Opus and medium effort. Claude Code's JSON result supplies the session ID and actual model usage. The author's selected model is recorded through Hermes's `pre_llm_call` hook (`agent/turn_context.py`); `review.author_model` can additionally reserve a model. Choose peer model capability when provisioning these routes.

Each completed response, coverage record, runtime model, session, focus and prompt digest is retained for its round. Retries reuse finished workers. Upgrading the panel routes supersedes the prior round for the same candidate. If either provider cannot return an authenticated report, the coordinator preserves the reason and available evidence, then follows `recover provider` for timed waits, diagnosis or human handoff. Strict mode rejects manual submissions, duplicate model identities and use of the author's model. Explicit legacy policies with `panel_required: false` retain manual review.

## Report format

```json
{"summary": "one or two sentences",
 "findings": [
   {"key": "existing key, omitted for a new finding",
    "class": "blocker | note | discovery",
    "kind": "defect | gap",
    "severity": "critical | high | medium | low", "confidence": "high | medium | low",
    "impact": "who is affected and the observable consequence",
    "title": "short name",
    "violates": "criterion:N | scenario:ID | regression",
    "location": "path:line",
    "trigger": "...", "expected": "...", "actual": "...",
    "mechanism": "short stable phrase naming the cause",
    "reproduction": "how to demonstrate it"}]}
```

A blocker needs `violates`, `location`, `trigger`, `expected` and `actual`. An empty `findings` list is a valid report.

### Subscription fallback for review

Each logical slot can allow specific cross-provider peers in `fallbacks`. Each peer has an exact `model`, `tier`, `output` runtime format and `argv`. A primary defaults to tier `frontier`. The operator assigns the capability tiers and a fallback must meet the slot's tier. The standard routes substitute Fable 5.2 for Astra and Astra 6.2 for Opus. Model identities already used by the implementation or reserved by the other review slot are excluded, including context-window aliases.

An author assigned to a primary slot is skipped before launch and its approved independent peer is selected. A classified quota, authentication or service-availability error from the runner dispatches an eligible peer in the same round. Invalid report content, missing runtime proof, local timeouts and command errors go to diagnosis. Every attempt retains its logical slot, model, provider, result and available runtime evidence. The bound task conversation receives the previous model, new model and reason for each route change. Two authenticated models on the same provider can complete the panel after an outage. The resulting summary and fix brief disclose the reduced provider diversity. When no independent peer is available, the gate stays closed and `ai-dev-card recover provider` drives recovery.

## Author and reviewer families

An author is excluded by model family for the entire card. This includes fallback models used during coding. For example, a change written by GPT-6.2 Astra excludes all Astra versions and context-window variants, including GPT-6 Astra. With the subscription panel an Astra implementation routes both review slots to Claude Fable 5.2 and Claude Opus 5.5. They run in independent processes on the same tree. Both are configured as frontier peers. If both slots use Anthropic the summary records reduced provider diversity. Author independence takes precedence over provider diversity.

GPT-6.2 Sol is an explicitly approved reserve for either slot. It can replace an unavailable Claude reviewer while the other independent reviewer remains active. The configured tier expresses review policy, not a benchmark result.
