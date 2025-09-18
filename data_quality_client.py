"""LLM-first Teradata MCP Data Quality Orchestrator (single-file version).

Usage:
    python data_quality_client.py --prompt "Assess data quality for schema X"

Seven conceptual steps (methods on DataQualityOrchestrator):
 1. ingest_user_prompt
 2. derive_intent_with_llm
 3. ensure_connection
 4. discover_schema
 5. run_quality_metrics
 6. (implicit collection)
 7. summarize_with_llm
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
import json
import logging
from typing import Any, Dict, List

import requests
from dotenv import load_dotenv

try:
    from openai import OpenAI  # type: ignore
except Exception:  # pragma: no cover
    OpenAI = None  # type: ignore


class LLMClient:
    """Isolated LLM helper for intent, planning, and summarization."""

    def __init__(self) -> None:
        load_dotenv()
        self.api_key = os.getenv("OPENAI_API_KEY", "")
        self.model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        self.base_url = os.getenv("OPENAI_BASE_URL", "").strip() or None
        self._client = None
        if self.api_key and OpenAI is not None:
            kwargs = {}
            if self.base_url:
                kwargs["base_url"] = self.base_url
            self._client = OpenAI(**kwargs)  # type: ignore

    @property
    def available(self) -> bool:
        return bool(self._client)

    def _chat_json(self, system: str, user: str, temperature: float = 0.2) -> Dict[str, Any]:
        if not self.available:
            return {}
        try:
            resp = self._client.chat.completions.create(  # type: ignore
                model=self.model,
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

    def parse_intent(self, prompt: str) -> Dict[str, Any]:
        system = (
            "You extract structured intent for Teradata data-quality assessment. "
            "Return JSON with keys: goal, target_patterns (list), constraints (list)."
        )
        user = f"Prompt: {prompt}\nReturn JSON only."
        data = self._chat_json(system, user)
        if not data:
            return {"goal": prompt, "target_patterns": [], "constraints": []}
        data.setdefault("goal", prompt)
        data.setdefault("target_patterns", [])
        data.setdefault("constraints", [])
        return data

    def plan_discovery(self, intent: Dict[str, Any]) -> Dict[str, Any]:
        system = (
            "Given a Teradata DQ intent object, decide discovery steps. "
            "Always include: databaseList, tableList. Optionally tableDDL, tablePreview."
        )
        user = f"Intent: {json.dumps(intent)}\nReturn JSON with steps list (each tool + rationale)."
        data = self._chat_json(system, user)
        steps = data.get("steps") if isinstance(data, dict) else None
        if not isinstance(steps, list):
            steps = [
                {"tool": "base_databaseList", "why": "List databases"},
                {"tool": "base_tableList", "why": "List tables in targets"},
            ]
        return {"steps": steps}

    def plan_quality(self, discovered: Dict[str, Any]) -> Dict[str, Any]:
        system = "Choose data quality metrics for Teradata tables. Prefer nulls, distinct, minmax."
        user = f"Discovered: {json.dumps(discovered)[:5000]}\nReturn JSON with dq_tools list."
        data = self._chat_json(system, user)
        tools = data.get("dq_tools") if isinstance(data, dict) else None
        if not isinstance(tools, list):
            tools = [
                {"tool": "qlty_missingValues", "reason": "Null ratios"},
                {"tool": "qlty_distinctCategories", "reason": "Distinct counts"},
                {"tool": "qlty_univariateStatistics", "reason": "Min/max"},
            ]
        return {"dq_tools": tools}

    def interpret_quality(self, raw_results: List[Dict[str, Any]]) -> Dict[str, Any]:
        system = "Summarize Teradata data-quality metrics. Rank issues; propose actions."
        user = f"Metrics: {json.dumps(raw_results)[:12000]}\nReturn JSON with keys: summary, issues (list), recommendations (list)."
        data = self._chat_json(system, user)
        if not data:
            return {"summary": "No interpretation available", "issues": [], "recommendations": []}
        data.setdefault("summary", "(missing summary)")
        data.setdefault("issues", [])
        data.setdefault("recommendations", [])
        return data


class DataQualityOrchestrator:
    """Coordinates LLM planning + MCP tool execution for data quality."""

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

        self.llm = LLMClient()
        self.user_prompt: str | None = None
        self.intent: Dict[str, Any] | None = None
        self.discovery_plan: Dict[str, Any] | None = None
        self.discovery_results: Dict[str, Any] = {"databases": [], "tables": [], "ddl": {}, "previews": {}}
        self.quality_plan: Dict[str, Any] | None = None
        self.quality_results: List[Dict[str, Any]] = []
        self.summary: Dict[str, Any] | None = None

    # ---- Low-level JSON-RPC -------------------------------------------------
    def _rpc(self, method: str, params: Dict[str, Any] | None = None, id_: str | None = None) -> Dict[str, Any]:
        """Send a JSON-RPC request and print request/response frames."""
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
        """Store raw natural language request."""
        self.user_prompt = prompt

    # ---- Step 2 --------------------------------------------------------------
    def derive_intent_with_llm(self) -> Dict[str, Any]:
        """Derive structured intent (goal, targets, constraints) via LLM."""
        if not self.user_prompt:
            raise ValueError("No user prompt set")
        self.intent = self.llm.parse_intent(self.user_prompt)
        return self.intent

    # ---- Step 3 --------------------------------------------------------------
    def ensure_connection(self) -> Dict[str, Any]:
        """Perform MCP initialize + initialized handshake."""
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
        """Execute metadata discovery tools per LLM plan (placeholder parsing)."""
        if self.intent is None:
            raise ValueError("Intent must be derived before discovery")
        self.discovery_plan = self.llm.plan_discovery(self.intent)
        for step in self.discovery_plan.get("steps", []):
            tool = step.get("tool")
            if not tool:
                continue
            self._rpc("tools/call", {"name": tool, "arguments": {}})
        return self.discovery_results

    # ---- Step 5 --------------------------------------------------------------
    def run_quality_metrics(self) -> List[Dict[str, Any]]:
        """Call LLM-selected quality tools and collect lightweight metadata."""
        self.quality_plan = self.llm.plan_quality(self.discovery_results)
        for spec in self.quality_plan.get("dq_tools", []):
            name = spec.get("tool")
            if not name:
                continue
            self._rpc("tools/call", {"name": name, "arguments": {}})
            self.quality_results.append({"tool": name})
        return self.quality_results

    # ---- Step 7 --------------------------------------------------------------
    def summarize_with_llm(self) -> Dict[str, Any]:
        """Produce LLM-generated summary + recommendations."""
        self.summary = self.llm.interpret_quality(self.quality_results)
        return self.summary

    # ---- Convenience ---------------------------------------------------------
    def run_full(self, prompt: str) -> Dict[str, Any]:
        """Run all orchestration steps and return final summary dict."""
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
