"""tests/test_template_aware_writer.py

Unit tests for ``TemplateAwareWriter`` and ``resolve_instructions``.

The writer's safety guarantees are the load-bearing ones:

* writes to formula-protected cells raise ``ProtectedCellError``
* token rows (``<<...>>`` in column A) are SKIPPED, not written
* the produced ``submission.xlsx`` actually carries the new values
* ``resolve_instructions`` honours the ``Origin Note`` join key and reports
  template lanes that are missing from the priced export as ``no_bid_lanes``
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from openpyxl import load_workbook

from src.function_app.models.contracts import (
    CellWriteInstruction,
    FieldMapping,
    ReverseMappingPlan,
)
from src.function_app.services.template_aware_writer import (
    ProtectedCellError,
    TemplateAwareWriter,
    resolve_instructions,
)
from src.function_app.services.template_profiler import TemplateProfiler
from tests._coupa_fixtures import (
    BID_SHEET_NAME,
    FIRST_DATA_ROW,
    ROUTE_NAMES,
    build_template_workbook,
)


def _writable_col_index(slot, header_substring: str) -> int:
    """Return the 1-based abs column index of the first writable header
    that case-insensitively contains ``header_substring``."""
    for col_name, abs_idx in zip(slot.columns, slot.column_indices):
        if header_substring.lower() in col_name.lower() and col_name in slot.writable_columns:
            return abs_idx
    raise AssertionError(
        f"No writable column containing {header_substring!r} found in slot "
        f"{slot.slot_index}"
    )


class TestTemplateAwareWriterSafety(unittest.TestCase):
    """The writer must enforce formula / token-row safety guarantees."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.template_path = build_template_workbook(self.tmp / "template.xlsx")
        self.profile = TemplateProfiler().profile(str(self.template_path))
        self.slot0 = next(s for s in self.profile.bid_sheets[0].bid_slots if s.slot_index == 0)
        self.writer = TemplateAwareWriter()

    def test_write_to_writable_cell_succeeds_and_persists(self) -> None:
        freight_col = _writable_col_index(self.slot0, "Freight")
        instr = CellWriteInstruction(
            sheet_name=BID_SHEET_NAME,
            row_index=FIRST_DATA_ROW,
            col_index=freight_col,
            value=1234.56,
            source_route_name=ROUTE_NAMES[0],
            source_field="RXO All In Customer Rate",
        )
        output_path = str(self.tmp / "submission.xlsx")

        report = self.writer.write(
            template_path=str(self.template_path),
            instructions=[instr],
            output_path=output_path,
            template_profile=self.profile,
        )

        self.assertEqual(report.cells_written, 1)
        self.assertEqual(report.cells_skipped, 0)
        self.assertEqual(len(report.write_log), 1)
        self.assertEqual(report.write_log[0]["action"], "written")
        # The value must actually be in the saved workbook
        wb = load_workbook(output_path)
        try:
            cell_val = wb[BID_SHEET_NAME].cell(row=FIRST_DATA_ROW, column=freight_col).value
            self.assertEqual(cell_val, 1234.56)
        finally:
            wb.close()

    def test_write_to_formula_column_raises_protected_cell_error(self) -> None:
        # Pick any column index registered as formula-protected
        formula_col = sorted(self.slot0.formula_col_indices)[0]
        instr = CellWriteInstruction(
            sheet_name=BID_SHEET_NAME,
            row_index=FIRST_DATA_ROW,
            col_index=formula_col,
            value=99.0,
            source_route_name=ROUTE_NAMES[0],
            source_field="(should not be allowed)",
        )

        with self.assertRaises(ProtectedCellError):
            self.writer.write(
                template_path=str(self.template_path),
                instructions=[instr],
                output_path=str(self.tmp / "should_not_be_written.xlsx"),
                template_profile=self.profile,
            )

    def test_write_to_token_row_is_skipped(self) -> None:
        # Plant a <<define>> token in col A on a data row and write to a writable col
        from openpyxl import load_workbook as _lw
        wb = _lw(str(self.template_path))
        wb[BID_SHEET_NAME].cell(row=FIRST_DATA_ROW, column=1).value = "<<define>>"
        wb.save(str(self.template_path))
        wb.close()
        # Re-profile so the lane provenance matches the mutated template
        self.profile = TemplateProfiler().profile(str(self.template_path))
        self.slot0 = next(
            s for s in self.profile.bid_sheets[0].bid_slots if s.slot_index == 0
        )

        freight_col = _writable_col_index(self.slot0, "Freight")
        instr = CellWriteInstruction(
            sheet_name=BID_SHEET_NAME,
            row_index=FIRST_DATA_ROW,
            col_index=freight_col,
            value=42.0,
            source_route_name=ROUTE_NAMES[0],
            source_field="RXO All In Customer Rate",
        )

        report = self.writer.write(
            template_path=str(self.template_path),
            instructions=[instr],
            output_path=str(self.tmp / "submission.xlsx"),
            template_profile=self.profile,
        )

        self.assertEqual(report.cells_written, 0)
        self.assertEqual(report.cells_skipped, 1)
        self.assertEqual(report.write_log[0]["action"], "skipped")
        self.assertEqual(report.write_log[0]["reason"], "token_row_protected")

    def test_write_to_unknown_sheet_is_skipped(self) -> None:
        instr = CellWriteInstruction(
            sheet_name="DoesNotExist",
            row_index=FIRST_DATA_ROW,
            col_index=9,
            value="X",
            source_route_name=ROUTE_NAMES[0],
            source_field="Currency",
        )

        report = self.writer.write(
            template_path=str(self.template_path),
            instructions=[instr],
            output_path=str(self.tmp / "submission.xlsx"),
            template_profile=self.profile,
        )

        self.assertEqual(report.cells_written, 0)
        self.assertEqual(report.cells_skipped, 1)
        self.assertEqual(report.write_log[0]["reason"], "sheet_not_found")


