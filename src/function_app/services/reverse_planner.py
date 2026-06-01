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

from ..models.contracts import FieldMapping, HumanReviewRequest, ReverseMappingPlan, TemplateProfile
from .foundry_agent_client import FoundryAgentClient
from .freight_semantics_validator import validate_mappings
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
    # Primary rate — write the LINEHAUL rate to Freight Price.
    # Freight Price is universally a linehaul-only cell when the template also
    # has a separate Fuel Surcharge column (which RXO templates do). Writing
    # an all-in value here would double-count fuel via the Total formula.
    # The template calculates Fuel Surcharge and Total Shipment Rate via its
    # own formulas, so we do NOT write to those (they are formula-protected).
    {
        "source_field": "Customer Linehaul Rate",
        "target_column": "Freight \nPrice",
        "bid_slot": 0,
        "value_transform": "round_2",
        "note": "Linehaul rate → Freight Price (linehaul-only cell; fuel is calculated separately)",
    },
    # Reference / classification fields with exact-match identity targets
    {
        "source_field": "Equipment Type Detail",
        "target_column": "Equipment Type",
        "bid_slot": 0,
        "value_transform": "none",
        "note": "Equipment Type Detail → Equipment Type (different vocabularies — flagged for review)",
    },
    {
        "source_field": "Currency",
        "target_column": "Currency",
        "bid_slot": 0,
        "value_transform": "none",
        "note": "Currency → Currency (identity match)",
    },
]

_MOCK_ASSUMPTIONS = [
    "Primary bid slot (slot 0) is targeted; alternative slots left blank.",
    "Freight Price receives Customer Linehaul Rate (LINEHAUL, not all-in). "
    "The template computes Fuel Surcharge and Total Shipment Rate from this "
    "value via its own formulas — writing an all-in value would double-count fuel.",
    "Fuel Surcharge and Total Shipment Rate are formula-protected mirror cells; they are NOT written.",
    "MX Cost / Border Crossing Fee / ORC Charges are not assumed to map from this export — "
    "those accessorial categories need explicit operator approval.",
    "Customer FSC Type (BreakthroughFuel etc.) is categorical and is not mapped to Fuel Type "
    "(which expects 'Diesel' / 'Gasoline'-style labels) without operator review.",
    "Equipment Type Detail is written into Equipment Type as-is even though the vocabularies "
    "may differ (V53DV vs T53DV); operator should confirm during mapping review.",
    "Currency → Currency is an exact identity match and is written as-is.",
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
            plan = self._mock_plan(template_profile)
        else:
            plan = self._live_plan(template_profile, export_columns or [])
        return self._apply_semantics_validator(plan)

    # ── deterministic post-validator ─────────────────────────────────────────

    @staticmethod
    def _apply_semantics_validator(plan: ReverseMappingPlan) -> ReverseMappingPlan:
        """Run the deterministic freight-semantics validator on the mappings.

        The validator mutates ``plan.mappings`` in place: it demotes confidence
        on anti-patterns (e.g. all-in source -> linehaul target) and boosts
        identity matches. After it runs we rebuild ``pending_review`` from the
        updated ``needs_review`` flags so the operator sees every demoted
        mapping during the review step.
        """
        result = validate_mappings(plan.mappings)
        if result.actions:
            plan.assumptions = list(plan.assumptions) + [
                f"[validator/{a.rule_id}] {a.message}" for a in result.actions
            ]
        plan.pending_review = _rebuild_pending_review(plan.mappings)
        return plan

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

        client = FoundryAgentClient(
            mode=self._foundry_mode,
            agent_name=os.getenv("FOUNDRY_REVERSE_MAPPER_AGENT_NAME", "RXO-Reverse-Mapper"),
            agent_version=os.getenv("FOUNDRY_REVERSE_MAPPER_AGENT_VERSION", "1"),
        )
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


def _rebuild_pending_review(mappings: list[FieldMapping]) -> list[HumanReviewRequest]:
    """Re-derive ``pending_review`` from the current ``needs_review`` flags.

    After the freight-semantics validator mutates mappings, the planner-supplied
    pending_review list may be stale (validator may have demoted previously
    high-confidence mappings). Rebuilding from scratch is cheap and ensures
    every mapping needing operator approval appears exactly once.
    """
    seen: set[tuple[str, str]] = set()
    out: list[HumanReviewRequest] = []
    for fm in mappings:
        if not fm.needs_review:
            continue
        key = (fm.source_field, fm.target_column)
        if key in seen:
            continue
        seen.add(key)
        out.append(HumanReviewRequest(
            source_field=fm.source_field,
            target_column=fm.target_column,
            confidence_score=fm.confidence_score,
            reasoning=fm.reasoning,
        ))
    return out
