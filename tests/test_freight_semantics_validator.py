"""tests/test_freight_semantics_validator.py

Unit tests for the deterministic freight-semantics validator.

The validator enforces three universal anti-patterns and one identity boost.
These tests cover both the positive (rule fires) and negative (rule does NOT
fire) cases for every rule, plus the edge cases that the original prompt
bug exposed (e.g. newlines in template column names).
"""
from __future__ import annotations

import unittest

from src.function_app.models.contracts import FieldMapping
from src.function_app.services.freight_semantics_validator import (
    DEMOTED_CONFIDENCE,
    IDENTITY_BOOST_CONFIDENCE,
    validate_mappings,
)


def _fm(source: str, target: str, confidence: float = 1.0) -> FieldMapping:
    return FieldMapping(
        source_field=source,
        target_column=target,
        confidence_score=confidence,
    )


class TestAllInToLinehaulRule(unittest.TestCase):
    """The headline bug — must demote any all-in source written to a
    linehaul/freight target."""

    def test_rxo_all_in_to_freight_price_is_demoted(self) -> None:
        m = _fm("RXO All In Customer Rate", "Freight \nPrice", confidence=0.95)
        result = validate_mappings([m])
        self.assertEqual(m.confidence_score, DEMOTED_CONFIDENCE)
        self.assertTrue(m.needs_review)
        self.assertIn("all-in", (m.reasoning or "").lower())
        self.assertEqual(len(result.actions), 1)
        self.assertEqual(result.actions[0].rule_id, "all_in_to_linehaul")
        self.assertEqual(result.actions[0].severity, "warning")

    def test_all_in_to_linehaul_target_is_demoted(self) -> None:
        m = _fm("Customer All-In Rate", "Linehaul Rate", confidence=0.9)
        validate_mappings([m])
        self.assertEqual(m.confidence_score, DEMOTED_CONFIDENCE)
        self.assertTrue(m.needs_review)

    def test_total_cost_to_base_rate_is_demoted(self) -> None:
        m = _fm("Total Cost Per Load", "Base Rate", confidence=0.85)
        validate_mappings([m])
        self.assertEqual(m.confidence_score, DEMOTED_CONFIDENCE)

    def test_linehaul_to_freight_price_is_NOT_demoted(self) -> None:
        m = _fm("Customer Linehaul Rate", "Freight \nPrice", confidence=0.9)
        validate_mappings([m])
        self.assertEqual(m.confidence_score, 0.9)
        self.assertFalse(m.needs_review)

    def test_base_rate_to_freight_price_is_NOT_demoted(self) -> None:
        m = _fm("Customer Base Rate", "Freight Price", confidence=0.8)
        validate_mappings([m])
        self.assertEqual(m.confidence_score, 0.8)

    def test_all_in_to_total_shipment_target_is_NOT_demoted(self) -> None:
        """If the target is the all-in/total column itself, the all-in source
        IS the right answer (even though we'd normally avoid writing to a
        formula cell — that's a different concern handled by the writer)."""
        m = _fm("RXO All In Customer Rate", "Total Shipment Rate", confidence=0.7)
        validate_mappings([m])
        self.assertEqual(m.confidence_score, 0.7)

    def test_total_rate_keyword_in_target_guards_against_demotion(self) -> None:
        """A target like 'Total Linehaul Rate' contains both 'total' and
        'linehaul'; the 'total' guard takes precedence and the rule does
        NOT fire."""
        m = _fm("All In Rate", "Total Linehaul Rate", confidence=0.7)
        validate_mappings([m])
        self.assertEqual(m.confidence_score, 0.7)


