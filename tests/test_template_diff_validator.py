"""tests/test_template_diff_validator.py

Unit tests for ``TemplateDiffValidator``.

The validator's job is to assert that the produced submission file differs
from the original template ONLY in cells that the writer claims to have
written.  Anything else is a safety violation.

The cases below cover the three categorizations:

* ``passed=True`` happy path — only writable cells changed and every change
  matches a write-log entry
* ``passed=False`` when an unexpected cell change is planted in the
  submission
* ``missing_writes`` when the write log claims a write but the submission
  still holds the original value
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from openpyxl import load_workbook

from src.function_app.models.contracts import CellWriteInstruction
from src.function_app.services.template_aware_writer import TemplateAwareWriter
from src.function_app.services.template_diff_validator import (
    TemplateDiffValidator,
)
from src.function_app.services.template_profiler import TemplateProfiler
from tests._coupa_fixtures import (
    BID_SHEET_NAME,
    FIRST_DATA_ROW,
    ROUTE_NAMES,
    build_template_workbook,
)


def _writable_col_index(slot, header_substring: str) -> int:
    for col_name, abs_idx in zip(slot.columns, slot.column_indices):
        if header_substring.lower() in col_name.lower() and col_name in slot.writable_columns:
            return abs_idx
    raise AssertionError(f"No writable column matching {header_substring!r}")


class TestTemplateDiffValidator(unittest.TestCase):
    """Exercise the diff validator against writer output."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.template_path = str(build_template_workbook(self.tmp / "template.xlsx"))
        self.profile = TemplateProfiler().profile(self.template_path)
        self.slot0 = next(
            s for s in self.profile.bid_sheets[0].bid_slots if s.slot_index == 0
        )
        self.freight_col = _writable_col_index(self.slot0, "Freight")
        self.equip_col = _writable_col_index(self.slot0, "Equipment Type")

    def _write_submission(
        self,
        instructions: list[CellWriteInstruction],
    ) -> tuple[str, object]:
        """Run the writer and return ``(submission_path, write_report)``."""
        submission_path = str(self.tmp / "submission.xlsx")
        writer = TemplateAwareWriter()
        report = writer.write(
            template_path=self.template_path,
            instructions=instructions,
            output_path=submission_path,
            template_profile=self.profile,
        )
        return submission_path, report

    def test_happy_path_passes_when_only_writable_cells_changed(self) -> None:
        instructions = [
            CellWriteInstruction(
                sheet_name=BID_SHEET_NAME,
                row_index=FIRST_DATA_ROW,
                col_index=self.freight_col,
                value=2500.55,
                source_route_name=ROUTE_NAMES[0],
                source_field="RXO All In Customer Rate",
            ),
        ]
        submission_path, report = self._write_submission(instructions)

        diff = TemplateDiffValidator().validate(
            template_path=self.template_path,
            submission_path=submission_path,
            template_profile=self.profile,
            write_report=report,
            run_id="test",
        )

        self.assertTrue(diff.passed, msg=f"violations: {diff.violations}")
        self.assertEqual(diff.unexpected_changes, 0)
        self.assertEqual(diff.expected_writes, 1)

    def test_unexpected_change_outside_writable_columns_fails_validation(self) -> None:
        # Write nothing legal, then plant a manual mutation in col A (a
        # column that is not in the writable set for any slot).
        submission_path, report = self._write_submission(instructions=[])

        # Manually mutate Origin State (col 6 — not writable in our setup
        # because the writer only ever writes through declared mappings;
        # the diff validator should still flag any non-writable change).
        # Use col A (1) which is definitively outside any slot.
        wb = load_workbook(submission_path)
        wb[BID_SHEET_NAME].cell(row=FIRST_DATA_ROW, column=1).value = "TAMPERED"
        wb.save(submission_path)
        wb.close()

        diff = TemplateDiffValidator().validate(
            template_path=self.template_path,
            submission_path=submission_path,
            template_profile=self.profile,
            write_report=report,
            run_id="test",
        )

        self.assertFalse(diff.passed)
        self.assertGreaterEqual(diff.unexpected_changes, 1)
        # The violation must reference the tampered cell
        tampered = [
            v for v in diff.violations
            if v.row == FIRST_DATA_ROW and v.col == 1
        ]
        self.assertTrue(tampered, "expected violation for the tampered cell")
        self.assertEqual(tampered[0].submission_value, "TAMPERED")
        self.assertEqual(tampered[0].category, "unexpected_change")

    def test_missing_write_when_writer_claims_write_but_submission_unchanged(self) -> None:
        # Write 2500.55 to freight col legitimately
        instructions = [
            CellWriteInstruction(
                sheet_name=BID_SHEET_NAME,
                row_index=FIRST_DATA_ROW,
                col_index=self.freight_col,
                value=2500.55,
                source_route_name=ROUTE_NAMES[0],
                source_field="RXO All In Customer Rate",
            ),
        ]
        submission_path, report = self._write_submission(instructions)

        # Now manually revert that cell back to None to simulate a write
        # that "didn't take" (the write_log still claims a write).
        wb = load_workbook(submission_path)
        wb[BID_SHEET_NAME].cell(row=FIRST_DATA_ROW, column=self.freight_col).value = None
        wb.save(submission_path)
        wb.close()

        diff = TemplateDiffValidator().validate(
            template_path=self.template_path,
            submission_path=submission_path,
            template_profile=self.profile,
            write_report=report,
            run_id="test",
        )

        self.assertEqual(diff.missing_writes, 1)
        self.assertEqual(diff.missing_writes_detail[0].category, "missing_write")
        self.assertEqual(diff.missing_writes_detail[0].row, FIRST_DATA_ROW)
        self.assertEqual(diff.missing_writes_detail[0].col, self.freight_col)

    def test_idempotent_noop_write_does_not_register_missing(self) -> None:
        """If write_log value equals the original (None == None), do not
        flag it as a missing_write.  This prevents spurious noise when
        the writer 'writes' a blank into an already-blank cell."""
        instructions = [
            CellWriteInstruction(
                sheet_name=BID_SHEET_NAME,
                row_index=FIRST_DATA_ROW,
                col_index=self.freight_col,
                value=None,  # template already blank here
                source_route_name=ROUTE_NAMES[0],
                source_field="RXO All In Customer Rate",
            ),
        ]
        submission_path, report = self._write_submission(instructions)

        diff = TemplateDiffValidator().validate(
            template_path=self.template_path,
            submission_path=submission_path,
            template_profile=self.profile,
            write_report=report,
            run_id="test",
        )

        self.assertEqual(diff.missing_writes, 0)
        self.assertTrue(diff.passed)

    def test_validate_persists_report_when_output_path_provided(self) -> None:
        instructions = [
            CellWriteInstruction(
                sheet_name=BID_SHEET_NAME,
                row_index=FIRST_DATA_ROW,
                col_index=self.freight_col,
                value=2500.55,
                source_route_name=ROUTE_NAMES[0],
                source_field="RXO All In Customer Rate",
            ),
        ]
        submission_path, report = self._write_submission(instructions)
        diff_json_path = self.tmp / "template_diff.json"

        TemplateDiffValidator().validate(
            template_path=self.template_path,
            submission_path=submission_path,
            template_profile=self.profile,
            write_report=report,
            run_id="persist-test",
            output_path=str(diff_json_path),
        )

        self.assertTrue(diff_json_path.exists())
        payload = json.loads(diff_json_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["run_id"], "persist-test")
        self.assertTrue(payload["passed"])


if __name__ == "__main__":
    unittest.main()
