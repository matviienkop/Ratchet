# Source integrations

Reviewed against primary documentation on 2026-11-26. Hermes has its own catalog of reviewed MCP servers: list it with `hermes mcp catalog` and prefer `hermes mcp install NAME` when the source is there. The templates here cover only what the catalog lacks or a stricter variant: read-only Linear, self-hosted Grafana, Snowflake, a database gateway and a logs gateway. Templates use the supported Hermes `mcp_servers` config format. They are deliberately disabled, with no tools selected. Replace project-qualified names and environment references with the project's configuration, authenticate, inspect the advertised tool schemas, select exact read tools and perform a bounded read before declaring the source available. Do not copy the whole template over config.yaml.

## Linear

Use Ratchet's existing `ai-dev-tracker PROJECT_DIR list` and `read KEY` when Linear is the registered tracker. It already reads scoped issues and discussions; do not create duplicate tracker bindings. For broader research in projects/documents, use the official read-only MCP endpoint `https://mcp.linear.app/mcp/readonly`. Native OAuth is supported; follow the provider's configuration for restricted API keys when using a service identity. Scope reads to the configured team/project and fetch issue comments and related work, not only the description. Use existing tracker authorization for writes.

Reference: [Linear MCP](https://linear.app/docs/mcp).

## Confluence

Use Atlassian's official remote MCP for Confluence Cloud from the Hermes catalog: `hermes mcp install atlassian`. The catalog entry carries the endpoint Hermes has verified and its OAuth setup, so this bundle ships no template for it. Rename the server to a project-qualified name if the profile serves several projects. This service also exposes other products and writes. Restrict the identity and exact selected tools to the intended Confluence cloud/space read/search operations. Read the page body, version and relevant comments/linked pages. A search snippet is not the complete specification. Prefer the known cloudId/space from project settings to repeated global discovery.

OAuth may require a pre-registered client if automatic registration is unavailable. Use Hermes's installed OAuth configuration options, not client syntax copied from a different app. Legacy SSE examples are obsolete. Confluence Data Center requires a separately selected compatible project gateway; do not send its credentials to the Cloud endpoint or claim this Cloud recipe supports it.

Reference: [Atlassian MCP](https://atlassian.github.io/atlassian-mcp-server/).

## Grafana and logs

For Grafana Cloud, install the catalog entry with `hermes mcp install grafana` and authorize read access to the intended stack; this bundle ships no template for it. Check subscription requirements and query/Assistant costs before activation. For self-hosted Grafana, use a pinned official `mcp-grafana` binary with `--disable-write`, a read-only service-account token and the correct GRAFANA_URL. Its MCP tools cover dashboards and supported data sources, including Prometheus metrics and Loki logs. Select only the tools needed for the project.

For metrics retain dashboard/panel or query, datasource, organization, labels, start/end/step and timezone. For Loki retain datasource, LogQL, labels, range, line limit and request/trace IDs. Start with a narrow window and increase deliberately; do not infer causation from coincident peaks. Logs outside Grafana can use a project-owned logs MCP gateway. Local/service logs can use existing authorized Hermes read tools with bounded ranges; no extra plugin is required merely to read a file.

References: [Grafana Cloud MCP](https://grafana.com/docs/grafana-cloud/ai-tools/mcp-servers/cloud-mcp/), [Grafana MCP server](https://github.com/grafana/mcp-grafana).

## Snowflake

Use the Snowflake-managed MCP server configured by the account operator. Its endpoint has the form `https://ACCOUNT.snowflakecomputing.com/api/v2/databases/DATABASE/schemas/SCHEMA/mcp-servers/NAME`. Actual tool names and semantic views depend on that server's specification; do not guess them. Prefer OAuth for interactive access; a service may use a restricted PAT through a secret reference. The template provides the documented PAT headers, not a token.

Expose only the required search/analyst/read-only SQL tools. For SQL require server configuration `read_only: true`, a read-only role, query_timeout and an approved warehouse/cost budget. The account-level `CORTEX_MCP_SERVER_FORCE_SQL_EXEC_READ_ONLY` control can enforce the SQL restriction. These are operator settings, not SQL to execute automatically from a task. Do not enable Cortex coding-agent or arbitrary custom procedure tools for routine evidence collection. Record role/warehouse, source data timestamp, query ID, aggregation and limitations; Cortex-generated explanations need verification against their underlying evidence.

Reference: [Snowflake-managed MCP](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-agents-mcp).

## Databases

Use the project's existing database MCP gateway or a reviewed server for its actual engine. There is no universal connection string or one server that safely covers every database. The template supports an operator-provided HTTPS MCP endpoint; stdio servers use Hermes's standard command/args/env fields. No specific PostgreSQL/MySQL/other driver is installed by this bundle.

Require an account with read-only privileges scoped to the intended database/schema, preferably a replica. Confirm environment and freshness. Set statement timeout, row/time filters and project cost limits; begin with schema/metadata and aggregates. Avoid unapproved stored functions/procedures, exports, DDL/DML, grants and database provisioning tools. Do not mistake a cloud database management MCP for a read-only query connection. Preserve query ID/text reference and observed bounds, not raw credentials or unrestricted row dumps.

## Activation check

1. Identify the project scope and selected server, then record `not_configured` in the inventory.
2. Configure only that project entry with secret references. Use `hermes mcp install NAME` for a catalog server, `hermes mcp add --help` for another one, or merge the reviewed snippet into the active profile configuration through the normal operator workflow. After a catalog install, select the exact read tools before use: a catalog entry may enable more than research needs.
3. Authenticate with `hermes mcp login NAME` when applicable. If consent is needed, ask for the specific account action; do not claim access before it completes.
4. Inspect server capabilities, select exact read tools using `hermes mcp configure NAME`, and confirm provider-side permissions. Templates' empty includes expose no tools until this step.
5. Run `hermes mcp test NAME` and a bounded project-specific read. Record verification time, selected tools and evidence reference. A successful tools/list alone is insufficient.
6. Make the connection visible in the authorized planner session. Hermes handles reload/discovery; follow the installed version's behavior. Keep unrelated project profiles isolated.
