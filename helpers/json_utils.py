"""Utility helpers for safe JSON handling and formatting."""
from __future__ import annotations
from typing import Any
import json

def safe_json_load(text: str):
    try:
        return json.loads(text)
    except Exception:
        return None

def truncate_str(value: str, max_len: int = 5000) -> str:
    if len(value) <= max_len:
        return value
    return value[: max_len - 3] + '...'

def ensure_dict(obj: Any) -> dict:
    return obj if isinstance(obj, dict) else {}
