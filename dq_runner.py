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

    def _call_tool_with_fallbacks(self, tool_name: str, base_args: Dict[str, Any]) -> Dict[str, Any]:
        # Try multiple shapes for params
        param_shapes = [
            ("name", "arguments"),
            ("tool_name", "arguments"),
            ("toolName", "arguments"),
            ("name", "args"),
            ("name", "parameters"),
        ]
        # Try argument key variants
        def arg_variants(args: Dict[str, Any]) -> List[Dict[str, Any]]:
            variants = [args]
            # snake_case to camelCase
            camel = {}
            for k, v in args.items():
                if "_" in k:
                    parts = k.split("_")
                    camel_k = parts[0] + "".join(p.title() for p in parts[1:])
                    camel[camel_k] = v
                else:
                    camel[k] = v
            variants.append(camel)
            # generic keys
            generic = args.copy()
            if "database_name" in args and "database" not in generic:
                generic["database"] = args["database_name"]
            if "table_name" in args and "table" not in generic:
                generic["table"] = args["table_name"]
            if "column_name" in args and "column" not in generic:
                generic["column"] = args["column_name"]
            variants.append(generic)
            return variants

        last_resp: Dict[str, Any] = {}
        for key_name, key_args in param_shapes:
            for av in arg_variants(base_args):
                # 1) arguments as object
                params = {key_name: tool_name, key_args: av}
                resp = self._rpc("tools/call", params)
                last_resp = resp if isinstance(resp, dict) else {}
                err = last_resp.get("error") if isinstance(last_resp, dict) else None
                if not err or not isinstance(err, dict) or err.get("code") != -32602:
                    return last_resp

                # 2) arguments as JSON string
                params = {key_name: tool_name, key_args: json.dumps(av)}
                resp = self._rpc("tools/call", params)
                last_resp = resp if isinstance(resp, dict) else {}
                err = last_resp.get("error") if isinstance(last_resp, dict) else None
                if not err or not isinstance(err, dict) or err.get("code") != -32602:
                    return last_resp

                # 3) content/text envelope
                content_env = {"content": [{"type": "text", "text": json.dumps(av)}]}
                params = {key_name: tool_name, key_args: content_env}
                resp = self._rpc("tools/call", params)
                last_resp = resp if isinstance(resp, dict) else {}
                err = last_resp.get("error") if isinstance(last_resp, dict) else None
                if not err or not isinstance(err, dict) or err.get("code") != -32602:
                    return last_resp
        return last_resp

    def initialize(self):
        params = {
            "protocolVersion": "2025-03-26",
            "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
            "clientInfo": {"name": "dq-oneoff", "version": "0.1.0"},
        }
        self.log.info("Initializing MCP session…")
        self._rpc("initialize", params)
        # Complete handshake with initialized notification (some servers require it)
        try:
            self._rpc("initialized", {"clientCapabilities": {}}, id_=0)
        except Exception:
            pass

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

        # Helper to split FQN into database_name and table_name (e.g., DB.table)
        def parse_table(fqn: str) -> tuple[str | None, str | None]:
            parts = (fqn or "").split(".")
            if len(parts) == 2:
                return parts[0], parts[1]
            if len(parts) == 1:
                return None, parts[0]
            # If more than 2 parts, take last two as db.table
            return parts[-2], parts[-1]

        # Iterate datasets and call known quality tools directly
        for dataset in cfg.get("datasets", []):
            table_fqn = dataset.get("table")
            if not table_fqn:
                continue
            db_name, tbl_name = parse_table(table_fqn)

            # NULL checks: table-level missing + per-column rows with missing
            null_cols = (dataset.get("null_check", {}) or {}).get("columns", [])
            if null_cols:
                # Table-level missing summary (if exposed by server)
                self._call_tool_with_fallbacks("qlty_missingValues", {"database_name": db_name, "table_name": tbl_name})
                for col in null_cols:
                    self._call_tool_with_fallbacks("qlty_rowsWithMissingValues", {"database_name": db_name, "table_name": tbl_name, "column_name": col})

            # RANGE checks: run univariate statistics for each column
            ranges = (dataset.get("range_check", {}) or {}).get("columns", [])
            for rng in ranges:
                col = rng.get("column")
                if not col:
                    continue
                self._call_tool_with_fallbacks("qlty_univariateStatistics", {"database_name": db_name, "table_name": tbl_name, "column_name": col})

            # UNIQUENESS checks: use distinct categories per column (best-effort)
            uniq_cols = (dataset.get("uniqueness_check", {}) or {}).get("columns", [])
            for col in uniq_cols:
                self._call_tool_with_fallbacks("qlty_distinctCategories", {"database_name": db_name, "table_name": tbl_name, "column_name": col})
