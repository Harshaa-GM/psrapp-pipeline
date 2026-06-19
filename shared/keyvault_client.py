"""
keyvault_client.py — Azure Key Vault secret retrieval helper.

Centralises all secret access. Azure Functions should NEVER read secrets
from plain environment variables or config files — always use Key Vault.

Authentication is via the same Service Principal / Managed Identity used
by other Azure SDK clients (shared.auth.get_credential).

Usage:
    from shared.keyvault_client import get_secret

    github_token = get_secret("github-app-token")
    supabase_url = get_secret("supabase-url")
"""

from __future__ import annotations

import logging
import os

from azure.keyvault.secrets import SecretClient

from shared.auth import get_credential

logger = logging.getLogger(__name__)

_secret_cache: dict[str, str] = {}


def _get_client() -> SecretClient:
    vault_url = os.environ["AZURE_KEY_VAULT_URL"]
    credential = get_credential()
    return SecretClient(vault_url=vault_url, credential=credential)


def get_secret(secret_name: str, use_cache: bool = True) -> str:
    """
    Retrieve a secret value from Azure Key Vault.

    Results are cached in-process for the lifetime of the Function App
    instance (warm function invocations). Set `use_cache=False` to force
    a fresh Key Vault fetch (e.g. after a secret rotation).
    """
    if use_cache and secret_name in _secret_cache:
        return _secret_cache[secret_name]

    client = _get_client()
    secret = client.get_secret(secret_name)
    value = secret.value

    if value is None:
        raise ValueError(f"Key Vault secret '{secret_name}' has no value.")

    if use_cache:
        _secret_cache[secret_name] = value
        logger.debug("Cached secret: %s", secret_name)

    return value


def invalidate_cache(secret_name: str | None = None) -> None:
    """
    Invalidate the in-process secret cache.

    Pass a name to invalidate a single secret, or None to clear all.
    """
    if secret_name:
        _secret_cache.pop(secret_name, None)
    else:
        _secret_cache.clear()
