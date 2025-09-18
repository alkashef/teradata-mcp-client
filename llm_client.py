"""Centralized LLM interface used for intent parsing, planning, and summarization.

This keeps all OpenAI usage isolated so the rest of the codebase never calls
the SDK directly. Each public method returns plain Python primitives.

Environment Variables:
  OPENAI_API_KEY   Required for any LLM calls. If absent, methods fall back.
  OPENAI_MODEL     Optional, default 'gpt-4o-mini'.
  OPENAI_BASE_URL  Optional base URL (Azure / proxy / gateway).
"""

from __future__ import annotations

import os
import json
from typing import Any, Dict, List
from dotenv import load_dotenv

try:
    from openai import OpenAI  # type: ignore
except Exception:  # pragma: no cover
    OpenAI = None  # type: ignore


class LLMClient:
    """Thin wrapper over OpenAI's chat completion API.

    If the API key is missing or the SDK import fails, all methods return
    conservative defaults rather than raising.
    """

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

    # ------------------------------------------------------------------
    # Core chat helper
    # ------------------------------------------------------------------
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
            # Try JSON parse, else return raw under 'raw'
            try:
                return json.loads(content)
            except Exception:
                return {"raw": content}
        except Exception:
            return {}

    # ------------------------------------------------------------------
    # Intent extraction
    # ------------------------------------------------------------------
    def parse_intent(self, prompt: str) -> Dict[str, Any]:
        """Extract target schemas, filters, or goals from a free-form user prompt."""
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

    # ------------------------------------------------------------------
    # Discovery planning
    # ------------------------------------------------------------------
    def plan_discovery(self, intent: Dict[str, Any]) -> Dict[str, Any]:
        """Plan which metadata tools to invoke given intent."""
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

    # ------------------------------------------------------------------
    # Quality planning
    # ------------------------------------------------------------------
    def plan_quality(self, discovered: Dict[str, Any]) -> Dict[str, Any]:
        """Select quality metrics to run based on discovered schema summary."""
        system = (
            "Choose data quality metrics for Teradata tables. Prefer nulls, distinct, minmax."
        )
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

    # ------------------------------------------------------------------
    # Result interpretation
    # ------------------------------------------------------------------
    def interpret_quality(self, raw_results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Produce a human-readable and structured summary of issues."""
        system = "Summarize Teradata data-quality metrics. Rank issues; propose actions."
        user = f"Metrics: {json.dumps(raw_results)[:12000]}\nReturn JSON with keys: summary, issues (list), recommendations (list)."
        data = self._chat_json(system, user)
        if not data:
            return {"summary": "No interpretation available", "issues": [], "recommendations": []}
        data.setdefault("summary", "(missing summary)")
        data.setdefault("issues", [])
        data.setdefault("recommendations", [])
        return data
