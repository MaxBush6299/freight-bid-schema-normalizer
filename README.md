# RXO Document Normalizer

This repository implements an AI-driven Excel schema standardization platform for RXO, as specified in ./docs/software_spec.md and ./docs/implementation_plan.md.

## Public Release Notes

- Copy `local.settings.template.json` to `local.settings.json` for local development.
- Never commit `local.settings.json`, `.env*`, or generated runtime artifacts.
- Report vulnerabilities via [SECURITY.md](SECURITY.md).

## Project Structure

- docs/: Specifications and implementation plans
- examples/: Sample input/output workbooks
- schema/: Output templates and schema definitions
- src/function_app/: Main application code
  - models/: Data contracts and enums
  - services/: Core logic modules
  - templates/: Canonical schema and output templates
  - prompts/: LLM prompt assets

## Tooling Baseline

- Python packaging and tool config: [pyproject.toml](pyproject.toml)
- Repository ignore rules: [.gitignore](.gitignore)

## Artifact Storage Modes

- Default: local artifact storage under per-run output folders
- Optional blob mirror mode:
  - `ARTIFACT_STORAGE_MODE=blob`
  - `ARTIFACT_BLOB_CONTAINER=<container-name>` (default `artifacts`)
  - Local development can use `AzureWebJobsStorage`
  - Cloud deployments use managed identity with `AzureWebJobsStorage__accountName`
  - Writes local artifacts and mirrors them to blob with a manifest

## Getting Started

1. Review ./docs/software_spec.md and ./docs/implementation_plan.md
2. Set up a Python virtual environment
3. Install dependencies: `pip install -r requirements.txt`
4. Run tests: `python -m pytest tests/`

## Local Service Restart

- One-command restart (opens separate terminals for Azurite, Functions, and Streamlit):
  - `./scripts/restart-local.ps1`
- Detailed startup and troubleshooting guide:
  - [.copilot/startup_procedure.md](.copilot/startup_procedure.md)

## Schema Lookup Backfill (Historical Runs)

- Command: `python scripts/backfill-schema-lookup.py`
- Purpose: backfills `schema_cache_lookup.json` for discovered historical `pipeline_*` run folders.
- Behavior:
  - Uses existing `schema_fingerprint.json` when present.
  - Rebuilds fingerprints from `workbook_profile.json` when possible.
  - Writes fallback `not_found` lookup records for legacy runs missing both artifacts.
- Optional flag: `--skip-fallback` to avoid writing fallback lookup files for runs with insufficient inputs.

## Approve Known Schema Signature

- Command by fingerprint: `python scripts/approve-schema-cache-entry.py --fingerprint <sha256>`
- Command by run directory: `python scripts/approve-schema-cache-entry.py --run-dir "artifacts/local_pipeline/pipeline_<runid>"`
- Result: sets schema cache entry `approval_status=approved` so exact matches can reuse cached planner output.

## Preliminary Localhost Smoke Run

- Command: `python -m src.function_app.local_smoke_runner --input "examples/inputs/Input 8.xlsx" --output-root "artifacts/local_smoke"`
- Artifacts produced per run:
  - workbook profile JSON
  - canonical CSV
  - canonical XLSX
  - deterministic validation report JSON

## Local Real Core Pipeline Run

- Command: `python -m src.function_app.local_pipeline_runner --input "examples/inputs/Input 8.xlsx" --output-root "artifacts/local_pipeline" --run-mode execute_with_validation --planner-mode mock`
- This executes the full core flow:
  - planner response generation
  - script policy enforcement
  - sandbox transform execution
  - canonical CSV/XLSX writing from sandbox-produced records
  - validation report + execution artifacts
- Planner mode options:
  - `mock` (default): built-in planner script for local deterministic execution
  - `live`: calls Foundry endpoint using `FOUNDRY_PROJECT_ENDPOINT`, `FOUNDRY_AGENT_NAME`, `FOUNDRY_API_KEY`

## Streamlit Companion App

- UI/UX plan: [docs/streamlit_uiux_plan.md](docs/streamlit_uiux_plan.md)
- Local launch command: `python -m streamlit run streamlit_app.py --server.headless true`
- Cloud hosting: Azure Container Apps backed by ACR image builds from [Dockerfile.streamlit](Dockerfile.streamlit)
- Capabilities:
  - Browse prior runs discovered from local artifact roots
  - Launch new runs from `examples/inputs/*.xlsx`
  - Submit blob-trigger runs against Azure Blob Storage containers
  - Run the **Rehydrate Submission** reverse pipeline (Direct or HTTP target)
  - Visualize canonical output table, planner payload/script, validation issues, and sandbox execution logs

