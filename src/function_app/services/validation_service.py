from __future__ import annotations

from typing import Any

from ..models.contracts import CanonicalSchema
from .validation_rules import (
    calculate_metrics,
    validate_cross_field_consistency,
    validate_enum_values,
    validate_exact_columns,
    validate_null_rate_thresholds,
    validate_required_fields,
    validate_type_consistency,
)

DEFAULT_NULL_RATE_THRESHOLDS: dict[str, float] = {
    "Customer Lane ID": 0.0,
    "FO Code": 0.95,
    "Origin City": 0.95,
    "Destination City": 0.95,
}


def _build_lineage_summary(
    records: list[dict[str, Any]],
    source_row_id_field: str,
) -> dict[str, Any]:
    row_lineage: list[dict[str, Any]] = []
    rows_with_source_id = 0

    for output_row_index, record in enumerate(records, start=1):
        source_row_id = record.get(source_row_id_field)
        if source_row_id not in (None, ""):
            rows_with_source_id += 1

        row_lineage.append(
            {
                "output_row_index": output_row_index,
                "source_row_id": source_row_id,
            }
        )

    total_rows = len(records)
    coverage = (rows_with_source_id / total_rows) if total_rows else 0.0
    return {
        "source_row_id_field": source_row_id_field,
        "rows_with_source_id": rows_with_source_id,
        "row_count": total_rows,
        "coverage": coverage,
        "row_lineage": row_lineage,
    }


def validate_canonical_records(
    records: list[dict[str, Any]],
    template_schema: CanonicalSchema,
    null_rate_thresholds: dict[str, float] | None = None,
    include_lineage: bool = False,
    source_row_id_field: str = "_source_row_id",
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []

    issues.extend(validate_exact_columns(records, template_schema))
    issues.extend(validate_required_fields(records, template_schema))
    issues.extend(validate_type_consistency(records, template_schema))
    issues.extend(validate_enum_values(records))
    issues.extend(validate_cross_field_consistency(records))

    metrics = calculate_metrics(records, template_schema)
    configured_thresholds = null_rate_thresholds or DEFAULT_NULL_RATE_THRESHOLDS
    issues.extend(validate_null_rate_thresholds(metrics, configured_thresholds))

    error_count = sum(1 for issue in issues if issue["severity"] == "error")
    warning_count = sum(1 for issue in issues if issue["severity"] == "warning")

    passed = error_count == 0
    status = "Passed" if passed else "Failed"

    result: dict[str, Any] = {
        "status": status,
        "passed": passed,
        "issues": issues,
        "issue_counts": {
            "error": error_count,
            "warning": warning_count,
        },
        "metrics": metrics,
    }

    if include_lineage:
        result["lineage"] = _build_lineage_summary(records, source_row_id_field)

    return result
