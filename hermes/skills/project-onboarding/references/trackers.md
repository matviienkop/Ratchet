# Register the project's tracker

The owner selects the tracker in chat. Keep the internal Kanban as the execution board. Link each external issue to the native card. Ask for an example issue and the destination only when they are missing. Store tracker options using the arguments of `ai-dev-discover`.

| Provider | `--tasks-scope-json` example | Example ticket |
|---|---|---|
| internal (default) | omitted | native card id |
| github-issues | `{"repository":"Acme/product","github_project":{"url":"https://github.com/orgs/Acme/projects/4","status_field":"Status"}}` | `Acme/product#31` |
| linear | `{"team_key":"DEV"}` | `DEV-31` |
| jira (Jira Cloud) | `{"project_key":"DEV","site":"https://acme.atlassian.net"}` | `DEV-31` |

For a GitHub Issues tracker without an organization Project, omit `github_project`; ticket intake still works. For Linear and Jira, record the team's actual status names with `--tasks-status-mapping-json`, for example `{"ready":"To Do","running":"In Progress","review":"In Review","blocked":"Blocked","done":"Done"}`. GitHub Projects accepts the same option for custom columns. Jira transitions must be available for the current issue and identify one destination. Set the corresponding phase only after the matching native transition.

Credentials are service-owned, private files (0600) in `~/.config/ai-dev/credentials/PROJECT/`. Use the provisioned `github-token` for GitHub, a personal API key in `linear-token` for Linear, or `jira.json` containing `site`, `email` and `token` for Jira Cloud basic authentication. The Jira credential's site is the exact HTTPS origin. Do not ask for credentials in chat.

Verify onboarding using `ai-dev-tracker PROJECT_DIR read TICKET`; report the title, state, and whether the discussion was retrieved. This verifies reads only. Without a sample ticket, use `ai-dev-tracker PROJECT_DIR list --limit 5` to check the scoped backlog. An empty response verifies access but supplies no ticket to test discussion intake. When the credential is missing, record the outstanding check. Registration itself does not change the external board. A native card's worker can sync its bound ticket via `ai-dev-tracker PROJECT_DIR stage PHASE --board BOARD --card ID`; retrying a completed status update checks the current destination before writing.

The operator starts `ai-dev-tracker WORKSPACE_ROOT watch --kanban-home HERMES_ROOT` as the Hermes service account (use `--once` for one pass). It polls the shared native Kanban databases every 30 seconds, reads the current confirmed status, then updates the configured tracker. A running reviewer's claim remains in review. An external outage schedules a retry with backoff. Each attempted transition and its receipt is stored in the project's `.tracker-bindings.sqlite3` table `mirror_history`. A successful unchanged phase makes no further remote requests. When recovering after downtime, the worker converges to the current native phase.
