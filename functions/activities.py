"""
functions/activities.py — Azure Durable Activity Functions
===========================================================
Contains all ActivityTrigger functions used by the orchestrator:

  • fetch_pr_metadata_activity   — fetches base/head SHA from GitHub PR API
  • discover_msapp_activity      — lists .msapp files in repo tree at a ref
  • fetch_artifact_activity      — downloads .msapp and uploads to Blob Storage

Each function is stateless and idempotent. All external I/O (GitHub API,
Blob Storage, Key Vault) uses Service Principal credentials via shared modules.

Blob path convention:
  powerapps-artifacts/
    {owner}/{repo}/pr-{pr_number}/{branch_type}/{sha}/{filename}.msapp

This layout lets the downstream ReviewOrchestrator (Phase 1 review pipeline)
locate both the base and head versions of every .msapp by PR number.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

import azure.functions as func

from shared.blob_client import blob_exists, ensure_container_exists, upload_blob
from shared.github_client import (
    download_msapp,
    find_msapp_files,
    get_pr_metadata,
)
from shared.models import IngestionStatus

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Activity 1 — Fetch PR metadata
# ---------------------------------------------------------------------------

def fetch_pr_metadata_activity(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Fetch GitHub pull-request metadata.

    Input:  { owner, repo, pr_number }
    Output: { base_ref, base_sha, head_ref, head_sha, pr_title, author }
    """
    owner: str = payload["owner"]
    repo: str = payload["repo"]
    pr_number: int = int(payload["pr_number"])

    logger.info("[fetch_pr_metadata] PR #%d — %s/%s", pr_number, owner, repo)

    meta = get_pr_metadata(owner, repo, pr_number)

    return {
        "pr_number": pr_number,
        "pr_title": meta.get("title", ""),
        "author": meta.get("user", {}).get("login", ""),
        "base_ref": meta["base"]["ref"],
        "base_sha": meta["base"]["sha"],
        "head_ref": meta["head"]["ref"],
        "head_sha": meta["head"]["sha"],
        "state": meta.get("state", ""),
    }


# ---------------------------------------------------------------------------
# Activity 2 — Discover .msapp files
# ---------------------------------------------------------------------------

def discover_msapp_activity(payload: Dict[str, Any]) -> List[str]:
    """
    Return a list of .msapp file paths present in the repo at the given ref.

    Input:  { owner, repo, ref, path? }
    Output: ["apps/MyApp/MyApp.msapp", ...]
    """
    owner: str = payload["owner"]
    repo: str = payload["repo"]
    ref: str = payload["ref"]
    path_prefix: str = payload.get("path", "")

    logger.info("[discover_msapp] %s/%s@%s prefix='%s'", owner, repo, ref, path_prefix)

    msapp_paths = find_msapp_files(owner, repo, ref, path=path_prefix)

    logger.info("[discover_msapp] Found %d .msapp files", len(msapp_paths))
    return msapp_paths


# ---------------------------------------------------------------------------
# Activity 3 — Fetch artifact and upload to Blob Storage
# ---------------------------------------------------------------------------

def fetch_artifact_activity(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Download a .msapp from GitHub and upload it to Azure Blob Storage.

    Input:
        owner, repo, ref, file_path, branch_type, pr_number, force_reingest?

    Output: IngestionResult dict (see shared.models)

    Blob path layout:
        {owner}/{repo}/pr-{pr_number}/{branch_type}/{sha_short}/{filename}

    If the blob already exists and `force_reingest` is False, the function
    returns with status=SKIPPED without re-downloading.
    """
    owner: str = payload["owner"]
    repo: str = payload["repo"]
    ref: str = payload["ref"]           # full commit SHA
    file_path: str = payload["file_path"]
    branch_type: str = payload["branch_type"]   # 'base' | 'head'
    pr_number: int = int(payload.get("pr_number", 0))
    force_reingest: bool = bool(payload.get("force_reingest", False))

    sha_short = ref[:8]
    filename = os.path.basename(file_path)

    blob_path = (
        f"{owner}/{repo}/pr-{pr_number}/"
        f"{branch_type}/{sha_short}/{filename}"
    )

    logger.info(
        "[fetch_artifact] %s/%s@%s → blob: %s",
        owner, repo, ref, blob_path,
    )

    # ── Idempotency check ────────────────────────────────────────────────
    if not force_reingest and blob_exists(blob_path):
        logger.info("[fetch_artifact] SKIPPED (already exists): %s", blob_path)
        return {
            **payload,
            "blob_path": blob_path,
            "blob_url": "",
            "size_bytes": 0,
            "status": IngestionStatus.SKIPPED.value,
            "error": None,
        }

    # ── Download from GitHub ─────────────────────────────────────────────
    try:
        msapp_bytes: bytes = download_msapp(owner, repo, ref, file_path)
    except Exception as exc:  # noqa: BLE001
        logger.error("[fetch_artifact] Download failed for %s@%s: %s", file_path, ref, exc)
        return {
            **payload,
            "blob_path": blob_path,
            "blob_url": "",
            "size_bytes": 0,
            "status": IngestionStatus.FAILED.value,
            "error": str(exc),
        }

    # ── Upload to Blob Storage ────────────────────────────────────────────
    ensure_container_exists()

    try:
        blob_url = upload_blob(
            blob_path=blob_path,
            data=msapp_bytes,
            content_type="application/zip",
            overwrite=True,
            metadata={
                "owner": owner,
                "repo": repo,
                "ref": ref,
                "branch_type": branch_type,
                "pr_number": str(pr_number),
                "original_path": file_path,
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("[fetch_artifact] Upload failed for %s: %s", blob_path, exc)
        return {
            **payload,
            "blob_path": blob_path,
            "blob_url": "",
            "size_bytes": len(msapp_bytes),
            "status": IngestionStatus.FAILED.value,
            "error": str(exc),
        }

    logger.info(
        "[fetch_artifact] COMPLETED: %s (%d bytes) → %s",
        filename, len(msapp_bytes), blob_url,
    )

    return {
        **payload,
        "blob_path": blob_path,
        "blob_url": blob_url,
        "size_bytes": len(msapp_bytes),
        "status": IngestionStatus.COMPLETED.value,
        "error": None,
    }
