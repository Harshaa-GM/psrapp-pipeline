"""
blob_client.py — Thin wrapper around Azure Blob Storage SDK.

Provides helpers for:
  • Uploading bytes/streams to a container
  • Checking blob existence
  • Listing blobs with a prefix
  • Downloading blobs

All operations authenticate via the shared `auth.get_credential()` which
uses the Azure Service Principal (or Managed Identity fallback).
"""

from __future__ import annotations

import logging
import os
from io import BytesIO
from typing import Iterator

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.storage.blob import (
    BlobClient,
    BlobServiceClient,
    ContentSettings,
    StorageStreamDownloader,
)

from shared.auth import get_credential

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_blob_service_client() -> BlobServiceClient:
    """
    Build a BlobServiceClient authenticated via Access Key or Service Principal.

    The storage account URL is read from AZURE_STORAGE_ACCOUNT_URL, e.g.:
        https://<account>.blob.core.windows.net
    """
    account_url = os.environ["AZURE_STORAGE_ACCOUNT_URL"]
    access_key = os.getenv("AZURE_STORAGE_ACCESS_KEY")
    if access_key:
        return BlobServiceClient(account_url=account_url, credential=access_key)

    credential = get_credential()
    return BlobServiceClient(account_url=account_url, credential=credential)


def _get_container_name() -> str:
    return os.environ.get("BLOB_CONTAINER_NAME", "powerapps-artifacts")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def upload_blob(
    blob_path: str,
    data: bytes | BytesIO,
    content_type: str = "application/octet-stream",
    overwrite: bool = True,
    metadata: dict[str, str] | None = None,
) -> str:
    """
    Upload `data` to `blob_path` inside the configured container.

    Returns the fully-qualified blob URL.
    """
    client = _get_blob_service_client()
    container = _get_container_name()
    blob_client: BlobClient = client.get_blob_client(
        container=container, blob=blob_path
    )

    content_settings = ContentSettings(content_type=content_type)

    blob_client.upload_blob(
        data,
        overwrite=overwrite,
        content_settings=content_settings,
        metadata=metadata or {},
    )

    logger.info("Uploaded blob: %s/%s", container, blob_path)
    return blob_client.url


def download_blob(blob_path: str) -> bytes:
    """Download and return the raw bytes of a blob."""
    client = _get_blob_service_client()
    container = _get_container_name()
    blob_client: BlobClient = client.get_blob_client(
        container=container, blob=blob_path
    )
    stream: StorageStreamDownloader = blob_client.download_blob()
    return stream.readall()


def blob_exists(blob_path: str) -> bool:
    """Return True if the blob exists in the container."""
    client = _get_blob_service_client()
    container = _get_container_name()
    blob_client: BlobClient = client.get_blob_client(
        container=container, blob=blob_path
    )
    return blob_client.exists()


def list_blobs(prefix: str = "") -> Iterator[str]:
    """Yield blob names under `prefix`."""
    client = _get_blob_service_client()
    container = _get_container_name()
    container_client = client.get_container_client(container)
    for blob in container_client.list_blobs(name_starts_with=prefix):
        yield blob.name


def ensure_container_exists() -> None:
    """Create the blob container if it does not already exist."""
    client = _get_blob_service_client()
    container = _get_container_name()
    try:
        client.create_container(container)
        logger.info("Created container: %s", container)
    except ResourceExistsError:
        logger.debug("Container already exists: %s", container)
