from __future__ import annotations

from typing import Any

from ..models.contracts import CanonicalSchema, CanonicalSchemaColumn

DEFAULT_ENUM_VALUES: dict[str, set[str]] = {
    "Origin Country": {"USA", "CAN", "MEX"},
    "Destination Country": {"USA", "CAN", "MEX"},
    "Border Crossing Country": {"USA", "CAN", "MEX"},
    "FSC Type": {"PerMileAmount", "Percentage"},
    "Rate Type": {"LineHaul", "AllIn", "PerMile", "Flat"},
}


def is_empty(value: Any) -> bool:
    return value is None or value == ""


def _is_valid_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"true", "false", "yes", "no", "1", "0", "y", "n"}
    return False


def _is_valid_int(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, str):
        try:
            int(value.strip())
            return True
        except ValueError:
            return False
    return False


def _is_valid_float(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        try:
            float(value.strip())
            return True
        except ValueError:
            return False
    return False


def _is_valid_type(value: Any, column: CanonicalSchemaColumn) -> bool:
    if is_empty(value):
        return True

    dtype = column.dtype.strip().lower()
    if dtype == "str":
        return isinstance(value, str)
    if dtype == "bool":
        return _is_valid_bool(value)
    if dtype == "int":
        return _is_valid_int(value)
    if dtype == "float":
        return _is_valid_float(value)

    return True


def validate_exact_columns(
    records: list[dict[str, Any]],
    template_schema: CanonicalSchema,
) -> list[dict[str, Any]]:
    canonical_columns = [column.name for column in template_schema.columns]
    canonical_set = set(canonical_columns)
    issues: list[dict[str, Any]] = []

    for row_index, record in enumerate(records, start=1):
        record_set = set(record.keys())
        missing = sorted(canonical_set - record_set)
        extra = sorted(record_set - canonical_set)

        if missing:
            issues.append(
                {
                    "code": "missing_columns",
                    "severity": "error",
                    "row_index": row_index,
                    "columns": missing,
                    "message": "Record is missing canonical columns.",
                }
            )
        if extra:
            issues.append(
                {
                    "code": "extra_columns",
                    "severity": "error",
                    "row_index": row_index,
                    "columns": extra,
                    "message": "Record contains non-canonical columns.",
                }
            )

    return issues


def validate_required_fields(
    records: list[dict[str, Any]],
    template_schema: CanonicalSchema,
) -> list[dict[str, Any]]:
    required_columns = [column.name for column in template_schema.columns if column.required]
    issues: list[dict[str, Any]] = []

    for row_index, record in enumerate(records, start=1):
        for column_name in required_columns:
            if is_empty(record.get(column_name)):
                issues.append(
                    {
                        "code": "required_missing",
                        "severity": "error",
                        "row_index": row_index,
                        "column": column_name,
                        "message": "Required field is empty.",
                    }
                )

    return issues


def validate_type_consistency(
    records: list[dict[str, Any]],
    template_schema: CanonicalSchema,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []

    for row_index, record in enumerate(records, start=1):
        for column in template_schema.columns:
            value = record.get(column.name)
            if not _is_valid_type(value, column):
                issues.append(
                    {
                        "code": "type_mismatch",
                        "severity": "error",
                        "row_index": row_index,
                        "column": column.name,
                        "expected_type": column.dtype,
                        "value": value,
                        "message": "Field value does not match expected data type.",
                    }
                )

    return issues


def validate_enum_values(
    records: list[dict[str, Any]],
    enum_values: dict[str, set[str]] | None = None,
) -> list[dict[str, Any]]:
    configured_enums = enum_values or DEFAULT_ENUM_VALUES
    issues: list[dict[str, Any]] = []

    for row_index, record in enumerate(records, start=1):
        for column_name, allowed_values in configured_enums.items():
            value = record.get(column_name)
            if is_empty(value):
                continue

            value_str = str(value).strip()
            if value_str not in allowed_values:
                issues.append(
                    {
                        "code": "enum_invalid",
                        "severity": "error",
                        "row_index": row_index,
                        "column": column_name,
                        "value": value,
                        "allowed_values": sorted(allowed_values),
                        "message": "Field value is outside the allowed enum set.",
                    }
                )

    return issues


def validate_cross_field_consistency(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []

    pairs_to_check = [
        ("Origin City", "Origin Country"),
        ("Destination City", "Destination Country"),
        ("Border Crossing City", "Border Crossing Country"),
    ]

    for row_index, record in enumerate(records, start=1):
        for primary_field, dependent_field in pairs_to_check:
            primary_value = record.get(primary_field)
            dependent_value = record.get(dependent_field)

            if not is_empty(primary_value) and is_empty(dependent_value):
                issues.append(
                    {
                        "code": "cross_field_missing_pair",
                        "severity": "warning",
                        "row_index": row_index,
                        "field": dependent_field,
                        "related_field": primary_field,
                        "message": "Related field is empty while dependent field has a value.",
                    }
                )

    return issues


def calculate_metrics(
    records: list[dict[str, Any]],
    template_schema: CanonicalSchema,
) -> dict[str, Any]:
    columns = [column.name for column in template_schema.columns]
    row_count = len(records)

    if row_count == 0:
        null_rates = {column: 1.0 for column in columns}
    else:
        null_rates = {}
        for column in columns:
            empty_count = sum(1 for row in records if is_empty(row.get(column)))
            null_rates[column] = empty_count / row_count

    return {
        "row_count": row_count,
        "column_count": len(columns),
        "null_rates": null_rates,
    }


def validate_null_rate_thresholds(
    metrics: dict[str, Any],
    thresholds: dict[str, float],
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    null_rates = metrics.get("null_rates", {})

    for column_name, threshold in thresholds.items():
        if column_name not in null_rates:
            continue
        null_rate = null_rates[column_name]
        if null_rate > threshold:
            issues.append(
                {
                    "code": "null_rate_exceeded",
                    "severity": "warning",
                    "column": column_name,
                    "null_rate": null_rate,
                    "threshold": threshold,
                    "message": "Column null rate exceeded configured threshold.",
                }
            )

    return issues
