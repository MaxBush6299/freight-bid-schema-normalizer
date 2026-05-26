import tempfile
import unittest

from src.function_app.services.artifact_store import (
    BlobArtifactStore,
    LocalArtifactStore,
    mirror_local_artifacts_to_blob,
)


class FakeBlob:
    def __init__(self, name: str) -> None:
        self.name = name


class FakeContainerClient:
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    def create_container(self) -> None:
        return None

    def upload_blob(self, name: str, payload, overwrite: bool = False) -> None:
        if isinstance(payload, bytes):
            content = payload
        else:
            content = bytes(payload)
        if not overwrite and name in self.data:
            raise ValueError("Blob already exists")
        self.data[name] = content

    def list_blobs(self, name_starts_with: str = ""):
        for key in sorted(self.data.keys()):
            if key.startswith(name_starts_with):
                yield FakeBlob(key)


class FakeBlobServiceClient:
    def __init__(self, container_client: FakeContainerClient) -> None:
        self.container_client = container_client

    def get_container_client(self, container_name: str) -> FakeContainerClient:
        return self.container_client


class TestBlobArtifactStore(unittest.TestCase):
    def test_write_and_list_blob_artifacts(self) -> None:
        container_client = FakeContainerClient()
        blob_service_client = FakeBlobServiceClient(container_client)
        store = BlobArtifactStore(
            connection_string="UseDevelopmentStorage=true",
            container_name="artifacts",
            run_prefix="runs/test-1",
            blob_service_client=blob_service_client,  # type: ignore[arg-type]
        )

        uri = store.write_text("a/file.txt", "hello")
        self.assertTrue(uri.endswith("runs/test-1/a/file.txt"))

        listed = store.list_artifacts()
        self.assertEqual(len(listed), 1)
        self.assertTrue(listed[0].endswith("runs/test-1/a/file.txt"))

    def test_mirror_local_artifacts_to_blob(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            local = LocalArtifactStore(temp_dir)
            local.write_text("one.txt", "1")
            local.write_text("nested/two.txt", "2")

            container_client = FakeContainerClient()
            blob_service_client = FakeBlobServiceClient(container_client)
            blob = BlobArtifactStore(
                connection_string="UseDevelopmentStorage=true",
                container_name="artifacts",
                run_prefix="runs/test-2",
                blob_service_client=blob_service_client,  # type: ignore[arg-type]
            )

            mirrored = mirror_local_artifacts_to_blob(local, blob)
            self.assertEqual(len(mirrored), 2)
            self.assertTrue(any(item.endswith("runs/test-2/one.txt") for item in mirrored))
            self.assertTrue(any(item.endswith("runs/test-2/nested/two.txt") for item in mirrored))


if __name__ == "__main__":
    unittest.main()
