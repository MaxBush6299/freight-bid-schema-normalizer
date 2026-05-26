"""tests/test_rehydrate_pipeline_runner.py

Smoke / integration test for the end-to-end ``run_rehydrate`` pipeline.

Wires ``TemplateProfiler → ReversePlanner (mock) → resolve_instructions
→ TemplateAwareWriter → TemplateDiffValidator`` against the synthetic
Coupa workbook and asserts that:

* every expected artifact file is emitted
* the summary dict carries the documented keys with the right shape
* the mock plan produces non-zero writes (3 matching mappings × 3 routes)
* unmatched template routes show up in ``no_bid_lane_names``
* the diff validator passes on the writer's own output
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from openpyxl import load_workbook

from src.function_app.local_rehydrate_runner import run_rehydrate
from tests._coupa_fixtures import (
    BID_SHEET_NAME,
    FIRST_DATA_ROW,
    ROUTE_NAMES,
    build_export_workbook,
    build_template_workbook,
)

_EXPECTED_ARTIFACTS = (
    "submission.xlsx",
    "template_profile.json",
    "mapping_plan.json",
    "write_report.json",
    "pending_review.json",
    "template_diff.json",
)


class TestRehydratePipelineRunner(unittest.TestCase):
    """End-to-end smoke test of the rehydrate pipeline."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_happy_path_writes_expected_cells_and_artifacts(self) -> None:
        template = build_template_workbook(self.tmp / "template.xlsx")
        export = build_export_workbook(self.tmp / "export.xlsx")
        out_root = self.tmp / "out"

        result = run_rehydrate(
            template_path=str(template),
            export_path=str(export),
            output_root=str(out_root),
            planner_mode="mock",
        )

        # 1 bid sheet × 3 routes, 3 of 5 mock mappings hit the fixture
        # (RXO All In Customer Rate → Freight \nPrice, Equipment Type
        #  Detail → Equipment Type, Currency → Currency).  MX Cost → ORC
        # and Customer FSC Type → Fuel Type have no matching template
        # header, so they are silently skipped.
        self.assertEqual(result["bid_sheets_profiled"], 1)
        self.assertEqual(result["export_rows_loaded"], len(ROUTE_NAMES))
        self.assertEqual(result["instructions_resolved"], 9)
        self.assertEqual(result["cells_written"], 9)
        self.assertEqual(result["cells_skipped"], 0)
        self.assertEqual(result["no_bid_lanes"], 0)
        self.assertEqual(result["no_bid_lane_names"], [])
        self.assertEqual(result["validation_warnings"], 0)
        self.assertEqual(result["pending_review_count"], 0)
        self.assertEqual(result["llm_iterations"], 0)
        self.assertTrue(result["diff_passed"])
        self.assertEqual(result["diff_violations"], 0)
        self.assertEqual(result["diff_missing_writes"], 0)

        run_dir = Path(result["run_dir"])
        self.assertTrue(run_dir.exists())
        for fname in _EXPECTED_ARTIFACTS:
            self.assertTrue(
                (run_dir / fname).exists(),
                msg=f"missing artifact: {fname}",
            )

        # Sanity-check the submission .xlsx — Freight \nPrice should be
        # populated in the first slot for every route.
        wb = load_workbook(result["submission"])
        try:
            sheet = wb[BID_SHEET_NAME]
            # Freight \nPrice header lives at col 9 in the fixture
            for row_offset in range(len(ROUTE_NAMES)):
                val = sheet.cell(
                    row=FIRST_DATA_ROW + row_offset,
                    column=9,
                ).value
                self.assertIsNotNone(val, f"row {row_offset}: freight cell is blank")
                self.assertIsInstance(val, (int, float))
        finally:
            wb.close()

    def test_no_bid_lane_when_template_route_missing_from_export(self) -> None:
        template = build_template_workbook(self.tmp / "template.xlsx")
        # Export covers only the first 2 routes; the third becomes no_bid
        export = build_export_workbook(
            self.tmp / "export.xlsx",
            route_names=ROUTE_NAMES[:-1],
        )

        result = run_rehydrate(
            template_path=str(template),
            export_path=str(export),
            output_root=str(self.tmp / "out"),
            planner_mode="mock",
        )

        # 3 mappings × 2 matched routes
        self.assertEqual(result["cells_written"], 6)
        self.assertEqual(result["no_bid_lanes"], 1)
        self.assertEqual(result["no_bid_lane_names"], [ROUTE_NAMES[-1]])
        self.assertEqual(result["validation_warnings"], 1)
        warning = result["warnings"][0]
        self.assertEqual(warning["code"], "no_bid_lane")
        self.assertEqual(warning["route_name"], ROUTE_NAMES[-1])
        self.assertEqual(warning["sheet_name"], BID_SHEET_NAME)
        # Diff still passes — no_bid lanes just leave cells blank
        self.assertTrue(result["diff_passed"])

    def test_extra_export_route_with_no_template_match_is_ignored(self) -> None:
        template = build_template_workbook(self.tmp / "template.xlsx")
        export = build_export_workbook(
            self.tmp / "export.xlsx",
            extra_route_without_template_match="GHOST-LANE",
        )

        result = run_rehydrate(
            template_path=str(template),
            export_path=str(export),
            output_root=str(self.tmp / "out"),
            planner_mode="mock",
        )

        # 4 export rows loaded but only 3 match → 9 instructions, 0 no_bid
        self.assertEqual(result["export_rows_loaded"], 4)
        self.assertEqual(result["instructions_resolved"], 9)
        self.assertEqual(result["no_bid_lanes"], 0)

    def test_mapping_plan_artifact_records_mock_mode(self) -> None:
        template = build_template_workbook(self.tmp / "template.xlsx")
        export = build_export_workbook(self.tmp / "export.xlsx")

        result = run_rehydrate(
            template_path=str(template),
            export_path=str(export),
            output_root=str(self.tmp / "out"),
            planner_mode="mock",
        )

        plan_doc = json.loads(Path(result["mapping_plan"]).read_text(encoding="utf-8"))
        self.assertEqual(plan_doc["planner_mode"], "mock")
        self.assertEqual(plan_doc["iterations_run"], 0)
        self.assertGreater(len(plan_doc["mappings"]), 0)


if __name__ == "__main__":
    unittest.main()