class TestCategoricalToNumericRule(unittest.TestCase):
    """Categorical source written to a $ column is broken — must be demoted."""

    def test_fsc_type_to_fuel_surcharge_amount_is_demoted(self) -> None:
        m = _fm("Customer FSC Type", "Fuel Surcharge Amount", confidence=0.9)
        validate_mappings([m])
        self.assertEqual(m.confidence_score, DEMOTED_CONFIDENCE)
        self.assertTrue(m.needs_review)

    def test_equipment_category_to_freight_charge_is_demoted(self) -> None:
        m = _fm("Equipment Category", "Freight Charge", confidence=0.85)
        validate_mappings([m])
        self.assertEqual(m.confidence_score, DEMOTED_CONFIDENCE)

    def test_currency_to_currency_is_NOT_demoted_even_though_categorical(self) -> None:
        """Currency -> Currency is identity; categorical target is fine."""
        m = _fm("Currency", "Currency", confidence=0.5)
        validate_mappings([m])
        self.assertGreaterEqual(m.confidence_score, IDENTITY_BOOST_CONFIDENCE)

    def test_rate_type_to_freight_price_NOT_treated_as_categorical(self) -> None:
        """Ambiguous source ('Rate Type' contains both 'rate' AND 'type')
        is treated as numeric to avoid false positives."""
        m = _fm("Rate Type", "Freight Price", confidence=0.6)
        validate_mappings([m])
        # Source 'rate type' has both numeric+categorical keywords so
        # _is_categorical_column returns False, and the rule doesn't fire.
        self.assertEqual(m.confidence_score, 0.6)


class TestNumericToCategoricalRule(unittest.TestCase):
    """Numeric source written to a Type/Code/Mode column is broken."""

    def test_fsc_percent_to_fuel_type_is_demoted(self) -> None:
        m = _fm("Customer FSC %", "Fuel Type", confidence=0.9)
        validate_mappings([m])
        self.assertEqual(m.confidence_score, DEMOTED_CONFIDENCE)
        self.assertTrue(m.needs_review)

    def test_linehaul_rate_to_equipment_type_is_demoted(self) -> None:
        m = _fm("Customer Linehaul Rate", "Equipment Type", confidence=0.95)
        validate_mappings([m])
        self.assertEqual(m.confidence_score, DEMOTED_CONFIDENCE)

    def test_freight_price_to_mode_of_transport_is_demoted(self) -> None:
        m = _fm("Freight Price", "Mode of Transport", confidence=0.8)
        validate_mappings([m])
        self.assertEqual(m.confidence_score, DEMOTED_CONFIDENCE)


class TestIdentityBoost(unittest.TestCase):
    """Identity matches must be lifted to ~certain confidence."""

    def test_exact_identity_is_boosted_to_floor(self) -> None:
        m = _fm("Currency", "Currency", confidence=0.5)
        result = validate_mappings([m])
        self.assertEqual(m.confidence_score, IDENTITY_BOOST_CONFIDENCE)
        self.assertFalse(m.needs_review)
        self.assertTrue(any(a.rule_id == "identity_boost" for a in result.actions))

    def test_identity_match_with_whitespace_diff_is_boosted(self) -> None:
        m = _fm("Equipment Type", "equipment type", confidence=0.4)
        validate_mappings([m])
        self.assertEqual(m.confidence_score, IDENTITY_BOOST_CONFIDENCE)

    def test_identity_match_with_newline_is_boosted(self) -> None:
        """Newlines in template column names must not break identity match."""
        m = _fm("Freight Price", "Freight \nPrice", confidence=0.0)
        validate_mappings([m])
        self.assertEqual(m.confidence_score, IDENTITY_BOOST_CONFIDENCE)

    def test_identity_above_boost_floor_is_left_alone(self) -> None:
        m = _fm("Currency", "Currency", confidence=1.0)
        result = validate_mappings([m])
        self.assertEqual(m.confidence_score, 1.0)
        # No action recorded since boost would not change the score
        self.assertFalse(any(a.rule_id == "identity_boost" for a in result.actions))


