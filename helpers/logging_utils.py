"""Logging helpers for consistent request/response framing with timestamps.

Provides a unified ``log_line`` function that writes to stdout and (if configured)
an append-only log file. Spacing standardization:
  * Every logical phase starts with HLINE
  * Each marker line stands alone
  * Content blocks end with HLINE
"""
from __future__ import annotations
import json
import os
import sys
from datetime import datetime
from typing import Optional, TextIO

HLINE = '-' * 80
_LOG_FILE_HANDLE: Optional[TextIO] = None

def _now() -> str:
    return datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.%fZ')

def setup_logging_from_env() -> None:
    """Initialize file logging if ``LOG_FILE`` env var is set.

    Safe to call multiple times; subsequent calls are no-ops if handle exists.
    """
    global _LOG_FILE_HANDLE
    if _LOG_FILE_HANDLE is not None:
        return
    path = os.getenv('LOG_FILE')
    if not path:
        return
    try:
        _LOG_FILE_HANDLE = open(path, 'a', encoding='utf-8')
    except Exception:
        _LOG_FILE_HANDLE = None  # silently degrade

def _write(line: str) -> None:
    sys.stdout.write(line + '\n')
    sys.stdout.flush()
    if _LOG_FILE_HANDLE:
        try:
            _LOG_FILE_HANDLE.write(line + '\n')
            _LOG_FILE_HANDLE.flush()
        except Exception:
            pass

def log_line(line: str = '', with_time: bool = True) -> None:
    """Log a single line with optional UTC timestamp prefix."""
    if with_time and line and not line.startswith('-'):
        _write(f'[{_now()}] {line}')
    else:
        _write(line)

def start_block(marker: str) -> None:
    log_line(HLINE, with_time=False)
    log_line(marker)

def end_block() -> None:
    log_line(HLINE, with_time=False)

def print_request(payload: dict) -> None:
    start_block('[mcp-client => mcp-server]')
    log_line(json.dumps(payload, indent=2))
    end_block()

def print_response(raw_text: str) -> None:
    start_block('[mcp-client <= mcp-server]')
    for line in raw_text.splitlines():
        log_line(line)
    end_block()
