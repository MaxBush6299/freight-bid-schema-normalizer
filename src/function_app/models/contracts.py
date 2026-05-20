# contracts.py
"""
Data contracts for RXO Document Normalizer
- WorkbookProfile
- CanonicalSchema
- AgentResponse
- ExecutionResult
"""
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


# ── TD-003: Lane provenance entry extracted from Coupa <<define>> rows ────────

class LaneProvenanceEntry(BaseModel):
    route_name: str
    sheet_name: str
    row_index: int  # 1-based row index in the original sheet


# ── TD-005: Repeating bid-slot column group ───────────────────────────────────

class ColumnGroup(BaseModel):
    """One repeating bid-slot group within a Coupa bid sheet.

    Coupa templates contain 4 identical column groups per data row, each
    separated by a hidden "Lot Name" sentinel column.
    """
    group_index: int            # 0-based index (slot 0 = primary bid)
    lot_name: str               # value of the hidden sentinel column
    col_offset: int             # 1-based column index where this group starts
    columns: List[str]          # header names within this group


# ── Sheet & Workbook profiles ─────────────────────────────────────────────────

class SheetProfile(BaseModel):
    name: str
    visible: bool
    used_range: Optional[str]
    header_row: Optional[int]
    header_row_candidates: List[int] = Field(default_factory=list)
    columns: List[str]
    inferred_types: Dict[str, str]
    sample_rows: List[Dict[str, Any]]
    duplicate_headers: List[str] = Field(default_factory=list)
    empty_column_ratio: Optional[float]
    likely_business_meaning: Optional[str]
    classifier_hints: List[str] = Field(default_factory=list)
    notes: Optional[str]
    # TD-002: Coupa control rows stripped before header detection
    control_rows: List[Dict[str, Any]] = Field(default_factory=list)
    # TD-004: Coupa sheet type when detected
    coupa_sheet_type: Optional[str] = None
    # TD-005: Repeating bid-slot column groups (populated for COUPA_BID_DATA sheets)
    column_groups: List[ColumnGroup] = Field(default_factory=list)
    # TD-008: Column indices (1-based) that contain formula cells
    formula_columns: List[int] = Field(default_factory=list)


class WorkbookProfile(BaseModel):
    workbook_name: str
    sheets: List[SheetProfile]
    notes: Optional[str]
    # TD-003: Route Name provenance extracted from <<define>> rows
    lane_provenance: List[LaneProvenanceEntry] = Field(default_factory=list)
    # TD-007: Dropdown allowed values from validationInfo sheet
    dropdown_catalog: Dict[str, List[str]] = Field(default_factory=dict)


class SchemaFingerprint(BaseModel):
    schema_fingerprint_sha256: str
    schema_signature_payload: Dict[str, Any]


class SchemaCacheEntry(BaseModel):
    id: str
    schema_fingerprint_sha256: str
    schema_signature_payload: Dict[str, Any]
    canonical_schema_name: str
    planner_output: Dict[str, Any]
    planner_output_hash: str
    approval_status: str = "draft"
    approval_source: Optional[str] = None
    auto_approve_enabled: bool = False
    first_seen_at: str
    last_seen_at: str
    use_count: int = 1
    created_from_run_id: Optional[str] = None
    last_used_run_id: Optional[str] = None
    notes: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

class CanonicalSchemaColumn(BaseModel):
    name: str
    dtype: str
    required: bool = False
    normalization: Optional[str] = None
    default: Optional[Any] = None
    allowed_values: Optional[List[Any]] = None
    derived_formula: Optional[str] = None

class CanonicalSchema(BaseModel):
    schema_name: str
    columns: List[CanonicalSchemaColumn]
    description: Optional[str] = None

class AgentResponse(BaseModel):
    relevant_sheets: List[str]
    ignored_sheets: List[str]
    mapping_plan: Dict[str, Any]
    constants: Dict[str, Any]
    enrichments: Dict[str, Any]
    assumptions: List[str]
    confidence_scores: Dict[str, float]
    python_script: str
    tests: Optional[List[str]]
    notes_json: Optional[List[Dict[str, Any]]] = Field(default_factory=list)

class ExecutionResult(BaseModel):
    status: str
    run_id: str
    output_path: Optional[str]
    artifacts: Optional[List[str]]
    validation_summary: Optional[Dict[str, Any]]
    error: Optional[str]


class SchemaFingerprint(BaseModel):
    schema_fingerprint_sha256: str
    schema_signature_payload: Dict[str, Any]


class SchemaCacheEntry(BaseModel):
    id: str
    schema_fingerprint_sha256: str
    schema_signature_payload: Dict[str, Any]
    canonical_schema_name: str
    planner_output: Dict[str, Any]
    planner_output_hash: str
    approval_status: str = "draft"
    approval_source: Optional[str] = None
    auto_approve_enabled: bool = False
    first_seen_at: str
    last_seen_at: str
    use_count: int = 1
    created_from_run_id: Optional[str] = None
    last_used_run_id: Optional[str] = None
    notes: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

class CanonicalSchemaColumn(BaseModel):
    name: str
    dtype: str
    required: bool = False
    normalization: Optional[str] = None
    default: Optional[Any] = None
    allowed_values: Optional[List[Any]] = None
    derived_formula: Optional[str] = None

class CanonicalSchema(BaseModel):
    schema_name: str
    columns: List[CanonicalSchemaColumn]
    description: Optional[str] = None

class AgentResponse(BaseModel):
    relevant_sheets: List[str]
    ignored_sheets: List[str]
    mapping_plan: Dict[str, Any]
    constants: Dict[str, Any]
    enrichments: Dict[str, Any]
    assumptions: List[str]
    confidence_scores: Dict[str, float]
    python_script: str
    tests: Optional[List[str]]
    notes_json: Optional[List[Dict[str, Any]]] = Field(default_factory=list)

class ExecutionResult(BaseModel):
    status: str
    run_id: str
    output_path: Optional[str]
    artifacts: Optional[List[str]]
    validation_summary: Optional[Dict[str, Any]]
    error: Optional[str]
