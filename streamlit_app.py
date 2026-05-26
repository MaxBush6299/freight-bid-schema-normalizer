from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient

from src.function_app.local_rehydrate_runner import run_rehydrate
from src.function_app.services.foundry_agent_client import FoundryAgentClient
from src.function_app.services.pipeline_runner import run_pipeline

WORKSPACE_ROOT = Path(__file__).resolve().parent
EXAMPLE_INPUTS_DIR = WORKSPACE_ROOT / "examples" / "inputs"
STREAMLIT_OUTPUT_ROOT = WORKSPACE_ROOT / "artifacts" / "streamlit_runs"
STREAMLIT_REHYDRATE_OUTPUT_ROOT = WORKSPACE_ROOT / "artifacts" / "streamlit_rehydrate_runs"
LOCAL_SETTINGS_PATH = WORKSPACE_ROOT / "local.settings.json"
AZURITE_COMPAT_API_VERSION = "2021-12-02"

DISCOVERY_ROOTS = [
    WORKSPACE_ROOT / "artifacts" / "function_runs",
    WORKSPACE_ROOT / "artifacts" / "live_pipeline",
    WORKSPACE_ROOT / "artifacts" / "local_pipeline",
    WORKSPACE_ROOT / "artifacts" / "local_real_run",
    STREAMLIT_OUTPUT_ROOT,
]


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_local_settings_env() -> None:
    payload = _read_json(LOCAL_SETTINGS_PATH)
    if not payload:
        return

    values = payload.get("Values", {})
    if not isinstance(values, dict):
        return

    for key, value in values.items():
        if isinstance(value, str) and key not in os.environ:
            os.environ[key] = value


def _validate_live_mode_configuration() -> list[str]:
    missing: list[str] = []
    required = ["FOUNDRY_PROJECT_ENDPOINT"]
    for key in required:
        if not os.getenv(key, "").strip():
            missing.append(key)
    return missing


def _validate_blob_trigger_configuration() -> list[str]:
    missing: list[str] = []
    # Storage access: connection string (local) OR account name + MI (cloud)
    has_conn_str = bool(os.getenv("AzureWebJobsStorage", "").strip())
    has_account_name = bool(os.getenv("STORAGE_ACCOUNT_NAME", "").strip())
    if not has_conn_str and not has_account_name:
        missing.append("AzureWebJobsStorage or STORAGE_ACCOUNT_NAME")
    for key in ["INPUT_CONTAINER", "OUTPUT_CONTAINER"]:
        if not os.getenv(key, "").strip():
            missing.append(key)
    return missing


def _validate_rehydrate_blob_configuration() -> list[str]:
    """Validate config needed to push export/template blobs and call the function."""
    missing: list[str] = []
    has_conn_str = bool(os.getenv("AzureWebJobsStorage", "").strip())
    has_account_name = bool(os.getenv("STORAGE_ACCOUNT_NAME", "").strip())
    if not has_conn_str and not has_account_name:
        missing.append("AzureWebJobsStorage or STORAGE_ACCOUNT_NAME")
    # Containers default to export/templates/outbox in the Function app — only
    # flag them when explicitly blanked out.
    return missing


def _create_blob_service_client(connection_string: str = "") -> BlobServiceClient:
    """Create a BlobServiceClient.

    Uses *connection_string* when provided (local dev / Azurite).
    Falls back to managed-identity via ``DefaultAzureCredential`` +
    ``STORAGE_ACCOUNT_NAME`` when running in Azure (Container App, Web App).
    """
    if connection_string.strip():
        is_local_emulator = (
            "UseDevelopmentStorage=true" in connection_string
            or "127.0.0.1" in connection_string
        )
        if is_local_emulator:
            api_version = os.getenv("AZURE_BLOB_API_VERSION", AZURITE_COMPAT_API_VERSION).strip()
            if api_version:
                return BlobServiceClient.from_connection_string(connection_string, api_version=api_version)
        return BlobServiceClient.from_connection_string(connection_string)

    # MI fallback — requires STORAGE_ACCOUNT_NAME env var
    account_name = os.getenv("STORAGE_ACCOUNT_NAME", "").strip()
    if not account_name:
        raise ValueError(
            "No blob storage configuration. Set AzureWebJobsStorage (connection string) "
            "or STORAGE_ACCOUNT_NAME (managed-identity) in your environment."
        )
    from azure.identity import DefaultAzureCredential

    account_url = f"https://{account_name}.blob.core.windows.net"
    return BlobServiceClient(account_url, credential=DefaultAzureCredential())


def _submit_blob_trigger_run(input_workbook: Path) -> str:
    connection_string = os.getenv("AzureWebJobsStorage", "")
    input_container = os.getenv("INPUT_CONTAINER", "input")

    blob_service = _create_blob_service_client(connection_string)
    container_client = blob_service.get_container_client(input_container)
    try:
        container_client.create_container()
    except Exception:
        pass

    timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    blob_name = f"{input_workbook.stem}-streamlit-{timestamp}{input_workbook.suffix}"
    container_client.upload_blob(blob_name, input_workbook.read_bytes(), overwrite=True)
    return blob_name


def _wait_for_blob_outputs(blob_name: str, timeout_seconds: int = 90) -> dict[str, bool]:
    connection_string = os.getenv("AzureWebJobsStorage", "")
    output_container = os.getenv("OUTPUT_CONTAINER", "output")

    expected_blobs = {
        f"{blob_name}.canonical.csv": False,
        f"{blob_name}.validation.json": False,
        f"{blob_name}.planner.json": False,
    }

    blob_service = _create_blob_service_client(connection_string)
    container_client = blob_service.get_container_client(output_container)

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        all_ready = True
        for expected_blob in list(expected_blobs.keys()):
            blob_client = container_client.get_blob_client(expected_blob)
            if expected_blobs[expected_blob]:
                continue
            if blob_client.exists():
                expected_blobs[expected_blob] = True
            else:
                all_ready = False

        if all_ready:
            break

        time.sleep(2)

    return expected_blobs


