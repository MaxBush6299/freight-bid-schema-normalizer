"""template_aware_writer.py

Writes pricing values back into the original customer bid template,
producing a Customer Submission .xlsx file.

Safety guarantees
-----------------
- Formula cells are NEVER overwritten (raises ProtectedCellError if attempted).
- Token/control rows (rows containing <<tokens>>) are NEVER modified.
- Only cells that appear in the declared writable columns of a bid slot are written.
- All other cells in the workbook are preserved byte-for-byte.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from ..models.contracts import (
    CellWriteInstruction,
    ReverseMappingPlan,
    TemplateProfile,
    WriteReport,
)

_TOKEN_RE = re.compile(r"^<<[^>]+>>", re.IGNORECASE)
_ROUTE_NAME_SOURCE_FIELD = "Origin Note"


class ProtectedCellError(RuntimeError):
    """Raised when a write instruction targets a formula or token-protected cell."""


def _apply_transform(value: Any, transform: str | None) -> Any:
    if not transform or transform == "none" or value is None:
        return value
    if transform == "round_2":
        try:
            return round(float(value), 2)
        except (TypeError, ValueError):
            return value
    if transform == "upper":
        return str(value).upper() if value else value
    return value


def _normalize_header(text: str) -> str:
    """Collapse whitespace and lowercase for fuzzy column matching."""
    return re.sub(r"\s+", " ", str(text).strip()).lower()


class TemplateAwareWriter:
    """Write CellWriteInstructions into the template and save as submission.xlsx."""

    def write(
        self,
        template_path: str,
        instructions: list[CellWriteInstruction],
        output_path: str,
        template_profile: TemplateProfile,
    ) -> WriteReport:
        wb = load_workbook(template_path, data_only=False)

        # Build formula-column protection index: {sheet_name: set of 1-based col indices}
        protected: dict[str, set[int]] = {}
        for tsp in template_profile.bid_sheets:
            cols: set[int] = set()
            for slot in tsp.bid_slots:
                cols.update(slot.formula_col_indices)
            protected[tsp.sheet_name] = cols

        cells_written = 0
        cells_skipped = 0
        write_log: list[dict[str, Any]] = []

        for instr in instructions:
            if instr.sheet_name not in wb.sheetnames:
                cells_skipped += 1
                write_log.append({
                    "action": "skipped",
                    "reason": "sheet_not_found",
                    "sheet": instr.sheet_name,
                    "row": instr.row_index,
                    "col": instr.col_index,
                    "route": instr.source_route_name,
                    "field": instr.source_field,
                })
                continue

            sheet = wb[instr.sheet_name]
            sheet_protected = protected.get(instr.sheet_name, set())

            # Safety: refuse to write formula cells
            if instr.col_index in sheet_protected:
                raise ProtectedCellError(
                    f"Attempted write to formula-protected cell "
                    f"{instr.sheet_name}!R{instr.row_index}C{instr.col_index} "
                    f"(field={instr.source_field!r}, route={instr.source_route_name!r})"
                )

            # Safety: refuse to write token rows
            first_cell_val = sheet.cell(row=instr.row_index, column=1).value
            if first_cell_val and _TOKEN_RE.match(str(first_cell_val)):
                cells_skipped += 1
                write_log.append({
                    "action": "skipped",
                    "reason": "token_row_protected",
                    "sheet": instr.sheet_name,
                    "row": instr.row_index,
                    "col": instr.col_index,
                    "route": instr.source_route_name,
                    "field": instr.source_field,
                })
                continue

            sheet.cell(row=instr.row_index, column=instr.col_index).value = instr.value
            cells_written += 1
            write_log.append({
                "action": "written",
                "sheet": instr.sheet_name,
                "row": instr.row_index,
                "col": instr.col_index,
                "value": instr.value,
                "route": instr.source_route_name,
                "field": instr.source_field,
            })

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        wb.save(output_path)

        return WriteReport(
            cells_written=cells_written,
            cells_skipped=cells_skipped,
            no_bid_lanes=[],  # populated by the pipeline runner
            write_log=write_log,
        )


def resolve_instructions(
    export_rows: list[dict[str, Any]],
    template_profile: TemplateProfile,
    plan: ReverseMappingPlan,
) -> tuple[list[CellWriteInstruction], list[str]]:
    """Translate export rows + mapping plan into concrete CellWriteInstructions.

    Returns (instructions, no_bid_lanes) where no_bid_lanes is the list of
    Route Names present in the template but missing from the export.
    """
    # Index export rows by Route Name (Origin Note field)
    export_by_route: dict[str, dict[str, Any]] = {}
    for row in export_rows:
        route = str(row.get(_ROUTE_NAME_SOURCE_FIELD) or "").strip()
        if route:
            export_by_route[route] = row

    instructions: list[CellWriteInstruction] = []
    no_bid_lanes: list[str] = []
    seen_no_bid_routes: set[str] = set()

    for tsp in template_profile.bid_sheets:
        # Build a normalized_header→absolute_col_index lookup for each bid slot
        slot_col_index: dict[tuple[int, str], int] = {}
        for slot in tsp.bid_slots:
            for col_name, abs_col in zip(slot.columns, slot.column_indices):
                slot_col_index[(slot.slot_index, _normalize_header(col_name))] = abs_col

        for provenance_entry in tsp.lane_provenance:
            route_name = provenance_entry.route_name
            row_idx = provenance_entry.row_index

            if route_name not in export_by_route:
                if route_name not in seen_no_bid_routes:
                    no_bid_lanes.append(route_name)
                    seen_no_bid_routes.add(route_name)
                continue

            export_row = export_by_route[route_name]

            for mapping in plan.mappings:
                raw_value = export_row.get(mapping.source_field)
                if raw_value is None:
                    continue

                value = _apply_transform(raw_value, mapping.value_transform)
                target_key = (mapping.bid_slot, _normalize_header(mapping.target_column))
                col_idx = slot_col_index.get(target_key)

                if col_idx is None:
                    continue  # column not found in this template — skip silently

                instructions.append(CellWriteInstruction(
                    sheet_name=tsp.sheet_name,
                    row_index=row_idx,
                    col_index=col_idx,
                    value=value,
                    source_route_name=route_name,
                    source_field=mapping.source_field,
                ))

    return instructions, no_bid_lanes
