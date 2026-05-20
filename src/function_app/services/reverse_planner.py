"""reverse_planner.py

Produces a ReverseMappingPlan — a list of FieldMapping rules that describe
how priced-export columns map to writable cells in the customer template.

Modes
-----
mock  (default) — deterministic rule-based mappings derived from inspection of
                  the RXO FTL LaneExport and Coupa bid template column names.
                  No network calls; suitable for local development and testing.
live  — iterative LLM mapping via LLMMappingService + FoundryAgentClient.
        Uses 3-round refinement (propose → self-critique → refine).
        Low-confidence mappings (< review_threshold) go to pending_review.

The plan is keyed by template_fingerprint so it can be cached and reused across
repeat bid cycles without re-running the planner.
"""
from __future__ import annotations

import os
import uuid
from typing import Any

from ..models.contracts import FieldMapping, ReverseMappingPlan, TemplateProfile
from .foundry_agent_client import FoundryAgentClient
from .llm_mapping_service import LLMMappingService

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
    """Build a ReverseMappingPlan from a TemplateProfile and export columns.

    Args:
        mode: ``"mock"`` for rule-based defaults, ``"live"`` for LLM iterative mapping.
        review_threshold: Confidence score below which a mapping is sent to pending_review.
        max_iterations: Maximum LLM refinement rounds (live mode only).
        foundry_mode: Passed to FoundryAgentClient (``"mock"`` or ``"live"``).
    """

    def __init__(
        self,
        mode: str = "mock",
        review_threshold: float = 0.70,
        max_iterations: int = 3,
        foundry_mode: str | None = None,
    ) -> None:
        if mode not in ("mock", "live"):
            raise ValueError(f"Unknown planner mode: {mode!r}. Use 'mock' or 'live'.")
        self.mode = mode
        self.review_threshold = review_threshold
        self.max_iterations = max_iterations
        # Foundry mode: honour explicit env var; default to mock for local testing.
        # planner_mode=live means "use iterative LLM logic", not necessarily a real endpoint.
        self._foundry_mode = foundry_mode or os.getenv("REHYDRATE_FOUNDRY_MODE", "mock")

    def build_plan(
        self,
        template_profile: TemplateProfile,
        export_columns: list[str] | None = None,
    ) -> ReverseMappingPlan:
        if self.mode == "mock":
            return self._mock_plan(template_profile)
        return self._live_plan(template_profile, export_columns or [])

    # ── mock ──────────────────────────────────────────────────────────────────

    def _mock_plan(self, template_profile: TemplateProfile) -> ReverseMappingPlan:
        mappings = [
            FieldMapping(
                source_field=m["source_field"],
                target_column=m["target_column"],
                bid_slot=m["bid_slot"],
                value_transform=m.get("value_transform"),
                confidence_score=1.0,
                reasoning="rule-based mock mapping",
            )
            for m in _MOCK_MAPPINGS
        ]
        return ReverseMappingPlan(
            plan_id=str(uuid.uuid4()),
            template_fingerprint=template_profile.template_fingerprint,
            planner_mode="mock",
            mappings=mappings,
            assumptions=_MOCK_ASSUMPTIONS,
            pending_review=[],
            iterations_run=0,
        )

    # ── live (LLM) ────────────────────────────────────────────────────────────

    def _live_plan(
        self,
        template_profile: TemplateProfile,
        export_columns: list[str],
    ) -> ReverseMappingPlan:
        # Collect writable template column names from all bid sheet profiles
        template_cols: list[str] = []
        for sheet_profile in template_profile.bid_sheets:
            for slot in sheet_profile.bid_slots:
                template_cols.extend(slot.writable_columns)
        # Deduplicate while preserving order
        seen: set[str] = set()
        unique_template_cols = [c for c in template_cols if not (c in seen or seen.add(c))]  # type: ignore[func-returns-value]

        client = FoundryAgentClient(mode=self._foundry_mode)
        svc = LLMMappingService(
            client=client,
            review_threshold=self.review_threshold,
            max_iterations=self.max_iterations,
        )
        field_mappings, pending_review, iterations_run = svc.map(
            export_columns=export_columns,
            template_columns=unique_template_cols,
        )

        assumptions = [
            f"LLM mapping ran {iterations_run} refinement round(s).",
            f"Review threshold: {self.review_threshold} — {len(pending_review)} mapping(s) flagged for human review.",
            "Only column names were provided to the LLM; no data values were shared.",
        ]

        return ReverseMappingPlan(
            plan_id=str(uuid.uuid4()),
            template_fingerprint=template_profile.template_fingerprint,
            planner_mode="live",
            mappings=field_mappings,
            assumptions=assumptions,
            pending_review=pending_review,
            iterations_run=iterations_run,
        )
