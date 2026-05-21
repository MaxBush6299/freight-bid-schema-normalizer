from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlparse

import azure.functions as func
from azure.storage.blob import BlobServiceClient

from src.function_app.local_rehydrate_runner import run_rehydrate
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


@app.function_name(name="RehydrateSubmissionBlob")
@app.event_grid_trigger(arg_name="event")
def rehydrate_submission_blob(event: func.EventGridEvent) -> None:
    """EventGrid trigger: fires when a priced export lands in the export container.

    Expects the blob to carry custom metadata:
        x-ms-meta-template-blob  — name of the customer template blob in the
                                   'templates' container (e.g. 'Original Customer File 1.xlsx')

    Writes the submission.xlsx and all artifacts to the 'outbox' container under
    <export_blob_name>/<run_id>/.
    """
    export_container = os.getenv("EXPORT_CONTAINER", "export").strip() or "export"
    template_container = os.getenv("TEMPLATE_CONTAINER", "templates").strip() or "templates"
    outbox_container = os.getenv("OUTBOX_CONTAINER", "outbox").strip() or "outbox"
    planner_mode = os.getenv("REHYDRATE_PLANNER_MODE", "live").strip().lower()
    if planner_mode not in {"mock", "live"}:
        planner_mode = "live"
    confidence_threshold = float(os.getenv("REHYDRATE_CONFIDENCE_THRESHOLD", "0.70"))

    event_payload = event.get_json() if event is not None else {}
    event_subject = (event.subject if event is not None else "") or event_payload.get("subject", "")
    event_data = event_payload.get("data", {}) if isinstance(event_payload, dict) else {}
    blob_url = event_data.get("url", "") if isinstance(event_data, dict) else ""

    blob_name = ""
    subject_marker = f"/containers/{export_container}/blobs/"
    if isinstance(event_subject, str) and subject_marker in event_subject:
        blob_name = unquote(event_subject.split(subject_marker, 1)[1])
    if not blob_name and isinstance(blob_url, str) and blob_url.strip():
        parsed_path = urlparse(blob_url).path.lstrip("/")
        prefix = f"{export_container}/"
        if parsed_path.startswith(prefix):
            blob_name = unquote(parsed_path[len(prefix):])

    if not blob_name:
        logger.warning("RehydrateSubmissionBlob: could not resolve blob name from event %s", event_subject)
        return

    blob_service = _create_blob_service_client()
    export_blob_client = blob_service.get_container_client(export_container).get_blob_client(blob_name)

    # Resolve the template blob name from custom metadata on the export blob
    blob_props = export_blob_client.get_blob_properties()
    template_blob_name = (blob_props.metadata or {}).get("template-blob", "")
    if not template_blob_name:
        logger.error(
            "RehydrateSubmissionBlob: export blob '%s' has no 'template-blob' metadata — cannot rehydrate.",
            blob_name,
        )
        return

    template_blob_client = blob_service.get_container_client(template_container).get_blob_client(template_blob_name)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        export_local = tmp_path / Path(blob_name).name
        template_local = tmp_path / Path(template_blob_name).name
        export_local.write_bytes(export_blob_client.download_blob().readall())
        template_local.write_bytes(template_blob_client.download_blob().readall())

        output_root = tmp_path / "rehydrate_out"
        result = run_rehydrate(
            template_path=str(template_local),
            export_path=str(export_local),
            output_root=str(output_root),
            planner_mode=planner_mode,
            confidence_threshold=confidence_threshold,
        )

        # Upload all artifacts to outbox/<blob_stem>/<run_id>/
        blob_stem = Path(blob_name).stem
        outbox_prefix = f"{blob_stem}/{result['run_id']}"
        outbox_client = blob_service.get_container_client(outbox_container)
        run_dir = Path(result["run_dir"])
        for artifact in run_dir.iterdir():
            outbox_client.upload_blob(
                f"{outbox_prefix}/{artifact.name}",
                artifact.read_bytes(),
                overwrite=True,
            )

    logger.info(
        "RehydrateSubmissionBlob complete: %s",
        json.dumps({
            "export_blob": blob_name,
            "template_blob": template_blob_name,
            "run_id": result["run_id"],
            "cells_written": result["cells_written"],
            "pending_review_count": result["pending_review_count"],
            "outbox_prefix": outbox_prefix,
        }),
    )


