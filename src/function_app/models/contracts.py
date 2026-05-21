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
    """One repeating bid-slot group within a customer bid sheet.

    The template contains 4 identical column groups per data row, each
    separated by a hidden "Lot Name" sentinel column.
    """
    group_index: int            # 0-based index (slot 0 = primary bid)
    lot_name: str               # value of the hidden sentinel column
    col_offset: int             # 1-based column index where this group starts
    columns: List[str]          # header names within this group
    column_indices: List[int] = Field(default_factory=list)  # absolute 1-based col index per column


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


# ── Phase 1: Reverse Pipeline (Rehydrate) contracts ──────────────────────────

class TemplateBidSlot(BaseModel):
    """One repeating bid-slot group within a template bid sheet."""
    slot_index: int             # 0-based; slot 0 is the primary bid
    lot_name: str               # value of the hidden Lot Name sentinel column
    col_offset: int             # 1-based absolute column where this slot starts
    columns: List[str]          # all header names in this slot
    column_indices: List[int]   # absolute 1-based col index for each entry in columns
    writable_columns: List[str] # subset of columns that are not formula/token-protected
    formula_col_indices: List[int]  # 1-based absolute column indices that are formulas


class TemplateSheetProfile(BaseModel):
    """Focused profile of a single bid sheet within the customer template."""
    sheet_name: str
    header_row: int
    route_name_col: int         # 1-based column index of the Route Name cell per data row
    bid_slots: List[TemplateBidSlot]
    lane_provenance: List[LaneProvenanceEntry]  # route_name → row_index map for this sheet


class TemplateProfile(BaseModel):
    """Full reverse-pipeline profile of the original customer template."""
    workbook_name: str
    bid_sheets: List[TemplateSheetProfile]
    dropdown_catalog: Dict[str, List[str]]
    template_fingerprint: str   # sha256[:16] of workbook bytes — cache key for ReversePlanner


class FieldMapping(BaseModel):
    """One field mapping rule: export column → template bid-slot column."""
    source_field: str           # column name in the priced export file
    target_column: str          # column name in the template bid slot
    bid_slot: int = 0           # 0-based slot index (default = primary bid)
    value_transform: Optional[str] = None  # e.g. "round_2", "upper", "none"
    confidence_score: float = 1.0          # 0.0–1.0; 1.0 = rule-based / certain
    reasoning: Optional[str] = None        # LLM explanation for this mapping
    needs_review: bool = False             # True when confidence_score < threshold


class HumanReviewRequest(BaseModel):
    """A low-confidence mapping surfaced for human confirmation."""
    source_field: str
    target_column: str
    confidence_score: float
    reasoning: Optional[str] = None
    override_value: Optional[Any] = None   # human-supplied override (None = accept LLM choice)


class ReverseMappingPlan(BaseModel):
    """Reusable plan that maps export fields to template cells."""
    plan_id: str
    template_fingerprint: str
    planner_mode: str           # "mock" or "live"
    mappings: List[FieldMapping]
    assumptions: List[str]
    pending_review: List[HumanReviewRequest] = []  # low-confidence mappings awaiting human sign-off
    iterations_run: int = 0                         # LLM refinement rounds used (0 for mock)


class CellWriteInstruction(BaseModel):
    """A single resolved cell write: sheet + row + col + value."""
    sheet_name: str
    row_index: int              # 1-based
    col_index: int              # 1-based
    value: Any
    source_route_name: str
    source_field: str


class ReverseValidationIssue(BaseModel):
    """Validation issue emitted by the reverse-pipeline submission builder."""
    code: str
    severity: str
    message: str
    route_name: Optional[str] = None
    sheet_name: Optional[str] = None
    row_index: Optional[int] = None


class ReverseValidationReport(BaseModel):
    """Validation summary for the reverse pipeline."""
    status: str
    passed: bool
    issues: List[ReverseValidationIssue] = Field(default_factory=list)
    issue_counts: Dict[str, int] = Field(default_factory=dict)
    no_bid_lanes: List[str] = Field(default_factory=list)


class WriteReport(BaseModel):
    """Summary artifact produced by TemplateAwareWriter."""
    cells_written: int
    cells_skipped: int
    no_bid_lanes: List[str]     # Route Names present in template but missing from export
    warnings: List[ReverseValidationIssue] = Field(default_factory=list)
    validation_summary: Optional[ReverseValidationReport] = None
    write_log: List[Dict[str, Any]]
