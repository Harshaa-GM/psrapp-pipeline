"""
unpacker.py — Load a .msapp file (local or from Azure Blob Storage)
and unzip it into an in-memory dict of filename → parsed content.

A .msapp is just a ZIP containing JSON + YAML files.
"""
import io
import os
import zipfile
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def unpack_from_file(path: str) -> dict[str, Any]:
    """Load a .msapp from a local file path."""
    with open(path, "rb") as f:
        return _unpack(f.read())


def unpack_from_blob(blob_path: str) -> dict[str, Any]:
    """
    Download a .msapp from Azure Blob Storage and unpack it.
    Needs env vars: AZURE_STORAGE_ACCOUNT_URL, AZURE_STORAGE_ACCESS_KEY
    """
    from azure.storage.blob import BlobServiceClient

    account_url = os.environ["AZURE_STORAGE_ACCOUNT_URL"]
    access_key  = os.environ.get("AZURE_STORAGE_ACCESS_KEY")
    container   = os.environ.get("BLOB_CONTAINER_NAME", "powerapps-artifacts")

    client      = BlobServiceClient(account_url=account_url, credential=access_key)
    blob_client = client.get_blob_client(container=container, blob=blob_path)
    data        = blob_client.download_blob().readall()

    logger.info("Downloaded %d bytes from blob: %s", len(data), blob_path)
    return _unpack(data)


def _unpack(msapp_bytes: bytes) -> dict[str, Any]:
    """Core unzip logic. Returns dict of normalised filename → content."""
    contents: dict[str, Any] = {}
    with zipfile.ZipFile(io.BytesIO(msapp_bytes)) as zf:
        for entry in zf.infolist():
            name = entry.filename.replace("\\", "/")  # fix Windows backslashes
            raw  = zf.read(entry.filename)

            if name.endswith(".json"):
                try:
                    contents[name] = json.loads(raw.decode("utf-8-sig"))
                except Exception:
                    contents[name] = raw.decode("utf-8-sig", errors="replace")
            elif name.endswith(".yaml"):
                contents[name] = raw.decode("utf-8-sig", errors="replace")
            else:
                contents[name] = raw  # images/fonts stay as bytes

    logger.info("Unpacked %d entries", len(contents))
    return contents


def unpack_from_solution_zip(zip_path: str) -> dict[str, Any]:
    """
    Handle a Dynamics 365 / Power Platform solution zip.
    Finds the .msapp inside CanvasApps/ and unpacks it.
    Works with both local files and bytes.
    """
    import zipfile as zf

    with zf.ZipFile(zip_path, "r") as outer:
        # Find the .msapp inside CanvasApps/
        msapp_entries = [
            f for f in outer.namelist()
            if "CanvasApps/" in f and f.endswith(".msapp")
        ]
        if not msapp_entries:
            raise FileNotFoundError("No .msapp found inside CanvasApps/ in this solution zip.")

        # Pick the first one (there's usually only one)
        msapp_entry = msapp_entries[0]
        logger.info("Found .msapp in solution zip: %s", msapp_entry)
        msapp_bytes = outer.read(msapp_entry)

    return _unpack(msapp_bytes)

