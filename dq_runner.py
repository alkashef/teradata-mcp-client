import os
import sys
import json
import uuid
import time
import logging
from typing import Any, Dict, List
import requests
import yaml
from dotenv import load_dotenv

class DataQualityRunner:
    def __init__(self):
        load_dotenv()
        self.MCP_ENDPOINT = os.getenv("MCP_ENDPOINT", "").rstrip("/")
        self.AUTH_BEARER = os.getenv("MCP_BEARER_TOKEN", "")
        self.SESSION_ID_HEADER = "Mcp-Session-Id"
        if not self.MCP_ENDPOINT:
            print("ERROR: MCP_ENDPOINT not set in .env", file=sys.stderr)
            sys.exit(1)
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        self.log = logging.getLogger("dq")
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        })
        if self.AUTH_BEARER:
            self.session.headers["Authorization"] = f"Bearer {self.AUTH_BEARER}"

    def _extract_json_from_sse(self, text: str) -> Dict[str, Any] | None:
        if not text:
            return None
        t = text.strip().replace("\r\n", "\n")
        # Look for 'data: {json}' lines
        for line in t.split("\n"):
            if line.startswith("data: "):
                json_part = line[len("data: "):].strip()
                try:
                    return json.loads(json_part)
                except Exception:
                    continue
        # Fallback: try whole body
        try:
            return json.loads(t)
        except Exception:
            return None

    def _rpc(self, method: str, params: Dict[str, Any] | None = None, id_: int | str | None = None):
        payload = {"jsonrpc": "2.0", "id": id_ or str(uuid.uuid4()), "method": method}
        if params is not None:
            payload["params"] = params
        # Print input (request payload) without exposing secrets
        print("[mcp-client => mcp-server]")
        print(json.dumps(payload, indent=2))
        r = self.session.post(self.MCP_ENDPOINT, data=json.dumps(payload).encode("utf-8"), timeout=60)
        r.raise_for_status()
        sid = r.headers.get(self.SESSION_ID_HEADER)
        if sid:
            self.session.headers[self.SESSION_ID_HEADER] = sid
        # Print output (raw server response) exactly as-is
        print("[mcp-client <= mcp-server]")
        print(r.text)
        # Parse JSON result (best-effort) so caller can proceed
        try:
            return r.json()
        except Exception:
            return self._extract_json_from_sse(r.text) or {}

    def initialize(self):
        params = {
            "protocolVersion": "2025-03-26",
            "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
            "clientInfo": {"name": "dq-oneoff", "version": "0.1.0"},
        }
        self.log.info("Initializing MCP session…")
        self._rpc("initialize", params)

    def list_tools(self) -> List[Dict[str, Any]]:
        resp = self._rpc("tools/list", {})
        if not isinstance(resp, dict):
            return []
        result = resp.get("result") or resp
        tools = result.get("tools") or result.get("result") or []
        return tools if isinstance(tools, list) else []

    def pick_quality_tools(self, tools: List[Dict[str, Any]]):
        def has_kw(t: Dict[str, Any], kws: List[str]) -> bool:
            name = (t.get("name") or "").lower()
            desc = (t.get("description") or "").lower()
            return any(k in name or k in desc for k in kws)
        t_null  = [t for t in tools if has_kw(t, ["qlty", "quality"]) and has_kw(t, ["null", "missing"])]
        t_range = [t for t in tools if has_kw(t, ["qlty", "quality"]) and has_kw(t, ["range", "min", "max"])]
        t_uniq  = [t for t in tools if has_kw(t, ["qlty", "quality"]) and has_kw(t, ["unique", "uniqueness", "distinct"])]
        if not any([t_null, t_range, t_uniq]):
            t_null  = [t for t in tools if has_kw(t, ["qlty"]) and has_kw(t, ["null"])]
            t_range = [t for t in tools if has_kw(t, ["qlty"]) and has_kw(t, ["range"])]
            t_uniq  = [t for t in tools if has_kw(t, ["qlty"]) and has_kw(t, ["unique", "distinct"])]
        return (
            (t_null[0]  if t_null  else None),
            (t_range[0] if t_range else None),
            (t_uniq[0]  if t_uniq  else None),
        )

    def tool_args_from_schema(self, tool: Dict[str, Any], proposed: Dict[str, Any]) -> Dict[str, Any]:
        schema = tool.get("inputSchema") or {}
        props = (schema.get("properties") or {}) if schema.get("type") == "object" else {}
        args = {}
        for k, v in proposed.items():
            if not props or k in props:
                args[k] = v
        for k in (schema.get("required") or []):
            if k not in args:
                if k.upper() in os.environ:
                    args[k] = os.environ[k.upper()]
        return args

    def call_tool(self, name: str, arguments: Dict[str, Any]):
        self._rpc("tools/call", {"name": name, "arguments": arguments})

    def load_config(self, path: str) -> Dict[str, Any]:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    def run(self):
        cfg_path = os.getenv("DQ_CONFIG", "dq_config.yml")
        cfg = self.load_config(cfg_path)
        self.initialize()
        tools = self.list_tools()
        # Heuristic picking of tools by keywords
        def has_kw(t: Dict[str, Any], kws: List[str]) -> bool:
            name = (t.get("name") or "").lower()
            desc = (t.get("description") or "").lower()
            return any(k in name or k in desc for k in kws)
        t_null  = next((t for t in tools if has_kw(t, ["qlty", "quality"]) and has_kw(t, ["null", "missing"])), None)
        t_range = next((t for t in tools if has_kw(t, ["qlty", "quality"]) and has_kw(t, ["range", "min", "max"])), None)
        t_uniq  = next((t for t in tools if has_kw(t, ["qlty", "quality"]) and has_kw(t, ["unique", "uniqueness", "distinct"])), None)

        # Iterate datasets from config and call matching tools. We only print I/O; no aggregation.
        for dataset in cfg.get("datasets", []):
            table_fqn = dataset.get("table")
            if not table_fqn:
                continue
            schema = dataset.get("schema")
            database = dataset.get("database")

            # NULLS / missing values
            null_cols = (dataset.get("null_check", {}) or {}).get("columns", [])
            if t_null and null_cols:
                args: Dict[str, Any] = {"table": table_fqn, "columns": null_cols}
                if schema: args["schema"] = schema
                if database: args["database"] = database
                self._rpc("tools/call", {"name": t_null.get("name"), "arguments": args})

            # RANGE checks (if server exposes a suitable tool)
            ranges = (dataset.get("range_check", {}) or {}).get("columns", [])
            if t_range and ranges:
                for rng in ranges:
                    col = rng.get("column")
                    if not col:
                        continue
                    args: Dict[str, Any] = {
                        "table": table_fqn,
                        "column": col,
                        "min": rng.get("min"),
                        "max": rng.get("max"),
                        "inclusive": rng.get("inclusive", True),
                    }
                    if schema: args["schema"] = schema
                    if database: args["database"] = database
                    self._rpc("tools/call", {"name": t_range.get("name"), "arguments": args})

            # UNIQUENESS checks
            uniq_cfg = dataset.get("uniqueness_check", {}) or {}
            uniq_cols = uniq_cfg.get("columns", [])
            if t_uniq and uniq_cols:
                args: Dict[str, Any] = {
                    "table": table_fqn,
                    "columns": uniq_cols,
                    "approximate": uniq_cfg.get("approximate", False),
                }
                if schema: args["schema"] = schema
                if database: args["database"] = database
                self._rpc("tools/call", {"name": t_uniq.get("name"), "arguments": args})
