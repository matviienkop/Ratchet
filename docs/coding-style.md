# Coding style for development workers

Resolve conventions for the checkout, component, language and files in the assignment. Owner decisions and applicable project rules come first. Preserve the repository's formatter and CI settings, directory-specific IDE rules, documented conventions and their exceptions. If explicit rules disagree, report the two sources and the affected files. Where settings leave a choice open, follow consistent nearby hand-written code. Scope a convention to the narrowest component supported by the evidence.

## Existing code

The onboarding worker records configured and observed conventions with their source lines. Finalization saves the profile under the checkout in `config/discovered.json` and links its references from `rules/style.md`. A partial analysis writes to `provisional/`. Reports from before style profiling are scheduled for a fresh analysis on the next onboarding run.

For each distinct affected scope, run `ai-dev-analyze PROJECT_DIR style CHECKOUT FILE --language LANGUAGE` on the registered checkout. The result contains candidate rules for that directory and language at its current analyzed commit. Apply each native selector, exception and override to the target file. Supply the applicable rules and formatter/check references in the implementer's and reviewer's briefs. A stale revision needs a refresh. Verify the team's existing formatter or lint target on the candidate; review semantic conventions such as naming or API structure that those tools cannot check. Keep formatting changes scoped to the task.

## New repositories and components

Use the stack chosen for the component or identified in its scaffold. Where it leaves conventions unspecified, select a widely used language or framework baseline. Prefer an official convention when one exists. Persist the selection before writing the first source file using `ai-dev-style-default PROJECT_DIR CHECKOUT --stack STACK [--scope SUBDIR]`. A repository can have separate Java and TypeScript baselines. For an unlisted stack, the agent supplies a maintained guide and its HTTPS source. If the stack has yet to be chosen, the first assignment that establishes it also records its conventions.

Saved baselines live in the project's `config/style-defaults/`. Workers retrieve a baseline with the same command and `--lookup --scope DIRECTORY`. A matching baseline fills gaps after explicit choices and established code conventions. Later onboarding can discover a different formatter without replacing the owner's configuration. Adding a formatter dependency or changing a checked-in IDE configuration follows the project's normal change process.
