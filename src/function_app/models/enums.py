# enums.py
"""
Enumerations for RXO Document Normalizer
"""
from enum import Enum

class RunStatus(str, Enum):
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"
    RUNNING = "Running"
    VALIDATION_FAILED = "ValidationFailed"

class OutputFormat(str, Enum):
    XLSX = "xlsx"
    CSV = "csv"
    BOTH = "both"

class SheetType(str, Enum):
    """TD-004 — Coupa-aware sheet classification."""
    DATA = "data"
    INSTRUCTIONAL = "instructional"
    REFERENCE = "reference"
    LIKELY_EXCLUDE = "likely_exclude"
    # Coupa-specific types
    COUPA_BID_DATA = "coupa_bid_data"   # contains <<bid|itemType:...>> token
    COUPA_ADMIN = "coupa_admin"         # <<hidesheet>> in A1 — hidden admin/config sheet
    COUPA_NAV = "coupa_nav"             # <<toc|...>> in A1 — table-of-contents nav sheet