## Reverse Pipeline (Rehydrate)

Fills the customer's original bid template back in with priced lanes
from an `RXO LaneExport` file.  Outputs a `submission.xlsx` plus a
template-diff report proving only writable cells changed.

- Local runner command:
  - `python -m src.function_app.local_rehydrate_runner --template "examples/inputs/Original Customer File 1.xlsx" --export "examples/inputs/FTL LaneExport.xlsx" --output-root "artifacts/local_rehydrate"`
- Planner mode flag: `--planner-mode mock|live` (live calls Foundry)
- Human-in-the-loop for low-confidence mappings: `--interactive --confidence-threshold 0.70`
- Auto-write confidence gate: `--auto-write-threshold 0.85` (default; only mappings at/above this confidence are written automatically — others are deferred to `pending_review.json` even if `--interactive` is off)
- Customer-data preservation (always on): the resolver and the writer both refuse to overwrite any cell that already has a value. Pre-filled bid inputs, descriptors, notes, etc. are reported in `write_report.json` as `skipped_preserved` instead of being clobbered.
- Per-row descriptor validation (always on when the template has Origin/Destination City/State/ZIP filled in): a row is skipped entirely if its template descriptors disagree with the matched export row.
- Ambiguous-route detection: duplicate `Origin Note` values in the export are listed in `write_report.duplicate_export_routes` and never written to.
- Artifacts produced per run (under `<output-root>/<run_id>/`):
  - `submission.xlsx` — original template with pricing filled in
  - `template_profile.json` — bid-sheet slot detection + writable column map
  - `mapping_plan.json` — planner-produced field→cell mapping plan
  - `write_report.json` — per-cell write log + skip log + counters (`cells_skipped_preserved`, `cells_skipped_low_confidence`, `rows_skipped_descriptor_mismatch`, `duplicate_export_routes`)
  - `pending_review.json` — low-confidence mappings awaiting sign-off
  - `template_diff.json` — pass/fail diff proving only writable cells changed
- Streamlit access: launch the app and choose the **Rehydrate Submission** sidebar mode
- Function App endpoints exposed for cloud / Function-host integration:
  - `POST /api/rehydrate` (HTTP) — body `{export_blob, template_blob, planner_mode}`
  - Event-grid blob trigger `RehydrateSubmissionBlob` on the `%EXPORT_CONTAINER%` container
  - `GET /api/rehydrate/{run_id}?file=<artifact>` — fetch a run artifact from outbox
- Required environment variables (in `local.settings.json` Values or your environment):
  - `EXPORT_CONTAINER` (default `export`) — priced export uploads land here
  - `TEMPLATE_CONTAINER` (default `templates`) — customer templates land here
  - `OUTBOX_CONTAINER` (default `outbox`) — artifacts are published here
  - `REHYDRATE_PLANNER_MODE` (`mock`|`live`, default `live`)
  - `REHYDRATE_CONFIDENCE_THRESHOLD` (default `0.70`) — review threshold; mappings below this are flagged in `pending_review.json`
  - `REHYDRATE_MIN_WRITE_CONFIDENCE` (default `0.85`) — auto-write threshold; mappings below this are NOT written automatically (CLI override: `--auto-write-threshold`)
  - `REHYDRATE_FUNCTION_URL` (Streamlit HTTP target; default `http://localhost:7071/api/rehydrate`)

## Blob Trigger Function App Entry

- Main Function App entrypoint: [function_app.py](function_app.py)
- Function name: `ProcessWorkbookBlob`
- Trigger: `eventGridTrigger` on Storage BlobCreated events (for `%INPUT_CONTAINER%`)
- Runtime behavior: reads uploaded blob from `%INPUT_CONTAINER%` and writes `%OUTPUT_CONTAINER%/{name}.canonical.csv|validation.json|planner.json`
- Flex Consumption note: Event Grid subscription must exist from storage account to `ProcessWorkbookBlob`
- Run mode via env var `RUN_MODE`:
  - `draft`
  - `execute_with_validation` (default)
- Planner mode via env var `PLANNER_MODE`:
  - `mock` (default)
  - `live`
- Outputs:
  - `%OUTPUT_CONTAINER%/{name}.canonical.csv`
  - `%OUTPUT_CONTAINER%/{name}.validation.json`
  - `%OUTPUT_CONTAINER%/{name}.planner.json`
