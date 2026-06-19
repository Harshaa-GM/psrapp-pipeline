"""
functions/http_trigger.py — Azure Function: ArtifactIngestionTrigger
=====================================================================
HttpTrigger that starts a Durable orchestrator instance.

Endpoint  : POST /api/ingest-artifacts
Auth level: function  (key required; key stored in Key Vault)

Expected JSON body:
    {
        "pr_number":      42,                                   // required
        "owner":          "AxleNet",                            // optional — defaults to GITHUB_OWNER env var
        "repo":           "APCMS",                              // optional — defaults to GITHUB_REPO env var
        "path_prefix":    "APCMS_PSRIntegration/CanvasApps",   // optional — defaults to CANVAS_APPS_PATH env var
        "force_reingest": false                                 // optional, default false
    }

Default owner/repo/path_prefix come from environment variables so that
callers (e.g. a GitHub webhook) only need to supply pr_number.

The trigger:
  1. Validates the request body
  2. Bootstraps Key Vault secrets into environment (GitHub token)
  3. Starts the Durable orchestrator (`ArtifactIngestionOrchestrator`)
  4. Returns 202 Accepted with the Durable management URLs

Error handling:
  • 400 Bad Request  — missing / invalid body fields
  • 500 Internal Server Error — Key Vault / orchestrator start failure
"""

from __future__ import annotations

import json
import logging
import os

import azure.functions as func
import azure.durable_functions as df

from shared.keyvault_client import get_secret
from shared.models import OrchestratorInput

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Function registration (v2 programming model)
# ---------------------------------------------------------------------------

bp = df.Blueprint()


@bp.route(route="ingest-artifacts", methods=["POST"])
@bp.durable_client_input(client_name="client")
async def artifact_ingestion_trigger(req: func.HttpRequest, client): 
    """HTTP entry-point — validate body and start the Durable orchestrator."""
    logger.info("ArtifactIngestionTrigger received request from %s", req.headers.get("X-Forwarded-For", "unknown"))

    # ── 1. Parse + validate body ────────────────────────────────────────────
    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse(
            json.dumps({"error": "Request body must be valid JSON."}),
            status_code=400,
            mimetype="application/json",
        )

    # pr_number is the only truly required field; owner/repo/path_prefix
    # fall back to environment-variable defaults so the caller only needs
    # to send pr_number for the known AxleNet/APCMS repository.
    if "pr_number" not in body:
        return func.HttpResponse(
            json.dumps({"error": "Missing required field: pr_number"}),
            status_code=400,
            mimetype="application/json",
        )

    # Resolve owner / repo / path_prefix (body > env-var > hardcoded default)
    owner: str = (
        body.get("owner")
        or os.environ.get("GITHUB_OWNER", "AxleNet")
    )
    repo: str = (
        body.get("repo")
        or os.environ.get("GITHUB_REPO", "APCMS")
    )
    path_prefix: str = (
        body.get("path_prefix")
        or os.environ.get("CANVAS_APPS_PATH", "APCMS_PSRIntegration/CanvasApps")
    )

    try:
        orchestrator_input = OrchestratorInput.from_dict({
            **body,
            "owner": owner,
            "repo": repo,
            "path_prefix": path_prefix,
        })
    except (KeyError, ValueError) as exc:
        return func.HttpResponse(
            json.dumps({"error": f"Invalid input: {exc}"}),
            status_code=400,
            mimetype="application/json",
        )

    # ── 2. Bootstrap GitHub token from Key Vault into env for this instance ─
    #      (The orchestrator and activities read GITHUB_TOKEN from env)
    #      For local development, GITHUB_TOKEN can be set directly in
    #      local.settings.json to bypass Key Vault.
    if os.environ.get("GITHUB_TOKEN"):
        logger.info("GitHub token loaded from environment variable (local dev mode).")
    else:
        try:
            github_token = get_secret("github-app-token")
            os.environ["GITHUB_TOKEN"] = github_token
            logger.info("GitHub token loaded from Key Vault.")
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to load GitHub token from Key Vault: %s", exc)
            return func.HttpResponse(
                json.dumps({"error": "Failed to retrieve GitHub token from Key Vault."}),
                status_code=500,
                mimetype="application/json",
            )

    # ── 3. Start the Durable orchestrator ───────────────────────────────────
    # Include a path-slug in the instance ID so that two different path
    # ingestions for the same PR don't collide in the Durable task hub.
    path_slug = orchestrator_input.path_prefix.replace("/", "-")
    instance_id = (
        f"ingest-pr-{orchestrator_input.owner}-"
        f"{orchestrator_input.repo}-"
        f"{orchestrator_input.pr_number}-"
        f"{path_slug}"
    )

    try:
        await client.start_new(
            "artifact_ingestion_orchestrator",
            instance_id=instance_id,
            client_input=vars(orchestrator_input),
        )
        logger.info("Orchestrator started: %s", instance_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to start orchestrator: %s", exc)
        return func.HttpResponse(
            json.dumps({"error": f"Orchestrator start failed: {exc}"}),
            status_code=500,
            mimetype="application/json",
        )

    # ── 4. Return 202 Accepted with management URLs ──────────────────────────
    return client.create_check_status_response(req, instance_id)
