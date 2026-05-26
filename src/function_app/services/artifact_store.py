from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from azure.storage.blob import BlobServiceClient

AZURITE_COMPAT_API_VERSION = "2021-12-02"


def _create_blob_service_client(connection_string: str) -> BlobServiceClient:
    is_local_emulator = "UseDevelopmentStorage=true" in connection_string or "127.0.0.1" in connection_string
    if is_local_emulator:
        api_version = os.getenv("AZURE_BLOB_API_VERSION", AZURITE_COMPAT_API_VERSION).strip()
        if api_version:
            return BlobServiceClient.from_connection_string(connection_string, api_version=api_version)
    return BlobServiceClient.from_connection_string(connection_string)


class LocalArtifactStore:
    def __init__(self, run_dir: str) -> None:
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)

    def write_text(self, relative_path: str, content: str) -> str:
        target = self.run_dir / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return str(target)

    def write_bytes(self, relative_path: str, content: bytes) -> str:
        target = self.run_dir / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return str(target)

    def write_json(self, relative_path: str, payload: dict[str, Any]) -> str:
        return self.write_text(relative_path, json.dumps(payload, indent=2))

    def list_artifacts(self) -> list[str]:
        return sorted(
            [
                str(path)
                for path in self.run_dir.rglob("*")
                if path.is_file()
            ]
        )


class BlobArtifactStore:
    def __init__(
        self,
        connection_string: str,
        container_name: str,
        run_prefix: str,
        blob_service_client: BlobServiceClient | None = None,
    ) -> None:
        self.connection_string = connection_string
        self.container_name = container_name
        self.run_prefix = run_prefix.strip("/")
        self.blob_service_client = blob_service_client or _create_blob_service_client(connection_string)
        self.container_client = self.blob_service_client.get_container_client(container_name)

        try:
            self.container_client.create_container()
        except Exception:
            pass

    def _blob_name(self, relative_path: str) -> str:
        normalized = relative_path.replace("\\", "/").lstrip("/")
        return f"{self.run_prefix}/{normalized}"

    def write_text(self, relative_path: str, content: str) -> str:
        blob_name = self._blob_name(relative_path)
        self.container_client.upload_blob(blob_name, content.encode("utf-8"), overwrite=True)
        return f"{self.container_name}/{blob_name}"

    def write_bytes(self, relative_path: str, content: bytes) -> str:
        blob_name = self._blob_name(relative_path)
        self.container_client.upload_blob(blob_name, content, overwrite=True)
        return f"{self.container_name}/{blob_name}"

    def write_json(self, relative_path: str, payload: dict[str, Any]) -> str:
        return self.write_text(relative_path, json.dumps(payload, indent=2))

    def upload_file(self, local_path: str, relative_path: str) -> str:
        data = Path(local_path).read_bytes()
        return self.write_bytes(relative_path, data)

    def list_artifacts(self) -> list[str]:
        return sorted(
            [
                f"{self.container_name}/{blob.name}"
                for blob in self.container_client.list_blobs(name_starts_with=f"{self.run_prefix}/")
            ]
        )


def create_blob_artifact_store_from_env(run_id: str) -> BlobArtifactStore | None:
    mode = os.getenv("ARTIFACT_STORAGE_MODE", "local").strip().lower()
    if mode != "blob":
        return None

    connection_string = os.getenv("AzureWebJobsStorage", "")
    container_name = os.getenv("ARTIFACT_BLOB_CONTAINER", "artifacts")
    if not connection_string:
        return None

    return BlobArtifactStore(
        connection_string=connection_string,
        container_name=container_name,
        run_prefix=f"runs/{run_id}",
    )


def mirror_local_artifacts_to_blob(
    local_store: LocalArtifactStore,
    blob_store: BlobArtifactStore,
) -> list[str]:
    mirrored: list[str] = []
    for file_path in local_store.list_artifacts():
        relative_path = str(Path(file_path).relative_to(local_store.run_dir)).replace("\\", "/")
        mirrored_uri = blob_store.upload_file(file_path, relative_path)
        mirrored.append(mirrored_uri)
    return sorted(mirrored)