class TestResolveInstructions(unittest.TestCase):
    """Plan-driven instruction resolution joins on ``Origin Note``."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.template_path = build_template_workbook(self.tmp / "template.xlsx")
        self.profile = TemplateProfiler().profile(str(self.template_path))

    def _plan(self) -> ReverseMappingPlan:
        return ReverseMappingPlan(
            plan_id="test-plan",
            template_fingerprint=self.profile.template_fingerprint,
            planner_mode="mock",
            mappings=[
                FieldMapping(
                    source_field="RXO All In Customer Rate",
                    target_column="Freight \nPrice",
                    bid_slot=0,
                    value_transform="round_2",
                ),
                FieldMapping(
                    source_field="Currency",
                    target_column="Currency",
                    bid_slot=0,
                    value_transform="upper",
                ),
            ],
            assumptions=[],
        )

    def test_resolves_one_instruction_per_mapping_per_matched_route(self) -> None:
        export_rows = [
            {"Origin Note": ROUTE_NAMES[0], "RXO All In Customer Rate": 1500.123, "Currency": "usd"},
            {"Origin Note": ROUTE_NAMES[1], "RXO All In Customer Rate": 1700.987, "Currency": "usd"},
            {"Origin Note": ROUTE_NAMES[2], "RXO All In Customer Rate": 1900.555, "Currency": "usd"},
        ]

        instructions, no_bid = resolve_instructions(export_rows, self.profile, self._plan())

        # 2 mappings × 3 routes = 6 instructions, all in slot 0
        self.assertEqual(len(instructions), 6)
        self.assertEqual(no_bid, [])
        # value_transform=round_2 must produce 2-decimal floats
        freight_writes = [i for i in instructions if i.source_field == "RXO All In Customer Rate"]
        self.assertTrue(all(isinstance(i.value, float) for i in freight_writes))
        self.assertTrue(all(round(i.value, 2) == i.value for i in freight_writes))
        # value_transform=upper must uppercase the currency
        currency_writes = [i for i in instructions if i.source_field == "Currency"]
        self.assertTrue(all(i.value == "USD" for i in currency_writes))

    def test_missing_template_route_in_export_appears_in_no_bid_lanes(self) -> None:
        export_rows = [
            {"Origin Note": ROUTE_NAMES[0], "RXO All In Customer Rate": 1500.0, "Currency": "USD"},
            # ROUTE_NAMES[1] and [2] omitted on purpose
        ]

        instructions, no_bid = resolve_instructions(export_rows, self.profile, self._plan())

        # Only 2 instructions × 1 matched route
        self.assertEqual(len(instructions), 2)
        self.assertEqual(sorted(no_bid), sorted([ROUTE_NAMES[1], ROUTE_NAMES[2]]))

    def test_extra_export_route_with_no_template_match_is_ignored(self) -> None:
        export_rows = [
            {"Origin Note": rn, "RXO All In Customer Rate": 1500.0, "Currency": "USD"}
            for rn in ROUTE_NAMES
        ]
        export_rows.append({
            "Origin Note": "GHOST-LANE-ONLY-IN-EXPORT",
            "RXO All In Customer Rate": 999.0,
            "Currency": "USD",
        })

        instructions, no_bid = resolve_instructions(export_rows, self.profile, self._plan())

        # 2 mappings × 3 routes — ghost lane produces nothing
        self.assertEqual(len(instructions), 6)
        self.assertEqual(no_bid, [])

    def test_target_column_not_in_template_is_silently_skipped(self) -> None:
        plan = ReverseMappingPlan(
            plan_id="bogus",
            template_fingerprint=self.profile.template_fingerprint,
            planner_mode="mock",
            mappings=[
                FieldMapping(
                    source_field="RXO All In Customer Rate",
                    target_column="Nonexistent Header That Will Never Match",
                    bid_slot=0,
                ),
            ],
            assumptions=[],
        )
        export_rows = [
            {"Origin Note": rn, "RXO All In Customer Rate": 1500.0}
            for rn in ROUTE_NAMES
        ]

        instructions, no_bid = resolve_instructions(export_rows, self.profile, plan)

        self.assertEqual(instructions, [])
        self.assertEqual(no_bid, [])


if __name__ == "__main__":
    unittest.main()
