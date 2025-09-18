"""MCP transport client wrapping JSON-RPC calls over HTTP.

Isolates networking concerns (session, headers, handshake) from orchestration.
"""
from __future__ import annotations
from typing import Any, Dict
import uuid
import json
import requests
import os
import sys
from dotenv import load_dotenv
from .logging_utils import start_block, end_block, log_line

class McpClient:
    """Lightweight JSON-RPC over HTTP client tailored for MCP server."""

    def __init__(self) -> None:
        load_dotenv()
        raw_endpoint = os.getenv('MCP_ENDPOINT', '').strip()
        if not raw_endpoint:
            print('ERROR: MCP_ENDPOINT not set', file=sys.stderr)
            sys.exit(1)
        # Always ensure a single trailing slash to avoid 307 redirects (/mcp -> /mcp/)
        self.endpoint = raw_endpoint.rstrip('/') + '/'
        self.auth = os.getenv('MCP_BEARER_TOKEN', '')
        self.session = requests.Session()
        self.session.headers.update({
            'Accept': 'application/json, text/event-stream',
            'Content-Type': 'application/json',
        })
        if self.auth:
            self.session.headers['Authorization'] = f'Bearer {self.auth}'
        self.session_id_header = 'Mcp-Session-Id'

    def call(self, method: str, params: Dict[str, Any] | None = None, id_: str | None = None) -> Dict[str, Any]:
        payload = {'jsonrpc': '2.0', 'id': id_ or str(uuid.uuid4()), 'method': method}
        if params is not None:
            payload['params'] = params
        start_block('[mcp-client => mcp-server]')
        log_line(json.dumps(payload, indent=2))
        end_block()
        resp = self.session.post(self.endpoint, data=json.dumps(payload).encode('utf-8'), timeout=60)
        start_block('[mcp-client <= mcp-server]')
        for line in resp.text.splitlines():
            log_line(line)
        end_block()
        # Attempt SSE-friendly parsing: collect JSON from lines beginning with 'data:'
        data: Dict[str, Any] = {}
        try:
            if 'data:' in resp.text:
                for line in resp.text.splitlines():
                    if line.startswith('data:'):
                        candidate = line[len('data:'):].strip()
                        if candidate:
                            try:
                                parsed = json.loads(candidate)
                                if isinstance(parsed, dict):
                                    data = parsed  # last dict wins (single response expected)
                            except Exception:
                                pass
            if not data:
                data_json = resp.json()
                if isinstance(data_json, dict):
                    data = data_json
        except Exception:
            data = {}
        sid = resp.headers.get(self.session_id_header)
        if sid:
            self.session.headers[self.session_id_header] = sid
        return data if isinstance(data, dict) else {}

    # Handshake helpers
    def initialize(self) -> Dict[str, Any]:
        return self.call('initialize', {
            'protocolVersion': '2025-03-26',
            'capabilities': {'tools': {}, 'resources': {}, 'prompts': {}},
            'clientInfo': {'name': 'dq-orchestrator', 'version': '0.1.0'},
        })


    # Capability queries
    def list_tools(self) -> Dict[str, Any]:
        """Return server-declared tool metadata if supported.

        Returns an empty dict on failure for resilience.
        """
        try:
            return self.call('tools/list', {})
        except Exception:
            return {}

    # Adaptive tool invocation -------------------------------------------------
    def call_tool(self, tool: str, arguments: Dict[str, Any] | None = None) -> Dict[str, Any]:
        """Invoke a tool with adaptive argument & naming strategy.

        Features:
        - Normalizes generic base/quality names to canonical prefixes.
        - Retries with argument key variants (table_name vs tableName, etc.).
        - Caches failing (tool, frozenset(arg_keys)) signature for -32602 suppression.
        """
        if not hasattr(self, '_failure_cache'):
            self._failure_cache: set[tuple[str, frozenset[str]]] = set()

        canonical = self._normalize_tool_name(tool)
        args = arguments.copy() if arguments else {}
        signature = (canonical, frozenset(args.keys()))
        if signature in self._failure_cache:
            return {'error': {'code': -32602, 'message': 'suppressed cached invalid params'}, 'tool': canonical}

        # First attempt direct
        result = self.call('tools/call', {'name': canonical, 'arguments': args})
        if self._is_invalid_params(result):
            # Retry with key variants if any
            for variant_args in self._argument_variants(args):
                variant_signature = (canonical, frozenset(variant_args.keys()))
                if variant_signature in self._failure_cache:
                    continue
                variant_result = self.call('tools/call', {'name': canonical, 'arguments': variant_args})
                if not self._is_invalid_params(variant_result):
                    return variant_result | {'_tool': canonical, '_args': variant_args}
                self._failure_cache.add(variant_signature)
            self._failure_cache.add(signature)
        return result | {'_tool': canonical, '_args': args}

    # Internal helpers ---------------------------------------------------------
    def _normalize_tool_name(self, name: str) -> str:
        # Map generic names to server canonical variants (td_ prefix where required)
        mapping = {
            'databaseList': 'base_databaseList',
            'tableList': 'base_tableList',
            'tableDDL': 'base_tableDDL',
            'tablePreview': 'base_tablePreview',
            # quality shortcuts
            'missingValues': 'qlty_missingValues',
            'distinctCategories': 'qlty_distinctCategories',
            'univariateStatistics': 'qlty_univariateStatistics',
        }
        if name in mapping:
            return mapping[name]
        # If already namespaced leave it
        if name.startswith(('base_', 'qlty_', 'td_base_', 'td_qlty_')):
            return name.replace('td_base_', 'base_').replace('td_qlty_', 'qlty_')
        return name

    def _is_invalid_params(self, resp: Dict[str, Any]) -> bool:
        err = resp.get('error') if isinstance(resp, dict) else None
        if not isinstance(err, dict):
            return False
        return err.get('code') == -32602

    def _argument_variants(self, args: Dict[str, Any]):
        if not args:
            yield {}
            return
        # For each key produce snake_case and camelCase variants
        def variants_for_key(k: str):
            base = k.replace('-', '_')
            snake = base
            parts = base.split('_')
            camel = parts[0] + ''.join(p.capitalize() for p in parts[1:]) if len(parts) > 1 else base
            return {snake, camel}
        keys = list(args.keys())
        variant_sets = [variants_for_key(k) for k in keys]
        # Simple cartesian expansion with pruning (limit combinations)
        def backtrack(i: int, current: Dict[str, Any]):
            if i == len(keys):
                yield current.copy()
                return
            original_key = keys[i]
            for vk in variant_sets[i]:
                current[vk] = args[original_key]
                yield from backtrack(i + 1, current)
            # remove for safety
            current.pop(vk, None)
        # Cap expansions to avoid explosion
        count = 0
        for combo in backtrack(0, {}):
            yield combo
            count += 1
            if count > 10:
                break
