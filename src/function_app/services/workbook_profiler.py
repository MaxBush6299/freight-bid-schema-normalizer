from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any

from openpyxl import load_workbook
from openpyxl.worksheet.worksheet import Worksheet

from ..models.contracts import ColumnGroup, LaneProvenanceEntry, SheetProfile, WorkbookProfile
from .sheet_classifier import classify_sheet

# ── TD-002: Coupa control-token pattern ───────────────────────────────────────
_COUPA_TOKEN_RE = re.compile(r"^<<[^>]+>>", re.IGNORECASE)

# TD-003: <<define>> rows carry the Route Name in column D (index 3, 0-based)
_COUPA_DEFINE_RE = re.compile(r"^<<\s*define\s*>>", re.IGNORECASE)

# TD-005: hidden "Lot Name" sentinel that delimits repeating bid-slot groups
_LOT_NAME_RE = re.compile(r"lot\s*name", re.IGNORECASE)

# TD-007: validationInfo sheet name
_VALIDATION_INFO_NAME_RE = re.compile(r"validationinfo", re.IGNORECASE)


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _infer_type(values: list[Any]) -> str:
    non_null_values = [value for value in values if value not in (None, "")]
    if not non_null_values:
        return "unknown"

    if all(isinstance(value, bool) for value in non_null_values):
        return "bool"
    if all(isinstance(value, int) and not isinstance(value, bool) for value in non_null_values):
        return "int"
    if all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in non_null_values):
        return "float"

    return "str"


def _is_coupa_token_row(row_values: list[Any]) -> bool:
    """TD-002: Return True if the first non-empty cell is a Coupa control token."""
    for value in row_values:
        text = _stringify(value)
        if text:
            return bool(_COUPA_TOKEN_RE.match(text))
    return False


def _header_score(row_values: list[Any]) -> int:
    normalized = [_stringify(value) for value in row_values]
    non_empty = [value for value in normalized if value]
    if not non_empty:
        return -1
    # TD-002: never score a Coupa token row as a header
    if _COUPA_TOKEN_RE.match(non_empty[0]):
        return -1

    unique_count = len(set(non_empty))
    alpha_count = len([value for value in non_empty if any(character.isalpha() for character in value)])
    return (len(non_empty) * 2) + unique_count + alpha_count


def _detect_header_row_candidates(sheet: Worksheet, scan_limit: int = 30) -> list[int]:
    candidates: list[tuple[int, int]] = []
    max_scan_row = min(sheet.max_row or 1, scan_limit)

    for row_index in range(1, max_scan_row + 1):
        row_values = [
            sheet.cell(row=row_index, column=column_index).value
            for column_index in range(1, (sheet.max_column or 1) + 1)
        ]
        score = _header_score(row_values)
        if score > 0:
            candidates.append((row_index, score))

    candidates.sort(key=lambda item: item[1], reverse=True)
    return [row_index for row_index, _ in candidates[:3]]


def _extract_columns(sheet: Worksheet, header_row: int) -> list[str]:
    columns: list[str] = []
    for column_index in range(1, (sheet.max_column or 1) + 1):
        header_value = _stringify(sheet.cell(row=header_row, column=column_index).value)
        if header_value:
            columns.append(header_value)
    return columns


def _extract_control_rows(sheet: Worksheet, scan_limit: int = 30) -> list[dict[str, Any]]:
    """TD-002: Collect all Coupa token rows before the real header."""
    control_rows: list[dict[str, Any]] = []
    max_scan = min(sheet.max_row or 1, scan_limit)
    for row_index in range(1, max_scan + 1):
        row_values = [
            sheet.cell(row=row_index, column=col_idx).value
            for col_idx in range(1, (sheet.max_column or 1) + 1)
        ]
        if _is_coupa_token_row(row_values):
            control_rows.append({"row_index": row_index, "values": row_values})
    return control_rows


def _extract_sample_rows(
    sheet: Worksheet,
    header_row: int,
    columns: list[str],
    sample_size: int = 10,
) -> list[dict[str, Any]]:
    sample_rows: list[dict[str, Any]] = []
    if not columns:
        return sample_rows

    for row_index in range(header_row + 1, (sheet.max_row or header_row) + 1):
        row_data: dict[str, Any] = {}
        has_value = False

        for column_index, column_name in enumerate(columns, start=1):
            value = sheet.cell(row=row_index, column=column_index).value
            row_data[column_name] = value
            if value not in (None, ""):
                has_value = True

        if has_value:
            sample_rows.append(row_data)
        if len(sample_rows) >= sample_size:
            break

    return sample_rows


