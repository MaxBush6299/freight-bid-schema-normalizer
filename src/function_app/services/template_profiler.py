"""template_profiler.py

Produces a TemplateProfile from the original customer bid template.

Wraps the existing workbook_profiler output and enriches it with
reverse-pipeline-specific metadata:
  - writable vs formula-protected columns per bid slot
  - route_name → row_index map per bid sheet
  - template fingerprint for ReversePlanner cache key
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from ..models.contracts import (
    LaneProvenanceEntry,
    TemplateBidSlot,
    TemplateProfile,
    TemplateSheetProfile,
)
from .workbook_profiler import profile_workbook

_FORMULA_COL_NAMES = re.compile(
    r"\(calc\)|fuel surcharge \(calc\)|total shipment rate \(calc\)",
    re.IGNORECASE,
)


class TemplateProfiler:
    """Build a TemplateProfile from an original customer template file."""

    def profile(self, template_path: str) -> TemplateProfile:
        wb_profile = profile_workbook(template_path, detect_formulas=True)

        # Build a quick route_name → row_index lookup from the full provenance list
        all_provenance = wb_profile.lane_provenance

        bid_sheets: list[TemplateSheetProfile] = []

        for sheet_profile in wb_profile.sheets:
            if not sheet_profile.column_groups:
                continue  # only process sheets with repeating bid-slot structure

            formula_col_set = set(sheet_profile.formula_columns)

            bid_slots: list[TemplateBidSlot] = []
            for cg in sheet_profile.column_groups:
                # Use stored absolute column indices (accounts for gaps in the header row)
                abs_indices = cg.column_indices if cg.column_indices else [
                    cg.col_offset + i for i in range(len(cg.columns))
                ]

                slot_formula_indices: list[int] = []
                writable_columns: list[str] = []

                for abs_idx, col_name in zip(abs_indices, cg.columns):
                    if abs_idx in formula_col_set or _FORMULA_COL_NAMES.search(col_name):
                        slot_formula_indices.append(abs_idx)
                    else:
                        writable_columns.append(col_name)

                bid_slots.append(TemplateBidSlot(
                    slot_index=cg.group_index,
                    lot_name=cg.lot_name,
                    col_offset=cg.col_offset,
                    columns=cg.columns,
                    column_indices=abs_indices,
                    writable_columns=writable_columns,
                    formula_col_indices=slot_formula_indices,
                ))

            # Route Name column: column D (index 4) by Coupa template convention;
            # scan headers in case the template shifts it
            route_name_col = 4
            for i, col in enumerate(sheet_profile.columns, 1):
                if "route name" in col.strip().lower():
                    route_name_col = i
                    break

            # Provenance entries scoped to this sheet
            sheet_provenance: list[LaneProvenanceEntry] = [
                e for e in all_provenance if e.sheet_name == sheet_profile.name
            ]

            bid_sheets.append(TemplateSheetProfile(
                sheet_name=sheet_profile.name,
                header_row=sheet_profile.header_row or 9,
                route_name_col=route_name_col,
                bid_slots=bid_slots,
                lane_provenance=sheet_provenance,
            ))

        fingerprint = hashlib.sha256(
            Path(template_path).read_bytes()
        ).hexdigest()[:16]

        return TemplateProfile(
            workbook_name=Path(template_path).name,
            bid_sheets=bid_sheets,
            dropdown_catalog=wb_profile.dropdown_catalog,
            template_fingerprint=fingerprint,
        )
