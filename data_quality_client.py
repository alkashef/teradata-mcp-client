#!/usr/bin/env python3
"""
One-off data quality runner for Teradata MCP Server (Streamable HTTP).
- Discovers qlty tools via tools/list
- Runs nulls, ranges, uniqueness checks from dq_config.yml
- Logs to console; emits JSON result to stdout
"""
import os, sys, json, uuid, time, logging
from typing import Any, Dict, List
import requests
from dotenv import load_dotenv
import yaml

# ---------- setup ----------
load_dotenv()
MCP_ENDPOINT = os.getenv("MCP_ENDPOINT", "").rstrip("/")
AUTH_BEARER  = os.getenv("MCP_BEARER_TOKEN", "")
SESSION_ID_HEADER = "Mcp-Session-Id"

if not MCP_ENDPOINT:
    print("ERROR: MCP_ENDPOINT not set in .env", file=sys.stderr)
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("dq")

session = requests.Session()
session.headers.update({
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
})
if AUTH_BEARER:
    session.headers["Authorization"] = f"Bearer {AUTH_BEARER}"

def _rpc(method: str, params: Dict[str, Any] | None = None, id_: int | str | None = None):
    """Send request to MCP server and print response as-is, no JSON parsing/checking."""
    payload = {"jsonrpc": "2.0", "id": id_ or str(uuid.uuid4()), "method": method}
    if params is not None:
        payload["params"] = params
    r = session.post(MCP_ENDPOINT, data=json.dumps(payload).encode("utf-8"), timeout=60)
    r.raise_for_status()
    sid = r.headers.get(SESSION_ID_HEADER)
    if sid:
        session.headers[SESSION_ID_HEADER] = sid
    print(r.text)
    sys.exit(0)

# ---------- lifecycle: initialize ----------
def initialize():
    # Per spec, initialization MUST be first. Keep capabilities minimal for tool usage.
    params = {
        "protocolVersion": "2025-03-26",
        "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
        "clientInfo": {"name": "dq-oneoff", "version": "0.1.0"},
    }
    log.info("Initializing MCP session…")
    resp = _rpc("initialize", params)
    if "error" in resp:
        raise RuntimeError(f"initialize error: {resp['error']}")
    # Optional 'initialized' notification (no response expected)
    try:
        _rpc("initialized", {"clientCapabilities": {}}, id_=0)
    except Exception:
        # some servers return 405/ignore notifications over HTTP; safe to continue
        pass

# ---------- discovery ----------
def list_tools() -> List[Dict[str, Any]]:
    resp = _rpc("tools/list")
    if "error" in resp:
        raise RuntimeError(f"tools/list error: {resp['error']}")
    result = resp.get("result") or resp  # tolerate variants
    tools = result.get("tools", result.get("result", []))
    return tools

def pick_quality_tools(tools: List[Dict[str, Any]]):
    """
    Heuristics: look for names or descriptions including:
    - 'qlty' or 'quality'
    - 'null', 'missing'
    - 'range'
    - 'unique', 'distinct'
    """
    def has_kw(t: Dict[str, Any], kws: List[str]) -> bool:
        name = (t.get("name") or "").lower()
        desc = (t.get("description") or "").lower()
        return any(k in name or k in desc for k in kws)

    t_null  = [t for t in tools if has_kw(t, ["qlty", "quality"]) and has_kw(t, ["null", "missing"])]
    t_range = [t for t in tools if has_kw(t, ["qlty", "quality"]) and has_kw(t, ["range", "min", "max"])]
    t_uniq  = [t for t in tools if has_kw(t, ["qlty", "quality"]) and has_kw(t, ["unique", "uniqueness", "distinct"])]

    # fallback: any module explicitly named qlty
    if not any([t_null, t_range, t_uniq]):
        t_null  = [t for t in tools if has_kw(t, ["qlty"]) and has_kw(t, ["null"])]
        t_range = [t for t in tools if has_kw(t, ["qlty"]) and has_kw(t, ["range"])]
        t_uniq  = [t for t in tools if has_kw(t, ["qlty"]) and has_kw(t, ["unique", "distinct"])]

    # choose first match for each category (server typically exposes a single tool per check type)
    return (
        (t_null[0]  if t_null  else None),
        (t_range[0] if t_range else None),
        (t_uniq[0]  if t_uniq  else None),
    )

