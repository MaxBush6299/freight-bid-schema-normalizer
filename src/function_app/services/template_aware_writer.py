"""template_aware_writer.py

Writes pricing values back into the original customer bid template,
producing a Customer Submission .xlsx file.

Safety guarantees
-----------------
- Formula cells are NEVER overwritten (raises ProtectedCellError if attempted).
- Token/control rows (rows containing <<tokens>>) are NEVER modified.
- **Pre-existing non-empty cells are NEVER overwritten** (P2-004). The writer
  refuses to touch any cell that already carries a value; the resolver filters
  these out up-front so the writer guard is defense-in-depth.
- Only cells that appear in the declared writable columns of a bid slot are written.
- All other cells in the workbook are preserved byte-for-byte.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable

from openpyxl import load_workbook

from ..models.contracts import (
    CellWriteInstruction,
    FieldMapping,
    InstructionResolutionResult,
    ReverseMappingPlan,
    TemplateProfile,
    WriteReport,
)

_TOKEN_RE = re.compile(r"^<<[^>]+>>", re.IGNORECASE)
_ROUTE_NAME_SOURCE_FIELD = "Origin Note"

# Descriptor cross-validation: (template column substring, export source field).
# Only fields likely to appear identically on both sides are checked; risky
# normalisation pairs (Country, Mode) are deliberately excluded.
_DESCRIPTOR_CHECK_PAIRS: tuple[tuple[str, str], ...] = (
    ("Origin City", "Origin City"),
    ("Origin State", "Origin State"),
    ("Origin Zip", "Origin Zip"),
    ("Origin ZIP", "Origin Zip"),
    ("Destination City", "Destination City"),
    ("Destination State", "Destination State"),
)


class ProtectedCellError(RuntimeError):
    """Raised when a write instruction targets a formula or token-protected cell."""


def _is_empty_cell(value: Any) -> bool:
    """True for cells safe to overwrite (None / "" / whitespace-only).

    Explicitly preserves ``0``, ``0.0`` and ``False`` — those are real
    customer-provided values even though they are falsy in Python.
    """
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    return False


def _normalize_descriptor(value: Any) -> str:
    """Normalise a descriptor value for cross-validation comparison."""
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).strip()).casefold()


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


def _find_template_col(sheet_profile_slot, header_substring: str) -> int | None:
    """Find the 1-based abs col index in ``slot`` whose header contains ``header_substring``
    (case-insensitive). Returns ``None`` if no column matches."""
    needle = header_substring.lower()
    for col_name, abs_idx in zip(sheet_profile_slot.columns, sheet_profile_slot.column_indices):
        if needle in str(col_name).lower():
            return abs_idx
    return None


def _row_descriptor_mismatches(
    ws,
    row_idx: int,
    slot,
    export_row: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return a list of descriptor mismatches between the template row and the
    export row. Empty list = either they all agree or we couldn't compare any."""
    mismatches: list[dict[str, Any]] = []
    for tmpl_substr, exp_field in _DESCRIPTOR_CHECK_PAIRS:
        col_idx = _find_template_col(slot, tmpl_substr)
        if col_idx is None:
            continue
        tmpl_val = ws.cell(row=row_idx, column=col_idx).value
        exp_val = export_row.get(exp_field)
        if _is_empty_cell(tmpl_val) or exp_val is None:
            continue  # cannot validate this pair
        if _normalize_descriptor(tmpl_val) != _normalize_descriptor(exp_val):
            mismatches.append({
                "descriptor": tmpl_substr,
                "template_value": tmpl_val,
                "export_value": exp_val,
            })
    return mismatches


