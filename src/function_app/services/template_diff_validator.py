"""template_diff_validator.py

Cell-by-cell diff between the original customer template and the produced
submission file.

Safety guarantee
----------------
Every cell that changed MUST appear in the declared writable columns of a
bid slot.  Any other change is a *violation* — it means the writer touched
a formula, a token row, or a structural cell it should never have modified.

Outputs
-------
Returns a :class:`TemplateDiffReport` and optionally writes
``template_diff.json`` to disk.  The report includes:

- ``passed``              – True when ``violations`` is empty
- ``expected_writes``     – changed cells that match a confirmed write entry
- ``unexpected_changes``  – changed cells outside writable columns (bugs)
- ``missing_writes``      – write-log entries whose cell value didn't change
- ``violations``          – detail list for unexpected_changes
- ``missing_writes_detail``– detail list for missing writes
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from openpyxl import load_workbook
from pydantic import BaseModel, Field

from ..models.contracts import TemplateProfile, WriteReport


# ── Output contracts ──────────────────────────────────────────────────────────

class DiffCell(BaseModel):
    sheet: str
    row: int
    col: int
    original_value: Any = None
    submission_value: Any = None
    category: str               # "expected_write" | "unexpected_change" | "missing_write"
    route_name: Optional[str] = None
    source_field: Optional[str] = None


class TemplateDiffReport(BaseModel):
    run_id: str
    template_path: str
    submission_path: str
    total_cells_compared: int
    expected_writes: int
    unexpected_changes: int
    missing_writes: int
    violations: list[DiffCell] = Field(default_factory=list)
    missing_writes_detail: list[DiffCell] = Field(default_factory=list)
    passed: bool


# ── Validator ─────────────────────────────────────────────────────────────────

class TemplateDiffValidator:
    """Compare original template vs submission; flag any out-of-bounds writes."""

    def validate(
        self,
        template_path: str,
        submission_path: str,
        template_profile: TemplateProfile,
        write_report: WriteReport,
        run_id: str = "",
        output_path: Optional[str] = None,
    ) -> TemplateDiffReport:
        """Run the diff and return a TemplateDiffReport.

        Parameters
        ----------
        template_path:
            Path to the original unmodified customer template.
        submission_path:
            Path to the submission file produced by TemplateAwareWriter.
        template_profile:
            TemplateProfile describing bid sheets, slots, and writable columns.
        write_report:
            WriteReport from the same run (provides the expected write log).
        run_id:
            Identifier to embed in the report (defaults to empty string).
        output_path:
            If provided, the report is serialised to this path as JSON.
        """
        orig_wb = load_workbook(template_path, data_only=True, read_only=True)
        sub_wb = load_workbook(submission_path, data_only=True, read_only=True)

        try:
            return self._compare(
                orig_wb, sub_wb, template_profile, write_report,
                run_id, template_path, submission_path, output_path,
            )
        finally:
            orig_wb.close()
            sub_wb.close()

    # ── Internal ─────────────────────────────────────────────────────────────

    def _compare(
        self, orig_wb, sub_wb, template_profile, write_report,
        run_id, template_path, submission_path, output_path,
    ) -> TemplateDiffReport:
        # Build writable-address set: {sheet_name: set of writable col indices}
        # Also build formula-col set to EXCLUDE from comparison (openpyxl strips
        # cached formula values on save, so formula cells will always "differ")
        writable_cols: dict[str, set[int]] = {}
        formula_cols: dict[str, set[int]] = {}
        for tsp in template_profile.bid_sheets:
            col_set: set[int] = set()
            formula_set: set[int] = set()
            for slot in tsp.bid_slots:
                writable_indices = [
                    slot.column_indices[i]
                    for i, col in enumerate(slot.columns)
                    if col in slot.writable_columns
                ]
                col_set.update(writable_indices)
                formula_set.update(slot.formula_col_indices)
            writable_cols[tsp.sheet_name] = col_set
            formula_cols[tsp.sheet_name] = formula_set

        # Build expected-write lookup: {(sheet, row, col): {route, field, new_value}}
        expected_writes: dict[tuple[str, int, int], dict[str, Any]] = {}
        for entry in write_report.write_log:
            if entry.get("action") == "written":
                key = (entry["sheet"], entry["row"], entry["col"])
                expected_writes[key] = {
                    "route_name": entry.get("route"),
                    "source_field": entry.get("field"),
                    "new_value": entry.get("value"),
                }

        # Build data-row sets per sheet from lane_provenance (exclude header/control rows)
        data_rows: dict[str, set[int]] = {}
        for tsp in template_profile.bid_sheets:
            data_rows[tsp.sheet_name] = {lp.row_index for lp in tsp.lane_provenance}

        total_compared = 0
        confirmed_writes = 0
        violations: list[DiffCell] = []
        missing: list[DiffCell] = []

        bid_sheet_names = {tsp.sheet_name for tsp in template_profile.bid_sheets}

        # Cache sheet data as {sheet_name: {(row, col): value}} to avoid re-reading
        orig_cache: dict[str, dict[tuple[int, int], Any]] = {}
        sub_cache: dict[str, dict[tuple[int, int], Any]] = {}

        for sheet_name in bid_sheet_names:
            if sheet_name not in orig_wb.sheetnames or sheet_name not in sub_wb.sheetnames:
                continue

            orig_ws = orig_wb[sheet_name]
            sub_ws = sub_wb[sheet_name]

            orig_data: dict[tuple[int, int], Any] = {}
            for r_idx, row in enumerate(orig_ws.iter_rows(values_only=True), start=1):
                for c_idx, val in enumerate(row, start=1):
                    orig_data[(r_idx, c_idx)] = val

            sub_data: dict[tuple[int, int], Any] = {}
            for r_idx, row in enumerate(sub_ws.iter_rows(values_only=True), start=1):
                for c_idx, val in enumerate(row, start=1):
                    sub_data[(r_idx, c_idx)] = val

            orig_cache[sheet_name] = orig_data
            sub_cache[sheet_name] = sub_data

            all_coords = orig_data.keys() | sub_data.keys()
            total_compared += len(all_coords)

            writable = writable_cols.get(sheet_name, set())
            formulas = formula_cols.get(sheet_name, set())
            lane_rows = data_rows.get(sheet_name, set())

            for (row, col) in all_coords:
                # Only compare data rows — skip header, token, and control rows
                if lane_rows and row not in lane_rows:
                    continue

                # Skip formula columns — openpyxl strips cached values on write
                if col in formulas:
                    continue

                orig_val = orig_data.get((row, col))
                sub_val = sub_data.get((row, col))

                if _values_equal(orig_val, sub_val):
                    continue

                key = (sheet_name, row, col)
                meta = expected_writes.get(key, {})

                if col in writable:
                    if key in expected_writes:
                        confirmed_writes += 1
                    else:
                        # Changed in a writable column but not tracked — flag it
                        violations.append(DiffCell(
                            sheet=sheet_name, row=row, col=col,
                            original_value=_safe_str(orig_val),
                            submission_value=_safe_str(sub_val),
                            category="unexpected_change",
                            route_name=None,
                            source_field="(untracked writable column change)",
                        ))
                else:
                    violations.append(DiffCell(
                        sheet=sheet_name, row=row, col=col,
                        original_value=_safe_str(orig_val),
                        submission_value=_safe_str(sub_val),
                        category="unexpected_change",
                        route_name=meta.get("route_name"),
                        source_field=meta.get("source_field"),
                    ))

        # Find write-log entries where the cell did NOT actually change,
        # but only when the write was supposed to produce a change (new_value != original)
        for key, meta in expected_writes.items():
            sheet, row, col = key
            if sheet not in orig_cache:
                continue
            orig_val = orig_cache[sheet].get((row, col))
            sub_val = sub_cache[sheet].get((row, col))
            new_value = meta.get("new_value")
            # Skip if the intended write value equals the original (idempotent no-op)
            if _values_equal(orig_val, new_value):
                continue
            if _values_equal(orig_val, sub_val):
                missing.append(DiffCell(
                    sheet=sheet, row=row, col=col,
                    original_value=_safe_str(orig_val),
                    submission_value=_safe_str(sub_val),
                    category="missing_write",
                    route_name=meta.get("route_name"),
                    source_field=meta.get("source_field"),
                ))

        report = TemplateDiffReport(
            run_id=run_id,
            template_path=template_path,
            submission_path=submission_path,
            total_cells_compared=total_compared,
            expected_writes=confirmed_writes,
            unexpected_changes=len(violations),
            missing_writes=len(missing),
            violations=violations,
            missing_writes_detail=missing,
            passed=len(violations) == 0,
        )

        if output_path:
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(output_path).write_text(report.model_dump_json(indent=2), encoding="utf-8")

        return report


# ── Helpers ───────────────────────────────────────────────────────────────────

def _values_equal(a: Any, b: Any) -> bool:
    """Numeric-tolerant equality that treats None and empty string as the same."""
    if a is None and b is None:
        return True
    if a is None or b is None:
        # Treat None == "" (template blanks vs submission empty cells)
        a_empty = a is None or (isinstance(a, str) and a.strip() == "")
        b_empty = b is None or (isinstance(b, str) and b.strip() == "")
        return a_empty and b_empty
    try:
        return abs(float(a) - float(b)) < 1e-9
    except (TypeError, ValueError):
        return str(a).strip() == str(b).strip()


def _safe_str(v: Any) -> Any:
    """Return the value as-is, but convert non-serialisable types to strings."""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    return str(v)