class TestUnchangedMappings(unittest.TestCase):
    """Mappings that don't trigger any rule must be left exactly as-is."""

    def test_equipment_type_detail_to_equipment_type_unchanged(self) -> None:
        """Both sides are categorical; vocabulary mismatch (V53DV vs T53DV)
        is the operator's problem — validator can't know that."""
        m = _fm("Equipment Type Detail", "Equipment Type", confidence=0.75)
        result = validate_mappings([m])
        self.assertEqual(m.confidence_score, 0.75)
        self.assertEqual(result.actions, [])

    def test_unrelated_accessorial_mappings_unchanged(self) -> None:
        """MX Cost -> ORC Charges is both-numeric; validator can't tell they
        refer to different accessorial categories — operator must catch it."""
        m = _fm("MX Cost", "ORC Charges", confidence=0.6)
        result = validate_mappings([m])
        self.assertEqual(m.confidence_score, 0.6)
        self.assertEqual(result.actions, [])

    def test_unmapped_target_is_skipped(self) -> None:
        """target_column == '' (UNMAPPED) bypasses all rules."""
        m = _fm("RXO All In Customer Rate", "", confidence=0.0)
        result = validate_mappings([m])
        self.assertEqual(m.confidence_score, 0.0)
        self.assertEqual(result.actions, [])

    def test_empty_source_is_skipped(self) -> None:
        m = _fm("", "Freight Price", confidence=0.0)
        result = validate_mappings([m])
        self.assertEqual(result.actions, [])


class TestMixedBatch(unittest.TestCase):
    """A realistic plan with a mix of good and bad mappings — each gets
    independently classified."""

    def test_mixed_batch_processes_each_mapping(self) -> None:
        mappings = [
            _fm("Customer Linehaul Rate", "Freight \nPrice", confidence=0.9),
            _fm("RXO All In Customer Rate", "Toll Charges", confidence=0.7),
            _fm("Currency", "Currency", confidence=0.5),
            _fm("Customer FSC Type", "Fuel Surcharge Amount", confidence=0.8),
            _fm("Equipment Type Detail", "Equipment Type", confidence=0.6),
        ]
        result = validate_mappings(mappings)

        # 1. Linehaul -> Freight Price: untouched
        self.assertEqual(mappings[0].confidence_score, 0.9)

        # 2. All-In -> Toll Charges: source IS all-in but target doesn't
        # match the linehaul keyword set, so no rule fires.
        self.assertEqual(mappings[1].confidence_score, 0.7)

        # 3. Currency -> Currency: identity boost
        self.assertEqual(mappings[2].confidence_score, IDENTITY_BOOST_CONFIDENCE)

        # 4. FSC Type -> Fuel Surcharge Amount: categorical -> numeric demoted
        self.assertEqual(mappings[3].confidence_score, DEMOTED_CONFIDENCE)
        self.assertTrue(mappings[3].needs_review)

        # 5. Equipment Type Detail -> Equipment Type: both categorical, no rule
        self.assertEqual(mappings[4].confidence_score, 0.6)

        # Action count: 1 identity boost + 1 categorical_to_numeric = 2
        rule_ids = sorted(a.rule_id for a in result.actions)
        self.assertEqual(rule_ids, ["categorical_to_numeric", "identity_boost"])


class TestReturnedResult(unittest.TestCase):
    """The ValidationResult must carry the same list reference (in-place
    mutation) and one action per rule firing."""

    def test_result_mappings_is_same_list(self) -> None:
        mappings = [_fm("Currency", "Currency", confidence=0.5)]
        result = validate_mappings(mappings)
        self.assertIs(result.mappings, mappings)

    def test_each_action_carries_before_and_after_confidence(self) -> None:
        m = _fm("RXO All In Customer Rate", "Freight Price", confidence=0.85)
        result = validate_mappings([m])
        self.assertEqual(len(result.actions), 1)
        action = result.actions[0]
        self.assertEqual(action.confidence_before, 0.85)
        self.assertEqual(action.confidence_after, DEMOTED_CONFIDENCE)


if __name__ == "__main__":
    unittest.main()
