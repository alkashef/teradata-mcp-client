"""LLM-first Teradata MCP Data Quality Orchestrator (single-file version).

Overview:
    This script exposes a single class, :class:`DataQualityOrchestrator`, which
    accepts a natural language prompt describing a Teradata data quality
    assessment objective. It then uses an (optional) LLM to:
      * Parse intent (goal, target patterns, constraints)
      * Plan metadata discovery tool calls
      * Plan quality metric tool calls
      * Summarize collected results into issues & recommendations

    All interaction with the Teradata MCP Server occurs over JSON-RPC via a
    simple HTTP POST endpoint that supports streamable events. Each outbound
    request and raw inbound response body is printed for full transparency.

Seven Orchestration Steps:
    1. ingest_user_prompt        – Capture raw user prompt
    2. derive_intent_with_llm    – Convert unstructured prompt into structured intent
    3. ensure_connection         – Perform `initialize` handshake
    4. discover_schema           – Run LLM-planned metadata inspection tools
    5. run_quality_metrics       – Run LLM-planned quality metric tools
    6. (implicit collection)     – Aggregate per-tool outputs internally
    7. summarize_with_llm        – Produce final human-readable + JSON summary

Environment Variables (.env):
    MCP_ENDPOINT        Base URL for MCP server (e.g. http://localhost:8001/mcp)
    MCP_BEARER_TOKEN    Optional bearer token for auth
    OPENAI_API_KEY      Optional – enables real LLM planning/summarization
    OPENAI_MODEL        Model name (default: gpt-4o-mini)
    OPENAI_BASE_URL     Optional override base URL for OpenAI-compatible APIs

Fallback Behavior:
    If no `OPENAI_API_KEY` is provided, deterministic safe defaults are used:
      * Intent: original prompt as goal, no patterns/constraints
      * Discovery plan: database list + table list
      * Quality plan: selected generic quality tools
      * Summary: basic placeholder summary structure

CLI Usage:
    python data_quality_client.py --prompt "Assess data quality for schema sales.*"

Programmatic Usage:
    from data_quality_client import DataQualityOrchestrator
    orch = DataQualityOrchestrator()
    summary = orch.run_full("Assess data quality for finance tables")
    print(summary)

Limitations / Future Enhancements:
    * Tool result parsing is placeholder; no deep extraction yet.
    * Plans are heuristic and may reference unavailable tool names.
    * Could add adaptive retries based on tool errors.
"""

from __future__ import annotations
from typing import Any, Dict, List
from dataclasses import asdict
import argparse
import logging
import time
import os

from helpers.mcp_client import McpClient
from helpers.llm_planner import LlmPlanner
from helpers.discovery_parser import DiscoveryParser
from helpers.models import Intent, DiscoveryPlan, QualityPlan, DiscoveryResults, Summary, TableProfile, ColumnProfile, QualityResults
from helpers.logging_utils import setup_logging_from_env, log_line, HLINE


