"""High-level orchestrator implementing the seven-step LLM-first workflow.

Flow (each is a public method, callable independently for testing):
 1. ingest_user_prompt(prompt)
 2. derive_intent_with_llm()
 3. ensure_connection()
 4. discover_schema()
 5. run_quality_metrics()
 6. collect_results()  (already integrated while running quality)
 7. summarize_with_llm()

Design Notes:
- Keeps JSON-RPC mechanics isolated in _rpc().
- Uses the same request/response printing convention as existing runner.
- Does not fallback; LLM is the first planning step.
"""

from __future__ import annotations

import os
import sys
import uuid
import json
import logging
from typing import Any, Dict, List

import requests
from dotenv import load_dotenv

from llm_client import LLMClient


class DataQualityOrchestrator:
    def __init__(self) -> None:
        """Initialize orchestrator state, HTTP session, and LLM client.

        Reads environment for MCP endpoint / token and prepares internal
        containers for intent, discovery, quality planning, and summaries.
        """
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
        self.log = logging.getLogger("dq-orch")
        logging.basicConfig(level=logging.INFO, format="%(message)s")

        self.llm = LLMClient()
        self.user_prompt: str | None = None
        self.intent: Dict[str, Any] | None = None
        self.discovery_plan: Dict[str, Any] | None = None
        self.discovery_results: Dict[str, Any] = {"databases": [], "tables": [], "ddl": {}, "previews": {}}
        self.quality_plan: Dict[str, Any] | None = None
        self.quality_results: List[Dict[str, Any]] = []
        self.summary: Dict[str, Any] | None = None

    # --------------------------------------------------------------
    # Low-level JSON-RPC
    # --------------------------------------------------------------
    def _rpc(self, method: str, params: Dict[str, Any] | None = None, id_: str | None = None) -> Dict[str, Any]:
        """Send a JSON-RPC request and print raw request/response frames.

        Parameters
        ----------
        method : str
            JSON-RPC method name.
        params : dict | None
            Parameters object; omitted if None.
        id_ : str | None
            Optional explicit id; random UUID if omitted.
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
            j = resp.json()
        except Exception:
            j = {}
        sid = resp.headers.get(self.session_id_header)
        if sid:
            self.session.headers[self.session_id_header] = sid
        return j if isinstance(j, dict) else {}

    # --------------------------------------------------------------
    # Step 1: Ingest user prompt
    # --------------------------------------------------------------
    def ingest_user_prompt(self, prompt: str) -> None:
        """Store the user's natural language request for later parsing."""
        self.user_prompt = prompt

    # --------------------------------------------------------------
    # Step 2: Derive intent with LLM
    # --------------------------------------------------------------
    def derive_intent_with_llm(self) -> Dict[str, Any]:
        """Use LLM to turn the raw prompt into structured intent.

        Returns a dictionary with at least keys: goal, target_patterns,
        constraints. Raises if no prompt has been ingested.
        """
        if not self.user_prompt:
            raise ValueError("No user prompt set")
        self.intent = self.llm.parse_intent(self.user_prompt)
        return self.intent

    # --------------------------------------------------------------
    # Step 3: Ensure connection (initialize handshake)
    # --------------------------------------------------------------
    def ensure_connection(self) -> Dict[str, Any]:
        """Initialize MCP session (initialize + initialized notification)."""
        init_params = {
            "protocolVersion": "2025-03-26",
            "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
            "clientInfo": {"name": "dq-orchestrator", "version": "0.1.0"},
        }
        init_resp = self._rpc("initialize", init_params)
        # Best-effort completion notification
        try:
            self._rpc("initialized", {"clientCapabilities": {}}, id_="0")
        except Exception:
            pass
        return init_resp

    # --------------------------------------------------------------
    # Step 4: Discovery (metadata tools)
    # --------------------------------------------------------------
    def discover_schema(self) -> Dict[str, Any]:
        """Run metadata discovery tools chosen by the LLM plan.

        Currently executes without parsing responses into structured storage.
        Returns the internal discovery_results dictionary (still placeholder).
        """
        if self.intent is None:
            raise ValueError("Intent must be derived before discovery")
        self.discovery_plan = self.llm.plan_discovery(self.intent)
        steps = self.discovery_plan.get("steps", [])
        for step in steps:
            tool = step.get("tool")
            if not tool:
                continue
            if tool == "base_databaseList":
                self._rpc("tools/call", {"name": tool, "arguments": {}})
            elif tool == "base_tableList":
                # Attempt without args first
                self._rpc("tools/call", {"name": tool, "arguments": {}})
            elif tool == "base_tableDDL":
                # Placeholder: In a real flow you'd loop over tables discovered.
                pass
            elif tool == "base_tablePreview":
                # Placeholder.
                pass
            else:
                # Unknown but try blindly
                self._rpc("tools/call", {"name": tool, "arguments": {}})
        return self.discovery_results

    # --------------------------------------------------------------
    # Step 5: Run quality metrics (LLM selects tools first)
    # --------------------------------------------------------------
    def run_quality_metrics(self) -> List[Dict[str, Any]]:
        """Execute data-quality metric tools selected by the LLM.

        Each tool call prints raw frames; minimal metadata about executed
        tools is appended to quality_results. Returns that list.
        """
        self.quality_plan = self.llm.plan_quality(self.discovery_results)
        for spec in self.quality_plan.get("dq_tools", []):
            tname = spec.get("tool")
            if not tname:
                continue
            # Basic attempt: no args; schema-aware enhancement could be added.
            self._rpc("tools/call", {"name": tname, "arguments": {}})
            self.quality_results.append({"tool": tname})
        return self.quality_results

    # --------------------------------------------------------------
    # Step 7: Summarize with LLM (6 was implicit result collection)
    # --------------------------------------------------------------
    def summarize_with_llm(self) -> Dict[str, Any]:
        """Invoke LLM to interpret collected metric outputs and produce summary."""
        self.summary = self.llm.interpret_quality(self.quality_results)
        return self.summary

    # --------------------------------------------------------------
    # Convenience full run
    # --------------------------------------------------------------
    def run_full(self, prompt: str) -> Dict[str, Any]:
        """Convenience wrapper running all seven conceptual steps in order."""
        self.ingest_user_prompt(prompt)
        self.derive_intent_with_llm()
        self.ensure_connection()
        self.discover_schema()
        self.run_quality_metrics()
        return self.summarize_with_llm()
