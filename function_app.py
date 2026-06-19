"""
function_app.py — Azure Functions v2 app entry-point.

Registers all functions (HTTP trigger, Durable orchestrator, activities)
with the Azure Functions runtime using the v2 programming model.

All functions use the same DFApp instance so that the Durable extension
can route messages between the orchestrator and activities correctly.
"""

from __future__ import annotations

import logging

import azure.durable_functions as df
import azure.functions as func

# ── Import activity and orchestrator implementations ──────────────────────
from functions.activities import (
    discover_msapp_activity,
    fetch_artifact_activity,
    fetch_pr_metadata_activity,
)
from functions.orchestrator import artifact_ingestion_orchestrator
from functions.http_trigger import bp as artifact_ingestion_bp

# ---------------------------------------------------------------------------
# App bootstrap
# ---------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO)

app = df.DFApp(http_auth_level=func.AuthLevel.FUNCTION)

# ── Register HTTP trigger blueprint ───────────────────────────────────────
app.register_blueprint(artifact_ingestion_bp)

# ── Register Durable orchestrator ─────────────────────────────────────────
app.orchestration_trigger(context_name="context")(
    artifact_ingestion_orchestrator
)

# ── Register Activity functions ────────────────────────────────────────────
app.activity_trigger(input_name="payload")(fetch_pr_metadata_activity)
app.activity_trigger(input_name="payload")(discover_msapp_activity)
app.activity_trigger(input_name="payload")(fetch_artifact_activity)