class DataQualityOrchestrator:
    """Coordinates high-level data quality workflow using modular helpers.

    Orchestrator delegates:
      * Transport & handshake -> McpClient
      * LLM intent/planning/summarization -> LlmPlanner
      * Discovery parsing -> DiscoveryParser
      * Data shapes -> dataclasses in models.py
    """

    def __init__(self) -> None:
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        self.log = logging.getLogger("dq-orch")
        setup_logging_from_env()
        log_line("[orchestrator] startup")
        log_line(HLINE, with_time=False)
        self.mcp = McpClient()
        self.planner = LlmPlanner()
        self.discovery_parser = DiscoveryParser()
        # State
        self.user_prompt: str | None = None
        self.intent: Intent | None = None
        self.schema_inventory: dict | None = None
        self.tool_inventory: dict | None = None
        self.discovery_plan: DiscoveryPlan | None = None
        self.discovery_results: DiscoveryResults = DiscoveryResults()
        self.quality_plan: QualityPlan | None = None
        self.quality_results: List[Dict[str, Any]] = []
        self.rich_quality = QualityResults()
        self.summary: Summary | None = None
        self.handshake_ok: bool = False

    # ---- Low-level JSON-RPC -------------------------------------------------
    # ---- Step 1 --------------------------------------------------------------
    def ingest_user_prompt(self, prompt: str) -> None:
        """Store raw natural language request.

        Parameters
        ----------
        prompt : str
            Free-form natural language description of the desired DQ assessment.
        """
        self.user_prompt = prompt
        log_line('[step] ingest_user_prompt')
        log_line(HLINE, with_time=False)

    # ---- New Step A: schema inventory --------------------------------------
    def inventory_schema(self) -> dict:
        """Collect list of tables & columns (placeholder until real tool calls).

        Returns
        -------
        dict
            Structure with keys: tables (list[str]), columns (dict[str, list[str]]).
        """
        if self.schema_inventory is not None:
            return self.schema_inventory
        import os, re
        target_db = os.getenv('DATABASE')
        if not target_db:
            uri = os.getenv('DATABASE_URI', '')
            # crude parse: teradata://user:pass@host:port/DB_NAME
            m = re.search(r'/([A-Za-z0-9_]+)$', uri)
            if m:
                target_db = m.group(1)
        tables: list[str] = []
        columns: dict[str, list[str]] = {}
        if target_db:
            try:
                tbl_list = self.mcp.call_tool("base_tableList", {"database_name": target_db})
                if isinstance(tbl_list, dict):
                    r2 = tbl_list.get('result') or {}
                    maybe_tables = r2.get('tables') if isinstance(r2, dict) else []
                    if isinstance(maybe_tables, list):
                        for t in maybe_tables:
                            if isinstance(t, str):
                                tables.append(f"{target_db}.{t}")
            except Exception:
                pass
        self.schema_inventory = {"database": target_db, "tables": tables, "columns": columns}
        log_line('[step] inventory_schema complete')
        log_line(HLINE, with_time=False)
        return self.schema_inventory

    # ---- New Step B: list tools --------------------------------------------
    def inventory_tools(self) -> dict:
        """Retrieve server-declared tools metadata if supported."""
        self.tool_inventory = self.tool_inventory or self.mcp.list_tools()
        log_line('[step] inventory_tools complete')
        log_line(HLINE, with_time=False)
        return self.tool_inventory

    # ---- Step 2 --------------------------------------------------------------
    def derive_intent_with_llm(self) -> Dict[str, Any]:
        """Derive structured intent (goal, target patterns, constraints) via LLM.

        Returns
        -------
        dict
            Intent object with keys: goal (str), target_patterns (list[str]), constraints (list[str]).
        """
        if not self.user_prompt:
            raise ValueError("No user prompt set")
        # Prefer contextual build if inventories gathered
        if self.schema_inventory is not None or self.tool_inventory is not None:
            self.intent = self.planner.build_contextual_intent(
                self.user_prompt,
                self.schema_inventory or {},
                self.tool_inventory or {},
            )
        else:
            self.intent = self.planner.parse_intent(self.user_prompt)
        log_line('[step] derive_intent_with_llm complete')
        log_line(HLINE, with_time=False)
        return asdict(self.intent)

    # ---- Step 3 --------------------------------------------------------------
    def ensure_connection(self) -> Dict[str, Any]:
        """Perform MCP `initialize` handshake only (no secondary call)."""
        resp = self.mcp.initialize()
        # Success heuristic: presence of 'result' and absence of 'error'
        if isinstance(resp, dict) and 'result' in resp and 'error' not in resp:
            self.handshake_ok = True
            log_line('[handshake] initialize success')
        else:
            self.handshake_ok = False
            log_line('[handshake] initialize FAILED – aborting further tool calls')
        log_line('[step] ensure_connection complete')
        log_line(HLINE, with_time=False)
        return resp

    # ---- Step 4 --------------------------------------------------------------
    def discover_schema(self) -> Dict[str, Any]:
        """Execute metadata discovery tools per LLM plan (placeholder parsing).

        Returns
        -------
        dict
            Accumulated discovery results structure (databases, tables, ddl, previews).
        """
        if self.intent is None:
            raise ValueError("Intent must be derived before discovery")
        self.discovery_plan = self.planner.plan_discovery(self.intent)
        available_tools = set()
        if isinstance(self.tool_inventory, dict):
            tl = self.tool_inventory.get('tools') if 'tools' in self.tool_inventory else []
            if isinstance(tl, list):
                for t in tl:
                    if isinstance(t, dict) and 'name' in t:
                        available_tools.add(t['name'])
        filtered_steps = []
        for step in self.discovery_plan.steps:
            if available_tools and step.tool not in available_tools:
                log_line(f"[validation] skipping unknown discovery tool: {step.tool}")
                continue
            filtered_steps.append(step)
        self.discovery_plan.steps = filtered_steps
        for step in self.discovery_plan.steps:
            raw = self.mcp.call_tool(step.tool, {})
            self.discovery_parser.apply(step.tool, raw, self.discovery_results)
        out = {
            "databases": self.discovery_results.databases,
            "tables": self.discovery_results.tables,
            "ddl": self.discovery_results.ddl,
            "previews": self.discovery_results.previews,
        }
        log_line('[step] discover_schema complete')
        log_line(HLINE, with_time=False)
        return out

    # ---- Deterministic discovery (new) --------------------------------------
    def deterministic_discovery(self, target_db: str | None) -> None:
        # List databases if none supplied
        log_line('[discover] start deterministic discovery')
        if not target_db:
            db_resp = self.mcp.call_tool('base_databaseList', {})
            # Simplistic extraction
            rows = (db_resp.get('result') or {}).get('data') if isinstance(db_resp, dict) else []
            if isinstance(rows, list):
                for r in rows:
                    name = r.get('DataBaseName') if isinstance(r, dict) else None
                    if isinstance(name, str):
                        self.discovery_results.databases.append(name)
            if self.discovery_results.databases:
                target_db = self.discovery_results.databases[0]
        if target_db and target_db not in self.discovery_results.databases:
            self.discovery_results.databases.append(target_db)
        # List tables for target_db
        if target_db:
            tbl_resp = self.mcp.call_tool('base_tableList', {'database_name': target_db})
            rows = (tbl_resp.get('result') or {}).get('data') if isinstance(tbl_resp, dict) else []
            max_tables = int(os.getenv('DQ_MAX_TABLES', '10'))
            count = 0
            if isinstance(rows, list):
                for r in rows:
                    name = r.get('TableName') if isinstance(r, dict) else None
                    if isinstance(name, str):
                        fq = f"{target_db}.{name}"
                        self.discovery_results.tables.append(fq)
                        count += 1
                        if count >= max_tables:
                            break
        # DDL for each table
        for fq in self.discovery_results.tables:
            db, table = fq.split('.', 1)
            log_line(f'[discover.ddl] {fq}')
            ddl_resp = self.mcp.call_tool('base_tableDDL', {'database_name': db, 'table_name': table})
            if isinstance(ddl_resp, dict) and 'result' in ddl_resp:
                self.discovery_results.ddl[fq] = 'available'
            log_line('[step] deterministic_discovery complete')
            log_line(HLINE, with_time=False)
            # Initialize profile
            if fq not in self.rich_quality.tables:
                self.rich_quality.tables[fq] = TableProfile(database=db, table=table)
                self.rich_quality.tables[fq].ddl_available = True

    # ---- Column stats collection (table-level summary first) ---------------
    def collect_column_summaries(self) -> None:
        log_line('[quality.columns] collecting column summaries')
        for fq, profile in self.rich_quality.tables.items():
            db, table = profile.database, profile.table
            log_line(f'[quality.columns.table] {fq}')
            col_sum = self.mcp.call_tool('qlty_columnSummary', {'database_name': db, 'table_name': table})
            if isinstance(col_sum, dict) and 'error' in col_sum and col_sum['error'].get('code') == -32602:
                self.rich_quality.skipped.append({'tool': 'qlty_columnSummary', 'table': fq, 'reason': 'invalid params suppressed'})
                continue
            rows = (col_sum.get('result') or {}).get('data') if isinstance(col_sum, dict) else []
            max_cols = int(os.getenv('DQ_MAX_COLUMNS_PER_TABLE', '25'))
            processed = 0
            if isinstance(rows, list):
                for r in rows:
                    cname = r.get('ColumnName') if isinstance(r, dict) else None
                    if not isinstance(cname, str):
                        continue
                    cp = ColumnProfile(name=cname)
                    cp.null_count = r.get('NullCount') if isinstance(r, dict) else None
                    cp.null_pct = r.get('NullPercentage') if isinstance(r, dict) else None
                    profile.columns[cname] = cp
                    processed += 1
                    if processed >= max_cols:
                        break
        log_line('[step] collect_column_summaries complete')
        log_line(HLINE, with_time=False)

    # ---- Column-level quality metrics --------------------------------------
    def run_column_quality(self) -> None:
        log_line('[quality.metrics] start column metrics')
        for fq, profile in self.rich_quality.tables.items():
            db, table = profile.database, profile.table
            log_line(f'[quality.metrics.table] {fq}')
            for cname, colprof in profile.columns.items():
                log_line(f'[quality.metrics.column] {fq}.{cname}')
                # Distinct categories
                d_resp = self.mcp.call_tool('qlty_distinctCategories', {'database_name': db, 'table_name': table, 'column_name': cname})
                if isinstance(d_resp, dict) and 'error' in d_resp and d_resp['error'].get('code') == -32602:
                    self.rich_quality.skipped.append({'tool': 'qlty_distinctCategories', 'table': fq, 'column': cname, 'reason': 'invalid params suppressed'})
                else:
                    d_rows = (d_resp.get('result') or {}).get('data') if isinstance(d_resp, dict) else []
                    if isinstance(d_rows, list):
                        colprof.distinct_count = len(d_rows)
                # Univariate stats
                u_resp = self.mcp.call_tool('qlty_univariateStatistics', {'database_name': db, 'table_name': table, 'column_name': cname})
                if isinstance(u_resp, dict) and 'error' in u_resp and u_resp['error'].get('code') == -32602:
                    self.rich_quality.skipped.append({'tool': 'qlty_univariateStatistics', 'table': fq, 'column': cname, 'reason': 'invalid params suppressed'})
                else:
                    u_rows = (u_resp.get('result') or {}).get('data') if isinstance(u_resp, dict) else []
                    if isinstance(u_rows, list) and u_rows:
                        colprof.stats = u_rows[0] if isinstance(u_rows[0], dict) else {}
                # Missing rows with values
                m_resp = self.mcp.call_tool('qlty_rowsWithMissingValues', {'database_name': db, 'table_name': table, 'column_name': cname})
                if isinstance(m_resp, dict) and 'error' in m_resp and m_resp['error'].get('code') == -32602:
                    self.rich_quality.skipped.append({'tool': 'qlty_rowsWithMissingValues', 'table': fq, 'column': cname, 'reason': 'invalid params suppressed'})
                else:
                    m_rows = (m_resp.get('result') or {}).get('data') if isinstance(m_resp, dict) else []
                    if isinstance(m_rows, list):
                        colprof.missing_rows = len(m_rows)
        log_line('[step] run_column_quality complete')
        log_line(HLINE, with_time=False)

    # ---- Build summary input structure for LLM -----------------------------
    def assemble_quality_summary_input(self) -> list[dict]:
        log_line('[quality.summary_input] assembling')
        summary_input: list[dict] = []
        for fq, profile in self.rich_quality.tables.items():
            table_entry = {
                'table': fq,
                'columns': []
            }
            for cname, cp in profile.columns.items():
                table_entry['columns'].append({
                    'name': cname,
                    'null_count': cp.null_count,
                    'null_pct': cp.null_pct,
                    'distinct_count': cp.distinct_count,
                    'missing_rows': cp.missing_rows,
                    'stats_keys': list(cp.stats.keys()) if cp.stats else []
                })
            summary_input.append(table_entry)
        log_line('[step] assemble_quality_summary_input complete')
        log_line(HLINE, with_time=False)
        return summary_input

    # ---- Step 5 --------------------------------------------------------------
    def run_quality_metrics(self) -> List[Dict[str, Any]]:
        """Call LLM-selected quality tools and collect lightweight metadata.

        Returns
        -------
        list[dict]
            List containing a dict per invoked quality tool (placeholder schema).
        """
        self.quality_plan = self.planner.plan_quality(self.discovery_results)
        available_tools = set()
        if isinstance(self.tool_inventory, dict):
            tl = self.tool_inventory.get('tools') if 'tools' in self.tool_inventory else []
            if isinstance(tl, list):
                for t in tl:
                    if isinstance(t, dict) and 'name' in t:
                        available_tools.add(t['name'])
        filtered_specs = []
        for spec in self.quality_plan.dq_tools:
            if available_tools and spec.tool not in available_tools:
                log_line(f"[validation] skipping unknown quality tool: {spec.tool}")
                continue
            filtered_specs.append(spec)
        self.quality_plan.dq_tools = filtered_specs
        # Provide basic argument injection: attempt database/table context if discoverable
        default_db = None
        if self.schema_inventory and isinstance(self.schema_inventory.get('database'), str):
            default_db = self.schema_inventory['database']
        first_table = None
        if self.discovery_results.tables:
            first_table = self.discovery_results.tables[0]
        for spec in self.quality_plan.dq_tools:
            args: dict[str, Any] = {}
            if default_db:
                args['database_name'] = default_db
            if first_table and 'table' in spec.tool:
                # naive: if tool seems table-oriented include table_name (strip db prefix if present)
                tbl_only = first_table.split('.', 1)[-1]
                args['table_name'] = tbl_only
            raw = self.mcp.call_tool(spec.tool, args)
            self.quality_results.append({"tool": spec.tool, "result": raw.get('result') if isinstance(raw, dict) else None})
        log_line('[step] run_quality_metrics complete')
        log_line(HLINE, with_time=False)
        return self.quality_results

    # ---- Step 7 --------------------------------------------------------------
    def summarize_with_llm(self) -> Dict[str, Any]:
        """Produce LLM-generated summary + recommendations.

        Returns
        -------
        dict
            Summary object with keys: summary (str), issues (list), recommendations (list).
        """
        self.summary = self.planner.interpret_quality(self.quality_results)
        out = {
            "summary": self.summary.summary if self.summary else "",
            "issues": self.summary.issues if self.summary else [],
            "recommendations": self.summary.recommendations if self.summary else [],
        }
        log_line('[step] summarize_with_llm complete')
        log_line(HLINE, with_time=False)
        return out

    # ---- Convenience ---------------------------------------------------------
    def run_full(self, prompt: str) -> Dict[str, Any]:
        """Run all orchestration steps and return final summary.

        Parameters
        ----------
        prompt : str
            Natural language description of data quality assessment goal.

        Returns
        -------
        dict
            Final summary structure (see :meth:`summarize_with_llm`).
        """
        self.ingest_user_prompt(prompt)
        # Handshake first so subsequent inventory/tool listing can rely on session
        self.ensure_connection()
        if not self.handshake_ok:
            # Produce minimal failure summary and stop.
            failure = {
                "summary": "Initialization failed – no further MCP tool calls executed.",
                "issues": ["Handshake with MCP server failed"],
                "recommendations": [
                    "Inspect server logs for initialize validation errors",
                    "Verify protocolVersion and capabilities payload",
                    "Ensure MCP_ENDPOINT is correct and reachable"
                ],
            }
            log_line('[step] run_full abort after handshake failure')
            log_line(HLINE, with_time=False)
            return failure
        # Optional sequencing delay to ensure server session readiness before first tool/list call
        try:
            delay_ms = int(os.getenv('MCP_POST_INIT_DELAY_MS', '150'))
        except ValueError:
            delay_ms = 150
        if delay_ms > 0:
            log_line(f"[handshake] post-init delay {delay_ms}ms")
            time.sleep(delay_ms / 1000.0)
        # Inventory now that handshake completed
        self.inventory_schema()
        self.inventory_tools()
        self.derive_intent_with_llm()
        # Decide deterministic quality path
        is_quality = self.planner.is_quality_request(prompt, self.intent)
        if is_quality:
            target_db = self.schema_inventory.get('database') if isinstance(self.schema_inventory, dict) else None
            self.deterministic_discovery(target_db)
            self.collect_column_summaries()
            self.run_column_quality()
            rich_input = self.assemble_quality_summary_input()
            self.summary = self.planner.interpret_quality(rich_input)
            out = {
                'summary': self.summary.summary if self.summary else '',
                'issues': self.summary.issues if self.summary else [],
                'recommendations': self.summary.recommendations if self.summary else [],
                'profiled_tables': len(self.rich_quality.tables)
            }
            log_line('[step] run_full deterministic path complete')
            log_line(HLINE, with_time=False)
            return out
        else:
            # Fallback legacy path
            self.discover_schema()
            self.run_quality_metrics()
            out = self.summarize_with_llm()
            log_line('[step] run_full legacy path complete')
            log_line(HLINE, with_time=False)
            return out


def main() -> None:
    parser = argparse.ArgumentParser(description="LLM-first Teradata Data Quality Orchestrator")
    parser.add_argument("--prompt", required=True, help="Natural language request (e.g. 'Assess data quality for schema sales.*')")
    args = parser.parse_args()
    orch = DataQualityOrchestrator()
    summary = orch.run_full(args.prompt)
    print(summary)


if __name__ == "__main__":  # pragma: no cover
    main()
