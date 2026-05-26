"""Notes post-processor service.

After the main transformation pipeline produces canonical records, this service
scans every row that has populated Notes JSON content, invokes a secondary
Foundry agent (or a deterministic mock) to infer field updates from the notes,
applies the updates, and returns a structured change log.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .foundry_agent_client import FoundryAgentClient
from .prompt_renderer import PromptRenderer

# ── Deterministic rule engine (used in mock mode) ────────────────────────────

_DROP_TRAILER_RE = re.compile(r"drop\s+trailer", re.IGNORECASE)
_DROP_ORIGIN_CTX = re.compile(r"(at\s+PU|pickup|at\s+origin|origin)", re.IGNORECASE)
_DROP_DEST_CTX = re.compile(r"(deliver|destination|receiver|consignee|at\s+del)", re.IGNORECASE)

_MULTI_STOP_RE = re.compile(
    r"(stop\s*off|multi[\s-]?stop|multiple\s+(stop|deliver)|delivery\s+stops|deliveries\s+to\s+\w+\s*[&,])",
    re.IGNORECASE,
)
_MULTI_STOP_ROUTE_RE = re.compile(r"deliver\s+.*\bdeliver\b", re.IGNORECASE)

_EQUIP_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(\d+['\u2019]\s*)?conestoga", re.IGNORECASE), "Conestoga"),
    (re.compile(r"(\d+['\u2019]\s*)?step\s*deck", re.IGNORECASE), "Step Deck"),
    (re.compile(r"(\d+['\u2019]\s*)?flatbed", re.IGNORECASE), "Flatbed"),
    (re.compile(r"(\d+['\u2019]\s*)?reefer|refrigerated", re.IGNORECASE), "Reefer"),
    (re.compile(r"(\d+['\u2019]\s*)?tanker", re.IGNORECASE), "Tanker"),
    (re.compile(r"(\d+['\u2019]\s*)?lowboy", re.IGNORECASE), "Lowboy"),
    (re.compile(r"(\d+['\u2019]\s*)?drop\s*deck", re.IGNORECASE), "Drop Deck"),
]

_HAZMAT_RE = re.compile(
    r"(hazmat|haz\s+mat|hazardous|placard|dangerous\s+goods|\bDG\b|class\s+\d|flammable|corrosive|explosive|radioactive)",
    re.IGNORECASE,
)

_TEAM_RE = re.compile(r"(team\s+driver|team\s+required|team\s+service|team\s+load)", re.IGNORECASE)

_ROUND_TRIP_RE = re.compile(
    r"(round\s*trip|return\s+load|reload\s+at\s+destination|deliver\s+and\s+reload)",
    re.IGNORECASE,
)

_PERMIT_RE = re.compile(
    r"(permit\s+required|permit\s+needed|oversize|oversized|overweight|over[\s-]?dimensional|wide\s+load|heavy\s+haul)",
    re.IGNORECASE,
)

_HRHV_RE = re.compile(
    r"(high[\s-]?value|HRHV|escort\s+required|escort\s+service|\$\d{3,}K|\$\d+M)",
    re.IGNORECASE,
)

_BORDER_RE = re.compile(
    r"(border\s+crossing|customs\s+(broker|paperwork)|cross[\s-]?border|C-TPAT|FAST\s+(card|lane)|ACI\s+eManifest)",
    re.IGNORECASE,
)

_BORDER_MX_RE = re.compile(r"(mexic|MX\b)", re.IGNORECASE)
_BORDER_CA_RE = re.compile(r"(canad|CA\s+eManifest|ACI)", re.IGNORECASE)

_TEMP_RE = re.compile(
    r"(\d+\s*[°]?\s*F|temperature\s+controlled|cold\s+chain|frozen|maintain\s+[\-\d]+)",
    re.IGNORECASE,
)
_SPECIAL_HANDLING_RE = re.compile(
    r"(returnable|pallet\s+jack|loading\s+requirement|weekly\s+run|every\s+\w{3}|seasonal\s+volume|inside\s+delivery|lumper|liftgate|appointment|gate\s+code)",
    re.IGNORECASE,
)


def _is_empty(value: Any) -> bool:
    """Check if a canonical field value is empty/null/false."""
    if value is None:
        return True
    s = str(value).strip().lower()
    return s in ("", "nan", "none", "false", "0", "0.0")


def _mock_post_process_row(
    row_index: int,
    lane_id: str,
    notes: list[dict[str, str]],
    current_values: dict[str, Any],
) -> list[dict[str, Any]]:
    """Apply deterministic rules to infer field updates from note text."""
    updates: list[dict[str, Any]] = []
    all_text = " ".join(n.get("value", "") for n in notes)

    # 1. Drop Trailer (Origin)
    if _is_empty(current_values.get("Drop Trailer (Origin)")) and _DROP_TRAILER_RE.search(all_text):
        if _DROP_ORIGIN_CTX.search(all_text) or not _DROP_DEST_CTX.search(all_text):
            match = _DROP_TRAILER_RE.search(all_text)
            snippet = all_text[max(0, match.start() - 10) : match.end() + 30].strip()
            updates.append({
                "field": "Drop Trailer (Origin)",
                "old_value": None,
                "new_value": True,
                "reason": f'Note mentions "{snippet}"',
            })

    # 2. Drop Trailer (Destination)
    if _is_empty(current_values.get("Drop Trailer (Destination)")) and _DROP_TRAILER_RE.search(all_text):
        if _DROP_DEST_CTX.search(all_text):
            match = _DROP_TRAILER_RE.search(all_text)
            snippet = all_text[max(0, match.start() - 10) : match.end() + 30].strip()
            updates.append({
                "field": "Drop Trailer (Destination)",
                "old_value": None,
                "new_value": True,
                "reason": f'Note mentions "{snippet}"',
            })

    # 3. Multi Stop
    if _is_empty(current_values.get("Multi Stop")):
        m = _MULTI_STOP_RE.search(all_text) or _MULTI_STOP_ROUTE_RE.search(all_text)
        if m:
            snippet = all_text[max(0, m.start() - 5) : m.end() + 30].strip()
            updates.append({
                "field": "Multi Stop",
                "old_value": None,
                "new_value": True,
                "reason": f'Note mentions "{snippet}"',
            })

    # 4. Equipment Type Detail
    current_equip_detail = current_values.get("Equipment Type Detail")
    equip_detail_is_default = (
        _is_empty(current_equip_detail)
        or current_values.get("Equipment Type Detail", "").strip().lower()
        in ("v", "van", "dry van", "")
    )
    if equip_detail_is_default:
        for pattern, equip_name in _EQUIP_PATTERNS:
            m = pattern.search(all_text)
            if m:
                matched_text = m.group(0).strip()
                # Include size prefix if present
                if m.group(1):
                    equip_value = matched_text
                else:
                    equip_value = equip_name
                old = current_values.get("Equipment Type Detail")
                if str(old).strip().lower() != equip_value.lower():
                    updates.append({
                        "field": "Equipment Type Detail",
                        "old_value": old if not _is_empty(old) else None,
                        "new_value": equip_value,
                        "reason": f'Note specifies equipment "{matched_text}"',
                    })
                break

    # 5. Hazmat
    if _is_empty(current_values.get("Hazmat")):
        m = _HAZMAT_RE.search(all_text)
        if m:
            updates.append({
                "field": "Hazmat",
                "old_value": None,
                "new_value": True,
                "reason": f'Note mentions hazmat indicator "{m.group(0)}"',
            })

    # 6. Team
    if _is_empty(current_values.get("Team")):
        m = _TEAM_RE.search(all_text)
        if m:
            updates.append({
                "field": "Team",
                "old_value": None,
                "new_value": True,
                "reason": f'Note mentions "{m.group(0)}"',
            })

    # 7. Round Trip
    if _is_empty(current_values.get("Round Trip")):
        m = _ROUND_TRIP_RE.search(all_text)
        if m:
            updates.append({
                "field": "Round Trip",
                "old_value": None,
                "new_value": True,
                "reason": f'Note mentions "{m.group(0)}"',
            })

    # 8. Permit
    if _is_empty(current_values.get("Permit")):
        m = _PERMIT_RE.search(all_text)
        if m:
            updates.append({
                "field": "Permit",
                "old_value": None,
                "new_value": True,
                "reason": f'Note mentions "{m.group(0)}"',
            })

    # 9. HRHV
    if _is_empty(current_values.get("HRHV")):
        m = _HRHV_RE.search(all_text)
        if m:
            updates.append({
                "field": "HRHV",
                "old_value": None,
                "new_value": True,
                "reason": f'Note mentions "{m.group(0)}"',
            })

    # 10. Border Crossing Fee
    if _is_empty(current_values.get("Border Crossing Fee")):
        m = _BORDER_RE.search(all_text)
        if m:
            updates.append({
                "field": "Border Crossing Fee",
                "old_value": None,
                "new_value": 1.0,
                "reason": f'Note mentions border indicator "{m.group(0)}"',
            })

    # 11. Border Crossing Country
    if _is_empty(current_values.get("Border Crossing Country")):
        if _BORDER_MX_RE.search(all_text):
            updates.append({
                "field": "Border Crossing Country",
                "old_value": None,
                "new_value": "MX",
                "reason": "Note references Mexico/MX border crossing",
            })
        elif _BORDER_CA_RE.search(all_text):
            updates.append({
                "field": "Border Crossing Country",
                "old_value": None,
                "new_value": "CA",
                "reason": "Note references Canada/CA border crossing",
            })

    # 12. Other — special handling text
    other_parts: list[str] = []
    current_other = str(current_values.get("Other", "")).strip()
    if _is_empty(current_other):
        current_other = ""

    # Extract temperature requirements cleanly
    temp_matches = _TEMP_RE.finditer(all_text)
    for tm in temp_matches:
        # Find the containing sentence/clause for context
        start = all_text.rfind(".", 0, tm.start())
        start = start + 1 if start >= 0 else max(0, tm.start() - 30)
        end = all_text.find(".", tm.end())
        end = end + 1 if end >= 0 else min(len(all_text), tm.end() + 30)
        temp_clause = all_text[start:end].strip().strip(",;. ")
        if temp_clause and temp_clause.lower() not in current_other.lower():
            other_parts.append(f"Temp: {temp_clause}")
        break  # only first temperature mention

    # Extract special handling keywords with sentence context
    if _SPECIAL_HANDLING_RE.search(all_text):
        for sm in _SPECIAL_HANDLING_RE.finditer(all_text):
            # Get surrounding clause for context
            start = all_text.rfind(".", 0, sm.start())
            start = start + 1 if start >= 0 else max(0, sm.start() - 20)
            end = all_text.find(".", sm.end())
            end = end + 1 if end >= 0 else min(len(all_text), sm.end() + 30)
            clause = all_text[start:end].strip().strip(",;. ")
            if clause and clause.lower() not in current_other.lower():
                # Avoid duplicating temp entries
                if not any(clause.lower() in p.lower() for p in other_parts):
                    other_parts.append(clause)

    if other_parts:
        new_other = "; ".join(other_parts)
        if current_other:
            new_other = f"{current_other}; {new_other}"
        updates.append({
            "field": "Other",
            "old_value": current_other if current_other else None,
            "new_value": new_other,
            "reason": f"Special handling detected in notes: {', '.join(other_parts)}",
        })

    return updates


# ── Updatable fields list ────────────────────────────────────────────────────

POSTPROCESS_FIELDS = [
    "Drop Trailer (Origin)",
    "Drop Trailer (Destination)",
    "Multi Stop",
    "Equipment Type Detail",
    "Hazmat",
    "Team",
    "Round Trip",
    "Permit",
    "HRHV",
    "Border Crossing Fee",
    "Border Crossing Country",
    "Other",
]


class NotesPostProcessor:
    """Runs note-driven field inference on canonical records."""

    def __init__(
        self,
        mode: str = "mock",
        agent_client: FoundryAgentClient | None = None,
    ) -> None:
        self.mode = mode
        self.prompt_renderer = PromptRenderer()

        if agent_client is not None:
            self.client = agent_client
        else:
            import os
            self.client = FoundryAgentClient(
                endpoint=os.getenv("FOUNDRY_PROJECT_ENDPOINT", ""),
                agent_name=os.getenv("FOUNDRY_POSTPROCESS_AGENT_NAME", "RXO-Notes-PostProcessor"),
                agent_version=os.getenv("FOUNDRY_POSTPROCESS_AGENT_VERSION", ""),
                api_key=os.getenv("FOUNDRY_API_KEY", ""),
                mode=mode,
            )

    def process(
        self,
        records: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Post-process canonical records, returning (updated_records, change_log).

        Parameters
        ----------
        records : list[dict]
            Canonical output records (already normalised).

        Returns
        -------
        updated_records : list[dict]
            Same records with fields updated based on notes.
        change_log : list[dict]
            Structured log of every field change made.
        """
        # 1. Identify rows with non-empty Notes JSON
        rows_with_notes = self._identify_rows_with_notes(records)

        if not rows_with_notes:
            return records, []

        # 2. Get field updates (mock or live)
        if self.mode == "mock":
            row_updates = self._mock_infer(rows_with_notes)
        else:
            row_updates = self._live_infer(rows_with_notes)

        # 3. Apply updates and build change log
        updated_records = list(records)  # shallow copy
        change_log: list[dict[str, Any]] = []

        for row_update in row_updates:
            idx = row_update["row_index"]
            lane_id = row_update["lane_id"]
            for update in row_update.get("updates", []):
                field = update["field"]
                new_value = update["new_value"]
                reason = update.get("reason", "")

                # Apply the update
                if 0 <= idx < len(updated_records):
                    actual_old = updated_records[idx].get(field)
                    updated_records[idx][field] = new_value

                    change_log.append({
                        "row_index": idx,
                        "lane_id": lane_id,
                        "field": field,
                        "old_value": actual_old,
                        "new_value": new_value,
                        "reason": reason,
                        "origin": "notes_postprocess",
                    })

        return updated_records, change_log

    def _identify_rows_with_notes(
        self, records: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Return rows that have non-empty Notes JSON content."""
        rows: list[dict[str, Any]] = []
        for idx, record in enumerate(records):
            notes_raw = record.get("Notes JSON", "")
            if not notes_raw or str(notes_raw).strip() in ("", "[]", "nan", "None"):
                continue
            try:
                notes_list = json.loads(str(notes_raw))
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(notes_list, list) or not notes_list:
                continue

            current_values = {f: record.get(f) for f in POSTPROCESS_FIELDS}
            rows.append({
                "row_index": idx,
                "lane_id": record.get("Customer Lane ID", f"ROW-{idx}"),
                "notes": notes_list,
                "current_values": current_values,
            })
        return rows

    def _mock_infer(
        self, rows_with_notes: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Use deterministic regex rules to infer updates."""
        results: list[dict[str, Any]] = []
        for row in rows_with_notes:
            updates = _mock_post_process_row(
                row_index=row["row_index"],
                lane_id=row["lane_id"],
                notes=row["notes"],
                current_values=row["current_values"],
            )
            if updates:
                results.append({
                    "row_index": row["row_index"],
                    "lane_id": row["lane_id"],
                    "updates": updates,
                })
        return results

    def _live_infer(
        self, rows_with_notes: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Call the Foundry post-process agent to infer updates."""
        system_prompt = self.prompt_renderer.load_prompt("notes_postprocess_system.txt")

        # Build compact user prompt with just the rows
        user_payload = json.dumps(rows_with_notes, indent=2, default=str)
        user_prompt = (
            "Analyze the following rows and return field updates based on note content.\n\n"
            + user_payload
        )

        try:
            raw_response = self.client.plan(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
        except Exception as exc:
            # On live failure, fall back to mock
            import logging
            logging.getLogger(__name__).warning(
                "Live post-process agent failed (%s), falling back to mock rules.", exc
            )
            return self._mock_infer(rows_with_notes)

        # Parse response
        row_updates = raw_response.get("row_updates", [])
        if not isinstance(row_updates, list):
            return self._mock_infer(rows_with_notes)

        return row_updates
