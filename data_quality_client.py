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
    3. ensure_connection         – Perform `initialize` + `initialized` handshake
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
        self.discovery_plan: DiscoveryPlan | None = None
        self.discovery_results: DiscoveryResults = DiscoveryResults()
        self.quality_plan: QualityPlan | None = None
        self.quality_results: List[Dict[str, Any]] = []
        self.summary: Summary | None = None

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
        self.intent = self.planner.parse_intent(self.user_prompt)
        return asdict(self.intent)

    # ---- Step 3 --------------------------------------------------------------
    def ensure_connection(self) -> Dict[str, Any]:
        """Perform MCP `initialize` + `initialized` handshake.

        Returns
        -------
        dict
            Raw response from `initialize` call (server-dependent structure).
        """
        resp = self.mcp.initialize()
        try:
            self.mcp.initialized()
        except Exception:
            pass
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
        self.derive_intent_with_llm()
        self.ensure_connection()
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
