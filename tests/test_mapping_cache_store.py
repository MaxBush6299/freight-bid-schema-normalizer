"""tests/test_mapping_cache_store.py

Round-trip + edge-case tests for MappingCacheStore.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from src.function_app.models.contracts import FieldMapping, ReverseMappingPlan
from src.function_app.services.mapping_cache_store import (
    CACHE_SCHEMA_VERSION,
    MappingCacheStore,
)


def _plan(fingerprint: str = "abcdef1234567890") -> ReverseMappingPlan:
    return ReverseMappingPlan(
        plan_id="plan-1",
        template_fingerprint=fingerprint,
        planner_mode="live",
        mappings=[
            FieldMapping(
                source_field="Customer Linehaul Rate",
                target_column="Freight \nPrice",
                bid_slot=0,
                value_transform="round_2",
                confidence_score=0.95,
                reasoning="approved by operator",
            ),
            FieldMapping(
                source_field="Currency",
                target_column="Currency",
                confidence_score=1.0,
            ),
        ],
        assumptions=["operator approved mapping"],
        pending_review=[],
        iterations_run=2,
    )


class TestRoundTrip(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = MappingCacheStore(root=self._tmp.name)

    def test_save_then_load_preserves_plan_fields(self) -> None:
        plan = _plan()
        path = self.store.save(plan, approved_by="maxbush@microsoft.com")
        self.assertTrue(Path(path).is_file())

        record = self.store.load(plan.template_fingerprint)
        self.assertIsNotNone(record)
        assert record is not None  # for type checker

        self.assertEqual(record.approved_by, "maxbush@microsoft.com")
        self.assertEqual(record.plan.template_fingerprint, plan.template_fingerprint)
        self.assertEqual(record.plan.planner_mode, "live")
        self.assertEqual(len(record.plan.mappings), 2)
        self.assertEqual(record.plan.mappings[0].source_field, "Customer Linehaul Rate")
        self.assertEqual(record.plan.mappings[0].target_column, "Freight \nPrice")
        self.assertEqual(record.plan.mappings[0].confidence_score, 0.95)
        # approved_at must be a UTC datetime, recently set
        self.assertIsInstance(record.approved_at, datetime)
        self.assertEqual(record.approved_at.tzinfo, UTC)

    def test_save_with_explicit_approved_at(self) -> None:
        plan = _plan()
        ts = datetime(2025, 6, 1, 12, 34, 56, tzinfo=UTC)
        self.store.save(plan, approved_by="alice", approved_at=ts)
        record = self.store.load(plan.template_fingerprint)
        assert record is not None
        self.assertEqual(record.approved_at, ts)

    def test_load_returns_none_for_missing_entry(self) -> None:
        self.assertIsNone(self.store.load("nonexistent1234"))

    def test_list_cached_empty(self) -> None:
        self.assertEqual(self.store.list_cached(), [])

    def test_list_cached_after_two_saves(self) -> None:
        self.store.save(_plan(fingerprint="aaaaaaaaaaaaaaaa"))
        self.store.save(_plan(fingerprint="bbbbbbbbbbbbbbbb"))
        self.assertEqual(
            self.store.list_cached(),
            ["aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"],
        )

    def test_save_overwrites_previous_entry_for_same_fingerprint(self) -> None:
        first = _plan()
        first.mappings[0].confidence_score = 0.5
        self.store.save(first, approved_by="bob")

        second = _plan()
        second.mappings[0].confidence_score = 0.99
        self.store.save(second, approved_by="alice")

        record = self.store.load(second.template_fingerprint)
        assert record is not None
        self.assertEqual(record.approved_by, "alice")
        self.assertEqual(record.plan.mappings[0].confidence_score, 0.99)

    def test_clear_removes_entry(self) -> None:
        plan = _plan()
        self.store.save(plan)
        self.assertTrue(self.store.clear(plan.template_fingerprint))
        self.assertIsNone(self.store.load(plan.template_fingerprint))
        self.assertFalse(self.store.clear(plan.template_fingerprint))


class TestErrorHandling(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = MappingCacheStore(root=self._tmp.name)

    def test_load_returns_none_for_malformed_json(self) -> None:
        path = Path(self._tmp.name) / "deadbeef12345678.json"
        path.write_text("{not really json", encoding="utf-8")
        self.assertIsNone(self.store.load("deadbeef12345678"))

    def test_load_returns_none_for_invalid_plan_shape(self) -> None:
        path = Path(self._tmp.name) / "deadbeef12345678.json"
        path.write_text(
            json.dumps({
                "template_fingerprint": "deadbeef12345678",
                "approved_at": "2025-06-01T12:00:00Z",
                "plan": {"this_is_not": "a_valid_plan"},
            }),
            encoding="utf-8",
        )
        self.assertIsNone(self.store.load("deadbeef12345678"))

    def test_load_returns_none_on_fingerprint_mismatch(self) -> None:
        plan = _plan(fingerprint="aaaaaaaaaaaaaaaa")
        path = self.store.save(plan)
        # Move the file under a different fingerprint name to simulate copy/paste.
        renamed = Path(path).with_name("bbbbbbbbbbbbbbbb.json")
        Path(path).rename(renamed)
        self.assertIsNone(self.store.load("bbbbbbbbbbbbbbbb"))

    def test_save_rejects_empty_fingerprint(self) -> None:
        plan = _plan(fingerprint="")
        with self.assertRaises(ValueError):
            self.store.save(plan)

    def test_save_rejects_path_traversal_fingerprint(self) -> None:
        plan = _plan(fingerprint="../etc/passwd")
        with self.assertRaises(ValueError):
            self.store.save(plan)

    def test_list_cached_ignores_temp_files(self) -> None:
        self.store.save(_plan(fingerprint="aaaaaaaaaaaaaaaa"))
        # Simulate an in-flight temp file
        (Path(self._tmp.name) / ".bbbb.tmpjson.tmp").write_text("garbage", encoding="utf-8")
        (Path(self._tmp.name) / "not_a_cache.txt").write_text("noise", encoding="utf-8")
        self.assertEqual(self.store.list_cached(), ["aaaaaaaaaaaaaaaa"])


class TestEnvOverride(unittest.TestCase):
    """The cache root respects $REHYDRATE_MAPPING_CACHE_ROOT."""

    def test_env_var_overrides_default_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            import os
            old = os.environ.get("REHYDRATE_MAPPING_CACHE_ROOT")
            os.environ["REHYDRATE_MAPPING_CACHE_ROOT"] = tmp
            try:
                store = MappingCacheStore()  # no explicit root
                plan = _plan()
                path = store.save(plan)
                self.assertTrue(str(path).startswith(tmp))
            finally:
                if old is None:
                    os.environ.pop("REHYDRATE_MAPPING_CACHE_ROOT", None)
                else:
                    os.environ["REHYDRATE_MAPPING_CACHE_ROOT"] = old


class TestSchemaVersion(unittest.TestCase):
    """The schema version constant is included in every saved record."""

    def test_saved_record_carries_schema_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MappingCacheStore(root=tmp)
            plan = _plan()
            path = store.save(plan)
            doc = json.loads(Path(path).read_text(encoding="utf-8"))
            self.assertEqual(doc["cache_schema_version"], CACHE_SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
