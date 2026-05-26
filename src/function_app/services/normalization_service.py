from __future__ import annotations

from typing import Any

COUNTRY_MAP = {
    "US": "USA",
    "USA": "USA",
    "UNITED STATES": "USA",
    "CA": "CAN",
    "CAN": "CAN",
    "CANADA": "CAN",
    "MX": "MEX",
    "MEX": "MEX",
    "MEXICO": "MEX",
}

TRUE_STRINGS = {"true", "1", "yes", "y"}
FALSE_STRINGS = {"false", "0", "no", "n"}


def normalize_country(value: Any) -> Any:
    if value is None:
        return ""
    normalized = str(value).strip().upper()
    if not normalized:
        return ""
    return COUNTRY_MAP.get(normalized, normalized)


def normalize_bool(value: Any) -> Any:
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return value

    normalized = str(value).strip().lower()
    if normalized in TRUE_STRINGS:
        return True
    if normalized in FALSE_STRINGS:
        return False

    return value


def normalize_string(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return value


def normalize_value(column_name: str, value: Any) -> Any:
    candidate = normalize_string(value)

    if "country" in column_name.lower():
        return normalize_country(candidate)

    return candidate


def normalize_record(record: dict[str, Any], bool_columns: set[str] | None = None) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    bool_column_set = bool_columns or set()

    for column_name, value in record.items():
        normalized_value = normalize_value(column_name, value)
        if column_name in bool_column_set:
            normalized_value = normalize_bool(normalized_value)
        normalized[column_name] = normalized_value

    return normalized
