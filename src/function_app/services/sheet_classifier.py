from __future__ import annotations

import re
from typing import Any


NON_DATA_KEYWORDS = {
    "requirement": "instructional",
    "requirements": "instructional",
    "instruction": "instructional",
    "readme": "instructional",
    "fsc": "reference",
    "location": "reference",
    "locations": "reference",
    "lookup": "reference",
}

# TD-004: Coupa control token patterns
_COUPA_HIDESHEET_RE = re.compile(r"<<\s*hidesheet\s*>>", re.IGNORECASE)
_COUPA_BID_RE = re.compile(r"<<\s*bid\s*\|", re.IGNORECASE)
_COUPA_TOC_RE = re.compile(r"<<\s*toc\s*\|", re.IGNORECASE)


def _detect_coupa_sheet_type(sheet_name: str, sample_rows: list[dict[str, Any]]) -> str | None:
    """Return a Coupa SheetType string when Coupa control tokens are detected.

    Detection priority:
    1. COUPA_ADMIN  — sheet name is 'validationinfo', OR first cell of first
       sample row looks like <<hidesheet>>
    2. COUPA_BID_DATA — any cell in the first few rows contains <<bid|...>>
    3. COUPA_NAV    — any cell in the first few rows contains <<toc|...>>
    """
    # validationInfo is always admin/hidden
    if sheet_name.strip().lower() == "validationinfo":
        return "coupa_admin"

    all_cell_text = " ".join(
        str(v) for row in sample_rows[:5] for v in row.values() if v is not None
    )

    if _COUPA_HIDESHEET_RE.search(all_cell_text):
        return "coupa_admin"
    if _COUPA_BID_RE.search(all_cell_text):
        return "coupa_bid_data"
    if _COUPA_TOC_RE.search(all_cell_text):
        return "coupa_nav"

    return None


def classify_sheet(sheet_name: str, columns: list[str], sample_rows: list[dict[str, Any]], control_rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    normalized_name = (sheet_name or "").strip().lower()
    hints: list[str] = []
    score = 0

    # TD-004: Coupa detection takes priority over generic heuristics.
    # Check sample_rows first; fall back to control_rows (which contain pre-header
    # tokens like <<bid|...>> that appear before the column header row).
    coupa_type = _detect_coupa_sheet_type(normalized_name, sample_rows)
    if coupa_type is None and control_rows:
        control_text_rows = [
            {str(i): v for i, v in enumerate(row.get("values", []))}
            for row in control_rows
        ]
        coupa_type = _detect_coupa_sheet_type(normalized_name, control_text_rows)
    if coupa_type == "coupa_bid_data":
        return {
            "likely_exclude": False,
            "business_meaning": "coupa_bid_data",
            "coupa_sheet_type": "coupa_bid_data",
            "hints": ["coupa_bid_token_detected"],
            "score": 10,
        }
    if coupa_type == "coupa_admin":
        return {
            "likely_exclude": True,
            "business_meaning": "coupa_admin",
            "coupa_sheet_type": "coupa_admin",
            "hints": ["coupa_admin_token_detected"],
            "score": -5,
        }
    if coupa_type == "coupa_nav":
        return {
            "likely_exclude": True,
            "business_meaning": "coupa_nav",
            "coupa_sheet_type": "coupa_nav",
            "hints": ["coupa_nav_token_detected"],
            "score": -5,
        }

    for keyword, label in NON_DATA_KEYWORDS.items():
        if keyword in normalized_name:
            hints.append(f"name_matches_{label}_{keyword}")
            score -= 3

    non_empty_columns = [column for column in columns if str(column).strip()]
    if len(non_empty_columns) >= 4:
        score += 2
        hints.append("has_multiple_columns")

    non_empty_sample_rows = [
        row for row in sample_rows if any(value not in (None, "") for value in row.values())
    ]
    if len(non_empty_sample_rows) >= 3:
        score += 2
        hints.append("has_non_empty_sample_rows")

    if not non_empty_sample_rows:
        score -= 2
        hints.append("no_data_rows_detected")

    likely_exclude = score < 1
    if any("instructional" in hint for hint in hints):
        business_meaning = "instructional"
    elif any("reference" in hint for hint in hints):
        business_meaning = "reference"
    elif likely_exclude:
        business_meaning = "likely_exclude"
    else:
        business_meaning = "data"

    return {
        "likely_exclude": likely_exclude,
        "business_meaning": business_meaning,
        "coupa_sheet_type": None,
        "hints": hints,
        "score": score,
    }
