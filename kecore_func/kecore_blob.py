"""Blob storage for the kecore pipeline: the Function's managed identity, no key, no connection string."""

from __future__ import annotations

import os

from azure.core.exceptions import ResourceNotFoundError
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient


class BlobStorage:
    """``kecore_pipeline.Storage`` over one storage account (KECORE_BLOB_ENDPOINT)."""

    def __init__(self, account_url: str | None = None, credential=None):
        self._service = BlobServiceClient(
            account_url=account_url or os.environ["KECORE_BLOB_ENDPOINT"],
            credential=credential or DefaultAzureCredential(),
        )

    def list(self, container: str, prefix: str) -> list[str]:
        client = self._service.get_container_client(container)
        return sorted(blob.name for blob in client.list_blobs(name_starts_with=prefix or None))

    def read(self, container: str, name: str) -> bytes | None:
        try:
            return self._service.get_blob_client(container, name).download_blob().readall()
        except ResourceNotFoundError:
            return None

    def write(self, container: str, name: str, data: bytes) -> None:
        self._service.get_blob_client(container, name).upload_blob(data, overwrite=True)
