"""LLM-driven iterative field mapping service for the reverse (Rehydrate) pipeline.

Design principles:
- LLM sees only *column name lists*, never actual data values.  This prevents
  overfitting to a single template and keeps the service usable for any new
  customer template without prior examples.
- Three-round refinement loop:
    Round 1 – Propose a full mapping from export→template column names.
    Round 2 – Self-critique every proposed mapping; assign a confidence score
              (0.0–1.0) and a brief reasoning string.
    Round 3 – Refine mappings that scored below `refinement_threshold` by
              applying freight-domain context clues (rate fields, lane IDs, etc.).
- Any mapping whose final confidence score remains below `review_threshold`
  (default 0.70) is flagged as `needs_review=True` and added to a
  `HumanReviewRequest` list so a human operator can confirm or override before
  cells are written.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, List

from ..models.contracts import FieldMapping, HumanReviewRequest
from .foundry_agent_client import FoundryAgentClient

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# ──────────────────────────────────────────────────────────────────────────────
# Prompt templates (inline fallback; live mode reads from prompts/ directory)
# ──────────────────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are a freight bid schema mapping expert.
Your task is to match columns from a priced export file to writable columns in a
customer bid template so that pricing can be written back into the template.

CORE PRINCIPLES
- Base your mappings solely on column *names*. Do not invent or infer data values.
- Every source column maps to at most one target column, or "UNMAPPED" if no safe
  match exists. "UNMAPPED" is always a safe answer when you are uncertain.
- Do NOT map to template columns whose header contains "(calc)" or which look
  like derived totals — they are formula cells the template computes itself.

FREIGHT RATE DOMAIN RULES (these prevent the most common bugs)

Rule 1 — Linehaul vs All-In rates (critical):
  Freight rates come in two flavours:
    * "Linehaul" / "Base" / "Flat" rate = base price only, excludes fuel surcharge
    * "All-In" / "Total" / "Door-to-Door" rate = includes fuel surcharge
  A template column whose header reads "Freight Price", "Linehaul Rate",
  "Base Rate", or "Flat Rate" expects LINEHAUL ONLY. If the template ALSO has
  a separate "Fuel Surcharge" column (writable or calc), the freight column
  MUST be linehaul; writing an all-in value there double-counts fuel.
  A column called "Total Shipment Rate" or "All-In Rate" expects the all-in
  value (but those are usually formula cells — leave them UNMAPPED).

Rule 2 — Categorical vs Numeric columns:
  A column header that ends in "Type", "Category", "Mode", "Currency", or
  "Code" expects a short string label, NEVER a dollar amount. A column
  containing "Rate", "Cost", "Charge", "Fee", "Price", "Amount", or "$"
  expects a number, NEVER a categorical label.

Rule 3 — Identity-first matching:
  When a source field and target field share the same word (case-insensitive,
  ignoring whitespace), prefer that mapping unless Rules 1 or 2 forbid it.
  Examples: "Currency" -> "Currency"; "Customer Linehaul Rate" -> "Linehaul Rate".

Rule 4 — Conservative when ambiguous:
  If a source field could plausibly map to two different target columns, pick
  the more specific one. If you cannot pick confidently, return "UNMAPPED" with
  a low confidence score rather than guessing.

GOOD EXAMPLES
  "Customer Linehaul Rate"   -> "Freight Price"          (linehaul -> linehaul OK)
  "Currency"                 -> "Currency"               (identity match OK)
  "Origin Note"              -> "Route Name"             (route join key OK)

BAD EXAMPLES -- DO NOT DO THESE
  "RXO All In Customer Rate" -> "Freight Price"          (all-in -> linehaul BAD: double-counts fuel)
  "Customer FSC %"           -> "Fuel Type"              (numeric -> categorical BAD)
  "Customer FSC Type"        -> "Fuel Surcharge Amount"  (categorical -> numeric BAD)
  "MX Cost"                  -> "ORC Charges"            (different accessorials BAD: unrelated)
  any source                 -> "Total Shipment Rate"    (it's a calc formula BAD)

OUTPUT
Return strictly valid JSON -- no prose, no markdown fences.
"""

_ROUND1_USER_TEMPLATE = """\
Round 1 – Propose mappings.

Export columns (source):
{export_cols_json}

Template writable columns (target):
{template_cols_json}

Return JSON:
{{
  "mappings": [
    {{"source": "<export col>", "target": "<template col or UNMAPPED>"}}
  ]
}}
"""