def tool_args_from_schema(tool: Dict[str, Any], proposed: Dict[str, Any]) -> Dict[str, Any]:
    """
    Align provided args with tool.inputSchema if present.
    This keeps us robust to different param names in the repo.
    """
    schema = tool.get("inputSchema") or {}
    props = (schema.get("properties") or {}) if schema.get("type") == "object" else {}
    args = {}
    for k, v in proposed.items():
        if not props or k in props:
            args[k] = v
    # fill required fields if obvious (table / database)
    for k in (schema.get("required") or []):
        if k not in args:
            # best-effort: pull from env defaults
            if k.upper() in os.environ:
                args[k] = os.environ[k.upper()]
    return args

def call_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    resp = _rpc("tools/call", {"name": name, "arguments": arguments})
    if "error" in resp:
        raise RuntimeError(f"tools/call error: {resp['error']}")
    return resp.get("result") or resp.get("result", {})

# ---------- run checks ----------
def load_config(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}

def main():
    cfg_path = os.getenv("DQ_CONFIG", "dq_config.yml")
    cfg = load_config(cfg_path)
    initialize()

    tools = list_tools()
    log.info(f"Discovered {len(tools)} tools from server.")
    t_null, t_range, t_uniq = pick_quality_tools(tools)

    if not any([t_null, t_range, t_uniq]):
        print(json.dumps({"error": "No Data Quality tools found in server"}, indent=2))
        sys.exit(2)

    results: Dict[str, Any] = {
        "run_id": str(uuid.uuid4()),
        "server": MCP_ENDPOINT,
        "timestamp": int(time.time()),
        "checks": [],
    }

    # iterate datasets
    for dataset in cfg.get("datasets", []):
        table_fqn = dataset.get("table")  # e.g., database.schema.table OR schema.table
        if not table_fqn:
            continue
        schema = dataset.get("schema")  # optional, to assist tools
        database = dataset.get("database")

        # 1) NULLS
        null_cols = dataset.get("null_check", {}).get("columns", [])
        if t_null and null_cols:
            args = {"table": table_fqn, "columns": null_cols}
            if schema: args["schema"] = schema
            if database: args["database"] = database
            args = tool_args_from_schema(t_null, args)
            log.info(f"NULL check -> tool={t_null['name']} table={table_fqn} cols={len(null_cols)}")
            res = call_tool(t_null["name"], args)
            results["checks"].append({"type": "nulls", "table": table_fqn, "result": res})

        # 2) RANGE
        ranges = dataset.get("range_check", {}).get("columns", [])
        if t_range and ranges:
            for rng in ranges:
                col = rng.get("column")
                if not col: continue
                args = {
                    "table": table_fqn,
                    "column": col,
                    "min": rng.get("min"),
                    "max": rng.get("max"),
                    "inclusive": rng.get("inclusive", True),
                }
                if schema: args["schema"] = schema
                if database: args["database"] = database
                args = tool_args_from_schema(t_range, args)
                log.info(f"RANGE check -> tool={t_range['name']} table={table_fqn} col={col}")
                res = call_tool(t_range["name"], args)
                results["checks"].append({"type": "range", "table": table_fqn, "column": col, "result": res})

        # 3) UNIQUENESS
        uniq = dataset.get("uniqueness_check", {})
        uniq_cols = uniq.get("columns", [])
        if t_uniq and uniq_cols:
            args = {
                "table": table_fqn,
                "columns": uniq_cols,
                "approximate": uniq.get("approximate", False),
            }
            if schema: args["schema"] = schema
            if database: args["database"] = database
            args = tool_args_from_schema(t_uniq, args)
            log.info(f"UNIQUENESS check -> tool={t_uniq['name']} table={table_fqn} cols={len(uniq_cols)}")
            res = call_tool(t_uniq["name"], args)
            results["checks"].append({"type": "uniqueness", "table": table_fqn, "result": res})

    # stdout JSON only
    print(json.dumps(results, indent=2, ensure_ascii=False))

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(str(e))
        sys.exit(1)
