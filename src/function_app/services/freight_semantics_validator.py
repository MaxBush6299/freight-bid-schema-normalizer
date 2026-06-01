"""freight_semantics_validator.py

Deterministic post-processor that enforces freight-domain semantics on
mappings produced by ``ReversePlanner`` (mock or live mode).

Detects three classes of anti-patterns:

1. **all-in -> linehaul** — source uses an all-in/total/door-to-door
   rate while the target is a linehaul/freight/base-rate column.
   Writing an all-in value into a linehaul cell *double-counts fuel*
   when the template's Total formula adds Fuel Surcharge on top.
2. **categorical -> numeric** — source looks like a label ("Type",
   "Category", "Code") and target expects a number ("Rate", "Cost",
   "Charge", "Fee", "Price", "Amount", "Surcharge").
3. **numeric -> categorical** — the reverse: source is a dollar/percent
   amount and target is a categorical column (excluding identity matches
   like Currency -> Currency).

When an anti-pattern fires the mapping's ``confidence_score`` is demoted
to ``DEMOTED_CONFIDENCE`` (well below the default auto-write threshold)
and ``needs_review`` is set to True with a clear reasoning string.

A single **boost** rule is applied first:

- Identity match (source and target share the same trimmed, lowercased,
  whitespace-collapsed text) is treated as certainty and its confidence
  is raised to at least 0.95 — these are essentially always correct and
  the LLM should not be allowed to demote them with low scores.

The validator is intentionally narrow: it only catches anti-patterns
that are universal to freight RFP templates (Linehaul vs All-In is the
universal industry distinction). It will leave any mapping it does not
recognise untouched, so it does not over-fire on novel templates.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..models.contracts import FieldMapping

# Confidence assigned to a mapping that violates a hard rule. Set well below
# the default auto-write threshold (0.85) so the writer will refuse to act on
# it without explicit operator approval.
DEMOTED_CONFIDENCE: float = 0.40

# Confidence floor applied to identity matches (source == target after
# normalisation). These are essentially always correct.
IDENTITY_BOOST_CONFIDENCE: float = 0.95

# ─────────────────────────────────────────────────────────────────────────────
# Keyword catalogues used by the deterministic rules.  All matching is done
# on lowercased, whitespace-normalised strings via ``_normalize``.
# ─────────────────────────────────────────────────────────────────────────────

# Phrases that identify an all-in / total rate (includes fuel surcharge).
_ALL_IN_SOURCE_KEYWORDS: tuple[str, ...] = (
    "all in",
    "all-in",
    "allin",
    "door to door",
    "door-to-door",
    "total rate",
    "total cost",
    "total shipment",
)

# Substrings that identify a target column that expects a linehaul-only value.
# Match must be the substring present AND the column must NOT contain "total"
# or "all" (which would indicate the target is actually the all-in column).
_LINEHAUL_TARGET_KEYWORDS: tuple[str, ...] = (
    "freight",
    "linehaul",
    "line haul",
    "base rate",
    "flat rate",
)

# Substrings that would indicate the target is itself an all-in / total column
# (where an all-in source IS the right answer).  When any of these appear, the
# all-in -> linehaul rule must NOT fire.
_ALL_IN_TARGET_GUARDS: tuple[str, ...] = (
    "all in",
    "all-in",
    "allin",
    "total",
)

# Substrings that mean "this column holds a category/label, not a number".
_CATEGORICAL_KEYWORDS: tuple[str, ...] = (
    "type",
    "category",
    "currency",
    "mode",
    "code",
    "class",
    "uom",
)

# Substrings that mean "this column holds a numeric monetary/percentage value".
_NUMERIC_KEYWORDS: tuple[str, ...] = (
    "rate",
    "cost",
    "charge",
    "fee",
    "price",
    "amount",
    "surcharge",
    "$",
    "%",
)


@dataclass(frozen=True)
class ValidatorAction:
    """One record describing how the validator changed a mapping."""

    source_field: str
    target_column: str
    rule_id: str
    severity: str  # "warning" | "info"
    message: str
    confidence_before: float
    confidence_after: float


@dataclass
class ValidationResult:
    """Container returned by :func:`validate_mappings`.

    ``mappings`` is the same list passed in (mutated in place — the validator
    rewrites ``confidence_score``, ``needs_review`` and ``reasoning`` on
    affected mappings).  ``actions`` records every rule that fired so it can
    be surfaced in the plan's assumptions / pending_review for the operator.
    """

    mappings: list[FieldMapping]
    actions: list[ValidatorAction]


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


_WHITESPACE_RE = re.compile(r"\s+")


def _normalize(value: str) -> str:
    """Lowercase + collapse whitespace + strip.  Newlines in template headers
    (e.g. ``"Freight \\nPrice"``) become single spaces so substring tests work
    consistently."""
    if value is None:
        return ""
    return _WHITESPACE_RE.sub(" ", str(value)).strip().casefold()


def _contains_any(haystack: str, needles: tuple[str, ...]) -> bool:
    return any(needle in haystack for needle in needles)


def _is_identity_match(source: str, target: str) -> bool:
    """Identity match = same normalised text (ignoring case/whitespace)."""
    if not source or not target:
        return False
    return _normalize(source) == _normalize(target)


def _is_categorical_column(name: str) -> bool:
    """True when the column name signals a categorical/label column.

    A column that contains BOTH a categorical keyword and a numeric keyword
    (e.g. "Rate Type") is ambiguous and treated as numeric to avoid false
    positives on the categorical-to-numeric rule.
    """
    return _contains_any(name, _CATEGORICAL_KEYWORDS) and not _contains_any(
        name, _NUMERIC_KEYWORDS
    )


def _is_numeric_column(name: str) -> bool:
    """True when the column name signals a numeric/monetary column."""
    if _contains_any(name, _NUMERIC_KEYWORDS):
        # "Currency" contains nothing numeric, but "Rate Type" contains "rate".
        # We treat "<numeric> Type" as categorical via _is_categorical_column.
        if _contains_any(name, _CATEGORICAL_KEYWORDS):
            return False
        return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Rule implementations
# Each rule returns a ``ValidatorAction`` when it fires, or None.
# Rules operate on the *normalised* source/target strings.
# ─────────────────────────────────────────────────────────────────────────────


def _rule_identity_boost(mapping: FieldMapping) -> ValidatorAction | None:
    """Boost an identity match (Source name == Target name) to ~certain."""
    if not _is_identity_match(mapping.source_field, mapping.target_column):
        return None
    if mapping.confidence_score >= IDENTITY_BOOST_CONFIDENCE:
        return None
    before = mapping.confidence_score
    mapping.confidence_score = IDENTITY_BOOST_CONFIDENCE
    mapping.needs_review = False
    mapping.reasoning = (
        f"Identity match: source and target both read "
        f"{mapping.source_field!r}; confidence boosted to "
        f"{IDENTITY_BOOST_CONFIDENCE:.2f}."
    )
    return ValidatorAction(
        source_field=mapping.source_field,
        target_column=mapping.target_column,
        rule_id="identity_boost",
        severity="info",
        message=(
            f"Identity match boosted to {IDENTITY_BOOST_CONFIDENCE:.0%} confidence."
        ),
        confidence_before=before,
        confidence_after=mapping.confidence_score,
    )


def _rule_all_in_to_linehaul(
    mapping: FieldMapping, source_n: str, target_n: str
) -> ValidatorAction | None:
    """Demote any mapping that puts an all-in/total source into a linehaul/
    freight/base-rate target.  Writing an all-in value there double-counts
    fuel via the template's Total formula."""
    if not _contains_any(source_n, _ALL_IN_SOURCE_KEYWORDS):
        return None
    if _contains_any(target_n, _ALL_IN_TARGET_GUARDS):
        # Target is itself the all-in column — this is the correct mapping.
        return None
    if not _contains_any(target_n, _LINEHAUL_TARGET_KEYWORDS):
        return None
    before = mapping.confidence_score
    mapping.confidence_score = min(mapping.confidence_score, DEMOTED_CONFIDENCE)
    mapping.needs_review = True
    mapping.reasoning = (
        f"BLOCKED: source {mapping.source_field!r} is an all-in / total rate, "
        f"but target {mapping.target_column!r} is a linehaul/freight column. "
        f"Writing an all-in value here double-counts fuel via the template's "
        f"Total formula. Pick a Linehaul/Base rate source instead, or approve "
        f"this mapping explicitly if you know the template lacks a fuel column."
    )
    return ValidatorAction(
        source_field=mapping.source_field,
        target_column=mapping.target_column,
        rule_id="all_in_to_linehaul",
        severity="warning",
        message=(
            "All-in rate -> linehaul column would double-count fuel; confidence "
            f"demoted to {DEMOTED_CONFIDENCE:.0%}."
        ),
        confidence_before=before,
        confidence_after=mapping.confidence_score,
    )