@st.cache_data(show_spinner=False)
def _fetch_foundry_agents_snapshot(
    endpoint: str,
    api_version: str,
    agent_name: str,
    assistant_id: str,
) -> dict[str, Any]:
    """Fetch only the system-expected agents from Foundry.

    Expected agents are read from local.settings.json Values:
      * FOUNDRY_AGENT_NAME / FOUNDRY_AGENT_VERSION  (Transform Planner)
      * FOUNDRY_POSTPROCESS_AGENT_NAME / FOUNDRY_POSTPROCESS_AGENT_VERSION (Notes Post-Processor)

    Only those two agents are returned regardless of how many agents
    exist in the Foundry project.
    """
    if not endpoint.strip():
        return {"agents": [], "error": "FOUNDRY_PROJECT_ENDPOINT is not configured."}

    # Build the list of expected agents from environment
    expected_agents: list[dict[str, Any]] = [
        {
            "role": "Transform Planner",
            "env_name_key": "FOUNDRY_AGENT_NAME",
            "env_version_key": "FOUNDRY_AGENT_VERSION",
            "configured_name": os.getenv("FOUNDRY_AGENT_NAME", "").strip(),
            "configured_version": os.getenv("FOUNDRY_AGENT_VERSION", "").strip(),
        },
        {
            "role": "Notes Post-Processor",
            "env_name_key": "FOUNDRY_POSTPROCESS_AGENT_NAME",
            "env_version_key": "FOUNDRY_POSTPROCESS_AGENT_VERSION",
            "configured_name": os.getenv("FOUNDRY_POSTPROCESS_AGENT_NAME", "").strip(),
            "configured_version": os.getenv("FOUNDRY_POSTPROCESS_AGENT_VERSION", "").strip(),
        },
    ]

    expected_names = {ea["configured_name"] for ea in expected_agents if ea["configured_name"]}

    # Fetch all agents from the project, then filter to expected ones
    client = FoundryAgentClient(
        endpoint=endpoint,
        agent_name=agent_name,
        mode="live",
    )
    client.assistant_id = assistant_id or ""

    all_remote: list[dict[str, Any]] = []

    def _upsert_remote(record: dict[str, Any]) -> None:
        remote_name = (record.get("agent_name") or "").strip()
        if not remote_name:
            return
        for existing in all_remote:
            if existing.get("agent_name") != remote_name:
                continue
            for key in ["assistant_id", "system_prompt", "model", "created_at"]:
                if not existing.get(key) and record.get(key):
                    existing[key] = record[key]
            return
        all_remote.append(record)

    fetch_error: str | None = None
    try:
        payload = client._get_json(f"{endpoint.rstrip('/')}/assistants?api-version={api_version}")
        data = payload.get("data", []) if isinstance(payload, dict) else []
        for item in data:
            if not isinstance(item, dict):
                continue
            remote_name = item.get("name") or item.get("id") or "Unknown"
            _upsert_remote({
                "assistant_id": item.get("id") or "",
                "agent_name": remote_name,
                "endpoint": endpoint,
                "system_prompt": item.get("instructions") or "",
                "model": item.get("model") or "",
                "created_at": item.get("created_at") or "",
            })
    except Exception as error:
        fetch_error = str(error)

    agents_sdk_error: str | None = None
    try:
        from azure.ai.projects import AIProjectClient

        credential = DefaultAzureCredential(exclude_interactive_browser_credential=False)
        project_client = AIProjectClient(endpoint=endpoint, credential=credential)
        for item in project_client.agents.list():
            remote_name = (getattr(item, "name", "") or getattr(item, "id", "") or "Unknown").strip()
            if not remote_name:
                continue
            _upsert_remote({
                "assistant_id": (getattr(item, "id", "") or "").strip(),
                "agent_name": remote_name,
                "endpoint": endpoint,
                "system_prompt": getattr(item, "instructions", "") or "",
                "model": getattr(item, "model", "") or "",
                "created_at": str(getattr(item, "created_at", "") or ""),
            })
    except Exception as error:
        agents_sdk_error = str(error)

    agents_rest_error: str | None = None
    try:
        payload = client._get_json(f"{endpoint.rstrip('/')}/agents?api-version={api_version}")
        data = payload.get("data", []) if isinstance(payload, dict) else []
        for item in data:
            if not isinstance(item, dict):
                continue
            remote_name = (item.get("name") or item.get("id") or "Unknown").strip()
            versions = item.get("versions") or {}
            latest = versions.get("latest") if isinstance(versions, dict) else {}
            latest = latest if isinstance(latest, dict) else {}
            definition = latest.get("definition") or {}
            definition = definition if isinstance(definition, dict) else {}

            _upsert_remote({
                "assistant_id": (latest.get("id") or item.get("id") or "").strip(),
                "agent_name": remote_name,
                "endpoint": endpoint,
                "system_prompt": definition.get("instructions") or item.get("instructions") or "",
                "model": definition.get("model") or item.get("model") or "",
                "created_at": latest.get("created_at") or item.get("created_at") or "",
            })
    except Exception as error:
        agents_rest_error = str(error)

    if fetch_error and agents_sdk_error and agents_rest_error:
        fetch_error = (
            f"assistants: {fetch_error}; "
            f"agents_sdk: {agents_sdk_error}; agents_rest: {agents_rest_error}"
        )
    elif not fetch_error and agents_sdk_error and agents_rest_error and not all_remote:
        fetch_error = f"agents_sdk: {agents_sdk_error}; agents_rest: {agents_rest_error}"

    # Build enriched agent cards — one per expected agent
    agents: list[dict[str, Any]] = []
    for ea in expected_agents:
        configured_name = ea["configured_name"]
        configured_version = ea["configured_version"]
        role = ea["role"]

        # Find matching remote agent(s) by name
        matches = [r for r in all_remote if r["agent_name"] == configured_name]
        remote = matches[0] if matches else None

        status = "connected" if remote else ("not_found" if not fetch_error else "fetch_error")

        agents.append({
            "role": role,
            "configured_name": configured_name or "(not configured)",
            "configured_version": configured_version or "(none)",
            "env_name_key": ea["env_name_key"],
            "env_version_key": ea["env_version_key"],
            "assistant_id": remote["assistant_id"] if remote else "",
            "system_prompt": remote["system_prompt"] if remote else "",
            "model": remote.get("model", "") if remote else "",
            "created_at": remote.get("created_at", "") if remote else "",
            "status": status,
            "endpoint": endpoint,
        })

    return {
        "agents": agents,
        "error": fetch_error,
        "expected_names": sorted(expected_names),
        "remote_agent_count": len(all_remote),
    }


