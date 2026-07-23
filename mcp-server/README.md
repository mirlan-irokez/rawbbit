# Rawbbit MCP server and Metabase

This guide shows how to deploy the Rawbbit MCP server, optional Metabase, or both on one VM.

The MCP server is a read-only analytical surface over a ClickHouse table that already contains Rawbbit events. It is not part of the ingestion path, and it does not replace the raw Parquet layer.

Use this guide after you have a ClickHouse table such as `analytics.events` populated from Rawbbit raw Parquet.

```text
Producer -> Collector API -> NATS JetStream -> Raw Writer -> Parquet in object storage
                                                              |
                                                              v
                                                        ClickHouse
                                                              |
                                      +-----------------------+-----------------------+
                                      |                                               |
                                      v                                               v
                              Rawbbit MCP server                                  Metabase
```

## What This Deploys

The `mcp-server/` package provides:

- a FastMCP server for querying a configured ClickHouse Rawbbit events table
- read-only MCP tools for event discovery, sampling, guarded SQL, DAU, and funnel checks
- optional Metabase with PostgreSQL as its application database
- Caddy ingress variants for MCP-only, Metabase-only, or shared MCP + Metabase HTTPS
- environment examples for ClickHouse, MCP auth, Metabase, and public hostnames

The separate `metabase/` guide remains useful for a standalone Metabase deployment. Use this guide when you want Metabase and the Rawbbit MCP server managed from the same deployment package.

## Prerequisites

You need:

- a VM or host that can run Docker Compose
- DNS records for the public endpoint or endpoints
- a ClickHouse server reachable from the MCP container
- a Rawbbit events table, usually `analytics.events`
- credentials for a ClickHouse user that can run read queries on that table
- a generated bearer token for MCP access if exposing MCP outside a private network

For the ClickHouse table setup, see [`../clickhouse/README.md`](../clickhouse/README.md).

## Runtime Shape

Recommended public endpoints:

```text
https://mcp.example.com/mcp
https://metabase.example.com
```

Local container traffic:

```text
Caddy -> mcp-server:8000 -> ClickHouse
Caddy -> metabase:3000
metabase -> postgres:5432
```

If another reverse proxy already owns ports `80` and `443`, run only the application services and point the host-level proxy at the localhost-bound ports:

```text
127.0.0.1:${MCP_BIND_HOST_PORT:-8000} -> MCP server
127.0.0.1:${METABASE_BIND_HOST_PORT:-3000} -> Metabase
```

## MCP Tools

The MCP server currently exposes:

- `healthcheck`
- `table_overview`
- `list_event_names`
- `discover_json_keys`
- `sample_events`
- `run_readonly_sql`
- `calculate_dau`
- `calculate_funnel`

The tools are intentionally raw-table oriented. They are useful for exploration and first analytical workflows, not a full semantic layer.

## Published image

The MCP server is published as a public GHCR image:

```text
ghcr.io/mirlan-irokez/rawbbit-mcp-server:0.0.2
```

The image also has a `latest` tag for convenience. Use the pinned version tag for deployments.

The included `docker-compose.yml` points at the pinned GHCR image by default.

## 1. Prepare Environment

From the public repository root:

```bash
cd mcp-server
cp .env.example .env
```

Set at least:

```env
MCP_DOMAIN=mcp.example.com
METABASE_DOMAIN=metabase.example.com
MCP_PATH=/mcp

CLICKHOUSE_HOST=clickhouse.example.com
CLICKHOUSE_PORT=443
CLICKHOUSE_USER=readonly_user
CLICKHOUSE_PASSWORD=replace_me
CLICKHOUSE_DATABASE=analytics
CLICKHOUSE_TABLE=events
CLICKHOUSE_SECURE=1
CLICKHOUSE_VERIFY=1

MCP_API_KEYS_JSON={"user1":"replace-with-long-random-token"}
MCP_ALLOW_UNAUTHENTICATED=0

POSTGRES_DB=metabase
POSTGRES_USER=metabase
POSTGRES_PASSWORD=CHANGE_THIS_TO_A_LONG_RANDOM_PASSWORD
METABASE_TIMEZONE=UTC
```

Keep real tokens and passwords in private `.env` files or a secret manager. Do not commit them.

## 2. Configure DNS

If using bundled Caddy, point DNS records at the VM:

```text
mcp.example.com -> VM_IP
metabase.example.com -> VM_IP
```

Open only the ports needed for SSH and HTTPS:

- `22/tcp`
- `80/tcp`
- `443/tcp`

Do not publish ClickHouse, PostgreSQL, or Metabase container ports directly to the public internet.

## 3. Deploy MCP Only

Use this when you only need agent or MCP-client access to ClickHouse-backed Rawbbit analytics.

```bash
docker compose pull mcp-server
docker compose up -d mcp-server caddy-mcp
docker compose logs -f mcp-server caddy-mcp
```

Expected endpoint:

```text
https://mcp.example.com/mcp
```

## 4. Deploy Metabase Only

Use this when you only need a BI UI from the combined deployment package.

```bash
docker compose --profile ingress-metabase up -d postgres metabase caddy-metabase
docker compose logs -f postgres metabase caddy-metabase
```

Expected endpoint:

```text
https://metabase.example.com
```

Metabase connects to ClickHouse from the Metabase admin UI after startup. The application database in this deployment is PostgreSQL, not the default embedded H2 database.

## 5. Deploy MCP and Metabase Together

Use the shared Caddy service when both public hostnames should live on the same VM.

