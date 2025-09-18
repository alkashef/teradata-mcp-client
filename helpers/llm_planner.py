"""LLM planning & summarization helper (OpenAI-compatible, fallback safe defaults)."""
from __future__ import annotations
import os
import json
from dataclasses import asdict
from dotenv import load_dotenv
from openai import OpenAI
from .models import Intent, DiscoveryPlan, DiscoveryStep, QualityPlan, QualityToolSpec, Summary, DiscoveryResults
from .logging_utils import HLINE, log_line, start_block, end_block

class LlmPlanner:
    """Encapsulates all LLM interactions with graceful fallback behavior."""

    def __init__(self) -> None:
        load_dotenv()
        self.api_key = os.getenv('OPENAI_API_KEY', '')
        self.model = os.getenv('OPENAI_MODEL', 'gpt-4o-mini')
        self.base_url = os.getenv('OPENAI_BASE_URL', '').strip() or None
        self._client = None
        if self.api_key:
            try:
                kwargs = {}
                if self.base_url:
                    kwargs['base_url'] = self.base_url
                self._client = OpenAI(**kwargs)  # type: ignore
            except Exception:
                self._client = None

    def _chat_json(self, system: str, user: str, temperature: float = 0.2) -> dict:
        if not self._client:
            return {}
        try:
            start_block('[user=>llm]')
            for line in user.splitlines():
                log_line(line)
            end_block()
            start_block('[llm<=user]')
            end_block()
            start_block('[llm=>mcp-client]')
            resp = self._client.chat.completions.create(  # type: ignore
                model=self.model,
                messages=[{'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
                temperature=temperature,
            )
            end_block()
            content = resp.choices[0].message.content if resp and resp.choices else None
            if not content:
                start_block('[llm<=mcp-client] (empty)')
                end_block()
                return {}
            try:
                parsed = json.loads(content)
                start_block('[llm<=mcp-client] (json)')
                end_block()
                return parsed
            except Exception:
                start_block('[llm<=mcp-client] (raw)')
                for line in content.splitlines():
                    log_line(line)
                end_block()
                return {'raw': content}
        except Exception:
            end_block()
            return {}

    def parse_intent(self, prompt: str) -> Intent:
        system = (
            'You extract structured intent for Teradata data-quality assessment. '
            'Return JSON with keys: goal, target_patterns (list), constraints (list).'
        )
        user = f'Prompt: {prompt}\nReturn JSON only.'
        data = self._chat_json(system, user)
        if not data:
            return Intent(goal=prompt)
        return Intent(
            goal=data.get('goal') or prompt,
            target_patterns=data.get('target_patterns') or [],
            constraints=data.get('constraints') or [],
        )

    def plan_discovery(self, intent: Intent) -> DiscoveryPlan:
        system = (
            'Given a Teradata DQ intent object, decide discovery steps. '
            'Always include: databaseList, tableList. Optionally tableDDL, tablePreview.'
        )
        user = f'Intent: {json.dumps(asdict(intent))}\nReturn JSON with steps list (each tool + rationale).'
        data = self._chat_json(system, user)
        steps_raw = []
        if isinstance(data, dict):
            steps_raw = data.get('steps') or []
        steps: list[DiscoveryStep] = []
        for s in steps_raw:
            if isinstance(s, dict) and 'tool' in s:
                steps.append(DiscoveryStep(tool=s['tool'], why=s.get('why')))
        if not steps:
            steps = [
                DiscoveryStep(tool='base_databaseList', why='List databases'),
                DiscoveryStep(tool='base_tableList', why='List tables in targets'),
            ]
        return DiscoveryPlan(steps=steps)

    def plan_quality(self, discovered: DiscoveryResults) -> QualityPlan:
        system = 'Choose data quality metrics for Teradata tables. Prefer nulls, distinct, minmax.'
        disco_dict = {
            'databases': discovered.databases,
            'tables': discovered.tables,
            'ddl_keys': list(discovered.ddl.keys()),
        }
        user = f'Discovered: {json.dumps(disco_dict)[:5000]}\nReturn JSON with dq_tools list.'
        data = self._chat_json(system, user)
        tools_raw = []
        if isinstance(data, dict):
            tools_raw = data.get('dq_tools') or []
        specs: list[QualityToolSpec] = []
        for t in tools_raw:
            if isinstance(t, dict) and 'tool' in t:
                specs.append(QualityToolSpec(tool=t['tool'], reason=t.get('reason')))
        if not specs:
            specs = [
                QualityToolSpec(tool='qlty_missingValues', reason='Null ratios'),
                QualityToolSpec(tool='qlty_distinctCategories', reason='Distinct counts'),
                QualityToolSpec(tool='qlty_univariateStatistics', reason='Min/max'),
            ]
        return QualityPlan(dq_tools=specs)

    def interpret_quality(self, raw_results: list[dict]) -> Summary:
        system = 'Summarize Teradata data-quality metrics. Rank issues; propose actions.'
        user = f'Metrics: {json.dumps(raw_results)[:12000]}\nReturn JSON with keys: summary, issues (list), recommendations (list).'
        data = self._chat_json(system, user)
        if not data:
            return Summary(summary='No interpretation available')
        return Summary(
            summary=data.get('summary') or '(missing summary)',
            issues=data.get('issues') or [],
            recommendations=data.get('recommendations') or [],
        )

    # New contextual intent builder
    def build_contextual_intent(self, prompt: str, schema: dict, tools: dict) -> Intent:
        system = (
            'You are a Teradata data quality intent parser. Given: a user prompt, a schema inventory '
            '(databases, tables, columns), and available tools metadata, produce JSON with keys: '
            'goal, target_patterns (list), constraints (list). Use table/column names when relevant.'
        )
        context = {
            'prompt': prompt,
            'schema_sample': {k: (v if isinstance(v, list) else str(v)) for k, v in list(schema.items())[:50]},
            'tools': tools.get('tools') if isinstance(tools, dict) else None,
        }
        user = f'Context: {json.dumps(context)[:12000]}\nReturn JSON only.'
        data = self._chat_json(system, user)
        if not data:
            return Intent(goal=prompt)
        return Intent(
            goal=data.get('goal') or prompt,
            target_patterns=data.get('target_patterns') or [],
            constraints=data.get('constraints') or [],
        )