def _render_foundry_agents_page(snapshot: dict[str, Any]) -> None:
    st.subheader("System Agents")
    error = snapshot.get("error")
    agents = snapshot.get("agents", [])
    remote_count = snapshot.get("remote_agent_count", 0)

    if isinstance(error, str) and error.strip():
        st.warning(f"Could not query Foundry: {error}")
        st.caption("Showing local configuration only (remote status unavailable).")

    st.caption(
        f"Showing {len(agents)} expected system agent(s). "
        f"{remote_count} total agent(s) exist in the Foundry project."
    )

    for agent in agents:
        role = agent.get("role", "Agent")
        configured_name = agent.get("configured_name", "")
        configured_version = agent.get("configured_version", "")
        status = agent.get("status", "unknown")
        assistant_id = agent.get("assistant_id", "")
        model = agent.get("model", "")
        system_prompt = agent.get("system_prompt", "")
        env_name_key = agent.get("env_name_key", "")
        env_version_key = agent.get("env_version_key", "")

        # Status badge
        if status == "connected":
            icon = "\u2705"
            status_label = "Connected"
        elif status == "not_found":
            icon = "\u274c"
            status_label = "Not Found in Foundry"
        else:
            icon = "\u26a0\ufe0f"
            status_label = "Fetch Error"

        header = f"{icon} **{role}** — `{configured_name}` v{configured_version}"
        with st.expander(header, expanded=(status == "connected")):
            col_a, col_b, col_c = st.columns(3)
            col_a.metric("Status", status_label)
            col_b.metric("Version", configured_version or "—")
            col_c.metric("Model", model or "—")

            st.markdown("**Configuration**")
            st.code(
                f"{env_name_key}={configured_name}\n{env_version_key}={configured_version}",
                language="ini",
            )

            if assistant_id:
                st.caption(f"Assistant ID: `{assistant_id}`")

            if system_prompt:
                prompt_preview = system_prompt[:300] + ("…" if len(system_prompt) > 300 else "")
                st.markdown("**System Prompt** (preview)")
                st.text(prompt_preview)
                with st.expander("Full System Prompt"):
                    st.code(system_prompt, language="text")


def _discover_pipeline_dirs() -> list[Path]:
    discovered: list[Path] = []
    seen: set[Path] = set()

    for pipeline_dir in _sync_blob_output_runs_to_local_cache():
        if pipeline_dir not in seen:
            discovered.append(pipeline_dir)
            seen.add(pipeline_dir)

    for root in DISCOVERY_ROOTS:
        if not root.exists() or not root.is_dir():
            continue

        if root.name == "function_runs":
            for blob_run_dir in root.iterdir():
                if not blob_run_dir.is_dir():
                    continue
                for pipeline_dir in blob_run_dir.glob("pipeline_*"):
                    if pipeline_dir.is_dir() and pipeline_dir not in seen:
                        discovered.append(pipeline_dir)
                        seen.add(pipeline_dir)
        else:
            for pipeline_dir in root.glob("pipeline_*"):
                if pipeline_dir.is_dir() and pipeline_dir not in seen:
                    discovered.append(pipeline_dir)
                    seen.add(pipeline_dir)

    discovered.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return discovered


def _safe_blob_run_name(blob_stem: str) -> str:
    safe = blob_stem.replace("/", "_").replace("\\", "_").replace(":", "_")
    return safe or "blob-run"


