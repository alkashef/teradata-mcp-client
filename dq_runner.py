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

    def _rpc(self, method: str, params: Dict[str, Any] | None = None, id_: int | str | None = None):
        payload = {"jsonrpc": "2.0", "id": id_ or str(uuid.uuid4()), "method": method}
        if params is not None:
            payload["params"] = params
        r = self.session.post(self.MCP_ENDPOINT, data=json.dumps(payload).encode("utf-8"), timeout=60)
        r.raise_for_status()
        sid = r.headers.get(self.SESSION_ID_HEADER)
        if sid:
            self.session.headers[self.SESSION_ID_HEADER] = sid
        print(r.text)
        sys.exit(0)

    def initialize(self):
        params = {
            "protocolVersion": "2025-03-26",
            "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
            "clientInfo": {"name": "dq-oneoff", "version": "0.1.0"},
        }
        self.log.info("Initializing MCP session…")
        self._rpc("initialize", params)

    def list_tools(self) -> List[Dict[str, Any]]:
        self._rpc("tools/list")

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
        # The rest of the logic is omitted since _rpc always prints and exits
