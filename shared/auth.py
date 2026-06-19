"""
auth.py — Azure Service Principal authentication helper.

Provides a single get_credential() entry point that returns an Azure credential
object suitable for all SDK clients (BlobServiceClient, SecretClient, etc.).

Authentication order (DefaultAzureCredential fallback chain):
  1. Environment variables  (AZURE_TENANT_ID / CLIENT_ID / CLIENT_SECRET)
  2. Workload Identity (AKS)
  3. Managed Identity (Function App system-assigned or user-assigned)
  4. Azure CLI (local dev only)

When AZURE_CLIENT_SECRET is explicitly set, a ClientSecretCredential is used
directly so the intent is explicit and the fallback chain is bypassed.
"""

from __future__ import annotations

import logging
import os

from azure.identity import (
    ClientSecretCredential,
    DefaultAzureCredential,
    ManagedIdentityCredential,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_credential():
    """
    Return an Azure credential object for authenticating SDK clients.

    Prefer an explicit Service Principal (client-secret) when the three
    required env-vars are present. Fall back to DefaultAzureCredential
    (Managed Identity → Azure CLI) for local dev and hosted environments.
    """
    tenant_id = os.getenv("AZURE_TENANT_ID")
    client_id = os.getenv("AZURE_CLIENT_ID")
    client_secret = os.getenv("AZURE_CLIENT_SECRET")

    if tenant_id and client_id and client_secret:
        logger.info(
            "Using Service Principal credential (tenant=%s, client=%s)",
            tenant_id,
            client_id,
        )
        return ClientSecretCredential(
            tenant_id=tenant_id,
            client_id=client_id,
            client_secret=client_secret,
        )

    # Fall back to DefaultAzureCredential — covers Managed Identity in Azure
    # and Azure CLI for local development.
    logger.info("Service Principal env-vars not fully set; using DefaultAzureCredential")
    return DefaultAzureCredential()


def get_managed_identity_credential(client_id: str | None = None):
    """
    Return a ManagedIdentityCredential for Function App system/user identity.

    Pass `client_id` when using a user-assigned Managed Identity.
    """
    if client_id:
        return ManagedIdentityCredential(client_id=client_id)
    return ManagedIdentityCredential()
