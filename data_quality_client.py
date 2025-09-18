"""LLM-first Teradata MCP Data Quality Orchestrator (single-file version).

Overview:
    This script exposes a single class, :class:`DataQualityOrchestrator`, which
    accepts a natural language prompt describing a Teradata data quality
    assessment objective. It then uses an (optional) LLM to:
      * Parse intent (goal, target patterns, constraints)
      * Plan metadata discovery tool calls
      * Plan quality metric tool calls
      * Summarize collected results into issues & recommendations

    All interaction with the Teradata MCP Server occurs over JSON-RPC via a
    simple HTTP POST endpoint that supports streamable events. Each outbound
    request and raw inbound response body is printed for full transparency.

Seven Orchestration Steps:
    1. ingest_user_prompt        – Capture raw user prompt
    2. derive_intent_with_llm    – Convert unstructured prompt into structured intent
    3. ensure_connection         – Perform `initialize` handshake
    4. discover_schema           – Run LLM-planned metadata inspection tools
    5. run_quality_metrics       – Run LLM-planned quality metric tools
    6. (implicit collection)     – Aggregate per-tool outputs internally
    7. summarize_with_llm        – Produce final human-readable + JSON summary

Environment Variables (.env):
    MCP_ENDPOINT        Base URL for MCP server (e.g. http://localhost:8001/mcp)
    MCP_BEARER_TOKEN    Optional bearer token for auth
    OPENAI_API_KEY      Optional – enables real LLM planning/summarization
    OPENAI_MODEL        Model name (default: gpt-4o-mini)
    OPENAI_BASE_URL     Optional override base URL for OpenAI-compatible APIs

Fallback Behavior:
    If no `OPENAI_API_KEY` is provided, deterministic safe defaults are used:
      * Intent: original prompt as goal, no patterns/constraints
      * Discovery plan: database list + table list
      * Quality plan: selected generic quality tools
      * Summary: basic placeholder summary structure

CLI Usage:
    python data_quality_client.py --prompt "Assess data quality for schema sales.*"

Programmatic Usage:
    from data_quality_client import DataQualityOrchestrator
    orch = DataQualityOrchestrator()
    summary = orch.run_full("Assess data quality for finance tables")
    print(summary)

Limitations / Future Enhancements:
    * Tool result parsing is placeholder; no deep extraction yet.
    * Plans are heuristic and may reference unavailable tool names.
    * Could add adaptive retries based on tool errors.
"""

from __future__ import annotations
from typing import Any, Dict, List
from dataclasses import asdict
import argparse
import logging
import time
import os

from helpers.mcp_client import McpClient
from helpers.llm_planner import LlmPlanner
from helpers.discovery_parser import DiscoveryParser
from helpers.models import Intent, DiscoveryPlan, QualityPlan, DiscoveryResults, Summary
from helpers.logging_utils import setup_logging_from_env, log_line, HLINE