_ROUND2_USER_TEMPLATE = """\
Round 2 – Self-critique.

Current mappings:
{current_mappings_json}

Export columns: {export_cols_json}
Template columns: {template_cols_json}

For each mapping assign:
  "confidence": float 0.0–1.0 (1.0 = certain, 0.0 = no idea)
  "reasoning": one-sentence explanation

Return JSON:
{{
  "mappings": [
    {{"source": "...", "target": "...", "confidence": 0.0, "reasoning": "..."}}
  ]
}}
"""

_ROUND3_USER_TEMPLATE = """\
Round 3 – Refine low-confidence mappings.

Focus ONLY on these low-confidence mappings (confidence < {threshold}):
{low_conf_json}

Export columns: {export_cols_json}
Template columns: {template_cols_json}

Re-apply the linehaul-vs-all-in rule, the categorical-vs-numeric rule, and the
identity-first rule from the system prompt. When a source name and target name
share a distinctive word (e.g. both contain "Linehaul" or both contain
"Currency") prefer that mapping. Mark a mapping UNMAPPED if no safe match
exists.

Return JSON with ALL previously proposed mappings (revised + unchanged):
{{
  "mappings": [
    {{"source": "...", "target": "...", "confidence": 0.0, "reasoning": "..."}}
  ]
}}
"""


# ──────────────────────────────────────────────────────────────────────────────
# Mock response (used when FoundryAgentClient is in mock mode)
# ──────────────────────────────────────────────────────────────────────────────

def _mock_map(export_cols: List[str], template_cols: List[str]) -> List[dict]:
    """Return a plausible high-confidence mock mapping for local testing.

    Matches on simple keyword heuristics so the mock result exercises the full
    downstream path without requiring an LLM endpoint.
    """
    template_lower = {c.lower().replace("\\n", " ").replace("\n", " "): c for c in template_cols}

    KEYWORD_MAP = [
        # Linehaul (base) rate → Freight Price (linehaul-only target).
        # The "all-in" alternative is intentionally NOT mapped here: when a
        # template has separate freight + fuel columns, writing an all-in
        # value into Freight Price double-counts fuel via the Total formula.
        (["linehaul", "base rate", "flat rate"],         ["freight", "linehaul", "base rate"]),
        (["equipment type detail", "equip type"],         ["equipment type"]),
        (["currency"],                                     ["currency"]),
    ]

    mappings: list[dict] = []
    used_targets: set[str] = set()

    for src in export_cols:
        src_low = src.lower()
        target = "UNMAPPED"
        for src_kw_list, tgt_kw_list in KEYWORD_MAP:
            if any(kw in src_low for kw in src_kw_list):
                for tgt_kw in tgt_kw_list:
                    for tgt_raw, tgt_original in template_lower.items():
                        if tgt_kw in tgt_raw and tgt_original not in used_targets:
                            target = tgt_original
                            used_targets.add(tgt_original)
                            break
                if target != "UNMAPPED":
                    break

        mappings.append({
            "source": src,
            "target": target,
            "confidence": 0.95 if target != "UNMAPPED" else 0.20,
            "reasoning": "mock heuristic match" if target != "UNMAPPED" else "no obvious match found",
        })

    return mappings


# ──────────────────────────────────────────────────────────────────────────────
# LLMMappingService
# ──────────────────────────────────────────────────────────────────────────────

