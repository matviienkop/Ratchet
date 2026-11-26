---
name: ai-dev-repair
description: Restore a project prerequisite for another Kanban worker and report evidence that the interrupted check can resume.
---

Read the repair card and the referring card with `kanban_show`. Read the cited verification log and the exact command, scope, known attempts and required result. Work in the repair scratch workspace. Use only the project's authorized resources. Determine the cause before changing a stand or tool. Preserve the candidate and the original test criteria.

Apply the narrow repair, validate it on the named resource and record the result and evidence locations on your repair card. Call `kanban_complete` with the observed cause, action, validation and a handoff for rerunning the interrupted check. The blocked worker resumes through the native dependency. If the required action needs unavailable credentials or authority, name it precisely and notify the coordinator. Reuse this repair card while the same failure is open.

For a guest incident, run `ai-dev-card stand list` for registered operations in the current project. Read initial power with `stand status NAME`. Use `stand test NAME -- COMMAND ARG...` for diagnosis; an opt-in disposable guest supports `stand repair NAME < repair.sh`. Restore its initial power state afterwards. Record the cause and evidence for the parent worker.

Write the repair card's result, comments and any tracker text in English and run them through the `unslop` skill. Messages to the owner's chat use the owner's language.