- Local settings template: [local.settings.template.json](local.settings.template.json)
- Local artifact persistence (for blob-trigger runs):
  - `FUNCTION_PERSIST_ARTIFACTS=true`
  - `FUNCTION_LOCAL_ARTIFACT_ROOT=artifacts/function_runs`
  - Per input blob, artifacts are cached under `artifacts/function_runs/<blob-stem>/pipeline_<runid>/`
  - Includes: `planner_response.json` (with `python_script`), `sandbox_execution_report.json`, `validation_report.json`, canonical outputs, and execution summary

To run locally with Azure Functions Core Tools:
- Copy `local.settings.template.json` to `local.settings.json`
- Ensure Azurite or an Azure Storage account is configured in `AzureWebJobsStorage`
- Start with: `func start`

## Infrastructure (Bicep)

- Azure Functions hosting: Flex Consumption (`FC1`)
- Streamlit hosting: Azure Container Apps
- Container registry: Azure Container Registry (`rxodocnormacr.azurecr.io`)
- Production storage auth: managed identity (no storage connection string required)

- Main template: `infra/main.bicep`
- Environment parameter files:
  - `infra/main.dev.bicepparam`
  - `infra/main.test.bicepparam`
  - `infra/main.prod.bicepparam`
- Modules included:
  - storage
  - monitoring
  - key vault
  - function plan
  - function app
  - streamlit container app
  - RBAC roles
  - optional container apps execution worker

Deploy examples:
- Dev: `az deployment group create --resource-group <rg-name> --parameters infra/main.dev.bicepparam --parameters acrLoginServer='rxodocnormacr.azurecr.io' acrUsername='<acr-admin-user>' acrPassword='<acr-admin-password>'`
- Test: `az deployment group create --resource-group <rg-name> --parameters infra/main.test.bicepparam --parameters acrLoginServer='rxodocnormacr.azurecr.io' acrUsername='<acr-admin-user>' acrPassword='<acr-admin-password>'`
- Prod: `az deployment group create --resource-group <rg-name> --parameters infra/main.prod.bicepparam --parameters acrLoginServer='rxodocnormacr.azurecr.io' acrUsername='<acr-admin-user>' acrPassword='<acr-admin-password>'`

### Minimal infra runbook (dev)

1) Build and push the Streamlit image to ACR
- `az acr build --registry rxodocnormacr --image streamlit-ui:<tag> --file Dockerfile.streamlit . --no-logs`

2) Deploy baseline dev infrastructure (Function App + Container App + core resources)
- `az deployment group create --resource-group <rg-name> --parameters infra/main.dev.bicepparam --parameters acrLoginServer='rxodocnormacr.azurecr.io' acrUsername='<acr-admin-user>' acrPassword='<acr-admin-password>' streamlitContainerImage='streamlit-ui:<tag>'`

3) Publish Function App code
- `func azure functionapp publish func-rxodocnorm-dev --python`

4) Roll forward the Streamlit Container App image on later UI changes
- `az containerapp update --name rxodocnorm-streamlit-dev-app --resource-group <rg-name> --image rxodocnormacr.azurecr.io/streamlit-ui:<tag>`

5) Optional: enable container worker (Container Apps environment + job)
- `az deployment group create --resource-group <rg-name> --parameters infra/main.dev.bicepparam --parameters acrLoginServer='rxodocnormacr.azurecr.io' acrUsername='<acr-admin-user>' acrPassword='<acr-admin-password>' enableContainerWorker=true streamlitContainerImage='streamlit-ui:<tag>'`

6) Optional: grant worker identity data-plane access (Storage + Key Vault)
- `az deployment group create --resource-group <rg-name> --parameters infra/main.dev.bicepparam --parameters acrLoginServer='rxodocnormacr.azurecr.io' acrUsername='<acr-admin-user>' acrPassword='<acr-admin-password>' enableContainerWorker=true assignContainerWorkerRoles=true streamlitContainerImage='streamlit-ui:<tag>'`

7) Optional: also grant worker Foundry access
- `az deployment group create --resource-group <rg-name> --parameters infra/main.dev.bicepparam --parameters acrLoginServer='rxodocnormacr.azurecr.io' acrUsername='<acr-admin-user>' acrPassword='<acr-admin-password>' enableContainerWorker=true assignContainerWorkerRoles=true assignContainerWorkerFoundryRoles=true assignFoundryRoles=true foundryAccountName=<foundry-account-name> foundryProjectName=<foundry-project-name> streamlitContainerImage='streamlit-ui:<tag>'`

