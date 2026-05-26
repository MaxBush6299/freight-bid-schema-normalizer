import json

from ..models.contracts import CanonicalSchema


def load_canonical_schema(schema_path: str) -> CanonicalSchema:
    """
    Load and validate the canonical schema from a JSON file.
    """
    with open(schema_path, "r", encoding="utf-8") as file_handle:
        data = json.load(file_handle)
    return CanonicalSchema(**data)
