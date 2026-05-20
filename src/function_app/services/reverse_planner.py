"""reverse_planner.py

Produces a ReverseMappingPlan — a list of FieldMapping rules that describe
how priced-export columns map to writable cells in the customer template.

Modes
-----
mock  (default) — deterministic rule-based mappings derived from inspection of
                  the RXO FTL LaneExport and Coupa bid template column names.
                  No network calls; suitable for local development and testing.
live  — reserved for future Foundry agent integration (raises NotImplementedError).

The plan is keyed by template_fingerprint so it can be cached and reused across
repeat bid cycles without re-running the planner.
"""
from __future__ import annotations

import uuid
from typing import Any

from ..models.contracts import FieldMapping, ReverseMappingPlan, TemplateProfile

# ---------------------------------------------------------------------------
# Mock mapping rules
# These were derived by inspecting:
#   - FTL LaneExport.xlsx column headers (export source)
#   - Original Customer File 1.xlsx FTL | Road bid slot headers (template target)
#
# Convention: target_column must match the header string in the template
# (whitespace/newlines stripped for comparison at write time).
# ---------------------------------------------------------------------------
_MOCK_MAPPINGS: list[dict[str, Any]] = [
    # Primary rate — write the all-in rate to Freight Price (the template's main input cell).
    # The template calculates Fuel Surcharge and Total Shipment Rate via its own formulas,
    # so we do NOT write to those (they are formula-protected mirror cells).
    {
        "source_field": "RXO All In Customer Rate",
        "target_column": "Freight \nPrice",
        "bid_slot": 0,
        "value_transform": "round_2",
        "note": "All-in customer rate → Freight Price (template's primary rate input)",
    },
    # Accessorial / cross-border
    {
        "source_field": "MX Cost",
        "target_column": "ORC \nCharges",
        "bid_slot": 0,
        "value_transform": "round_2",
        "note": "Mexico cost → ORC Charges (closest available template field)",
    },
    # Reference / classification fields
    {
        "source_field": "Equipment Type Detail",
        "target_column": "Equipment Type",
        "bid_slot": 0,
        "value_transform": "none",
    },
    {
        "source_field": "Customer FSC Type",
        "target_column": "Fuel Type",
        "bid_slot": 0,
        "value_transform": "none",
    },
    {
        "source_field": "Currency",
        "target_column": "Currency",
        "bid_slot": 0,
        "value_transform": "none",
    },
]

_MOCK_ASSUMPTIONS = [
    "Primary bid slot (slot 0) is targeted; alternative slots left blank.",
    "RXO All In Customer Rate is written to Freight Price (the template's only writable rate field).",
    "Fuel Surcharge and Total Shipment Rate are formula-protected mirror cells; they are NOT written.",
    "The template auto-calculates Fuel Surcharge and Total Shipment Rate from its built-in formulas.",
    "MX Cost is mapped to ORC Charges as the closest available writable template field.",
    "Equipment Type Detail, Currency, and Fuel Type are written as-is (no transform).",
    "Route Name matching uses Origin Note field from export (exact string match).",
]


class ReversePlanner:
    """Build a ReverseMappingPlan from a TemplateProfile and export columns."""

    def __init__(self, mode: str = "mock") -> None:
        if mode not in ("mock", "live"):
            raise ValueError(f"Unknown planner mode: {mode!r}. Use 'mock' or 'live'.")
        self.mode = mode

    def build_plan(
        self,
        template_profile: TemplateProfile,
        export_columns: list[str] | None = None,
    ) -> ReverseMappingPlan:
        if self.mode == "live":
            raise NotImplementedError(
                "Live Foundry agent planning is not yet implemented. Use mode='mock'."
            )
        return self._mock_plan(template_profile)

    def _mock_plan(self, template_profile: TemplateProfile) -> ReverseMappingPlan:
        mappings = [
            FieldMapping(
                source_field=m["source_field"],
                target_column=m["target_column"],
                bid_slot=m["bid_slot"],
                value_transform=m.get("value_transform"),
            )
            for m in _MOCK_MAPPINGS
        ]
        return ReverseMappingPlan(
            plan_id=str(uuid.uuid4()),
            template_fingerprint=template_profile.template_fingerprint,
            planner_mode="mock",
            mappings=mappings,
            assumptions=_MOCK_ASSUMPTIONS,
        )
