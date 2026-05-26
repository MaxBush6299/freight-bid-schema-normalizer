"""tests/test_template_profiler.py

Unit tests for ``TemplateProfiler.profile``.

Uses the synthetic Coupa-style workbook from ``tests/_coupa_fixtures.py``
to exercise:

* the leading descriptor + first bid slot being merged into ``slot 0``
  (no Lot Name sentinel before the first bid group)
* formula-protected columns filtered out of ``writable_columns`` purely
  by header name regex (``(calc)``)
* dropdown catalog harvested from ``validationInfo``
* deterministic ``template_fingerprint`` (sha256[:16])
"""
from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from src.function_app.models.contracts import TemplateProfile
from src.function_app.services.template_profiler import TemplateProfiler
from tests._coupa_fixtures import (
    BID_SHEET_NAME,
    HEADER_ROW,
    ROUTE_NAMES,
    build_template_workbook,
)


class TestTemplateProfiler(unittest.TestCase):
    """Exercise TemplateProfiler against the synthetic Coupa workbook."""

    def test_profile_returns_template_profile_with_expected_shape(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tpl_path = build_template_workbook(Path(td) / "template.xlsx")

            profile = TemplateProfiler().profile(str(tpl_path))

            self.assertIsInstance(profile, TemplateProfile)
            self.assertEqual(profile.workbook_name, "template.xlsx")
            # exactly one bid sheet was constructed
            self.assertEqual(len(profile.bid_sheets), 1)
            bid_sheet = profile.bid_sheets[0]
            self.assertEqual(bid_sheet.sheet_name, BID_SHEET_NAME)
            self.assertEqual(bid_sheet.header_row, HEADER_ROW)
            # Quirk: TemplateProfiler scans ``sheet_profile.columns`` which is the
            # *non-empty* header list (col C is blank in the fixture, so it is
            # skipped).  ``route_name_col`` is therefore the 1-based index into
            # that compacted list — Route Name is the first non-empty header.
            self.assertEqual(bid_sheet.route_name_col, 1)
            # lane_provenance covers every <<define>> row
            self.assertEqual(len(bid_sheet.lane_provenance), len(ROUTE_NAMES))
            self.assertEqual(
                [lp.route_name for lp in bid_sheet.lane_provenance],
                ROUTE_NAMES,
            )

    def test_bid_slots_split_on_lot_name_sentinel(self) -> None:
        """Slot 0 = descriptors + first bid group, Slot 1 = second bid group."""
        with tempfile.TemporaryDirectory() as td:
            tpl_path = build_template_workbook(Path(td) / "template.xlsx")
            profile = TemplateProfiler().profile(str(tpl_path))
            slots = profile.bid_sheets[0].bid_slots

            self.assertEqual(len(slots), 2)
            # Slot 0 absorbs the descriptor columns (4-6) plus the primary
            # bid columns (7-11) because there is no Lot Name sentinel before
            # the first bid group in real Coupa templates.
            slot0 = next(s for s in slots if s.slot_index == 0)
            self.assertEqual(slot0.col_offset, 4)
            self.assertIn("Route Name", slot0.columns)
            self.assertIn("Freight \nPrice", slot0.columns)
            self.assertIn("Equipment Type", slot0.columns)
            # Slot 1 is the trailing bid block after the Lot Name sentinel
            slot1 = next(s for s in slots if s.slot_index == 1)
            self.assertEqual(slot1.col_offset, 13)
            self.assertIn("Currency", slot1.columns)
            self.assertIn("Freight \nPrice", slot1.columns)

    def test_formula_columns_excluded_from_writable_columns(self) -> None:
        """Headers ending in ``(calc)`` must not appear in writable_columns."""
        with tempfile.TemporaryDirectory() as td:
            tpl_path = build_template_workbook(Path(td) / "template.xlsx")
            profile = TemplateProfiler().profile(str(tpl_path))

            for slot in profile.bid_sheets[0].bid_slots:
                for col in slot.writable_columns:
                    self.assertNotRegex(col, r"\(calc\)")
                # Each "(calc)" column header must end up in formula_col_indices
                for col_name, abs_idx in zip(slot.columns, slot.column_indices):
                    if "(calc)" in col_name.lower():
                        self.assertIn(abs_idx, slot.formula_col_indices)
                # formula and writable indices must be disjoint
                writable_indices = {
                    slot.column_indices[i]
                    for i, c in enumerate(slot.columns)
                    if c in slot.writable_columns
                }
                self.assertTrue(
                    writable_indices.isdisjoint(slot.formula_col_indices),
                    "writable and formula column index sets must not overlap",
                )

    def test_dropdown_catalog_harvested_from_validation_info(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tpl_path = build_template_workbook(Path(td) / "template.xlsx")
            profile = TemplateProfiler().profile(str(tpl_path))

            self.assertIn("Currency", profile.dropdown_catalog)
            self.assertIn("Equipment Type", profile.dropdown_catalog)
            self.assertEqual(profile.dropdown_catalog["Currency"], ["USD", "CAD"])

    def test_template_fingerprint_is_stable_16_char_hex(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tpl_path = build_template_workbook(Path(td) / "template.xlsx")

            p1 = TemplateProfiler().profile(str(tpl_path))
            p2 = TemplateProfiler().profile(str(tpl_path))

            self.assertEqual(len(p1.template_fingerprint), 16)
            self.assertRegex(p1.template_fingerprint, r"^[0-9a-f]{16}$")
            # Identical bytes ⇒ identical fingerprint
            self.assertEqual(p1.template_fingerprint, p2.template_fingerprint)

    def test_template_fingerprint_changes_when_template_changes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            a = build_template_workbook(
                Path(td) / "a.xlsx",
                route_names=["FTL-A-1"],
            )
            b = build_template_workbook(
                Path(td) / "b.xlsx",
                route_names=["FTL-A-1", "FTL-A-2"],
            )

            fp_a = TemplateProfiler().profile(str(a)).template_fingerprint
            fp_b = TemplateProfiler().profile(str(b)).template_fingerprint

            self.assertTrue(
                re.fullmatch(r"[0-9a-f]{16}", fp_a) is not None,
                "fingerprint A must be 16 hex chars",
            )
            self.assertNotEqual(fp_a, fp_b)


if __name__ == "__main__":
    unittest.main()