class LLMMappingService:
    """Iterative LLM-based mapping between export columns and template columns.

    Args:
        client: A ``FoundryAgentClient`` instance (mock or live).
        review_threshold: Mappings below this score are flagged for human review.
        refinement_threshold: Mappings below this score are sent to Round 3.
        max_iterations: Maximum number of refinement rounds (default 3).
    """

    def __init__(
        self,
        client: FoundryAgentClient,
        review_threshold: float = 0.70,
        refinement_threshold: float = 0.80,
        max_iterations: int = 3,
    ) -> None:
        self.client = client
        self.review_threshold = review_threshold
        self.refinement_threshold = refinement_threshold
        self.max_iterations = max_iterations

    # ── public ────────────────────────────────────────────────────────────────

    def map(
        self,
        export_columns: List[str],
        template_columns: List[str],
    ) -> tuple[List[FieldMapping], List[HumanReviewRequest], int]:
        """Run the iterative mapping loop.

        Returns:
            (mappings, pending_review, iterations_run)
        """
        if self.client.mode == "mock":
            raw = _mock_map(export_columns, template_columns)
            field_mappings = self._raw_to_field_mappings(raw)
            pending = self._build_pending_review(field_mappings)
            return field_mappings, pending, 0

        return self._live_map(export_columns, template_columns)

    # ── private ───────────────────────────────────────────────────────────────

    def _live_map(
        self,
        export_cols: List[str],
        template_cols: List[str],
    ) -> tuple[List[FieldMapping], List[HumanReviewRequest], int]:
        export_json = json.dumps(export_cols)
        template_json = json.dumps(template_cols)

        # Round 1 – propose
        r1_response = self._call(
            system=_SYSTEM_PROMPT,
            user=_ROUND1_USER_TEMPLATE.format(
                export_cols_json=export_json,
                template_cols_json=template_json,
            ),
        )
        current_mappings: List[dict] = r1_response.get("mappings", [])
        logger.debug("Round 1: %d mappings proposed", len(current_mappings))

        iterations = 1

        for iteration in range(2, self.max_iterations + 1):
            if iteration == 2:
                # Round 2 – self-critique + confidence
                r2_response = self._call(
                    system=_SYSTEM_PROMPT,
                    user=_ROUND2_USER_TEMPLATE.format(
                        current_mappings_json=json.dumps(current_mappings),
                        export_cols_json=export_json,
                        template_cols_json=template_json,
                    ),
                )
                current_mappings = r2_response.get("mappings", current_mappings)
                iterations = 2
                logger.debug("Round 2: confidence scores added")
            else:
                # Round 3+ – refine low-confidence mappings
                low_conf = [
                    m for m in current_mappings
                    if m.get("confidence", 1.0) < self.refinement_threshold
                ]
                if not low_conf:
                    logger.debug("No low-confidence mappings; stopping at iteration %d", iteration)
                    break

                r3_response = self._call(
                    system=_SYSTEM_PROMPT,
                    user=_ROUND3_USER_TEMPLATE.format(
                        low_conf_json=json.dumps(low_conf),
                        threshold=self.refinement_threshold,
                        export_cols_json=export_json,
                        template_cols_json=template_json,
                    ),
                )
                refined = r3_response.get("mappings", [])
                # Merge refinements back by source key
                refined_by_src = {m["source"]: m for m in refined}
                current_mappings = [
                    refined_by_src.get(m["source"], m) for m in current_mappings
                ]
                iterations = iteration
                logger.debug("Round %d: %d low-conf mappings refined", iteration, len(low_conf))

        field_mappings = self._raw_to_field_mappings(current_mappings)
        pending = self._build_pending_review(field_mappings)
        return field_mappings, pending, iterations

    def _call(self, system: str, user: str) -> dict[str, Any]:
        """Invoke the LLM and parse the JSON response."""
        raw = self.client.plan(system_prompt=system, user_prompt=user)
        if isinstance(raw, dict) and "mappings" in raw:
            return raw
        # Try to parse text field if wrapped
        text = raw.get("text") or raw.get("content") or ""
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("LLM response parse failed: %s — raw: %s", exc, str(raw)[:200])
            return {}

    @staticmethod
    def _raw_to_field_mappings(raw: List[dict]) -> List[FieldMapping]:
        result: List[FieldMapping] = []
        for m in raw:
            src = m.get("source", "")
            tgt = m.get("target", "UNMAPPED")
            conf = float(m.get("confidence", 1.0))
            reasoning = m.get("reasoning")
            if not src:
                continue
            result.append(FieldMapping(
                source_field=src,
                target_column=tgt if tgt != "UNMAPPED" else "",
                confidence_score=conf,
                reasoning=reasoning,
                needs_review=False,  # set by _build_pending_review
            ))
        return result

    def _build_pending_review(
        self, mappings: List[FieldMapping]
    ) -> List[HumanReviewRequest]:
        pending: List[HumanReviewRequest] = []
        for fm in mappings:
            if fm.confidence_score < self.review_threshold:
                fm.needs_review = True
                pending.append(HumanReviewRequest(
                    source_field=fm.source_field,
                    target_column=fm.target_column,
                    confidence_score=fm.confidence_score,
                    reasoning=fm.reasoning,
                ))
        return pending
