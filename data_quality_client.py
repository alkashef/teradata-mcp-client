"""Single-mode CLI entrypoint: LLM-first orchestrated data quality assessment.

Usage:
    python data_quality_client.py --prompt "Assess data quality for schema X"

The orchestrator performs planning, discovery, metric execution, and
summarization using the Model Context Protocol Teradata server plus an LLM.
"""

import argparse
from orchestrator import DataQualityOrchestrator


def main() -> None:
    parser = argparse.ArgumentParser(description="LLM-first Teradata Data Quality Orchestrator")
    parser.add_argument("--prompt", required=True, help="Natural language request (e.g. 'Assess data quality for schema sales.*')")
    args = parser.parse_args()
    orch = DataQualityOrchestrator()
    summary = orch.run_full(args.prompt)
    print(summary)


if __name__ == "__main__":  # pragma: no cover
    main()
