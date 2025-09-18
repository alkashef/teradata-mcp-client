# Teradata MCP Data Quality Orchestrator (LLM-First)

Single-mode, LLM-driven data quality assessment for Teradata via the Model Context Protocol (MCP). You provide a natural language prompt, the orchestrator plans metadata discovery and quality metrics, executes MCP tool calls, then summarizes issues.

---

## 📂 Project Structure

```
.
├── data_quality_client.py        # CLI entrypoint + orchestrator
├── helpers/
│   ├── __init__.py               # Re-exports key helper classes
│   ├── mcp_client.py             # JSON-RPC transport + handshake
│   ├── llm_planner.py            # LLM intent/discovery/quality planning + summary
│   ├── discovery_parser.py       # Heuristic parsing of discovery tool outputs
│   ├── models.py                 # Dataclasses (Intent, Plans, Results, Summary)
│   ├── json_utils.py             # Safe JSON helpers
│   └── logging_utils.py          # Framed logging + separator
├── .env                          # MCP + optional OpenAI credentials
├── run_mcp_server.bat            # Helper script to launch MCP server (Windows)
├── environment.yml               # Conda environment spec
└── README.md                     # This file
```

---

## ⚙️ Requirements

- **Teradata database** running and accessible.
- **Python 3.11** (via Conda).
- **Teradata MCP Server** (installed via `pip` or `uv`).

---

## 🚀 Setup

### 1. Clone and enter project
```bash
git clone <this-repo>
cd <this-repo>
```

### 2. Create Conda environment
```bash
conda env create -f environment.yml
conda activate teradata-mcp-dq
```

### 3. Configure `.env`
Create `.env` with at minimum:
```env
MCP_ENDPOINT=http://localhost:8001/mcp
MCP_BEARER_TOKEN=optional-token-if-required
# Optional LLM
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini
```

### 4. Start MCP Server

On **Windows**:
```bat
run_mcp_server.bat
```

This will open a new terminal, set Teradata connection variables, and launch:
```
teradata-mcp-server --mcp_transport streamable-http --mcp_port 8001 --profile all
```

### 5. Run the Orchestrator

Provide a natural language prompt:
```bash
python data_quality_client.py --prompt "Assess data quality for sales and customer related tables"
```

The program prints each JSON-RPC request and raw response framed as:
```
[mcp-client => mcp-server]
{ ...request json... }
[mcp-client <= mcp-server]
...raw server body...
```
Finally it prints a summarized dictionary with issues and recommendations.

---

## 🔄 Seven-Step Workflow

Implemented inside `data_quality_client.py` (`DataQualityOrchestrator`):
1. `ingest_user_prompt(prompt)` – store raw user request.
2. `derive_intent_with_llm()` – LLM parses goal, targets, constraints.
3. `ensure_connection()` – MCP handshake (`initialize` + `initialized`).
4. `discover_schema()` – LLM-planned metadata tools (e.g. `base_databaseList`, `base_tableList`).
5. `run_quality_metrics()` – LLM chooses quality tool names (e.g. `qlty_missingValues`).
6. (Implicit collection) – Results accumulated internally.
7. `summarize_with_llm()` – LLM produces issues & recommendations.

Shortcut: `run_full(prompt)` executes all steps in order.

Example programmatic usage:
```python
from data_quality_client import DataQualityOrchestrator

orch = DataQualityOrchestrator()
summary = orch.run_full("Assess data quality for finance tables focusing on transactions")
print(summary)
```

If `OPENAI_API_KEY` is absent, LLM methods return structured fallbacks.

## 🧩 Module Responsibilities

- `data_quality_client.py`: Orchestrates the 7-step flow; delegates to helper package.
- `helpers.mcp_client.McpClient`: JSON-RPC calls (`initialize`, `initialized`, `tools/call`).
- `helpers.llm_planner.LlmPlanner`: LLM (or fallback) for intent parsing, planning, summarization.
- `helpers.discovery_parser.DiscoveryParser`: Extracts databases, tables, DDL, previews.
- `helpers.models`: Dataclasses for intent, plans, discovery/quality results, summary.
- `helpers.json_utils`: Defensive JSON helpers.
- `helpers.logging_utils`: Standardized request/response framing & separators.

## 📘 Protocol Field Notes

- `protocolVersion`: MCP protocol contract version the client proposes (here `2025-03-26`). The server may reject if incompatible.
- `capabilities`: Declares which capability groups (tools/resources/prompts) the client can handle; sending empty dict objects signals basic support.
- `clientInfo`: Metadata for logging/analytics on the server side.
- SSE `data:` lines: In streamable HTTP mode, each server event includes lines beginning with `data:` containing JSON-RPC response payload fragments. The client prints raw body; parsing targets JSON objects following those markers.

## 📝 Notes

- LLM selection of tools is heuristic and may reference unavailable names—calls still print for traceability.
- Discovery parsing now attempts heuristic extraction (databases, tables, DDL, previews).
- Add authentication headers/secrets only via `.env`; never hard-code credentials.

## 📖 References

- [Teradata MCP Server (GitHub)](https://github.com/Teradata/teradata-mcp-server)  
- [MCP Protocol](https://modelcontextprotocol.io)  
