from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..models.contracts import AgentResponse, ExecutionResult
from .artifact_store import (
    LocalArtifactStore,
    create_blob_artifact_store_from_env,
    mirror_local_artifacts_to_blob,
)
from .foundry_agent_client import FoundryAgentClient
from .notes_postprocessor import NotesPostProcessor
from .output_writer import normalize_records_to_canonical, write_canonical_csv, write_canonical_xlsx, write_notes_json
from .planning_service import TransformationPlanningService
from .sandbox_executor import execute_script_in_sandbox
from .schema_cache import LocalSchemaCacheRepository, resolve_local_schema_cache_root
from .schema_fingerprint import compute_schema_fingerprint
from .script_policy import evaluate_script_policy
from .template_loader import load_canonical_schema
from .validation_service import validate_canonical_records
from .workbook_profiler import profile_workbook
from .xls_converter import ensure_xlsx


def _extract_records_from_sandbox_result(result: Any) -> list[dict[str, Any]]:
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]

    if isinstance(result, dict):
        for key in ("records", "rows", "data"):
            payload = result.get(key)
            if isinstance(payload, list):
                return [item for item in payload if isinstance(item, dict)]

        dataframe_payload = result.get("dataframe")
        if isinstance(dataframe_payload, list):
            return [item for item in dataframe_payload if isinstance(item, dict)]

    return []


def _extract_notes_from_sandbox_result(result: Any) -> list[dict[str, Any]]:
    """Extract notes list from sandbox transform result."""
    if isinstance(result, dict):
        notes = result.get("notes")
        if isinstance(notes, list):
            return [item for item in notes if isinstance(item, dict)]
    return []


def _collect_note_field_candidates(profile: Any) -> list[dict[str, Any]]:
    note_pattern = re.compile(
        r"\b(note|notes|comment|comments|remark|remarks|instruction|instructions)\b",
        re.IGNORECASE,
    )
    candidates: list[dict[str, Any]] = []

    for sheet in getattr(profile, "sheets", []):
        sheet_name = getattr(sheet, "name", "")
        for column_name in getattr(sheet, "columns", []):
            if not isinstance(column_name, str):
                continue
            normalized_column_name = column_name.strip()
            if not normalized_column_name:
                continue
            if note_pattern.search(normalized_column_name):
                candidates.append(
                    {
                        "sheet": sheet_name,
                        "column": normalized_column_name,
                    }
                )

    return candidates


def _build_planning_constraints(schema: Any, profile: Any) -> dict[str, Any]:
    note_field_candidates = _collect_note_field_candidates(profile)
    canonical_note_fields = [
        column.name
        for column in getattr(schema, "columns", [])
        if isinstance(getattr(column, "name", None), str) and "note" in column.name.lower()
    ]

    return {
        "note_field_candidates": note_field_candidates,
        "canonical_note_fields": canonical_note_fields,
        "note_field_preservation": {
            "enabled": bool(note_field_candidates),
            "instruction": (
                "If note-like source columns exist, map them to canonical note "
                "fields and preserve original text content exactly."
            ),
        },
    }