```bash
docker compose pull mcp-server
docker compose --profile ingress-shared up -d mcp-server postgres metabase caddy-shared
docker compose logs -f mcp-server postgres metabase caddy-shared
```

Important rule:

- only one Caddy service should bind ports `80` and `443` on the VM
- if switching from MCP-only Caddy to shared Caddy, stop and remove the MCP-only Caddy service first

Example:

```bash
docker compose stop caddy-mcp
docker compose rm -f caddy-mcp
docker compose --profile ingress-shared up -d postgres metabase caddy-shared
```

## 6. Use an Existing Host-Level Caddy

If the VM already has a host-level Caddy or another reverse proxy, do not start the bundled Caddy services.

Start only the application services:

```bash
docker compose pull mcp-server
docker compose up -d mcp-server
docker compose up -d postgres metabase
```

Then proxy:

```text
mcp.example.com      -> 127.0.0.1:${MCP_BIND_HOST_PORT:-8000}
metabase.example.com -> 127.0.0.1:${METABASE_BIND_HOST_PORT:-3000}
```

## MCP Authentication

For simple self-hosted deployments, use static bearer tokens:

```env
MCP_API_KEYS_JSON={"user1":"replace-with-long-random-token","user2":"another-long-random-token"}
MCP_ALLOW_UNAUTHENTICATED=0
```

The server fails startup when no static token or JWT verifier is configured. For local-only development without auth, set `MCP_ALLOW_UNAUTHENTICATED=1`.

Clients send:

```text
Authorization: Bearer replace-with-long-random-token
```

FastMCP remote clients commonly connect to HTTP MCP endpoints at the full path, for example:

```text
https://mcp.example.com/mcp
```

Do not expose MCP publicly without an authentication layer. Static bearer tokens are simple and useful for early self-hosted deployments, but they still need operational care:

- generate long random values
- keep tokens out of git
- rotate tokens manually when access should be revoked
- prefer HTTPS for every remote MCP endpoint

## Connect AI Agents

AI agents and MCP clients can connect to the Rawbbit MCP endpoint after the server is deployed.

Use the full MCP endpoint path unless your client explicitly expects a server base URL:

```text
https://mcp.example.com/mcp
```

The token in client configuration must match one of the values in `MCP_API_KEYS_JSON`.

### Codex example

Add a remote MCP server entry to `~/.codex/config.toml`:

```toml
[mcp_servers.rawbbit-demo]
url = "https://mcp.example.com/mcp"
http_headers = { Authorization = "Bearer MCP_AUTH_TOKEN" }
```

Replace `MCP_AUTH_TOKEN` with the bearer token issued for the deployed MCP server.

### OpenCode example

Add a remote MCP server entry to `opencode.jsonc`:

```jsonc
{
  "mcp": {
    "rawbbit_clickhouse": {
      "type": "remote",
      "url": "https://mcp.example.com/mcp",
      "enabled": true,
      "headers": {
        "Authorization": "Bearer {file:~/.secrets/rawbbit-mcp-token}"
      }
    }
  }
}
```

Store the token in `~/.secrets/rawbbit-mcp-token`.

### OpenClaw example

Add a streamable HTTP MCP server entry to your OpenClaw workspace `openclaw.json` file:

```json
{
  "mcp": {
    "servers": {
      "rawbbit": {
        "url": "https://mcp.example.com/mcp",
        "transport": "streamable-http",
        "headers": {
          "Authorization": "Bearer MCP_AUTH_TOKEN"
        }
      }
    }
  }
}
```

Replace `MCP_AUTH_TOKEN` with the bearer token issued for the deployed MCP server.
Do not commit real bearer tokens in client configuration. Use local secret files, environment variables, or machine-private config where your client supports them.

## ClickHouse Connection Notes

If ClickHouse is exposed through HTTPS behind a reverse proxy:

```env
CLICKHOUSE_HOST=clickhouse.example.com
CLICKHOUSE_PORT=443
CLICKHOUSE_SECURE=1
CLICKHOUSE_VERIFY=1
```

If ClickHouse is reachable on a private network or same host, use the host and port that the MCP container can actually reach. A loopback-only host binding is often not reachable from inside a Linux container.

Use a ClickHouse user with the minimum permissions needed for read-only analytics. The MCP server also applies query guardrails, but database permissions are still the stronger boundary.

## Metabase Notes

Metabase is a BI layer, not the Rawbbit ingestion runtime.

After Metabase starts:

1. open the Metabase URL
2. create the admin user
3. add ClickHouse as a database using the ClickHouse host, port, user, password, and TLS settings
4. let Metabase sync metadata
5. build questions and dashboards on top of the ClickHouse Rawbbit table

Back up the Metabase PostgreSQL application database if dashboards, saved questions, or user settings matter.

## Operational Checklist

- raw Parquet remains the rebuildable source of truth
- ClickHouse can be rebuilt from raw storage if serving tables are lost or redesigned
- MCP should be authenticated before public exposure
- Caddy should be the only public ingress in the bundled deployment
- ClickHouse and PostgreSQL ports should stay private
- `.env` values should never be committed
- Metabase application data should be backed up

## Related Docs

- [`../README.md`](../README.md)
- [`../docs/architecture.md`](../docs/architecture.md)
- [`../docs/configuration.md`](../docs/configuration.md)
- [`../clickhouse/README.md`](../clickhouse/README.md)
- [`../metabase/README.md`](../metabase/README.md)
