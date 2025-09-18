"""Logging helpers for consistent request/response framing."""
from __future__ import annotations
import json

HLINE = '-' * 80

def print_request(payload: dict) -> None:
    print('[mcp-client => mcp-server]')
    print(json.dumps(payload, indent=2))
    print(HLINE)

def print_response(raw_text: str) -> None:
    print('[mcp-client <= mcp-server]')
    print(raw_text)
    print(HLINE)
