# Following a card

Run `ai-dev-card --board BOARD --card ID history` for recent decisions and handoffs. Use `history --reviews` to list round outcomes with the recorded findings and their severity at that time. Add `--details --native` during an investigation. The default limit is 20 entries each for local events and tool activity. Native history includes the board's own event stream, comments and worker runs. Receipts carry the revision, executed checks, exit codes and evidence paths. Handoffs carry the outcome, the next stage and worker, inputs, and the reason for the decision.

`AI_DEV_HOME/cards/BOARD/ID/card.json` holds durable decisions and receipts; the adjacent `activity.sqlite` records completed worker tool calls. Events carry worker run and session identifiers when supplied by Hermes. Failed attempts are identifiable by hook status when the installed version supplies it. Raw tool payloads, shell commands and output are excluded from the activity log. Use Hermes worker logs for an individual tool's transcript. Preserve the card directory, Hermes board and session store together for postmortems.

From the originating planner session, `ai-dev-card --board BOARD --card ID bind` subscribes Hermes to the chat and thread and records the originating session ID when supplied. Repeating the command renews the same subscription. Hermes delivers supported board transitions. The outgoing worker places the detailed handoff in the board summary or reason for review requests, changes and completion. Within a lane the delivery service posts queued handoffs to the bound route; stage comments and details also remain available on demand. Individual tool calls stay in local storage.

A Slack thread or Telegram topic has its own delivery address. In a plain Telegram private chat, Hermes sends notifications to the shared chat. Use the card id to identify each notification. Name the owner session with `/title BOARD-ID` and switch conversations via `/sessions` or `/resume BOARD-ID`. Switching the active conversation does not retroactively move shared-chat notifications into its transcript.

Hermes integration surfaces: `model_tools.py` (post-tool hook), `hermes_cli/kanban.py` (show, subscriptions), `gateway/session_context.py` (route variables), native gateway session commands and Kanban notifier. The implementation uses the CLI and plugin hooks without importing Hermes modules.

## Stage delivery

Each new handoff and its delivery entry are written together. The dispatcher retries in order, tracks attempts and saves the native acknowledgement. A repeated handoff command for the same transition reuses the entry. No model is involved. Messages use the stage, actor, result and inputs supplied by the worker. In Hermes 0.21.5, `hermes_cli/send_cmd.py` accepts stdin, `--to` and `--json`. `tools/send_message_targets.py` parses Slack conversation/thread timestamps and Telegram chat/topic IDs. The native sender attempts to mirror successful sends into the destination gateway session. If a Telegram topic disappears, Hermes can fall back to the parent chat. A crash after remote acceptance and before recording the receipt can repeat the same labelled message.

Install the systemd unit on a system where the planner runs as `hermes`:

```sh
sudo install -m 644 hermes/ai-dev-notifications.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ai-dev-notifications.service
```

Other deployments can run `ai-dev-card notifications --watch` beside the gateway with the same card store and planner credentials. For diagnostics run `ai-dev-card notifications` once, then check each handoff's delivery status in `history`.

The [lifecycle analytics projection](analytics.md) joins these records with native runs and request usage for a future process analysis agent. Its ten-day retention is separate from the authoritative history described here.
