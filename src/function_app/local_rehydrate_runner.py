"""local_rehydrate_runner.py

Local entry point for the Rehydrate (reverse) pipeline.

Usage
-----
    python -m src.function_app.local_rehydrate_runner \\
        --template  "source_docs/Original Customer File 1.xlsx" \\
        --export    "source_docs/FTL LaneExport.xlsx" \\
        --output-root artifacts/local_rehydrate

Outputs (written to <output-root>/<run_id>/)
--------------------------------------------
    submission.xlsx        — original template with pricing filled in
    write_report.json      — cells written / skipped / no-bid lanes
    template_profile.json  — TemplateProfile used for this run
    mapping_plan.json      — ReverseMappingPlan used for this run
"""
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from .services.reverse_planner import ReversePlanner
from .services.template_aware_writer import TemplateAwareWriter, resolve_instructions
from .services.template_profiler import TemplateProfiler
from .services.xls_converter import ensure_xlsx


def _load_export_rows(export_path: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Load all rows from the priced export file as dicts keyed by header."""
    xlsx_path, _did_convert = ensure_xlsx(export_path)
    wb = load_workbook(str(xlsx_path), data_only=True, read_only=True)
    try:
        sheet = wb.active
        rows = list(sheet.iter_rows(values_only=True))
        if not rows:
            return [], []
        headers = [str(h).strip() if h is not None else "" for h in rows[0]]
        records = [
            {headers[i]: row[i] for i in range(len(headers)) if headers[i]}
            for row in rows[1:]
            if any(v is not None for v in row)
        ]
        return records, headers
    finally:
        wb.close()


def run_rehydrate(
    template_path: str,
    export_path: str,
    output_root: str,
    planner_mode: str = "mock",
) -> dict[str, Any]:
    run_id = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    run_dir = Path(output_root) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # 1. Convert template .xls → .xlsx if needed
    template_xlsx, _did_convert = ensure_xlsx(template_path)

    # 2. Profile the template
    profiler = TemplateProfiler()
    template_profile = profiler.profile(str(template_xlsx))

    # 3. Load priced export rows
    export_rows, export_columns = _load_export_rows(export_path)

    # 4. Build mapping plan
    planner = ReversePlanner(mode=planner_mode)
    plan = planner.build_plan(template_profile, export_columns)

    # 5. Resolve CellWriteInstructions
    instructions, no_bid_lanes = resolve_instructions(export_rows, template_profile, plan)

    # 6. Write submission
    submission_path = str(run_dir / "submission.xlsx")
    writer = TemplateAwareWriter()
    write_report = writer.write(
        template_path=str(template_xlsx),
        instructions=instructions,
        output_path=submission_path,
        template_profile=template_profile,
    )
    write_report.no_bid_lanes = no_bid_lanes

    # 7. Emit artifacts
    profile_path = run_dir / "template_profile.json"
    plan_path = run_dir / "mapping_plan.json"
    report_path = run_dir / "write_report.json"

    profile_path.write_text(template_profile.model_dump_json(indent=2), encoding="utf-8")
    plan_path.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    report_path.write_text(write_report.model_dump_json(indent=2), encoding="utf-8")

    return {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "bid_sheets_profiled": len(template_profile.bid_sheets),
        "export_rows_loaded": len(export_rows),
        "instructions_resolved": len(instructions),
        "cells_written": write_report.cells_written,
        "cells_skipped": write_report.cells_skipped,
        "no_bid_lanes": len(no_bid_lanes),
        "no_bid_lane_names": no_bid_lanes,
        "submission": submission_path,
        "write_report": str(report_path),
        "template_profile": str(profile_path),
        "mapping_plan": str(plan_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the Rehydrate (reverse) pipeline locally."
    )
    parser.add_argument(
        "--template",
        required=True,
        help="Path to the original customer template (.xls or .xlsx).",
    )
    parser.add_argument(
        "--export",
        required=True,
        help="Path to the priced export file (FTL LaneExport.xlsx).",
    )
    parser.add_argument(
        "--output-root",
        default="artifacts/local_rehydrate",
        help="Directory where rehydrate artifacts are written.",
    )
    parser.add_argument(
        "--planner-mode",
        default="mock",
        choices=["mock", "live"],
        help="ReversePlanner mode: 'mock' (default) or 'live' (Foundry agent).",
    )
    args = parser.parse_args()

    result = run_rehydrate(
        template_path=args.template,
        export_path=args.export,
        output_root=args.output_root,
        planner_mode=args.planner_mode,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
