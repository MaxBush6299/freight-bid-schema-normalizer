"""tests/_coupa_fixtures.py

Shared helpers for reverse-pipeline tests.

Builds a *minimal* Coupa-style customer bid template in-memory using openpyxl
so the reverse-pipeline tests do not depend on the real customer files in
``source_docs/``.  The synthetic workbook intentionally exercises every
mechanism the reverse pipeline relies on:

* Coupa control banner rows (``<<HIDEROW>>``, ``<<hidecolumns:>>``,
  ``<<bid|itemType:ftl|...>>``) above the column-header row.
* ``<<define>>`` rows that carry the Route Name in the next column
  (TD-003 — drives ``lane_provenance``).
* A ``Lot Name`` sentinel column between repeating bid-slot groups
  (TD-005 — drives ``column_groups``).
* Formula-protected column headers ending in ``(calc)``
  (TD-008 — drives ``formula_columns``/``writable_columns``).
* A ``validationInfo`` sheet for the dropdown catalog (TD-007).

The leading underscore in the module name keeps pytest's test collector
from treating it as a test module.
"""
from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook

# ── Layout constants (1-based column indices) ────────────────────────────────
# Row 9 is the header row (rows 1-8 hold Coupa control banner tokens).
HEADER_ROW = 9
# Data rows start at row 10 and run downward.
FIRST_DATA_ROW = 10

# Bid-data sheet layout — what the synthetic header looks like:
#
#   col  C     col D-F           col G          col H       col I            col J/K              col L      col M-P
#   ---  ---   --------------    ------------   --------    -------------    -----------------    --------   -----------
#   --   --    Route Name        Equipment      Currency    Freight\nPrice   Fuel/Total (calc)    Lot Name   bid slot 1
#   --   --    Origin City/St    Type           (write)     (write)          (formula)            (sentinel) (curr...)
#
# Real Coupa templates do NOT put a Lot Name sentinel before the first bid
# group, so descriptors and the primary bid slot are merged into ``group 0``.
# A single Lot Name sentinel between slot-0 and slot-1 is enough to satisfy
# ``_detect_column_groups`` (which only returns groups when ``len > 1``).
#
# Col C is empty in the header row; it is populated with ``<<define>>``
# tokens on data rows so ``_extract_lane_provenance`` can find them.
BID_SHEET_NAME = "FTL | Road"
VALIDATION_INFO_NAME = "validationInfo"

ROUTE_NAMES = ["FTL-001-AB-1-100-XYZ-1", "FTL-002-CD-2-200-XYZ-1", "FTL-003-EF-3-300-XYZ-1"]


def build_template_workbook(
    path: Path,
    *,
    route_names: list[str] | None = None,
    include_validation_info: bool = True,
) -> Path:
    """Write a minimal Coupa-style bid template to ``path`` and return it."""
    routes = list(route_names) if route_names is not None else list(ROUTE_NAMES)

    wb = Workbook()
    bid_sheet = wb.active
    bid_sheet.title = BID_SHEET_NAME

    # 1) Coupa control banner rows (rows 1-8)
    bid_sheet.cell(row=1, column=1).value = "<<HIDEROW>>"
    bid_sheet.cell(row=2, column=1).value = "<<hidecolumns:>>"
    bid_sheet.cell(row=3, column=1).value = "<<bid|itemType:ftl|prefix:FTL>>"
    # Rows 4-8 left intentionally blank; the profiler skips them via header scoring.

    # 2) Header row at row 9
    headers = {
        # Col C (3) is left blank — `<<define>>` tokens land there on data rows.
        4: "Route Name",
        5: "Origin City",
        6: "Origin State",
        7: "Equipment Type",                      # writable; mock-planner target
        8: "Currency",                            # writable; mock-planner target
        9: "Freight \nPrice",                     # writable; mock-planner target (note newline)
        10: "Fuel Surcharge (calc)",              # formula-protected by name
        11: "Total Shipment Rate (calc)",         # formula-protected by name
        12: "Lot Name",                           # sentinel between slot 0 and slot 1
        13: "Currency",
        14: "Freight \nPrice",
        15: "Fuel Surcharge (calc)",
        16: "Total Shipment Rate (calc)",
    }
    for col_idx, value in headers.items():
        bid_sheet.cell(row=HEADER_ROW, column=col_idx).value = value

    # 3) Data rows — one `<<define>>` row per route name
    for offset, route_name in enumerate(routes):
        row = FIRST_DATA_ROW + offset
        bid_sheet.cell(row=row, column=3).value = "<<define>>"
        bid_sheet.cell(row=row, column=4).value = route_name
        bid_sheet.cell(row=row, column=5).value = "OriginCity"
        bid_sheet.cell(row=row, column=6).value = "ST"
        # Slot-0 (primary bid) currency is left blank so the reverse writer can
        # populate it; slot-1 currency is pre-set so we can confirm it is not
        # mutated by writes targeted only at slot 0.
        bid_sheet.cell(row=row, column=13).value = "CAD"

    # 4) Optional validationInfo dropdown sheet (TD-007)
    if include_validation_info:
        vi = wb.create_sheet(VALIDATION_INFO_NAME)
        vi.cell(row=1, column=1).value = "Currency"
        vi.cell(row=2, column=1).value = "USD"
        vi.cell(row=3, column=1).value = "CAD"
        vi.cell(row=1, column=2).value = "Equipment Type"
        vi.cell(row=2, column=2).value = "V53DV"
        vi.cell(row=3, column=2).value = "V53RE"

    wb.save(str(path))
    return path


def build_export_workbook(
    path: Path,
    *,
    route_names: list[str] | None = None,
    extra_route_without_template_match: str | None = None,
) -> Path:
    """Write a synthetic priced-export workbook compatible with the mock plan.

    The mock ReversePlanner expects these source headers:
    ``Origin Note`` (join key — Route Name), ``RXO All In Customer Rate``,
    ``MX Cost``, ``Equipment Type Detail``, ``Customer FSC Type``, ``Currency``.
    """
    routes = list(route_names) if route_names is not None else list(ROUTE_NAMES)

    wb = Workbook()
    ws = wb.active
    ws.title = "LaneExport"

    headers = [
        "Origin Note",
        "RXO All In Customer Rate",
        "MX Cost",
        "Equipment Type Detail",
        "Customer FSC Type",
        "Currency",
    ]
    for col_idx, header in enumerate(headers, start=1):
        ws.cell(row=1, column=col_idx).value = header

    for row_offset, route_name in enumerate(routes, start=2):
        ws.cell(row=row_offset, column=1).value = route_name
        ws.cell(row=row_offset, column=2).value = 1500.0 + row_offset  # rate
        ws.cell(row=row_offset, column=3).value = 0.0                  # MX cost
        ws.cell(row=row_offset, column=4).value = "V53DV"
        ws.cell(row=row_offset, column=5).value = "Diesel"
        ws.cell(row=row_offset, column=6).value = "USD"

    if extra_route_without_template_match:
        extra_row = len(routes) + 2
        ws.cell(row=extra_row, column=1).value = extra_route_without_template_match
        ws.cell(row=extra_row, column=2).value = 9999.0

    wb.save(str(path))
    return path