def _rule_categorical_to_numeric(
    mapping: FieldMapping, source_n: str, target_n: str
) -> ValidatorAction | None:
    """Demote categorical source -> numeric target (e.g. 'Customer FSC Type'
    -> 'Fuel Surcharge Amount').  Writing a string label into a $ column
    breaks every downstream formula."""
    if not _is_categorical_column(source_n):
        return None
    if not _is_numeric_column(target_n):
        return None
    before = mapping.confidence_score
    mapping.confidence_score = min(mapping.confidence_score, DEMOTED_CONFIDENCE)
    mapping.needs_review = True
    mapping.reasoning = (
        f"BLOCKED: source {mapping.source_field!r} looks categorical (label/code) "
        f"but target {mapping.target_column!r} expects a numeric amount. "
        f"This mapping would write a string into a money column."
    )
    return ValidatorAction(
        source_field=mapping.source_field,
        target_column=mapping.target_column,
        rule_id="categorical_to_numeric",
        severity="warning",
        message=(
            "Categorical source -> numeric target column; confidence demoted to "
            f"{DEMOTED_CONFIDENCE:.0%}."
        ),
        confidence_before=before,
        confidence_after=mapping.confidence_score,
    )


def _rule_numeric_to_categorical(
    mapping: FieldMapping, source_n: str, target_n: str
) -> ValidatorAction | None:
    """Demote numeric source -> categorical target (e.g. 'Customer FSC %'
    -> 'Fuel Type').  Writing a percentage into a Type column produces
    nonsense in the dropdown-validated cell."""
    if _is_identity_match(mapping.source_field, mapping.target_column):
        # Identity matches (Currency -> Currency) are always fine, even if
        # the column name happens to live in the categorical bucket.
        return None
    if not _is_numeric_column(source_n):
        return None
    if not _is_categorical_column(target_n):
        return None
    before = mapping.confidence_score
    mapping.confidence_score = min(mapping.confidence_score, DEMOTED_CONFIDENCE)
    mapping.needs_review = True
    mapping.reasoning = (
        f"BLOCKED: source {mapping.source_field!r} is a numeric/monetary value "
        f"but target {mapping.target_column!r} expects a categorical label. "
        f"This mapping would write a number into a dropdown column."
    )
    return ValidatorAction(
        source_field=mapping.source_field,
        target_column=mapping.target_column,
        rule_id="numeric_to_categorical",
        severity="warning",
        message=(
            "Numeric source -> categorical target column; confidence demoted to "
            f"{DEMOTED_CONFIDENCE:.0%}."
        ),
        confidence_before=before,
        confidence_after=mapping.confidence_score,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────


def validate_mappings(mappings: list[FieldMapping]) -> ValidationResult:
    """Run the deterministic freight-semantics rules over ``mappings``.

    Mappings are mutated in place; ``mappings`` is also returned in the
    ``ValidationResult`` for convenience.  Mappings whose ``target_column``
    is empty (UNMAPPED) are skipped entirely.
    """
    actions: list[ValidatorAction] = []

    for mapping in mappings:
        # Skip unmapped entries — nothing to validate.
        if not mapping.target_column or not mapping.source_field:
            continue

        # Identity boost first (so it doesn't get demoted by later rules in
        # the rare case a column name contains both "currency" and "rate").
        identity_action = _rule_identity_boost(mapping)
        if identity_action is not None:
            actions.append(identity_action)
            # Even after a boost we still want to scan anti-patterns — they
            # are mutually exclusive with identity matches, but cheap to check.

        source_n = _normalize(mapping.source_field)
        target_n = _normalize(mapping.target_column)

        # Apply each anti-pattern rule.  Each rule short-circuits on its own
        # conditions; only the FIRST matching anti-pattern record is kept,
        # since they all demote to the same confidence.
        for rule in (
            _rule_all_in_to_linehaul,
            _rule_categorical_to_numeric,
            _rule_numeric_to_categorical,
        ):
            action = rule(mapping, source_n, target_n)
            if action is not None:
                actions.append(action)
                break  # one anti-pattern is enough; don't double-report

    return ValidationResult(mappings=mappings, actions=actions)
