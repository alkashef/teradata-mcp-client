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
from logging_utils import print_request, print_response

class McpClient:
    """Lightweight JSON-RPC over HTTP client tailored for MCP server."""

    def __init__(self) -> None:
        load_dotenv()
        self.endpoint = os.getenv('MCP_ENDPOINT', '').rstrip('/')
        if not self.endpoint:
            print('ERROR: MCP_ENDPOINT not set', file=sys.stderr)
            sys.exit(1)
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
        print_request(payload)
        resp = self.session.post(self.endpoint, data=json.dumps(payload).encode('utf-8'), timeout=60)
        print_response(resp.text)
        try:
            data = resp.json()
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

    def initialized(self) -> Dict[str, Any]:
        return self.call('initialized', {'clientCapabilities': {}}, id_='0')