def _find_duplicate_headers(columns: list[str]) -> list[str]:
    counts = Counter([column.strip().lower() for column in columns if column.strip()])
    duplicates = [name for name, count in counts.items() if count > 1]
    return sorted(duplicates)


def _calculate_empty_column_ratio(columns: list[str], sample_rows: list[dict[str, Any]]) -> float:
    if not columns:
        return 1.0
    if not sample_rows:
        return 1.0

    empty_columns = 0
    for column in columns:
        if all(row.get(column) in (None, "") for row in sample_rows):
            empty_columns += 1

    return empty_columns / len(columns)


def _extract_lane_provenance(sheet: Worksheet, sheet_name: str) -> list[LaneProvenanceEntry]:
    """TD-003: Scan for <<define>> rows and return one entry per Route Name.

    The <<define>> token appears in column 3 (C) of the Coupa template, with
    Route Name in column 4 (D). We scan the first non-empty cell in each row
    to locate the token rather than assuming column 1.
    """
    entries: list[LaneProvenanceEntry] = []
    max_row = sheet.max_row or 1
    for row_index in range(1, max_row + 1):
        # Scan the first few columns for the <<define>> token
        for col_idx in range(1, 6):
            cell_text = _stringify(sheet.cell(row=row_index, column=col_idx).value)
            if _COUPA_DEFINE_RE.match(cell_text):
                # Route Name is one column to the right of <<define>>
                route_name = _stringify(sheet.cell(row=row_index, column=col_idx + 1).value)
                if route_name:
                    entries.append(LaneProvenanceEntry(
                        route_name=route_name,
                        sheet_name=sheet_name,
                        row_index=row_index,
                    ))
                break
    return entries


def _detect_column_groups(sheet: Worksheet, header_row: int) -> list[ColumnGroup]:
    """TD-005: Detect repeating bid-slot column groups separated by hidden Lot Name sentinels."""
    if not header_row:
        return []

    max_col = sheet.max_column or 1
    groups: list[ColumnGroup] = []
    current_group_cols: list[str] = []
    current_group_indices: list[int] = []
    current_group_start: int | None = None
    group_index = 0

    for col_idx in range(1, max_col + 1):
        header_val = _stringify(sheet.cell(row=header_row, column=col_idx).value)

        if _LOT_NAME_RE.search(header_val):
            # Sentinel column — start a new group
            if current_group_cols and current_group_start is not None:
                groups.append(ColumnGroup(
                    group_index=group_index,
                    lot_name=header_val,
                    col_offset=current_group_start,
                    columns=list(current_group_cols),
                    column_indices=list(current_group_indices),
                ))
                group_index += 1
            current_group_cols = []
            current_group_indices = []
            current_group_start = None
        else:
            if header_val:
                if current_group_start is None:
                    current_group_start = col_idx
                current_group_cols.append(header_val)
                current_group_indices.append(col_idx)

    # Flush last group
    if current_group_cols and current_group_start is not None:
        groups.append(ColumnGroup(
            group_index=group_index,
            lot_name="",
            col_offset=current_group_start,
            columns=current_group_cols,
            column_indices=current_group_indices,
        ))

    # Only return groups if we found more than one (otherwise it's not a repeating structure)
    return groups if len(groups) > 1 else []


def _parse_validation_info(sheet: Worksheet) -> dict[str, list[str]]:
    """TD-007: Parse the validationInfo sheet into a dropdown catalog dict."""
    catalog: dict[str, list[str]] = {}
    if sheet is None:
        return catalog

    max_col = sheet.max_column or 1
    for col_idx in range(1, max_col + 1):
        header = _stringify(sheet.cell(row=1, column=col_idx).value)
        if not header:
            continue
        values: list[str] = []
        for row_idx in range(2, (sheet.max_row or 1) + 1):
            val = _stringify(sheet.cell(row=row_idx, column=col_idx).value)
            if val:
                values.append(val)
        if values:
            catalog[header] = values
    return catalog


