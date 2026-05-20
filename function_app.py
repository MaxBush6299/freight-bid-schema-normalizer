from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlparse

import azure.functions as func
from azure.storage.blob import BlobServiceClient

from src.function_app.services.pipeline_runner import run_pipeline
from src.function_app.services.xls_converter import ensure_xlsx

app = func.FunctionApp()
logger = logging.getLogger(__name__)


@app.function_name(name="ProcessWorkbookBlob")
@app.event_grid_trigger(arg_name="event")
def process_workbook_blob(event: func.EventGridEvent) -> None:
    input_container = os.getenv("INPUT_CONTAINER", "input").strip() or "input"
    output_container = os.getenv("OUTPUT_CONTAINER", "output").strip() or "output"

    event_payload = event.get_json() if event is not None else {}
    event_subject = (event.subject if event is not None else "") or event_payload.get("subject", "")
    event_data = event_payload.get("data", {}) if isinstance(event_payload, dict) else {}
    blob_url = event_data.get("url", "") if isinstance(event_data, dict) else ""

    blob_name = ""
    subject_marker = f"/containers/{input_container}/blobs/"
    if isinstance(event_subject, str) and subject_marker in event_subject:
        blob_name = unquote(event_subject.split(subject_marker, 1)[1])

    if not blob_name and isinstance(blob_url, str) and blob_url.strip():
        parsed_path = urlparse(blob_url).path.lstrip("/")
        prefix = f"{input_container}/"
        if parsed_path.startswith(prefix):
            blob_name = unquote(parsed_path[len(prefix) :])

    if not blob_name:
        logger.warning(
            "Skipping Event Grid message because blob name could not be resolved: %s",
            json.dumps({"subject": event_subject, "data": event_data}),
        )
        return

    blob_service = _create_blob_service_client()
    input_blob_client = blob_service.get_container_client(input_container).get_blob_client(blob_name)
    blob_stem = Path(blob_name).stem
    run_mode = os.getenv("RUN_MODE", "execute_with_validation").strip().lower()
    if run_mode not in {"draft", "execute_with_validation"}:
        run_mode = "execute_with_validation"
    planner_mode = os.getenv("PLANNER_MODE", "mock").strip().lower()
    if planner_mode not in {"mock", "live"}:
        planner_mode = "mock"
    persist_artifacts = os.getenv("FUNCTION_PERSIST_ARTIFACTS", "false").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    local_artifact_root = os.getenv("FUNCTION_LOCAL_ARTIFACT_ROOT", "").strip()

    with tempfile.TemporaryDirectory() as temp_dir:
        input_path = Path(temp_dir) / blob_name
        output_root = Path(temp_dir) / "run_artifacts"
        if persist_artifacts and local_artifact_root:
            output_root = Path(local_artifact_root) / blob_stem

        input_path.write_bytes(input_blob_client.download_blob().readall())
        pipeline_result = run_pipeline(
            str(input_path),
            str(output_root),
            run_mode=run_mode,
            planner_mode=planner_mode,
        )

        csv_bytes = Path(pipeline_result["csv"]).read_bytes()
        validation_text = Path(pipeline_result["validation_report"]).read_text(encoding="utf-8")
        planner_text = Path(pipeline_result["planner_response"]).read_text(encoding="utf-8")

        output_container_client = blob_service.get_container_client(output_container)
        output_container_client.upload_blob(f"{blob_name}.canonical.csv", csv_bytes, overwrite=True)
        output_container_client.upload_blob(
            f"{blob_name}.validation.json",
            validation_text.encode("utf-8"),
            overwrite=True,
        )
        output_container_client.upload_blob(
            f"{blob_name}.planner.json",
            planner_text.encode("utf-8"),
            overwrite=True,
        )

        logger.info(
            "Blob trigger run complete: %s",
            json.dumps(
                {
                    "name": blob_name,
                    "event_subject": event_subject,
                    "run_mode": run_mode,
                    "planner_mode": planner_mode,
                    "validation_status": pipeline_result["validation_status"],
                    "validation_errors": pipeline_result["validation_errors"],
                    "validation_warnings": pipeline_result["validation_warnings"],
                    "row_count": pipeline_result["row_count"],
                    "artifact_run_dir": pipeline_result["run_dir"],
                }
            ),
        )


def _create_blob_service_client() -> BlobServiceClient:
    connection_string = os.getenv("AzureWebJobsStorage", "").strip()
    if connection_string:
        return BlobServiceClient.from_connection_string(connection_string)

    account_name = os.getenv("AzureWebJobsStorage__accountName", "").strip()
    if not account_name:
        raise ValueError(
            "No storage account configured. Set AzureWebJobsStorage (connection string) "
            "or AzureWebJobsStorage__accountName for managed identity."
        )

    from azure.identity import DefaultAzureCredential

    account_url = f"https://{account_name}.blob.core.windows.net"
    return BlobServiceClient(account_url, credential=DefaultAzureCredential())
