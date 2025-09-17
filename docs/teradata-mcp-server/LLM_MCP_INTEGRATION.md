# LLM ↔ MCP Integration (PoC)

This guide explains how the OpenAI backend (`AI_OpenAI`) talks to the Teradata MCP Server over streamable HTTP, what to configure, and what to expect.

## Overview
- Transport: streamable HTTP (recommended on Windows)
- Scope: read-only tools; one-shot interactions
- Output: include SQL (if used) + short summary
- Defaults: DB falls back to `TD_NAME` from `config/.env`
- Logging: `[LLM=>MCP]` for tool payloads, `[MCP=>LLM]` for tool outputs

## Prerequisites
- `pip install teradata-mcp-server langchain-mcp-adapters openai>=1.30`
- Configure `config/.env` with:
  - `OPENAI_API_KEY` and optional `OPENAI_MODEL`
  - `MCP_URL` (e.g., `http://localhost:8001/mcp/`)
  - `TD_NAME` as default database

## Starting the Server
Start MCP server in another terminal:

```cmd
set TD_USER=your_user
set TD_PASSWORD=your_password
set TD_HOST=your_host
set TD_NAME=your_database
set TD_PORT=1025
set DATABASE_URI=teradata://%TD_USER%:%TD_PASSWORD%@%TD_HOST%:%TD_PORT%/%TD_NAME%
teradata-mcp-server --mcp_transport streamable-http --mcp_port 8001 --profile all
```

## Using the OpenAI Backend
Set the backend and URL, then run the app:

```cmd
set AI_BACKEND=openai
set MCP_URL=http://localhost:8001/mcp/
python -m streamlit run app.py
```

Ask questions like:
- "List tables in BANK_DB"
- "Show sample rows from BANK_DB.CUSTOMERS"
- "Run SQL: select top 5 * from BANK_DB.CUSTOMERS"
- "Univariate stats for BANK_DB.CUSTOMERS AGE"
- "Missing values in BANK_DB.CUSTOMERS"
- "Distinct categories in BANK_DB.CUSTOMERS SEGMENT"

## Behavior Summary
- Planner chooses one action: `list_tables`, `describe_table`, `preview_table`, `read_query`, `dq_univariate`, `dq_missing`, or `dq_distinct`.
- Tools mapped to MCP:
  - `base_tableList`, `base_tableDDL`, `base_tablePreview`, `base_readQuery`
  - `qlty_univariateStatistics`, `qlty_missingValues`, `qlty_distinctCategories`
- Responses include a short natural language summary and SQL if executed.
- Logging adds explicit prefixes for tool I/O.

## Notes
- This is a dev-only PoC to validate LLM+MCP integration.
- By default, no client-side row caps are applied; rely on server limits unless the user specifies otherwise.
- If outputs are huge, logs truncate to head+tail for readability.
