# ai-dev project instructions

ai-dev is a thin layer on Hermes Kanban: a verification runner, a review protocol and a gate plugin. Hermes owns scheduling, worker lifecycle, waiting and notifications. Do not rebuild those here.

## Read on demand

| Concern | Source |
| --- | --- |
| What Hermes does and what ai-dev does, card state, limits | `docs/architecture.md` |
| Card history and owner conversation | `docs/card-history.md` |
| Review rules and outcomes | `docs/review-protocol.md` |
| Policy file, running a card, first live run | `docs/operations.md` |
| Installing into Hermes profiles | `INSTALL.md` |

## Rules for changes

- Ratchet is a Hermes extension. Reuse Hermes capabilities instead of maintaining local equivalents. Prefer the public plugin API. Isolate required internal imports in integration modules and cover them with compatibility tests against the supported Hermes version. Patch Hermes source only when no suitable API exists.
- Verify each Hermes dependency against the deployed source and name the file in the docs. Standalone portability is not a requirement.
- Policy belongs in code and the policy file. Skills describe a role and the commands to run; they are not the place for case-specific patches.
- A gate decision must be testable without a model. Add the test in `tests/test_card.py`.
- Every refusal message names the command the worker should run next.
- Write shared documentation and code in English; use the owner's language in chat.
- Keep session notes and connected projects' secrets out of the repository. `codex-session` stays ignored.

## Tests

```sh
python3 -m unittest discover -s tests
```