class DataQualityOrchestrator:
    """Coordinates high-level data quality workflow using modular helpers.

    Orchestrator delegates:
      * Transport & handshake -> McpClient
      * LLM intent/planning/summarization -> LlmPlanner
      * Discovery parsing -> DiscoveryParser
      * Data shapes -> dataclasses in models.py
    """

    def __init__(self) -> None:
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        self.log = logging.getLogger("dq-orch")
        setup_logging_from_env()
        log_line(HLINE, with_time=False)
        log_line("[orchestrator] startup")
        log_line(HLINE, with_time=False)
        self.mcp = McpClient()
        self.planner = LlmPlanner()
        self.discovery_parser = DiscoveryParser()
        # State
        self.user_prompt: str | None = None
        self.intent: Intent | None = None
        self.schema_inventory: dict | None = None
        self.tool_inventory: dict | None = None
        self.discovery_plan: DiscoveryPlan | None = None
        self.discovery_results: DiscoveryResults = DiscoveryResults()
        self.quality_plan: QualityPlan | None = None
        self.quality_results: List[Dict[str, Any]] = []
        self.summary: Summary | None = None
        self.handshake_ok: bool = False

    # ---- Low-level JSON-RPC -------------------------------------------------
    # ---- Step 1 --------------------------------------------------------------
    def ingest_user_prompt(self, prompt: str) -> None:
        """Store raw natural language request.

        Parameters
        ----------
        prompt : str
            Free-form natural language description of the desired DQ assessment.
        """
        self.user_prompt = prompt

    # ---- New Step A: schema inventory --------------------------------------
    def inventory_schema(self) -> dict:
        """Collect list of tables & columns (placeholder until real tool calls).

        Returns
        -------
        dict
            Structure with keys: tables (list[str]), columns (dict[str, list[str]]).
        """
        if self.schema_inventory is not None:
            return self.schema_inventory
        import os, re
        target_db = os.getenv('DATABASE')
        if not target_db:
            uri = os.getenv('DATABASE_URI', '')
            # crude parse: teradata://user:pass@host:port/DB_NAME
            m = re.search(r'/([A-Za-z0-9_]+)$', uri)
            if m:
                target_db = m.group(1)
        tables: list[str] = []
        columns: dict[str, list[str]] = {}
        if target_db:
            try:
                tbl_list = self.mcp.call("tools/call", {"name": "base_tableList", "arguments": {"database_name": target_db}})
                if isinstance(tbl_list, dict):
                    r2 = tbl_list.get('result') or {}
                    maybe_tables = r2.get('tables') if isinstance(r2, dict) else []
                    if isinstance(maybe_tables, list):
                        for t in maybe_tables:
                            if isinstance(t, str):
                                tables.append(f"{target_db}.{t}")
            except Exception:
                pass
        self.schema_inventory = {"database": target_db, "tables": tables, "columns": columns}
        return self.schema_inventory

    # ---- New Step B: list tools --------------------------------------------
    def inventory_tools(self) -> dict:
        """Retrieve server-declared tools metadata if supported."""
        self.tool_inventory = self.tool_inventory or self.mcp.list_tools()
        return self.tool_inventory

    # ---- Step 2 --------------------------------------------------------------
    def derive_intent_with_llm(self) -> Dict[str, Any]:
        """Derive structured intent (goal, target patterns, constraints) via LLM.

        Returns
        -------
        dict
            Intent object with keys: goal (str), target_patterns (list[str]), constraints (list[str]).
        """
        if not self.user_prompt:
            raise ValueError("No user prompt set")
        # Prefer contextual build if inventories gathered
        if self.schema_inventory is not None or self.tool_inventory is not None:
            self.intent = self.planner.build_contextual_intent(
                self.user_prompt,
                self.schema_inventory or {},
                self.tool_inventory or {},
            )
        else:
            self.intent = self.planner.parse_intent(self.user_prompt)
        return asdict(self.intent)

    # ---- Step 3 --------------------------------------------------------------
    def ensure_connection(self) -> Dict[str, Any]:
        """Perform MCP `initialize` handshake only (no secondary call)."""
        resp = self.mcp.initialize()
        # Success heuristic: presence of 'result' and absence of 'error'
        if isinstance(resp, dict) and 'result' in resp and 'error' not in resp:
            self.handshake_ok = True
            log_line('[handshake] initialize success')
        else:
            self.handshake_ok = False
            log_line('[handshake] initialize FAILED – aborting further tool calls')
        return resp

    # ---- Step 4 --------------------------------------------------------------
    def discover_schema(self) -> Dict[str, Any]:
        """Execute metadata discovery tools per LLM plan (placeholder parsing).

        Returns
        -------
        dict
            Accumulated discovery results structure (databases, tables, ddl, previews).
        """
        if self.intent is None:
            raise ValueError("Intent must be derived before discovery")
        self.discovery_plan = self.planner.plan_discovery(self.intent)
        available_tools = set()
        if isinstance(self.tool_inventory, dict):
            tl = self.tool_inventory.get('tools') if 'tools' in self.tool_inventory else []
            if isinstance(tl, list):
                for t in tl:
                    if isinstance(t, dict) and 'name' in t:
                        available_tools.add(t['name'])
        filtered_steps = []
        for step in self.discovery_plan.steps:
            if available_tools and step.tool not in available_tools:
                log_line(f"[validation] skipping unknown discovery tool: {step.tool}")
                continue
            filtered_steps.append(step)
        self.discovery_plan.steps = filtered_steps
        for step in self.discovery_plan.steps:
            raw = self.mcp.call("tools/call", {"name": step.tool, "arguments": {}})
            self.discovery_parser.apply(step.tool, raw, self.discovery_results)
        return {
            "databases": self.discovery_results.databases,
            "tables": self.discovery_results.tables,
            "ddl": self.discovery_results.ddl,
            "previews": self.discovery_results.previews,
        }

    # ---- Step 5 --------------------------------------------------------------
    def run_quality_metrics(self) -> List[Dict[str, Any]]:
        """Call LLM-selected quality tools and collect lightweight metadata.

        Returns
        -------
        list[dict]
            List containing a dict per invoked quality tool (placeholder schema).
        """
        self.quality_plan = self.planner.plan_quality(self.discovery_results)
        available_tools = set()
        if isinstance(self.tool_inventory, dict):
            tl = self.tool_inventory.get('tools') if 'tools' in self.tool_inventory else []
            if isinstance(tl, list):
                for t in tl:
                    if isinstance(t, dict) and 'name' in t:
                        available_tools.add(t['name'])
        filtered_specs = []
        for spec in self.quality_plan.dq_tools:
            if available_tools and spec.tool not in available_tools:
                log_line(f"[validation] skipping unknown quality tool: {spec.tool}")
                continue
            filtered_specs.append(spec)
        self.quality_plan.dq_tools = filtered_specs
        for spec in self.quality_plan.dq_tools:
            self.mcp.call("tools/call", {"name": spec.tool, "arguments": {}})
            self.quality_results.append({"tool": spec.tool})
        return self.quality_results

    # ---- Step 7 --------------------------------------------------------------
    def summarize_with_llm(self) -> Dict[str, Any]:
        """Produce LLM-generated summary + recommendations.

        Returns
        -------
        dict
            Summary object with keys: summary (str), issues (list), recommendations (list).
        """
        self.summary = self.planner.interpret_quality(self.quality_results)
        return {
            "summary": self.summary.summary if self.summary else "",
            "issues": self.summary.issues if self.summary else [],
            "recommendations": self.summary.recommendations if self.summary else [],
        }

    # ---- Convenience ---------------------------------------------------------
    def run_full(self, prompt: str) -> Dict[str, Any]:
        """Run all orchestration steps and return final summary.

        Parameters
        ----------
        prompt : str
            Natural language description of data quality assessment goal.

        Returns
        -------
        dict
            Final summary structure (see :meth:`summarize_with_llm`).
        """
        self.ingest_user_prompt(prompt)
        # Handshake first so subsequent inventory/tool listing can rely on session
        self.ensure_connection()
        if not self.handshake_ok:
            # Produce minimal failure summary and stop.
            failure = {
                "summary": "Initialization failed – no further MCP tool calls executed.",
                "issues": ["Handshake with MCP server failed"],
                "recommendations": [
                    "Inspect server logs for initialize validation errors",
                    "Verify protocolVersion and capabilities payload",
                    "Ensure MCP_ENDPOINT is correct and reachable"
                ],
            }
            return failure
        # Optional sequencing delay to ensure server session readiness before first tool/list call
        try:
            delay_ms = int(os.getenv('MCP_POST_INIT_DELAY_MS', '150'))
        except ValueError:
            delay_ms = 150
        if delay_ms > 0:
            log_line(f"[handshake] post-init delay {delay_ms}ms")
            time.sleep(delay_ms / 1000.0)
        # Inventory now that handshake completed
        self.inventory_schema()
        self.inventory_tools()
        self.derive_intent_with_llm()
        self.discover_schema()
        self.run_quality_metrics()
        return self.summarize_with_llm()


def main() -> None:
    parser = argparse.ArgumentParser(description="LLM-first Teradata Data Quality Orchestrator")
    parser.add_argument("--prompt", required=True, help="Natural language request (e.g. 'Assess data quality for schema sales.*')")
    args = parser.parse_args()
    orch = DataQualityOrchestrator()
    summary = orch.run_full(args.prompt)
    print(summary)


if __name__ == "__main__":  # pragma: no cover
    main()