def _is_mapping_eligible(mapping: FieldMapping, auto_write_threshold: float) -> bool:
    """A mapping is eligible to produce writes when its confidence is at/above
    the auto-write threshold OR it was explicitly approved by a human reviewer
    (``needs_review=False`` after pending-review processing)."""
    if mapping.confidence_score >= auto_write_threshold:
        return True
    return not mapping.needs_review


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
        cells_skipped_preserved = 0
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

            # P2-004 defense-in-depth: refuse to overwrite a non-empty cell.
            existing_value = sheet.cell(row=instr.row_index, column=instr.col_index).value
            if not _is_empty_cell(existing_value):
                cells_skipped += 1
                cells_skipped_preserved += 1
                write_log.append({
                    "action": "skipped_preserved",
                    "reason": "cell_already_populated",
                    "sheet": instr.sheet_name,
                    "row": instr.row_index,
                    "col": instr.col_index,
                    "route": instr.source_route_name,
                    "field": instr.source_field,
                    "existing_value": existing_value,
                    "attempted_value": instr.value,
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
            cells_skipped_preserved=cells_skipped_preserved,
            no_bid_lanes=[],  # populated by the pipeline runner
            write_log=write_log,
        )


def _build_export_index(
    export_rows: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Index export rows by ``Origin Note`` (template Route Name).

    Returns ``(index, duplicate_routes)``. If a route appears more than once,
    it is treated as ambiguous: removed from the index and reported in
    ``duplicate_routes`` so the resolver can warn and skip those routes.
    """
    seen: dict[str, dict[str, Any]] = {}
    duplicates: set[str] = set()
    for row in export_rows:
        route = str(row.get(_ROUTE_NAME_SOURCE_FIELD) or "").strip()
        if not route:
            continue
        if route in seen or route in duplicates:
            duplicates.add(route)
            seen.pop(route, None)
            continue
        seen[route] = row
    return seen, sorted(duplicates)


def resolve_instructions(
    export_rows: list[dict[str, Any]],
    template_profile: TemplateProfile,
    plan: ReverseMappingPlan,
    template_path: str | None = None,
    auto_write_threshold: float = 0.0,
    validate_descriptors: bool = True,
) -> InstructionResolutionResult:
    """Translate export rows + mapping plan into concrete CellWriteInstructions.

    The resolver enforces three policies BEFORE handing instructions to the
    writer (the writer also re-checks the preservation invariant as
    defense-in-depth):

    1. **Confidence gating** — mappings with ``confidence_score`` below
       ``auto_write_threshold`` are skipped unless a human reviewer has
       explicitly approved them (``needs_review=False`` after review).
    2. **Per-cell preservation** — when ``template_path`` is provided, the
       resolver peeks at the current cell value; non-empty cells are skipped.
    3. **Per-row descriptor validation** — when ``validate_descriptors=True``
       and ``template_path`` is provided, the resolver compares a small set of
       pre-filled descriptors (Origin City/State/ZIP, Destination City/State)
       in the template against the matched export row. A mismatch on any
       comparable pair skips ALL writes for that row.

    Returns an ``InstructionResolutionResult`` carrying instructions plus skip
    counters. For backwards compatibility, the result yields
    ``(instructions, no_bid_lanes)`` when iterated.
    """
    export_by_route, duplicate_routes = _build_export_index(export_rows)

    # Open the template once (read-only) for preservation + descriptor checks.
    wb = None
    if template_path is not None:
        wb = load_workbook(template_path, data_only=True, read_only=False)

    instructions: list[CellWriteInstruction] = []
    no_bid_lanes: list[str] = []
    seen_no_bid_routes: set[str] = set()
    cells_skipped_preserved = 0
    cells_skipped_low_confidence = 0
    rows_skipped_descriptor_mismatch = 0
    skip_log: list[dict[str, Any]] = []

    # Pre-compute which mappings are eligible by confidence.
    # Mappings whose ``target_column`` is blank are "unmapped" (the planner
    # could not find a sensible template column) — they are dropped silently
    # rather than counted as low-confidence skips, since there is nothing to
    # ever write for them.
    eligible_mappings: list[FieldMapping] = []
    for mapping in plan.mappings:
        if not (mapping.target_column or "").strip():
            continue
        if _is_mapping_eligible(mapping, auto_write_threshold):
            eligible_mappings.append(mapping)
        else:
            cells_skipped_low_confidence += 1  # counted once per mapping, not per cell
            skip_log.append({
                "action": "skipped_low_confidence",
                "reason": "confidence_below_threshold",
                "source_field": mapping.source_field,
                "target_column": mapping.target_column,
                "confidence_score": mapping.confidence_score,
                "auto_write_threshold": auto_write_threshold,
            })

    try:
        for tsp in template_profile.bid_sheets:
            ws = wb[tsp.sheet_name] if wb is not None and tsp.sheet_name in wb.sheetnames else None

            # Build a normalized_header→absolute_col_index lookup for each bid slot
            slot_col_index: dict[tuple[int, str], int] = {}
            slot_by_index: dict[int, Any] = {}
            for slot in tsp.bid_slots:
                slot_by_index[slot.slot_index] = slot
                for col_name, abs_col in zip(slot.columns, slot.column_indices):
                    slot_col_index[(slot.slot_index, _normalize_header(col_name))] = abs_col

            for provenance_entry in tsp.lane_provenance:
                route_name = provenance_entry.route_name
                row_idx = provenance_entry.row_index

                if route_name in duplicate_routes:
                    skip_log.append({
                        "action": "skipped_duplicate_route",
                        "reason": "ambiguous_origin_note",
                        "sheet": tsp.sheet_name,
                        "row": row_idx,
                        "route": route_name,
                    })
                    continue

                if route_name not in export_by_route:
                    if route_name not in seen_no_bid_routes:
                        no_bid_lanes.append(route_name)
                        seen_no_bid_routes.add(route_name)
                    continue

                export_row = export_by_route[route_name]

                # Per-row descriptor validation (uses slot 0 — descriptors live there).
                if validate_descriptors and ws is not None:
                    slot0 = slot_by_index.get(0)
                    if slot0 is not None:
                        mismatches = _row_descriptor_mismatches(ws, row_idx, slot0, export_row)
                        if mismatches:
                            rows_skipped_descriptor_mismatch += 1
                            skip_log.append({
                                "action": "skipped_descriptor_mismatch",
                                "reason": "row_descriptors_disagree_with_export",
                                "sheet": tsp.sheet_name,
                                "row": row_idx,
                                "route": route_name,
                                "mismatches": mismatches,
                            })
                            continue

                for mapping in eligible_mappings:
                    raw_value = export_row.get(mapping.source_field)
                    if raw_value is None:
                        continue

                    value = _apply_transform(raw_value, mapping.value_transform)
                    target_key = (mapping.bid_slot, _normalize_header(mapping.target_column))
                    col_idx = slot_col_index.get(target_key)

                    if col_idx is None:
                        continue  # column not found in this template — skip silently

                    # Per-cell preservation check.
                    if ws is not None:
                        existing = ws.cell(row=row_idx, column=col_idx).value
                        if not _is_empty_cell(existing):
                            cells_skipped_preserved += 1
                            skip_log.append({
                                "action": "skipped_preserved",
                                "reason": "cell_already_populated",
                                "sheet": tsp.sheet_name,
                                "row": row_idx,
                                "col": col_idx,
                                "route": route_name,
                                "field": mapping.source_field,
                                "target_column": mapping.target_column,
                                "existing_value": existing,
                                "attempted_value": value,
                                "confidence_score": mapping.confidence_score,
                            })
                            continue

                    instructions.append(CellWriteInstruction(
                        sheet_name=tsp.sheet_name,
                        row_index=row_idx,
                        col_index=col_idx,
                        value=value,
                        source_route_name=route_name,
                        source_field=mapping.source_field,
                    ))
    finally:
        if wb is not None:
            wb.close()

    return InstructionResolutionResult(
        instructions=instructions,
        no_bid_lanes=no_bid_lanes,
        cells_skipped_preserved=cells_skipped_preserved,
        cells_skipped_low_confidence=cells_skipped_low_confidence,
        rows_skipped_descriptor_mismatch=rows_skipped_descriptor_mismatch,
        duplicate_export_routes=duplicate_routes,
        skip_log=skip_log,
    )


__all__: Iterable[str] = (
    "ProtectedCellError",
    "TemplateAwareWriter",
    "resolve_instructions",
    "_is_empty_cell",
)
