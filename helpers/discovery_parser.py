"""Discovery tool response parsing utilities."""
from __future__ import annotations
from typing import Any, List
import re
from .models import DiscoveryResults

class DiscoveryParser:
    """Heuristically interprets discovery tool outputs into structured results."""

    def apply(self, tool: str, raw_response: dict, results: DiscoveryResults) -> None:
        if not isinstance(raw_response, dict):
            return
        result = raw_response.get('result') if isinstance(raw_response.get('result'), dict) else raw_response
        payload = None
        if isinstance(result, dict):
            if 'content' in result:
                payload = result.get('content')
            elif 'data' in result:
                payload = result.get('data')
        if payload is None:
            payload = raw_response
        if isinstance(payload, list):
            self._classify_list_payload(tool, payload, results)
            return
        if isinstance(payload, dict):
            for key in ('databases', 'databaseList', 'dbs'):
                if isinstance(payload.get(key), list):
                    self._merge_unique(results.databases, payload[key])
            for key in ('tables', 'tableList', 'tbls'):
                if isinstance(payload.get(key), list):
                    self._merge_unique(results.tables, payload[key])
            ddl_text = None
            for k, v in payload.items():
                if isinstance(v, str) and 'CREATE TABLE' in v.upper():
                    ddl_text = v
                    break
            if ddl_text:
                self._store_ddl(ddl_text, results)
            for key in ('rows', 'preview', 'sample'):
                rows = payload.get(key)
                if isinstance(rows, list) and rows and isinstance(rows[0], dict):
                    results.previews[tool] = rows[:50]

    def _classify_list_payload(self, tool: str, values: List[Any], results: DiscoveryResults) -> None:
        if not values:
            return
        if all(isinstance(v, str) for v in values):
            upper_score = sum(1 for v in values if v.isupper()) / len(values)
            contains_dot = any('.' in v for v in values)
            if not contains_dot and upper_score > 0.3 and tool.endswith('databaseList'):
                self._merge_unique(results.databases, values)
                return
            if contains_dot or tool.endswith('tableList'):
                self._merge_unique(results.tables, values)
                return

    def _merge_unique(self, target: List[Any], new_values: List[Any]) -> None:
        seen = set(target)
        for v in new_values:
            if v not in seen:
                target.append(v)
                seen.add(v)

    def _store_ddl(self, ddl_text: str, results: DiscoveryResults) -> None:
        match = re.search(r'CREATE\s+TABLE\s+([A-Za-z0-9_\.]+)', ddl_text, re.IGNORECASE)
        table_name = match.group(1) if match else f'table_{len(results.ddl)+1}'
        results.ddl[table_name] = ddl_text
