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
from openai import OpenAI 
from dotenv import load_dotenv
import argparse
import os
import sys
import uuid
import json
import logging
import requests


class DataQualityOrchestrator:
    """Single-class orchestrator for LLM-guided Teradata data quality tasks.

    Responsibilities:
        * Maintain session state (intent, plans, raw results, summary)
        * Perform MCP JSON-RPC handshake and tool invocation
        * Inline LLM lifecycle: intent parsing, discovery planning, quality planning, summarization
        * Provide graceful fallback defaults when LLM is unavailable

    Public Step Methods (invoke in order or use :meth:`run_full`):
        ingest_user_prompt -> derive_intent_with_llm -> ensure_connection ->
        discover_schema -> run_quality_metrics -> summarize_with_llm

    Internal LLM Helpers:
        _llm_chat_json, _llm_parse_intent, _llm_plan_discovery,
        _llm_plan_quality, _llm_interpret_quality

    Error Handling Philosophy:
        Keep surface area minimal. Only raise for missing prerequisites (e.g.,
        deriving intent before prompt). Network errors return empty dicts where
        feasible; caller can augment with retries as needed.
    """

    def __init__(self) -> None:
        """Initialize environment, HTTP session, and internal state containers."""
        load_dotenv()
        self.endpoint = os.getenv("MCP_ENDPOINT", "").rstrip("/")
        if not self.endpoint:
            print("ERROR: MCP_ENDPOINT not set", file=sys.stderr)
            sys.exit(1)
        self.auth = os.getenv("MCP_BEARER_TOKEN", "")
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        })
        if self.auth:
            self.session.headers["Authorization"] = f"Bearer {self.auth}"
        self.session_id_header = "Mcp-Session-Id"
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        self.log = logging.getLogger("dq-orch")

        # LLM client (inline)
        self._llm_client = None
        self._llm_model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        if os.getenv("OPENAI_API_KEY", "") and OpenAI is not None:
            kwargs = {}
            base_url = os.getenv("OPENAI_BASE_URL", "").strip() or None
            if base_url:
                kwargs["base_url"] = base_url
            try:
                self._llm_client = OpenAI(**kwargs)  # type: ignore
            except Exception:
                self._llm_client = None
        self.user_prompt: str | None = None
        self.intent: Dict[str, Any] | None = None
        self.discovery_plan: Dict[str, Any] | None = None
        self.discovery_results: Dict[str, Any] = {"databases": [], "tables": [], "ddl": {}, "previews": {}}
        self.quality_plan: Dict[str, Any] | None = None
        self.quality_results: List[Dict[str, Any]] = []
        self.summary: Dict[str, Any] | None = None

    # ---- Low-level JSON-RPC -------------------------------------------------
    def _rpc(self, method: str, params: Dict[str, Any] | None = None, id_: str | None = None) -> Dict[str, Any]:
        """Send a JSON-RPC request and print request/response frames.

        Parameters
        ----------
        method : str
            JSON-RPC method name (e.g. 'initialize', 'tools/call').
        params : dict | None
            Parameters object for the call; omitted if None.
        id_ : str | None
            Optional explicit request id; auto-generated UUID if absent.

        Returns
        -------
        dict
            Parsed JSON response (dict) or empty dict on parse/error failure.
        """
        payload = {"jsonrpc": "2.0", "id": id_ or str(uuid.uuid4()), "method": method}
        if params is not None:
            payload["params"] = params
        print("[mcp-client => mcp-server]")
        print(json.dumps(payload, indent=2))
        resp = self.session.post(self.endpoint, data=json.dumps(payload).encode("utf-8"), timeout=60)
        print("[mcp-client <= mcp-server]")
        print(resp.text)
        try:
            data = resp.json()
        except Exception:
            data = {}
        sid = resp.headers.get(self.session_id_header)
        if sid:
            self.session.headers[self.session_id_header] = sid
        return data if isinstance(data, dict) else {}

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
        self.intent = self._llm_parse_intent(self.user_prompt)
        return self.intent

    # ---- Step 3 --------------------------------------------------------------
    def ensure_connection(self) -> Dict[str, Any]:
        """Perform MCP `initialize` + `initialized` handshake.

        Returns
        -------
        dict
            Raw response from `initialize` call (server-dependent structure).
        """
        init_params = {
            "protocolVersion": "2025-03-26",
            "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
            "clientInfo": {"name": "dq-orchestrator", "version": "0.1.0"},
        }
        resp = self._rpc("initialize", init_params)
        try:
            self._rpc("initialized", {"clientCapabilities": {}}, id_="0")
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
        self.discovery_plan = self._llm_plan_discovery(self.intent)
        for step in self.discovery_plan.get("steps", []):
            tool = step.get("tool")
            if not tool:
                continue
            self._rpc("tools/call", {"name": tool, "arguments": {}})
        return self.discovery_results

    # ---- Step 5 --------------------------------------------------------------
    def run_quality_metrics(self) -> List[Dict[str, Any]]:
        """Call LLM-selected quality tools and collect lightweight metadata.

        Returns
        -------
        list[dict]
            List containing a dict per invoked quality tool (placeholder schema).
        """
        self.quality_plan = self._llm_plan_quality(self.discovery_results)
        for spec in self.quality_plan.get("dq_tools", []):
            name = spec.get("tool")
            if not name:
                continue
            self._rpc("tools/call", {"name": name, "arguments": {}})
            self.quality_results.append({"tool": name})
        return self.quality_results

    # ---- Step 7 --------------------------------------------------------------
    def summarize_with_llm(self) -> Dict[str, Any]:
        """Produce LLM-generated summary + recommendations.

        Returns
        -------
        dict
            Summary object with keys: summary (str), issues (list), recommendations (list).
        """
        self.summary = self._llm_interpret_quality(self.quality_results)
        return self.summary

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

    # ---- Internal LLM helpers ----------------------------------------------
    def _llm_available(self) -> bool:
        """Return True if a real LLM client is configured."""
        return bool(self._llm_client)

    def _llm_chat_json(self, system: str, user: str, temperature: float = 0.2) -> Dict[str, Any]:
        """Execute chat completion and attempt to parse JSON content.

        Returns empty dict when LLM unavailable or parsing fails.
        """
        if not self._llm_available():
            return {}
        try:
            resp = self._llm_client.chat.completions.create(  # type: ignore
                model=self._llm_model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                temperature=temperature,
            )
            content = resp.choices[0].message.content if resp and resp.choices else None
            if not content:
                return {}
            try:
                return json.loads(content)
            except Exception:
                return {"raw": content}
        except Exception:
            return {}

    def _llm_parse_intent(self, prompt: str) -> Dict[str, Any]:
        """Convert free-form user prompt into structured intent object."""
        system = (
            "You extract structured intent for Teradata data-quality assessment. "
            "Return JSON with keys: goal, target_patterns (list), constraints (list)."
        )
        user = f"Prompt: {prompt}\nReturn JSON only."
        data = self._llm_chat_json(system, user)
        if not data:
            return {"goal": prompt, "target_patterns": [], "constraints": []}
        data.setdefault("goal", prompt)
        data.setdefault("target_patterns", [])
        data.setdefault("constraints", [])
        return data

    def _llm_plan_discovery(self, intent: Dict[str, Any]) -> Dict[str, Any]:
        """Generate ordered discovery tool plan given intent."""
        system = (
            "Given a Teradata DQ intent object, decide discovery steps. "
            "Always include: databaseList, tableList. Optionally tableDDL, tablePreview."
        )
        user = f"Intent: {json.dumps(intent)}\nReturn JSON with steps list (each tool + rationale)."
        data = self._llm_chat_json(system, user)
        steps = data.get("steps") if isinstance(data, dict) else None
        if not isinstance(steps, list):
            steps = [
                {"tool": "base_databaseList", "why": "List databases"},
                {"tool": "base_tableList", "why": "List tables in targets"},
            ]
        return {"steps": steps}

    def _llm_plan_quality(self, discovered: Dict[str, Any]) -> Dict[str, Any]:
        """Select quality metric tools based on discovered metadata."""
        system = "Choose data quality metrics for Teradata tables. Prefer nulls, distinct, minmax."
        user = f"Discovered: {json.dumps(discovered)[:5000]}\nReturn JSON with dq_tools list."
        data = self._llm_chat_json(system, user)
        tools = data.get("dq_tools") if isinstance(data, dict) else None
        if not isinstance(tools, list):
            tools = [
                {"tool": "qlty_missingValues", "reason": "Null ratios"},
                {"tool": "qlty_distinctCategories", "reason": "Distinct counts"},
                {"tool": "qlty_univariateStatistics", "reason": "Min/max"},
            ]
        return {"dq_tools": tools}

    def _llm_interpret_quality(self, raw_results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Interpret raw per-tool metric results into human-oriented summary."""
        system = "Summarize Teradata data-quality metrics. Rank issues; propose actions."
        user = f"Metrics: {json.dumps(raw_results)[:12000]}\nReturn JSON with keys: summary, issues (list), recommendations (list)."
        data = self._llm_chat_json(system, user)
        if not data:
            return {"summary": "No interpretation available", "issues": [], "recommendations": []}
        data.setdefault("summary", "(missing summary)")
        data.setdefault("issues", [])
        data.setdefault("recommendations", [])
        return data


def main() -> None:
    parser = argparse.ArgumentParser(description="LLM-first Teradata Data Quality Orchestrator")
    parser.add_argument("--prompt", required=True, help="Natural language request (e.g. 'Assess data quality for schema sales.*')")
    args = parser.parse_args()
    orch = DataQualityOrchestrator()
    summary = orch.run_full(args.prompt)
    print(summary)


if __name__ == "__main__":  # pragma: no cover
    main()