@app.function_name(name="RehydrateSubmissionHttp")
@app.route(route="rehydrate", methods=["POST"])
def rehydrate_submission_http(req: func.HttpRequest) -> func.HttpResponse:
    """HTTP POST trigger for on-demand rehydrate from Streamlit or API.

    Expected JSON body:
        {
            "export_blob":   "<blob name in export container>",
            "template_blob": "<blob name in templates container>",
            "planner_mode":  "live" | "mock"  (optional, default live)
        }

    Returns JSON with the run result summary.
    """
    export_container = os.getenv("EXPORT_CONTAINER", "export").strip() or "export"
    template_container = os.getenv("TEMPLATE_CONTAINER", "templates").strip() or "templates"
    outbox_container = os.getenv("OUTBOX_CONTAINER", "outbox").strip() or "outbox"
    default_planner_mode = os.getenv("REHYDRATE_PLANNER_MODE", "live").strip().lower()
    confidence_threshold = float(os.getenv("REHYDRATE_CONFIDENCE_THRESHOLD", "0.70"))

    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse("Request body must be valid JSON.", status_code=400)

    export_blob_name = (body.get("export_blob") or "").strip()
    template_blob_name = (body.get("template_blob") or "").strip()
    planner_mode = (body.get("planner_mode") or default_planner_mode).strip().lower()
    if planner_mode not in {"mock", "live"}:
        planner_mode = "live"

    if not export_blob_name or not template_blob_name:
        return func.HttpResponse(
            json.dumps({"error": "Both 'export_blob' and 'template_blob' are required."}),
            mimetype="application/json",
            status_code=400,
        )

    blob_service = _create_blob_service_client()

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        export_local = tmp_path / Path(export_blob_name).name
        template_local = tmp_path / Path(template_blob_name).name

        try:
            export_local.write_bytes(
                blob_service.get_container_client(export_container)
                .get_blob_client(export_blob_name)
                .download_blob()
                .readall()
            )
            template_local.write_bytes(
                blob_service.get_container_client(template_container)
                .get_blob_client(template_blob_name)
                .download_blob()
                .readall()
            )
        except Exception as e:
            logger.exception("RehydrateSubmissionHttp: failed to download blobs")
            return func.HttpResponse(
                json.dumps({"error": str(e)}),
                mimetype="application/json",
                status_code=404,
            )

        output_root = tmp_path / "rehydrate_out"
        try:
            result = run_rehydrate(
                template_path=str(template_local),
                export_path=str(export_local),
                output_root=str(output_root),
                planner_mode=planner_mode,
                confidence_threshold=confidence_threshold,
            )
        except Exception as e:
            logger.exception("RehydrateSubmissionHttp: pipeline error")
            return func.HttpResponse(
                json.dumps({"error": str(e)}),
                mimetype="application/json",
                status_code=500,
            )

        # Upload artifacts to outbox
        blob_stem = Path(export_blob_name).stem
        outbox_prefix = f"{blob_stem}/{result['run_id']}"
        outbox_client = blob_service.get_container_client(outbox_container)
        run_dir = Path(result["run_dir"])
        for artifact in run_dir.iterdir():
            outbox_client.upload_blob(
                f"{outbox_prefix}/{artifact.name}",
                artifact.read_bytes(),
                overwrite=True,
            )

    summary = {k: v for k, v in result.items() if k not in ("submission", "write_report", "template_profile", "mapping_plan", "pending_review")}
    summary["outbox_prefix"] = outbox_prefix
    summary["submission_blob"] = f"{outbox_prefix}/submission.xlsx"
    summary["pending_review_blob"] = f"{outbox_prefix}/pending_review.json"

    return func.HttpResponse(
        json.dumps(summary),
        mimetype="application/json",
        status_code=200,
    )


@app.function_name(name="GetSubmissionStatus")
@app.route(route="rehydrate/{run_id}", methods=["GET"])
def get_submission_status(req: func.HttpRequest) -> func.HttpResponse:
    """HTTP GET trigger to fetch artifacts for a completed rehydrate run.

    Route params:
        run_id  — the run ID returned by RehydrateSubmissionHttp

    Query params:
        export_blob  — the export blob name used (needed to compute outbox prefix)
        artifact     — specific artifact to download: submission | mapping_plan |
                        write_report | template_profile | pending_review
                        (omit to get a JSON index of all artifacts)
    """
    outbox_container = os.getenv("OUTBOX_CONTAINER", "outbox").strip() or "outbox"

    run_id = req.route_params.get("run_id", "").strip()
    export_blob = req.params.get("export_blob", "").strip()
    artifact_name = req.params.get("artifact", "").strip()

    if not run_id or not export_blob:
        return func.HttpResponse(
            json.dumps({"error": "'run_id' route param and 'export_blob' query param are required."}),
            mimetype="application/json",
            status_code=400,
        )

    blob_stem = Path(export_blob).stem
    outbox_prefix = f"{blob_stem}/{run_id}"
    blob_service = _create_blob_service_client()
    outbox_client = blob_service.get_container_client(outbox_container)

    _ARTIFACT_MAP = {
        "submission": "submission.xlsx",
        "mapping_plan": "mapping_plan.json",
        "write_report": "write_report.json",
        "template_profile": "template_profile.json",
        "pending_review": "pending_review.json",
    }

    if artifact_name:
        filename = _ARTIFACT_MAP.get(artifact_name, artifact_name)
        blob_path = f"{outbox_prefix}/{filename}"
        try:
            data = outbox_client.get_blob_client(blob_path).download_blob().readall()
        except Exception:
            return func.HttpResponse(
                json.dumps({"error": f"Artifact '{blob_path}' not found."}),
                mimetype="application/json",
                status_code=404,
            )
        content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" if filename.endswith(".xlsx") else "application/json"
        return func.HttpResponse(data, mimetype=content_type, status_code=200)

    # Return index of all available artifacts
    try:
        blobs = [b.name for b in outbox_client.list_blobs(name_starts_with=f"{outbox_prefix}/")]
    except Exception as e:
        return func.HttpResponse(json.dumps({"error": str(e)}), mimetype="application/json", status_code=500)

    index = {
        "run_id": run_id,
        "outbox_prefix": outbox_prefix,
        "artifacts": [Path(b).name for b in blobs],
        "download_urls": {
            key: f"/api/rehydrate/{run_id}?export_blob={export_blob}&artifact={key}"
            for key in _ARTIFACT_MAP
        },
    }
    return func.HttpResponse(json.dumps(index), mimetype="application/json", status_code=200)


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