def _detect_all_formula_columns(workbook_path: str, sheet_names: list[str]) -> dict[str, list[int]]:
    """TD-008: Single-pass formula detection across all sheets.

    Opens the workbook once with data_only=False and scans the first 60 rows
    of each requested sheet using iter_rows() (efficient for ReadOnlyWorksheet).
    Returns {sheet_name: [1-based col indices]}.

    Only rows where col A does NOT contain a Coupa control token are considered.
    This prevents control/banner rows (<<hidecolumn>>, <<bid|...>>, etc.) from
    tainting input columns with template-behaviour formulas.

    NOTE: Only meaningful for native .xlsx files. Files converted from .xls
    via xlrd will never contain formula strings, so callers should pass an
    empty sheet_names list to skip this pass entirely.
    """
    result: dict[str, list[int]] = {name: [] for name in sheet_names}
    if not sheet_names:
        return result
    try:
        wb = load_workbook(workbook_path, data_only=False, read_only=True)
        for sheet_name in sheet_names:
            if sheet_name not in wb.sheetnames:
                continue
            sheet = wb[sheet_name]
            formula_cols: set[int] = set()
            for row in sheet.iter_rows(max_row=60):
                # Skip control rows (col A contains a Coupa token)
                col_a_val = row[0].value if row else None
                if col_a_val is not None and isinstance(col_a_val, str) and _COUPA_TOKEN_RE.match(col_a_val):
                    continue
                for cell in row:
                    if isinstance(cell.value, str) and cell.value.startswith("="):
                        formula_cols.add(cell.column)
            result[sheet_name] = sorted(formula_cols)
        wb.close()
    except Exception:
        pass
    return result


def profile_workbook(workbook_path: str, sample_size: int = 10, detect_formulas: bool = True) -> WorkbookProfile:
    # Load without read_only so .cell(row, col) access is O(1) throughout all helpers.
    workbook = load_workbook(workbook_path, data_only=True)
    try:
        sheet_profiles: list[SheetProfile] = []
        all_provenance: list[LaneProvenanceEntry] = []
        dropdown_catalog: dict[str, list[str]] = {}

        # TD-007: parse validationInfo — reuse the already-open workbook
        for ws_name in workbook.sheetnames:
            if _VALIDATION_INFO_NAME_RE.match(ws_name):
                dropdown_catalog = _parse_validation_info(workbook[ws_name])
                break

        # TD-008: single-pass formula detection (skipped for xls-converted files —
        # xlrd strips formulas to values, so the pass would always return empty).
        # Uses a separate open with data_only=False so formula strings are visible.
        all_sheet_names = [ws.title for ws in workbook.worksheets]
        formula_columns_by_sheet = _detect_all_formula_columns(
            workbook_path, all_sheet_names if detect_formulas else []
        )

        for sheet in workbook.worksheets:
            # TD-002: collect control rows before header detection
            control_rows = _extract_control_rows(sheet)

            header_candidates = _detect_header_row_candidates(sheet)
            selected_header = header_candidates[0] if header_candidates else 1

            columns = _extract_columns(sheet, selected_header)
            sample_rows = _extract_sample_rows(sheet, selected_header, columns, sample_size=sample_size)

            inferred_types: dict[str, str] = {}
            for column in columns:
                values = [row.get(column) for row in sample_rows]
                inferred_types[column] = _infer_type(values)

            classification = classify_sheet(sheet.title, columns, sample_rows, control_rows=control_rows)
            duplicate_headers = _find_duplicate_headers(columns)
            empty_ratio = _calculate_empty_column_ratio(columns, sample_rows)

            # TD-003: extract provenance from Coupa bid sheets
            provenance: list[LaneProvenanceEntry] = []
            if classification.get("coupa_sheet_type") == "coupa_bid_data":
                provenance = _extract_lane_provenance(sheet, sheet.title)
                all_provenance.extend(provenance)

            # TD-005: detect repeating bid-slot column groups
            column_groups = _detect_column_groups(sheet, selected_header)

            # TD-008: formula columns from the pre-computed single-pass map
            formula_columns = formula_columns_by_sheet.get(sheet.title, [])

            notes = f"classification_score={classification['score']}"
            profile = SheetProfile(
                name=sheet.title,
                visible=(sheet.sheet_state == "visible"),
                used_range=sheet.calculate_dimension(),
                header_row=selected_header,
                header_row_candidates=header_candidates,
                columns=columns,
                inferred_types=inferred_types,
                sample_rows=sample_rows,
                duplicate_headers=duplicate_headers,
                empty_column_ratio=empty_ratio,
                likely_business_meaning=classification["business_meaning"],
                classifier_hints=classification["hints"],
                notes=notes,
                control_rows=control_rows,
                coupa_sheet_type=classification.get("coupa_sheet_type"),
                column_groups=column_groups,
                formula_columns=formula_columns,
            )
            sheet_profiles.append(profile)

        workbook_name = Path(workbook_path).name
        return WorkbookProfile(
            workbook_name=workbook_name,
            sheets=sheet_profiles,
            notes=f"profiled_sheet_count={len(sheet_profiles)}",
            lane_provenance=all_provenance,
            dropdown_catalog=dropdown_catalog,
        )
    finally:
        workbook.close()
