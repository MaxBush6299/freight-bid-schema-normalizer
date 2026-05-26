from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .services.output_writer import normalize_records_to_canonical, write_canonical_csv, write_canonical_xlsx
from .services.template_loader import load_canonical_schema
from .services.validation_service import validate_canonical_records
from .services.workbook_profiler import profile_workbook
from .services.xls_converter import ensure_xlsx


def _pick_value(row: dict[str, Any], candidates: list[str]) -> Any:
    row_upper = {str(key).strip().upper(): value for key, value in row.items()}
    for candidate in candidates:
        candidate_upper = candidate.strip().upper()
        if candidate_upper in row_upper:
            return row_upper[candidate_upper]
    return ""


def _build_smoke_records(profile: Any, max_rows: int = 3) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []

    for sheet in profile.sheets:
        if sheet.likely_business_meaning != "data":
            continue

        for sample_index, sample_row in enumerate(sheet.sample_rows[:max_rows], start=1):
            records.append(
                {
                    "Customer Lane ID": f"SMOKE-{sheet.name[:8]}-{sample_index}",
                    "FO Code": "RXOCode",
                    "Origin City": _pick_value(sample_row, ["ORIGIN CITY", "ORIGIN", "ORIGIN_MARKET"]),
                    "Destination City": _pick_value(sample_row, ["DESTINATION CITY", "DESTINATION", "DEST_MARKET"]),
                    "Origin Country": "US",
                    "Destination Country": "US",
                    "Annual Volume": _pick_value(sample_row, ["SHIPMENT COUNT", "ANNUAL VOLUME", "VOLUME"]),
                }
            )

        if records:
            break

    if not records:
        records.append(
            {
                "Customer Lane ID": "SMOKE-FALLBACK-1",
                "FO Code": "RXOCode",
                "Origin City": "",
                "Destination City": "",
                "Origin Country": "US",
                "Destination Country": "US",
            }
        )

    return records


def run_smoke(input_workbook: str, output_root: str) -> dict[str, Any]:
    schema = load_canonical_schema("src/function_app/templates/canonical_schema.freight_bid_v1.json")
    # TD-001: pre-convert .xls to .xlsx before profiling
    _converted, _did_convert = ensure_xlsx(input_workbook)
    profile = profile_workbook(str(_converted), detect_formulas=not _did_convert)

    run_id = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    run_dir = Path(output_root) / f"smoke_{run_id}"
    run_dir.mkdir(parents=True, exist_ok=True)

    raw_records = _build_smoke_records(profile)
    canonical_records = normalize_records_to_canonical(raw_records, schema)

    csv_path = run_dir / "canonical_output.csv"
    xlsx_path = run_dir / "canonical_output.xlsx"
    profile_path = run_dir / "workbook_profile.json"
    validation_path = run_dir / "validation_report.json"

    write_canonical_csv(canonical_records, schema, str(csv_path))
    write_canonical_xlsx(canonical_records, schema, str(xlsx_path))

    validation_result = validate_canonical_records(canonical_records, schema, include_lineage=True)

    profile_path.write_text(profile.model_dump_json(indent=2), encoding="utf-8")
    validation_path.write_text(json.dumps(validation_result, indent=2), encoding="utf-8")

    return {
        "run_dir": str(run_dir),
        "row_count": len(canonical_records),
        "validation_status": validation_result["status"],
        "validation_errors": validation_result["issue_counts"]["error"],
        "validation_warnings": validation_result["issue_counts"]["warning"],
        "csv": str(csv_path),
        "xlsx": str(xlsx_path),
        "profile": str(profile_path),
        "validation_report": str(validation_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a preliminary deterministic localhost smoke flow.")
    parser.add_argument(
        "--input",
        default="examples/inputs/Input 8.xlsx",
        help="Path to input workbook for profiling.",
    )
    parser.add_argument(
        "--output-root",
        default="artifacts/local_smoke",
        help="Directory where smoke artifacts are written.",
    )
    args = parser.parse_args()

    result = run_smoke(args.input, args.output_root)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