8) Inspect useful deployment outputs
- `functionAppName`
- `streamlitAppName`
- `streamlitFqdn`
- `containerWorkerJobResourceId`
- `containerWorkerManagedEnvironmentId`
- `containerWorkerPrincipalId`

For the detailed cloud deployment workflow, see [infra/ghcp_deploy_kickoff.md](infra/ghcp_deploy_kickoff.md) and [infra/rbac_assignments.md](infra/rbac_assignments.md).

New Foundry runtime configuration is parameterized in Bicep via:
- `foundryProjectEndpoint`
- `foundryAgentName`
- `foundryAgentVersion`
- `foundryAssistantId` (kept empty for strict `agent_reference` mode)

Foundry agent lifecycle boundary:
- Bicep does **not** create or version Foundry agents; it only injects runtime config (`FOUNDRY_*` env vars).
- Agent creation/versioning is handled by scripts:
  - `scripts/deploy-foundry-agent.py`
  - `scripts/deploy-postprocess-agent.py`

System prompt text is versioned in-repo:
- `src/function_app/prompts/transform_planner_system.txt`
- `src/function_app/prompts/notes_postprocess_system.txt`

## Implemented Slices

- Phase 1 foundational contracts (`src/function_app/models/contracts.py`, `src/function_app/models/enums.py`)
- Canonical schema contract and deterministic loader (`src/function_app/templates/canonical_schema.freight_bid_v1.json`, `src/function_app/services/template_loader.py`)
- Phase 2 deterministic workbook profiling and sheet classification (`src/function_app/services/workbook_profiler.py`, `src/function_app/services/sheet_classifier.py`)
- Phase 3 deterministic canonical output writing for CSV/XLSX (`src/function_app/services/output_writer.py`)
- Phase 4 deterministic validation service for schema shape, required fields, type checks, enum enforcement, null-rate threshold warnings, and cross-field consistency checks (`src/function_app/services/validation_rules.py`, `src/function_app/services/validation_service.py`)
- Phase 5 deterministic normalization helpers and lineage-aware validation output (`src/function_app/services/normalization_service.py`, `src/function_app/services/validation_service.py`)
- Preliminary deterministic localhost smoke runner (`src/function_app/local_smoke_runner.py`)
- Blob-trigger Function App entrypoint (`function_app.py`)
- Phase 6 Foundry planning scaffolding (prompt assets + client seam + planning service) (`src/function_app/prompts/`, `src/function_app/services/foundry_agent_client.py`, `src/function_app/services/planning_service.py`)
- Phase 7 mode-aware pipeline orchestration with planner artifact persistence (`src/function_app/services/pipeline_runner.py`, `function_app.py`)
- Phase 8 script policy guardrails for planner-generated code (`src/function_app/services/script_policy.py`)
- Phase 9 sandbox execution scaffolding with structured execution reports (`src/function_app/services/sandbox_executor.py`)
- Phase 10 artifact store seam + execution result artifact alignment (`src/function_app/services/artifact_store.py`, `execution_result.json` in pipeline outputs)
- Phase 11 packaging/tooling baseline (`pyproject.toml`, `.gitignore`)
- Phase 12 blob-backed artifact store seam and local-to-blob mirroring (`src/function_app/services/artifact_store.py`)
- Schema cache fingerprinting + local cache repository (`src/function_app/services/schema_fingerprint.py`, `src/function_app/services/schema_cache.py`)
- Notes post-processing service + agent prompt (`src/function_app/services/notes_postprocessor.py`, `src/function_app/prompts/notes_postprocess_system.txt`)
- Streamlit Container App deployment module + ACR-based image delivery (`infra/modules/streamlitApp.bicep`, `Dockerfile.streamlit`)
- Foundry agent deployment and backfill utility scripts (`scripts/deploy-foundry-agent.py`, `scripts/deploy-postprocess-agent.py`, `scripts/backfill-*.py`)
- Early unit tests covering classifier behavior and workbook profiling on sample workbook (`tests/`)

## Implementation Status

See ./.copilot/TODO.md for current progress and next steps.

## Operating Principles

- All architecture and implementation must align with the software spec and implementation plan.
- All infrastructure is provisioned via Bicep.
- Deterministic, testable, and auditable transformation is required.
- See ./.copilot/copilot_instructions.md for agent operating rules.
