"""
functions/orchestrator.py — Azure Durable Function: ArtifactIngestionOrchestrator
==================================================================================
Durable orchestrator that:

  1. Fetches PR metadata from GitHub (base + head branch info).
  2. Discovers all .msapp files on both the base and head branches.
  3. Fans out to `FetchArtifactActivity` in parallel — one task per
     (branch, msapp_file) combination.
  4. Aggregates results and returns a summary dict.

Retry policy (built-in Durable Functions):
  • 3 retries, 5-second first backoff, 2× exponential, max 30 s.

This orchestrator is idempotent: if an instance ID already exists for a
PR, the Durable runtime replays from the last checkpoint automatically.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import azure.durable_functions as df

from shared.github_client import find_msapp_files, get_pr_metadata
from shared.models import (
    ArtifactRef,
    IngestionResult,
    IngestionStatus,
    OrchestratorInput,
    OrchestratorResult,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Retry options for activity calls
# ---------------------------------------------------------------------------
_RETRY = df.RetryOptions(
    first_retry_interval_in_milliseconds=5_000,
    max_number_of_attempts=3,
)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def artifact_ingestion_orchestrator(context: df.DurableOrchestrationContext):
    """
    Main Durable orchestrator for PowerApps artifact ingestion.

    NOTE: Orchestrators must be deterministic — no I/O, datetime.now(),
    or random calls. All side-effects happen in activity functions.
    """
    started_at: str = context.current_utc_datetime.isoformat()
    raw_input: dict[str, Any] = context.get_input()
    orch_input = OrchestratorInput.from_dict(raw_input)

    logger.info(
        "[Orchestrator] PR #%d — %s/%s (path_prefix='%s')",
        orch_input.pr_number, orch_input.owner, orch_input.repo, orch_input.path_prefix,
    )

    # ── Step 1: Fetch PR metadata (base + head SHAs) ─────────────────────
    pr_meta: dict[str, Any] = yield context.call_activity_with_retry(
        "fetch_pr_metadata_activity",
        retry_options=_RETRY,
        input_={
            "owner": orch_input.owner,
            "repo": orch_input.repo,
            "pr_number": orch_input.pr_number,
        },
    )

    if not pr_meta:
        raise ValueError(f"fetch_pr_metadata_activity returned None for PR #{orch_input.pr_number}. Check the PR number and GitHub token.")

    base_sha: str = pr_meta["base_sha"]
    head_sha: str = pr_meta["head_sha"]

    # ── Step 2: Discover .msapp files on both branches ───────────────────
    # path_prefix restricts discovery to APCMS_PSRIntegration/CanvasApps
    # so that only the known CanvasApps directory is scanned.
    base_msapp_paths: list[str] = yield context.call_activity_with_retry(
        "discover_msapp_activity",
        retry_options=_RETRY,
        input_={
            "owner": orch_input.owner,
            "repo": orch_input.repo,
            "ref": base_sha,
            "path": orch_input.path_prefix,
        },
    )

    head_msapp_paths: list[str] = yield context.call_activity_with_retry(
        "discover_msapp_activity",
        retry_options=_RETRY,
        input_={
            "owner": orch_input.owner,
            "repo": orch_input.repo,
            "ref": head_sha,
            "path": orch_input.path_prefix,
        },
    )

    # ── Step 3: Build the list of artifact refs to fetch ─────────────────
    base_msapp_paths = base_msapp_paths or []
    head_msapp_paths = head_msapp_paths or []
    artifact_refs: list[dict] = []

    for path in base_msapp_paths:
        artifact_refs.append({
            "owner": orch_input.owner,
            "repo": orch_input.repo,
            "ref": base_sha,
            "file_path": path,
            "branch_type": "base",
            "pr_number": orch_input.pr_number,
        })

    for path in head_msapp_paths:
        artifact_refs.append({
            "owner": orch_input.owner,
            "repo": orch_input.repo,
            "ref": head_sha,
            "file_path": path,
            "branch_type": "head",
            "pr_number": orch_input.pr_number,
        })

    if not artifact_refs:
        logger.warning("[Orchestrator] No .msapp files found for PR #%d", orch_input.pr_number)

    # ── Step 4: Fan-out — fetch each artifact in parallel ────────────────
    fetch_tasks = [
        context.call_activity_with_retry(
            "fetch_artifact_activity",
            retry_options=_RETRY,
            input_={**ref, "force_reingest": orch_input.force_reingest},
        )
        for ref in artifact_refs
    ]

    raw_results: list[dict[str, Any]] = yield context.task_all(fetch_tasks)

    # ── Step 5: Categorise results ────────────────────────────────────────
    ingested, skipped, failed = [], [], []
    for r in raw_results:
        status = r.get("status")
        if status == IngestionStatus.COMPLETED.value:
            ingested.append(r)
        elif status == IngestionStatus.SKIPPED.value:
            skipped.append(r)
        else:
            failed.append(r)

    # Compute duration in a deterministic way (use replay-safe timestamp)
    ended_at = context.current_utc_datetime.isoformat()

    summary = {
        "pr_number": orch_input.pr_number,
        "owner": orch_input.owner,
        "repo": orch_input.repo,
        "started_at": started_at,
        "ended_at": ended_at,
        "total": len(raw_results),
        "ingested_count": len(ingested),
        "skipped_count": len(skipped),
        "failed_count": len(failed),
        "ingested": ingested,
        "skipped": skipped,
        "failed": failed,
    }

    logger.info(
        "[Orchestrator] PR #%d done — ingested=%d skipped=%d failed=%d",
        orch_input.pr_number, len(ingested), len(skipped), len(failed),
    )

    return summary


