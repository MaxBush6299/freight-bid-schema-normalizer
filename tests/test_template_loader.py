import json
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from src.function_app.services.template_loader import load_canonical_schema


class TestTemplateLoader(unittest.TestCase):
    def test_load_canonical_schema_succeeds_for_valid_template(self) -> None:
        schema = load_canonical_schema(
            "src/function_app/templates/canonical_schema.freight_bid_v1.json"
        )

        self.assertEqual(schema.schema_name, "freight_bid_v1")
        self.assertEqual(len(schema.columns), 41)
        self.assertEqual(schema.columns[0].name, "Customer Lane ID")
        self.assertEqual(schema.columns[-1].name, "Notes JSON")

    def test_load_canonical_schema_raises_for_invalid_template(self) -> None:
        invalid_payload = {
            "schema_name": "bad_schema",
            "columns": [{"name": "Only Name"}],
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir) / "invalid_schema.json"
            temp_path.write_text(json.dumps(invalid_payload), encoding="utf-8")

            with self.assertRaises(ValidationError):
                load_canonical_schema(str(temp_path))


if __name__ == "__main__":
    unittest.main()