def _sync_blob_output_runs_to_local_cache() -> list[Path]:
    """Mirror recent blob outputs into local pipeline folders for Prior Runs browsing.

    Hosted Streamlit instances usually do not have the function's local artifacts folder,
    so this builds compatible local run folders from blobs in OUTPUT_CONTAINER.
    """
    output_container = os.getenv("OUTPUT_CONTAINER", "output").strip()
    connection_string = os.getenv("AzureWebJobsStorage", "")
    if not output_container:
        return []

    try:
        blob_service = _create_blob_service_client(connection_string)
        container_client = blob_service.get_container_client(output_container)
        blob_items = list(container_client.list_blobs())
    except Exception:
        return []

    suffix_to_file = {
        ".validation.json": "validation_report.json",
        ".planner.json": "planner_response.json",
        ".canonical.csv": "canonical_output.csv",
    }

    grouped: dict[str, dict[str, Any]] = {}
    for blob in blob_items:
        blob_name = getattr(blob, "name", "")
        if not isinstance(blob_name, str) or not blob_name:
            continue

        matched_suffix = ""
        target_file = ""
        for suffix, mapped_file in suffix_to_file.items():
            if blob_name.endswith(suffix):
                matched_suffix = suffix
                target_file = mapped_file
                break
        if not matched_suffix:
            continue

        stem = blob_name[: -len(matched_suffix)]
        group = grouped.setdefault(stem, {"files": {}, "last_modified": None})
        group["files"][target_file] = blob_name

        last_modified = getattr(blob, "last_modified", None)
        if group["last_modified"] is None or (
            last_modified is not None and last_modified > group["last_modified"]
        ):
            group["last_modified"] = last_modified

    if not grouped:
        return []

    run_limit = int(os.getenv("STREAMLIT_BLOB_RUN_LIMIT", "40") or "40")
    sorted_runs = sorted(
        grouped.items(),
        key=lambda item: item[1].get("last_modified") or datetime.min,
        reverse=True,
    )[: max(1, run_limit)]

    cache_root = WORKSPACE_ROOT / "artifacts" / "function_runs"
    discovered: list[Path] = []

    for stem, metadata in sorted_runs:
        files = metadata.get("files", {}) if isinstance(metadata, dict) else {}
        if not isinstance(files, dict) or "validation_report.json" not in files:
            continue

        run_folder = cache_root / f"{_safe_blob_run_name(stem)}-blob" / "pipeline_blob"
        run_folder.mkdir(parents=True, exist_ok=True)
        discovered.append(run_folder)

        for local_name, blob_name in files.items():
            local_path = run_folder / local_name
            try:
                payload = container_client.download_blob(blob_name).readall()
                local_path.write_bytes(payload)
            except Exception:
                continue

        # Keep a minimal execution_result for summary cards.
        execution_result_path = run_folder / "execution_result.json"
        if not execution_result_path.exists():
            validation_payload = _read_json(run_folder / "validation_report.json") or {}
            validation_status = str(validation_payload.get("status", "Unknown"))
            execution_result_path.write_text(
                json.dumps(
                    {
                        "status": "Success" if validation_status.lower() in {"pass", "passed"} else "Unknown",
                        "source": "blob_output_container",
                        "blob_stem": stem,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

    return discovered


def _build_run_index() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for run_dir in _discover_pipeline_dirs():
        validation = _read_json(run_dir / "validation_report.json") or {}
        execution = _read_json(run_dir / "execution_result.json") or {}

        mtime = datetime.fromtimestamp(run_dir.stat().st_mtime)
        status = execution.get("status", "Unknown")
        validation_status = validation.get("status", "Unknown")
        issue_counts = validation.get("issue_counts", {})
        row_count = (validation.get("metrics", {}) or {}).get("row_count")

        rows.append(
            {
                "run_dir": run_dir,
                "modified": mtime,
                "status": status,
                "validation_status": validation_status,
                "errors": issue_counts.get("error", 0),
                "warnings": issue_counts.get("warning", 0),
                "row_count": row_count if row_count is not None else 0,
            }
        )
    return rows


def _run_label(entry: dict[str, Any]) -> str:
    return (
        f"{entry['modified'].strftime('%Y-%m-%d %H:%M:%S')} | "
        f"{entry['validation_status']} | rows={entry['row_count']} | {entry['run_dir']}"
    )


def _find_run_index_by_dir(run_index: list[dict[str, Any]], run_dir: Path) -> int | None:
    target = run_dir.resolve()
    for index, entry in enumerate(run_index):
        if entry["run_dir"].resolve() == target:
            return index
    return None


def _find_latest_run_for_blob_stem(run_index: list[dict[str, Any]], blob_stem: str) -> dict[str, Any] | None:
    for entry in run_index:
        if entry["run_dir"].parent.name == blob_stem:
            return entry
    return None


def _open_run_in_browse_mode(run_dir: Path) -> None:
    st.session_state["mode"] = "Browse Prior Runs"
    st.session_state["target_run_dir"] = str(run_dir.resolve())
    st.rerun()


def _render_overview(entry: dict[str, Any]) -> None:
    st.subheader("Run Summary")
    st.write(str(entry["run_dir"]))

    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Run Status", str(entry["status"]))
    col2.metric("Validation", str(entry["validation_status"]))
    col3.metric("Rows", int(entry["row_count"]))
    col4.metric("Errors", int(entry["errors"]))
    col5.metric("Warnings", int(entry["warnings"]))


def _approve_schema_cache_entry(lookup: dict[str, Any]) -> tuple[bool, str]:
    fingerprint = str(lookup.get("schema_fingerprint_sha256", "")).strip()
    if not fingerprint:
        return False, "Schema fingerprint is missing in lookup payload."

    matched_entry_path = str(lookup.get("matched_entry_path", "")).strip()
    cache_root = str(lookup.get("cache_root", "")).strip()

    if matched_entry_path:
        entry_path = Path(matched_entry_path)
    elif cache_root:
        entry_path = Path(cache_root) / "entries" / f"{fingerprint}.json"
    else:
        entry_path = WORKSPACE_ROOT / "artifacts" / "schema_cache" / "entries" / f"{fingerprint}.json"

    if not entry_path.exists():
        return False, f"Schema cache entry not found: {entry_path}"

    try:
        payload = json.loads(entry_path.read_text(encoding="utf-8"))
    except Exception as error:
        return False, f"Unable to read cache entry: {error}"

    approved_at = datetime.utcnow().isoformat() + "Z"
    payload["approval_status"] = "approved"
    payload["approval_source"] = "human_ui"
    payload["auto_approve_enabled"] = True
    payload["last_seen_at"] = approved_at
    metadata = payload.get("metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
    metadata["approved_at"] = approved_at
    metadata["approved_via"] = "streamlit_ui"
    payload["metadata"] = metadata

    try:
        entry_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception as error:
        return False, f"Unable to write cache entry: {error}"

    return True, str(entry_path)


def _render_schema_lookup(run_dir: Path) -> None:
    st.subheader("Schema Lookup")
    lookup = _read_json(run_dir / "schema_cache_lookup.json")

    if lookup is None:
        st.warning("schema_cache_lookup.json not found or unreadable.")
        return

    lookup_status = str(lookup.get("lookup_status", "unknown"))
    match_found = bool(lookup.get("match_found", False))
    cache_hit_approved = bool(lookup.get("cache_hit_approved", False))
    planning_source = str(lookup.get("planning_source", "unknown"))
    approval_status = str(lookup.get("matched_entry_approval_status") or "")
    fingerprint = str(lookup.get("schema_fingerprint_sha256", "")).strip()

    matched_entry_path = str(lookup.get("matched_entry_path", "")).strip()
    cache_root = str(lookup.get("cache_root", "")).strip()
    if matched_entry_path:
        entry_path = Path(matched_entry_path)
    elif cache_root and fingerprint:
        entry_path = Path(cache_root) / "entries" / f"{fingerprint}.json"
    elif fingerprint:
        entry_path = WORKSPACE_ROOT / "artifacts" / "schema_cache" / "entries" / f"{fingerprint}.json"
    else:
        entry_path = None

    current_entry_approval_status = ""
    if entry_path is not None and entry_path.exists():
        try:
            current_entry_payload = json.loads(entry_path.read_text(encoding="utf-8"))
            current_entry_approval_status = str(current_entry_payload.get("approval_status") or "")
        except Exception:
            current_entry_approval_status = ""

    effective_approval_status = current_entry_approval_status or approval_status
    approval_button_available = bool(entry_path is not None and entry_path.exists() and effective_approval_status != "approved")

    label_map = {
        "known_input_schema": "Known input schema",
        "known_input_schema_unapproved": "Known input schema (unapproved)",
        "not_found": "Not found",
    }
    display_status = label_map.get(lookup_status, lookup_status)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Lookup Result", display_status)
    c2.metric("Match Found", "Yes" if match_found else "No")
    c3.metric("Planning Source", planning_source)
    c4.metric("Approval", effective_approval_status or "N/A")

    if lookup_status == "known_input_schema":
        st.success("Known input schema found. Cached planner output was used.")
    elif lookup_status == "known_input_schema_unapproved":
        st.info("Known schema signature found, but it is not approved yet. Planner was executed.")
    elif lookup_status == "not_found":
        st.info("No known input schema match was found. Planner was executed.")

    if approval_button_available:
        if st.button(
            "Approve this schema for cache reuse",
            type="primary",
            key=f"approve-schema-{fingerprint or run_dir.name}",
        ):
            ok, detail = _approve_schema_cache_entry(lookup)
            if ok:
                st.success(f"Schema approved. Cache entry updated at: {detail}")
                st.rerun()
            else:
                st.error(detail)
    elif effective_approval_status == "approved":
        st.success("This schema is approved for cache reuse.")
    elif not match_found:
        st.caption("No matched cache entry was recorded at lookup time for this run.")
        if entry_path is not None and entry_path.exists():
            st.info("A draft cache entry now exists for this fingerprint, but the run snapshot still shows not_found.")

    st.caption("Schema fingerprint")
    st.code(str(lookup.get("schema_fingerprint_sha256", "")), language="text")

    with st.expander("Lookup details", expanded=False):
        st.json(lookup)


def _render_output_notes(run_dir: Path) -> None:
    """Render the notes list from notes.json (excludes post-processing details)."""
    st.subheader("Output Notes")
    notes_path = run_dir / "notes.json"
    if not notes_path.exists():
        st.caption("notes.json not found for this run.")
        return

    notes_data = _read_json(notes_path)
    if not notes_data:
        st.caption("notes.json is empty.")
        return

    total = notes_data.get("total_notes", 0)
    planner_count = notes_data.get("planner_note_count", 0)
    transform_count = notes_data.get("transform_note_count", 0)

    col1, col2, col3 = st.columns(3)
    col1.metric("Total Notes", total)
    col2.metric("Planner Notes", planner_count)
    col3.metric("Transform Notes", transform_count)

    notes_list = notes_data.get("notes", [])
    if notes_list:
        severity_order = {"critical": 0, "warning": 1, "info": 2}
        sorted_notes = sorted(notes_list, key=lambda n: severity_order.get(n.get("severity", "info"), 99))

        for note in sorted_notes:
            severity = note.get("severity", "info")
            category = note.get("category", "general")
            origin = note.get("origin", "")
            text = note.get("note", "")
            icon = {"critical": "\u274c", "warning": "\u26a0\ufe0f", "info": "\u2139\ufe0f"}.get(severity, "\u2139\ufe0f")
            badge = f"`{category}`" + (f" | `{origin}`" if origin else "")
            st.markdown(f"{icon} {badge} — {text}")

        with st.expander("Raw notes.json"):
            st.json(notes_data)
    else:
        st.info("No notes were generated for this run.")


def _render_postprocessing(run_dir: Path) -> None:
    """Render post-processing change log and postprocess report."""
    st.subheader("Post-Processing")

    notes_path = run_dir / "notes.json"
    notes_data = _read_json(notes_path) if notes_path.exists() else {}
    change_log_data = (notes_data or {}).get("post_process_change_log", {})
    change_count = change_log_data.get("total_updates", 0)

    st.metric("Post-Process Field Updates", change_count)

    # ── Post-Process Change Log ───────────────────────────────────
    if change_count > 0:
        fields_updated = change_log_data.get("fields_updated", [])
        st.caption(f"{change_count} field updates across {len(fields_updated)} field types: {', '.join(fields_updated)}")

        changes = change_log_data.get("changes", [])

        # Summary table
        change_rows = []
        for c in changes:
            change_rows.append({
                "Lane ID": c.get("lane_id", ""),
                "Row": c.get("row_index", ""),
                "Field": c.get("field", ""),
                "Old Value": str(c.get("old_value")) if c.get("old_value") is not None else "(empty)",
                "New Value": str(c.get("new_value", "")),
                "Reason": c.get("reason", ""),
            })
        if change_rows:
            st.dataframe(pd.DataFrame(change_rows), use_container_width=True, height=min(400, 35 * len(change_rows) + 38))

        with st.expander("Raw change log JSON"):
            st.json(change_log_data)
    else:
        st.info("No post-process field updates were applied for this run.")

    # ── Also show the postprocess_report.json if present ──────────
    postprocess_report_path = run_dir / "postprocess_report.json"
    if postprocess_report_path.exists():
        report = _read_json(postprocess_report_path)
        if report:
            with st.expander("Post-Process Report (postprocess_report.json)"):
                st.json(report)
    elif change_count == 0:
        st.caption("No postprocess_report.json found for this run.")


def _render_final_results(run_dir: Path) -> None:
    st.subheader("Canonical Output")
    csv_path = run_dir / "canonical_output.csv"
    xlsx_path = run_dir / "canonical_output.xlsx"

    if not csv_path.exists():
        st.warning("canonical_output.csv not found for this run.")
        return

    dataframe = pd.read_csv(csv_path)
    st.write(f"Rows: {len(dataframe)}")
    st.dataframe(dataframe, use_container_width=True, height=450)

    st.download_button(
        "Download canonical CSV",
        data=csv_path.read_bytes(),
        file_name=csv_path.name,
        mime="text/csv",
    )

    if xlsx_path.exists():
        st.caption(f"XLSX path: {xlsx_path}")

    # ── Per-row Notes JSON breakdown ──────────────────────────────
    if "Notes JSON" in dataframe.columns:
        notes_col = dataframe["Notes JSON"].dropna().astype(str)
        non_empty = notes_col[notes_col.str.strip() != "[]"]
        st.markdown(f"**Per-Row Notes:** {len(non_empty)} of {len(dataframe)} rows have notes")

        if len(non_empty) > 0:
            with st.expander(f"Show rows with notes ({len(non_empty)})", expanded=False):
                for idx, raw_json in non_empty.items():
                    try:
                        notes_list = json.loads(raw_json)
                    except Exception:
                        notes_list = []
                    if not notes_list:
                        continue
                    lane_id = dataframe.at[idx, "Customer Lane ID"] if "Customer Lane ID" in dataframe.columns else f"Row {idx}"
                    items = ", ".join(f"**{n.get('field', '?')}**: {n.get('value', '')}" for n in notes_list)
                    st.markdown(f"- `{lane_id}` — {items}")


def _render_planner_output(run_dir: Path) -> None:
    st.subheader("Planner Output")
    planner_path = run_dir / "planner_response.json"
    planner = _read_json(planner_path)
    note_detection = _read_json(run_dir / "note_field_detection.json")

    if planner is None:
        st.warning("planner_response.json not found or unreadable.")
        return

    left, right = st.columns(2)
    left.write("Relevant sheets")
    left.json(planner.get("relevant_sheets", []))
    right.write("Ignored sheets")
    right.json(planner.get("ignored_sheets", []))

    with st.expander("Mapping Plan", expanded=False):
        st.json(planner.get("mapping_plan", {}))

    with st.expander("Constants", expanded=False):
        st.json(planner.get("constants", {}))

    with st.expander("Enrichments", expanded=False):
        st.json(planner.get("enrichments", {}))

    with st.expander("Assumptions", expanded=False):
        st.json(planner.get("assumptions", []))

    script = planner.get("python_script", "")
    st.markdown("**Generated Transform Script**")
    st.code(script, language="python")

    st.markdown("**Identified Note Fields**")
    if isinstance(note_detection, dict):
        note_candidates = note_detection.get("note_field_candidates", [])
        canonical_note_fields = note_detection.get("canonical_note_fields", [])
        st.write("Canonical note fields")
        st.json(canonical_note_fields)
        st.write("Detected source note-like columns")
        if isinstance(note_candidates, list) and note_candidates:
            st.json(note_candidates)
        else:
            st.caption("No note-like source columns detected for this run.")
    else:
        st.caption("note_field_detection.json not found for this run.")


def _render_validation(run_dir: Path) -> None:
    st.subheader("Validation")
    validation = _read_json(run_dir / "validation_report.json")

    if validation is None:
        st.warning("validation_report.json not found or unreadable.")
        return

    issue_counts = validation.get("issue_counts", {})
    metrics = validation.get("metrics", {})

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Status", str(validation.get("status", "Unknown")))
    c2.metric("Errors", int(issue_counts.get("error", 0)))
    c3.metric("Warnings", int(issue_counts.get("warning", 0)))
    c4.metric("Row Count", int((metrics or {}).get("row_count", 0)))

    issues = validation.get("issues", [])
    if issues:
        issues_frame = pd.DataFrame(issues)
        st.dataframe(issues_frame, use_container_width=True, height=350)
    else:
        st.success("No validation issues.")

    with st.expander("Validation Metrics", expanded=False):
        st.json(metrics)


def _render_execution_logs(run_dir: Path) -> None:
    st.subheader("Execution Logs")

    sandbox = _read_json(run_dir / "sandbox_execution_report.json")
    execution = _read_json(run_dir / "execution_result.json")

    if sandbox is not None:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Sandbox Status", str(sandbox.get("status", "Unknown")))
        c2.metric("Duration (ms)", int(sandbox.get("duration_ms", 0) or 0))
        c3.metric("Return Code", int(sandbox.get("return_code", 0) or 0))
        c4.metric("Timed Out", str(bool(sandbox.get("timed_out", False))))

        with st.expander("Sandbox stdout", expanded=False):
            st.text(sandbox.get("stdout", ""))

        with st.expander("Sandbox stderr", expanded=False):
            st.text(sandbox.get("stderr", ""))

    else:
        st.info("sandbox_execution_report.json not found for this run.")

    if execution is not None:
        with st.expander("Execution Result JSON", expanded=False):
            st.json(execution)


def _render_run_details(entry: dict[str, Any]) -> None:
    run_dir = entry["run_dir"]
    _render_overview(entry)

    tab1, tab2, tab3, tab4, tab5, tab6, tab7 = st.tabs(
        ["Output Planner", "Output Notes", "Post-Processing", "Final Results", "Schema Lookup", "Validation", "Execution Logs"]
    )

    with tab1:
        _render_planner_output(run_dir)
    with tab2:
        _render_output_notes(run_dir)
    with tab3:
        _render_postprocessing(run_dir)
    with tab4:
        _render_final_results(run_dir)
    with tab5:
        _render_schema_lookup(run_dir)
    with tab6:
        _render_validation(run_dir)
    with tab7:
        _render_execution_logs(run_dir)


def _create_new_run() -> dict[str, Any] | None:
    st.subheader("Create New Run")

    example_inputs = sorted(EXAMPLE_INPUTS_DIR.glob("*.xlsx"))

    # ── File upload ───────────────────────────────────────────────
    uploaded_file = st.file_uploader(
        "Upload a new workbook (will be saved to examples/inputs)",
        type=["xlsx", "xls"],
        key="upload_input_workbook",
    )
    if uploaded_file is not None:
        dest = EXAMPLE_INPUTS_DIR / uploaded_file.name
        if not dest.exists() or st.checkbox(f"Overwrite existing {uploaded_file.name}?", value=False):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(uploaded_file.getvalue())
            st.success(f"Saved to {dest}")
            # Refresh the list so the new file shows up
            example_inputs = sorted(EXAMPLE_INPUTS_DIR.glob("*.xlsx"))

    if not example_inputs:
        st.error("No example inputs found under examples/inputs.")
        return None

    # ── Pre-select uploaded file if it was just added ─────────────
    default_idx = 0
    if uploaded_file is not None:
        target = EXAMPLE_INPUTS_DIR / uploaded_file.name
        for i, p in enumerate(example_inputs):
            if p.name == target.name:
                default_idx = i
                break

    selected_input = st.selectbox("Input workbook", options=example_inputs, index=default_idx, format_func=lambda path: path.name)
    run_mode = st.selectbox("Run mode", options=["execute_with_validation", "draft"])
    planner_mode = st.selectbox("Planner mode", options=["live", "mock"])
    run_target = st.selectbox(
        "Run target",
        options=["Blob trigger (input/output containers)", "Direct pipeline (local artifacts)"],
    )

    if planner_mode == "live":
        missing = _validate_live_mode_configuration()
        if missing:
            st.error(
                "Live planner mode is missing required configuration: "
                + ", ".join(missing)
                + ". Add these to local.settings.json (Values) or your environment."
            )

    st.caption(f"Output root: {STREAMLIT_OUTPUT_ROOT}")
    if run_target.startswith("Direct"):
        st.info(
            "Direct pipeline mode writes artifacts under artifacts/streamlit_runs and does not write to input/output blob containers."
        )
    else:
        missing_blob = _validate_blob_trigger_configuration()
        if missing_blob:
            st.error(
                "Blob trigger mode is missing required configuration: "
                + ", ".join(missing_blob)
                + ". Add these to local.settings.json (Values) or your environment."
            )

    if st.button("Run Pipeline", type="primary"):
        if planner_mode == "live":
            missing = _validate_live_mode_configuration()
            if missing:
                st.stop()

        if run_target.startswith("Blob trigger"):
            missing_blob = _validate_blob_trigger_configuration()
            if missing_blob:
                st.stop()

            with st.spinner("Uploading input blob and waiting for function output blobs..."):
                blob_name = _submit_blob_trigger_run(selected_input)
                outputs = _wait_for_blob_outputs(blob_name, timeout_seconds=90)

            st.success(f"Blob trigger submitted: {blob_name}")
            st.write("Output blob status")
            st.json(outputs)
            if all(outputs.values()):
                st.success("All expected output blobs are present in the output container.")
                blob_stem = Path(blob_name).stem
                run_index = _build_run_index()
                matching = _find_latest_run_for_blob_stem(run_index, blob_stem)
                if matching is not None:
                    if st.button("Open Finished Report", type="secondary"):
                        _open_run_in_browse_mode(matching["run_dir"])
            else:
                st.warning("Not all output blobs were detected before timeout. Check Function host logs and refresh run index.")
            return None

        with st.spinner("Running pipeline..."):
            result = run_pipeline(
                input_workbook=str(selected_input),
                output_root=str(STREAMLIT_OUTPUT_ROOT),
                run_mode=run_mode,
                planner_mode=planner_mode,
            )
        st.success("Run completed")
        if st.button("Open Finished Report", type="secondary"):
            _open_run_in_browse_mode(Path(result["run_dir"]))
        return result

    return None


def _create_rehydrate_run() -> dict[str, Any] | None:
    """Render the Rehydrate Submission page.

    Lets the user pick a customer template + priced export and run the
    reverse pipeline via one of two targets:

    * **Direct (local artifacts)** — calls ``run_rehydrate`` in-process and
      writes artifacts under ``artifacts/streamlit_rehydrate_runs/<run_id>/``.
    * **Function HTTP endpoint** — uploads both files to the configured
      blob containers, then POSTs ``{export_blob, template_blob,
      planner_mode}`` to the ``rehydrate`` HTTP-triggered function.
    """
    import requests  # local import to avoid hard dependency for non-HTTP users

    st.subheader("Rehydrate Submission (Reverse Pipeline)")
    st.caption(
        "Fill a customer's original bid template back in with priced "
        "lanes from a LaneExport file."
    )

    example_files = sorted(EXAMPLE_INPUTS_DIR.glob("*.xls*"))

    # ── File uploads ──────────────────────────────────────────────
    uploaded = st.file_uploader(
        "Upload an additional template or export (saved under examples/inputs)",
        type=["xlsx", "xls"],
        key="rehydrate_upload",
    )
    if uploaded is not None:
        dest = EXAMPLE_INPUTS_DIR / uploaded.name
        if not dest.exists() or st.checkbox(
            f"Overwrite existing {uploaded.name}?", value=False, key="rehydrate_overwrite"
        ):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(uploaded.getvalue())
            st.success(f"Saved to {dest}")
            example_files = sorted(EXAMPLE_INPUTS_DIR.glob("*.xls*"))

    if not example_files:
        st.error("No example files found under examples/inputs. Upload a template and an export to continue.")
        return None

    col1, col2 = st.columns(2)
    with col1:
        template_path = st.selectbox(
            "Customer template (.xlsx / .xls)",
            options=example_files,
            format_func=lambda p: p.name,
            key="rehydrate_template_select",
        )
    with col2:
        export_path = st.selectbox(
            "Priced export (LaneExport)",
            options=example_files,
            index=min(1, len(example_files) - 1),
            format_func=lambda p: p.name,
            key="rehydrate_export_select",
        )

    if template_path is not None and export_path is not None and template_path == export_path:
        st.warning("Template and export are the same file — select two different workbooks.")

    planner_mode = st.selectbox("Planner mode", options=["mock", "live"], key="rehydrate_planner_mode")
    confidence_threshold = st.slider(
        "Confidence threshold (live mode)",
        min_value=0.0,
        max_value=1.0,
        value=0.70,
        step=0.05,
        key="rehydrate_confidence",
    )
    run_target = st.selectbox(
        "Run target",
        options=["Direct (local artifacts)", "Function HTTP endpoint"],
        key="rehydrate_run_target",
    )

    # ── Per-target config + warnings ──────────────────────────────
    if planner_mode == "live":
        missing_live = _validate_live_mode_configuration()
        if missing_live:
            st.error(
                "Live planner mode is missing required configuration: "
                + ", ".join(missing_live)
                + ". Add these to local.settings.json (Values) or your environment."
            )

    if run_target.startswith("Direct"):
        st.info(
            f"Direct mode writes artifacts under `{STREAMLIT_REHYDRATE_OUTPUT_ROOT.relative_to(WORKSPACE_ROOT)}` "
            "and does NOT contact blob storage or the Function host."
        )
    else:
        function_url = st.text_input(
            "Function endpoint URL",
            value=os.getenv("REHYDRATE_FUNCTION_URL", "http://localhost:7071/api/rehydrate"),
            key="rehydrate_function_url",
            help=(
                "Local dev: http://localhost:7071/api/rehydrate · "
                "Azure: https://<funcapp>.azurewebsites.net/api/rehydrate?code=<key>"
            ),
        )
        export_container = os.getenv("EXPORT_CONTAINER", "export").strip() or "export"
        template_container = os.getenv("TEMPLATE_CONTAINER", "templates").strip() or "templates"
        outbox_container = os.getenv("OUTBOX_CONTAINER", "outbox").strip() or "outbox"
        st.caption(
            f"Blob containers — export: `{export_container}`, "
            f"templates: `{template_container}`, outbox: `{outbox_container}`"
        )
        st.warning(
            "HTTP mode uploads the selected files to blob storage and calls the "
            "function. If your storage account has public access disabled, ensure "
            "this Streamlit host is allowed via managed identity or a connection "
            "string in `AzureWebJobsStorage`."
        )
        missing_blob = _validate_rehydrate_blob_configuration()
        if missing_blob:
            st.error(
                "HTTP mode is missing required configuration: "
                + ", ".join(missing_blob)
                + "."
            )

    # ── Run button ────────────────────────────────────────────────
    if not st.button("Run Rehydrate", type="primary", key="rehydrate_run_button"):
        return None

    if template_path == export_path:
        st.error("Cannot run — template and export point to the same file.")
        return None

    if planner_mode == "live" and _validate_live_mode_configuration():
        st.stop()

    if run_target.startswith("Direct"):
        return _run_rehydrate_direct(
            template_path=Path(template_path),
            export_path=Path(export_path),
            planner_mode=planner_mode,
            confidence_threshold=confidence_threshold,
        )

    if _validate_rehydrate_blob_configuration():
        st.stop()

    return _run_rehydrate_http(
        template_path=Path(template_path),
        export_path=Path(export_path),
        planner_mode=planner_mode,
        function_url=function_url,
        requests_module=requests,
    )


def _run_rehydrate_direct(
    *,
    template_path: Path,
    export_path: Path,
    planner_mode: str,
    confidence_threshold: float,
) -> dict[str, Any] | None:
    STREAMLIT_REHYDRATE_OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    with st.spinner("Running rehydrate pipeline locally..."):
        try:
            result = run_rehydrate(
                template_path=str(template_path),
                export_path=str(export_path),
                output_root=str(STREAMLIT_REHYDRATE_OUTPUT_ROOT),
                planner_mode=planner_mode,
                confidence_threshold=confidence_threshold,
            )
        except Exception as exc:
            st.error(f"Rehydrate failed: {exc}")
            return None

    _render_rehydrate_result(result)
    return result


def _run_rehydrate_http(
    *,
    template_path: Path,
    export_path: Path,
    planner_mode: str,
    function_url: str,
    requests_module: Any,
) -> dict[str, Any] | None:
    export_container = os.getenv("EXPORT_CONTAINER", "export").strip() or "export"
    template_container = os.getenv("TEMPLATE_CONTAINER", "templates").strip() or "templates"

    connection_string = os.getenv("AzureWebJobsStorage", "")
    try:
        blob_service = _create_blob_service_client(connection_string)
    except Exception as exc:
        st.error(f"Could not create blob client: {exc}")
        return None

    # Ensure containers exist before uploading
    for container_name in (export_container, template_container):
        try:
            blob_service.get_container_client(container_name).create_container()
        except Exception:
            pass  # already exists or no permission to create — upload will surface a clearer error

    with st.spinner(f"Uploading {template_path.name} to '{template_container}'..."):
        try:
            blob_service.get_container_client(template_container).upload_blob(
                name=template_path.name,
                data=template_path.read_bytes(),
                overwrite=True,
            )
        except Exception as exc:
            st.error(f"Template upload failed: {exc}")
            return None

    with st.spinner(f"Uploading {export_path.name} to '{export_container}'..."):
        try:
            blob_service.get_container_client(export_container).upload_blob(
                name=export_path.name,
                data=export_path.read_bytes(),
                overwrite=True,
            )
        except Exception as exc:
            st.error(f"Export upload failed: {exc}")
            return None

    payload = {
        "export_blob": export_path.name,
        "template_blob": template_path.name,
        "planner_mode": planner_mode,
    }
    with st.spinner(f"POST {function_url}..."):
        try:
            response = requests_module.post(function_url, json=payload, timeout=300)
        except Exception as exc:
            st.error(f"HTTP request failed: {exc}")
            return None

    if response.status_code != 200:
        st.error(f"Function returned HTTP {response.status_code}: {response.text}")
        return None

    try:
        result = response.json()
    except ValueError:
        st.error(f"Function returned non-JSON body: {response.text}")
        return None

    st.success(f"Rehydrate complete (HTTP, run_id={result.get('run_id')!s})")
    _render_rehydrate_result(result)
    return result


def _render_rehydrate_result(result: dict[str, Any]) -> None:
    """Render the rehydrate result summary plus artifact download links."""
    if not result:
        st.warning("No result returned from rehydrate run.")
        return

    diff_passed = bool(result.get("diff_passed", False))
    if diff_passed and result.get("diff_violations", 0) == 0:
        st.success("Template diff validation PASSED — only writable cells changed.")
    else:
        st.error(
            f"Template diff validation FAILED: {result.get('diff_violations', 0)} "
            f"unexpected change(s), {result.get('diff_missing_writes', 0)} missing write(s)."
        )

    metric_cols = st.columns(4)
    metric_cols[0].metric("Cells written", result.get("cells_written", 0))
    metric_cols[1].metric("Cells skipped", result.get("cells_skipped", 0))
    metric_cols[2].metric("No-bid lanes", result.get("no_bid_lanes", 0))
    metric_cols[3].metric("Pending review", result.get("pending_review_count", 0))

    if result.get("no_bid_lane_names"):
        st.warning(
            "No-bid lanes (template routes missing from export):\n- "
            + "\n- ".join(result["no_bid_lane_names"])
        )

    if result.get("warnings"):
        with st.expander(f"Warnings ({len(result['warnings'])})"):
            st.json(result["warnings"])

    st.subheader("Run summary")
    summary_keys = {
        k: result[k]
        for k in (
            "run_id", "bid_sheets_profiled", "export_rows_loaded",
            "instructions_resolved", "cells_written", "cells_skipped",
            "no_bid_lanes", "validation_warnings", "pending_review_count",
            "llm_iterations", "diff_passed", "diff_violations", "diff_missing_writes",
        )
        if k in result
    }
    st.json(summary_keys)

    # Direct-mode result carries local file paths; HTTP result carries blob URIs.
    submission = result.get("submission")
    if submission and isinstance(submission, str) and Path(submission).exists():
        st.subheader("Download artifacts")
        run_dir = Path(submission).parent
        st.caption(f"Run directory: `{run_dir}`")
        cols = st.columns(3)
        for idx, fname in enumerate([
            "submission.xlsx", "template_diff.json", "write_report.json",
            "mapping_plan.json", "template_profile.json", "pending_review.json",
        ]):
            artifact = run_dir / fname
            if artifact.exists():
                with cols[idx % 3]:
                    st.download_button(
                        label=f"⬇ {fname}",
                        data=artifact.read_bytes(),
                        file_name=fname,
                        key=f"rehydrate_dl_{fname}",
                    )
    elif result.get("outbox_prefix"):
        st.subheader("Outbox blobs")
        st.code(
            "\n".join([
                f"submission:     {result.get('submission_blob', '')}",
                f"template_diff:  {result.get('template_diff_blob', '')}",
                f"pending_review: {result.get('pending_review_blob', '')}",
            ]),
        )


def main() -> None:
    _load_local_settings_env()

    foundry_endpoint = os.getenv("FOUNDRY_PROJECT_ENDPOINT", "")
    foundry_api_version = os.getenv("FOUNDRY_API_VERSION", "2025-05-15-preview")
    foundry_agent_name = os.getenv("FOUNDRY_AGENT_NAME", "")
    foundry_assistant_id = os.getenv("FOUNDRY_ASSISTANT_ID", "")
    foundry_agents_snapshot = _fetch_foundry_agents_snapshot(
        endpoint=foundry_endpoint,
        api_version=foundry_api_version,
        agent_name=foundry_agent_name,
        assistant_id=foundry_assistant_id,
    )

    st.set_page_config(page_title="RXO Pipeline Companion", layout="wide")
    st.title("RXO Normalizer - Companion Test & Visualization App")

    with st.sidebar:
        st.header("Controls")
        if "mode" not in st.session_state:
            st.session_state["mode"] = "Browse Prior Runs"
        mode = st.radio(
            "Mode",
            options=["Browse Prior Runs", "Create New Run", "Rehydrate Submission", "System Agents"],
            key="mode",
        )
        refresh = st.button("Refresh Run Index")

    if refresh:
        st.rerun()

    run_index = _build_run_index()

    if mode == "Create New Run":
        result = _create_new_run()
        if result is not None:
            run_dir = Path(result["run_dir"]).resolve()
            matching = [entry for entry in _build_run_index() if entry["run_dir"].resolve() == run_dir]
            if matching:
                st.divider()
                _render_run_details(matching[0])
            else:
                st.warning(f"Run created at {run_dir} but not found in discovery index yet.")
        return

    if mode == "Rehydrate Submission":
        _create_rehydrate_run()
        return

    if mode == "System Agents":
        _render_foundry_agents_page(foundry_agents_snapshot)
        return

    st.subheader("Prior Runs")
    if not run_index:
        st.info("No prior runs discovered yet.")
        return

    default_index = 0
    target_run_dir_text = st.session_state.get("target_run_dir")
    if isinstance(target_run_dir_text, str) and target_run_dir_text.strip():
        match_index = _find_run_index_by_dir(run_index, Path(target_run_dir_text))
        if match_index is not None:
            default_index = match_index
        st.session_state.pop("target_run_dir", None)

    selected_entry = st.selectbox("Select run", options=run_index, index=default_index, format_func=_run_label)
    _render_run_details(selected_entry)


if __name__ == "__main__":
    main()