def run_pipeline(
    input_workbook: str,
    output_root: str,
    run_mode: str = "execute_with_validation",
    planner_mode: str | None = None,
) -> dict[str, Any]:
    input_workbook_path = str(Path(input_workbook).resolve())

    # TD-001: pre-convert .xls to .xlsx so openpyxl can process it
    _converted_path, _did_convert = ensure_xlsx(input_workbook_path)
    if _did_convert:
        input_workbook_path = str(_converted_path)

    schema = load_canonical_schema("src/function_app/templates/canonical_schema.freight_bid_v1.json")
    # Skip formula detection for xls-converted files (xlrd strips formulas to values)
    profile = profile_workbook(input_workbook_path, detect_formulas=not _did_convert)
    planning_constraints = _build_planning_constraints(schema, profile)
    schema_fingerprint = compute_schema_fingerprint(profile)
    selected_planner_mode = (planner_mode or os.getenv("PLANNER_MODE", "mock")).strip().lower()
    if selected_planner_mode not in {"mock", "live"}:
        selected_planner_mode = "mock"

    planning_service = TransformationPlanningService(
        client=FoundryAgentClient(mode=selected_planner_mode)
    )

    run_id = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    run_dir = Path(output_root) / f"pipeline_{run_id}"
    run_dir.mkdir(parents=True, exist_ok=True)
    artifact_store = LocalArtifactStore(str(run_dir))
    blob_artifact_store = create_blob_artifact_store_from_env(run_id)
    schema_cache_repository = LocalSchemaCacheRepository(str(resolve_local_schema_cache_root(output_root)))

    csv_path = str(run_dir / "canonical_output.csv")
    xlsx_path = str(run_dir / "canonical_output.xlsx")
    notes_json_path = str(run_dir / "notes.json")

    system_prompt = planning_service.prompt_renderer.load_prompt("transform_planner_system.txt")
    user_prompt = planning_service.prompt_renderer.render(
        "transform_planner_user.txt.j2",
        {
            "run_mode": run_mode,
            "canonical_schema_json": schema.model_dump_json(indent=2),
            "workbook_profile_json": profile.model_dump_json(indent=2),
            "reference_data_json": json.dumps({}, indent=2),
            "constraints_json": json.dumps(planning_constraints, indent=2),
        },
    )

    schema_fingerprint_hash = schema_fingerprint.schema_fingerprint_sha256
    matching_cache_entry = schema_cache_repository.get_by_fingerprint(schema_fingerprint_hash)
    cache_entry_path = schema_cache_repository.get_entry_path(schema_fingerprint_hash)
    cache_match_found = matching_cache_entry is not None
    cache_hit_approved = bool(cache_match_found and matching_cache_entry.approval_status == "approved")

    if cache_hit_approved and matching_cache_entry is not None:
        plan = AgentResponse.model_validate(matching_cache_entry.planner_output)
        planning_source = "schema_cache"
        cache_decision = "known_input_schema"
    else:
        plan = planning_service.build_plan(
            schema,
            profile,
            run_mode=run_mode,
            constraints=planning_constraints,
        )
        planning_source = "planner"
        cache_decision = "known_input_schema_unapproved" if cache_match_found else "not_found"

    schema_fingerprint_path = artifact_store.write_text(
        "schema_fingerprint.json",
        schema_fingerprint.model_dump_json(indent=2),
    )
    system_prompt_path = artifact_store.write_text("planner_system_prompt.txt", system_prompt)
    user_prompt_path = artifact_store.write_text("planner_user_prompt.txt", user_prompt)
    planner_response_path = artifact_store.write_text("planner_response.json", plan.model_dump_json(indent=2))
    profile_path = artifact_store.write_text("workbook_profile.json", profile.model_dump_json(indent=2))
    # TD-003: emit lane provenance artifact when Coupa <<define>> rows were found
    if profile.lane_provenance:
        artifact_store.write_json(
            "lane_provenance.json",
            [entry.model_dump() for entry in profile.lane_provenance],
        )
    note_field_detection_path = artifact_store.write_json("note_field_detection.json", planning_constraints)
    schema_cache_lookup_payload = {
        "schema_fingerprint_sha256": schema_fingerprint_hash,
        "lookup_status": cache_decision,
        "match_found": cache_match_found,
        "cache_hit_approved": cache_hit_approved,
        "matched_entry_path": cache_entry_path if cache_match_found else "",
        "matched_entry_approval_status": matching_cache_entry.approval_status if matching_cache_entry else None,
        "planning_source": planning_source,
        "cache_root": str(schema_cache_repository.root_dir),
    }
    schema_cache_lookup_path = artifact_store.write_json("schema_cache_lookup.json", schema_cache_lookup_payload)

    schema_cache_metadata = {
        "input_workbook_path": input_workbook_path,
        "planner_mode": selected_planner_mode,
        "run_mode": run_mode,
        "run_dir": str(run_dir),
        "planning_source": planning_source,
    }
    if cache_hit_approved and matching_cache_entry is not None:
        schema_cache_write_payload = schema_cache_repository.record_usage(
            entry=matching_cache_entry,
            run_id=run_id,
            metadata=schema_cache_metadata,
        )
    else:
        schema_cache_write_payload = schema_cache_repository.upsert_candidate(
            fingerprint=schema_fingerprint,
            canonical_schema_name=schema.schema_name,
            planner_output=plan,
            run_id=run_id,
            metadata=schema_cache_metadata,
        )
    schema_cache_write_path = artifact_store.write_json("schema_cache_write.json", schema_cache_write_payload)
    foundry_invocation_payload: dict[str, Any] = {
        "mode": selected_planner_mode,
        "path": "skipped_schema_cache" if planning_source == "schema_cache" else "unknown",
        "planning_source": planning_source,
    }
    if planning_source == "planner" and hasattr(planning_service.client, "get_last_invocation_report"):
        try:
            foundry_invocation_payload = planning_service.client.get_last_invocation_report()
        except Exception:
            foundry_invocation_payload = {"mode": selected_planner_mode, "path": "report_unavailable"}
    foundry_invocation_report_path = artifact_store.write_json(
        "foundry_invocation_report.json",
        foundry_invocation_payload,
    )

    planner_notes: list[dict[str, Any]] = list(plan.notes_json or [])

    policy_result = evaluate_script_policy(plan.python_script)
    policy_report_path = artifact_store.write_json("script_policy_report.json", policy_result)

    if not policy_result["passed"]:
        write_canonical_csv([], schema, csv_path)
        write_notes_json(planner_notes, [], notes_json_path)
        policy_failed_validation = {
            "status": "PolicyFailed",
            "passed": False,
            "issues": policy_result["findings"],
            "issue_counts": {
                "error": policy_result["error_count"],
                "warning": policy_result["warning_count"],
            },
            "metrics": {"row_count": 0, "column_count": len(schema.columns), "null_rates": {}},
        }
        validation_path = artifact_store.write_json("validation_report.json", policy_failed_validation)
        execution_result = ExecutionResult(
            status="Failed",
            run_id=run_id,
            output_path=csv_path,
            artifacts=artifact_store.list_artifacts(),
            validation_summary=policy_failed_validation,
            error="Script policy failed",
        )
        execution_result_path = artifact_store.write_text(
            "execution_result.json",
            execution_result.model_dump_json(indent=2),
        )
        blob_manifest = []
        if blob_artifact_store is not None:
            blob_manifest = mirror_local_artifacts_to_blob(artifact_store, blob_artifact_store)
            artifact_store.write_json("blob_artifact_manifest.json", {"artifacts": blob_manifest})

        return {
            "run_mode": run_mode,
            "planner_mode": selected_planner_mode,
            "run_dir": str(run_dir),
            "row_count": 0,
            "validation_status": policy_failed_validation["status"],
            "validation_errors": policy_result["error_count"],
            "validation_warnings": policy_result["warning_count"],
            "csv": str(csv_path),
            "xlsx": "",
            "notes_json": str(notes_json_path),
            "profile": str(profile_path),
            "schema_fingerprint": str(schema_fingerprint_path),
            "schema_cache_lookup": str(schema_cache_lookup_path),
            "schema_cache_write": str(schema_cache_write_path),
            "note_field_detection": str(note_field_detection_path),
            "validation_report": str(validation_path),
            "planner_system_prompt": str(system_prompt_path),
            "planner_user_prompt": str(user_prompt_path),
            "planner_response": str(planner_response_path),
            "foundry_invocation_report": str(foundry_invocation_report_path),
            "script_policy_report": str(policy_report_path),
            "sandbox_execution_report": "",
            "execution_result": str(execution_result_path),
            "blob_artifact_manifest": blob_manifest,
        }

    if run_mode == "draft":
        canonical_records: list[dict[str, Any]] = []
        write_canonical_csv(canonical_records, schema, csv_path)
        write_notes_json(planner_notes, [], notes_json_path)
        sandbox_report_path = artifact_store.write_json(
            "sandbox_execution_report.json",
            {
                "status": "Skipped",
                "passed": True,
                "timed_out": False,
                "duration_ms": 0,
                "return_code": None,
                "stdout": "",
                "stderr": "",
                "result": None,
                "error": None,
            },
        )
        validation_result = {
            "status": "Draft",
            "passed": True,
            "issues": [],
            "issue_counts": {"error": 0, "warning": 0},
            "metrics": {"row_count": 0, "column_count": len(schema.columns), "null_rates": {}},
        }
        validation_path = artifact_store.write_json("validation_report.json", validation_result)
        execution_result = ExecutionResult(
            status="Succeeded",
            run_id=run_id,
            output_path=csv_path,
            artifacts=artifact_store.list_artifacts(),
            validation_summary=validation_result,
            error=None,
        )
        execution_result_path = artifact_store.write_text(
            "execution_result.json",
            execution_result.model_dump_json(indent=2),
        )
        blob_manifest = []
        if blob_artifact_store is not None:
            blob_manifest = mirror_local_artifacts_to_blob(artifact_store, blob_artifact_store)
            artifact_store.write_json("blob_artifact_manifest.json", {"artifacts": blob_manifest})

        return {
            "run_mode": run_mode,
            "planner_mode": selected_planner_mode,
            "run_dir": str(run_dir),
            "row_count": 0,
            "validation_status": validation_result["status"],
            "validation_errors": 0,
            "validation_warnings": 0,
            "csv": str(csv_path),
            "xlsx": "",
            "notes_json": str(notes_json_path),
            "profile": str(profile_path),
            "schema_fingerprint": str(schema_fingerprint_path),
            "schema_cache_lookup": str(schema_cache_lookup_path),
            "schema_cache_write": str(schema_cache_write_path),
            "note_field_detection": str(note_field_detection_path),
            "validation_report": str(validation_path),
            "planner_system_prompt": str(system_prompt_path),
            "planner_user_prompt": str(user_prompt_path),
            "planner_response": str(planner_response_path),
            "foundry_invocation_report": str(foundry_invocation_report_path),
            "script_policy_report": str(policy_report_path),
            "sandbox_execution_report": str(sandbox_report_path),
            "execution_result": str(execution_result_path),
            "blob_artifact_manifest": blob_manifest,
        }

    sandbox_result = execute_script_in_sandbox(
        plan.python_script,
        {
            "input_workbook_path": input_workbook_path,
            "output_dir": str(run_dir),
            "run_mode": run_mode,
            "canonical_schema": schema.model_dump(),
        },
        timeout_seconds=30,
    )
    sandbox_report_path = artifact_store.write_json("sandbox_execution_report.json", sandbox_result)

    if not sandbox_result["passed"]:
        write_canonical_csv([], schema, csv_path)
        write_notes_json(planner_notes, [], notes_json_path)
        sandbox_failed_validation = {
            "status": "SandboxFailed",
            "passed": False,
            "issues": [
                {
                    "code": "sandbox_execution_failed",
                    "severity": "error",
                    "message": sandbox_result.get("error") or "Sandbox execution failed.",
                }
            ],
            "issue_counts": {"error": 1, "warning": 0},
            "metrics": {"row_count": 0, "column_count": len(schema.columns), "null_rates": {}},
        }
        validation_path = artifact_store.write_json("validation_report.json", sandbox_failed_validation)
        execution_result = ExecutionResult(
            status="Failed",
            run_id=run_id,
            output_path=csv_path,
            artifacts=artifact_store.list_artifacts(),
            validation_summary=sandbox_failed_validation,
            error=sandbox_result.get("error") or "Sandbox execution failed",
        )
        execution_result_path = artifact_store.write_text(
            "execution_result.json",
            execution_result.model_dump_json(indent=2),
        )
        blob_manifest = []
        if blob_artifact_store is not None:
            blob_manifest = mirror_local_artifacts_to_blob(artifact_store, blob_artifact_store)
            artifact_store.write_json("blob_artifact_manifest.json", {"artifacts": blob_manifest})

        return {
            "run_mode": run_mode,
            "planner_mode": selected_planner_mode,
            "run_dir": str(run_dir),
            "row_count": 0,
            "validation_status": sandbox_failed_validation["status"],
            "validation_errors": 1,
            "validation_warnings": 0,
            "csv": str(csv_path),
            "xlsx": "",
            "notes_json": str(notes_json_path),
            "profile": str(profile_path),
            "schema_fingerprint": str(schema_fingerprint_path),
            "schema_cache_lookup": str(schema_cache_lookup_path),
            "schema_cache_write": str(schema_cache_write_path),
            "note_field_detection": str(note_field_detection_path),
            "validation_report": str(validation_path),
            "planner_system_prompt": str(system_prompt_path),
            "planner_user_prompt": str(user_prompt_path),
            "planner_response": str(planner_response_path),
            "foundry_invocation_report": str(foundry_invocation_report_path),
            "script_policy_report": str(policy_report_path),
            "sandbox_execution_report": str(sandbox_report_path),
            "execution_result": str(execution_result_path),
            "blob_artifact_manifest": blob_manifest,
        }

    raw_records = _extract_records_from_sandbox_result(sandbox_result.get("result"))
    sandbox_notes = _extract_notes_from_sandbox_result(sandbox_result.get("result"))
    canonical_records = normalize_records_to_canonical(raw_records, schema)

    # ── Notes-driven post-processing ──────────────────────────────
    postprocess_mode = os.getenv("POSTPROCESS_MODE", selected_planner_mode)
    post_processor = NotesPostProcessor(mode=postprocess_mode)
    canonical_records, post_process_change_log = post_processor.process(canonical_records)
    postprocess_report = {
        "mode": postprocess_mode,
        "rows_with_notes": sum(
            1 for r in canonical_records
            if str(r.get("Notes JSON", "")).strip() not in ("", "[]", "nan", "None")
        ),
        "total_updates": len(post_process_change_log),
        "fields_updated": list({c["field"] for c in post_process_change_log}),
        "change_log": post_process_change_log,
    }
    postprocess_report_path = artifact_store.write_json(
        "postprocess_report.json", postprocess_report
    )

    write_canonical_csv(canonical_records, schema, csv_path)
    write_canonical_xlsx(canonical_records, schema, xlsx_path)
    write_notes_json(planner_notes, sandbox_notes, notes_json_path, change_log=post_process_change_log)

    validation_result = validate_canonical_records(canonical_records, schema, include_lineage=True)
    validation_path = artifact_store.write_json("validation_report.json", validation_result)
    execution_result = ExecutionResult(
        status="Succeeded",
        run_id=run_id,
        output_path=xlsx_path,
        artifacts=artifact_store.list_artifacts(),
        validation_summary=validation_result,
        error=None,
    )
    execution_result_path = artifact_store.write_text(
        "execution_result.json",
        execution_result.model_dump_json(indent=2),
    )
    blob_manifest = []
    if blob_artifact_store is not None:
        blob_manifest = mirror_local_artifacts_to_blob(artifact_store, blob_artifact_store)
        artifact_store.write_json("blob_artifact_manifest.json", {"artifacts": blob_manifest})

    return {
        "run_mode": run_mode,
        "planner_mode": selected_planner_mode,
        "run_dir": str(run_dir),
        "row_count": len(canonical_records),
        "validation_status": validation_result["status"],
        "validation_errors": validation_result["issue_counts"]["error"],
        "validation_warnings": validation_result["issue_counts"]["warning"],
        "csv": str(csv_path),
        "xlsx": str(xlsx_path),
        "notes_json": str(notes_json_path),
        "profile": str(profile_path),
        "schema_fingerprint": str(schema_fingerprint_path),
        "schema_cache_lookup": str(schema_cache_lookup_path),
        "schema_cache_write": str(schema_cache_write_path),
        "note_field_detection": str(note_field_detection_path),
        "validation_report": str(validation_path),
        "planner_system_prompt": str(system_prompt_path),
        "planner_user_prompt": str(user_prompt_path),
        "planner_response": str(planner_response_path),
        "foundry_invocation_report": str(foundry_invocation_report_path),
        "script_policy_report": str(policy_report_path),
        "sandbox_execution_report": str(sandbox_report_path),
        "postprocess_report": str(postprocess_report_path),
        "execution_result": str(execution_result_path),
        "blob_artifact_manifest": blob_manifest,
    }
