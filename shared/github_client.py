"""
github_client.py — GitHub REST API helpers.

Fetches .msapp artifacts and branch/PR metadata from a GitHub repository.

Authentication uses a GitHub App installation token (preferred) or a
Personal Access Token stored in Azure Key Vault. The token is injected
via the GITHUB_TOKEN environment variable — never hard-coded.

Key operations:
  • Download the .msapp file from a specific branch/commit
  • List .msapp files in the repo tree
  • Fetch PR metadata (base branch, head SHA, changed files)
"""

from __future__ import annotations

import logging
import os
from io import BytesIO
from typing import Any

import requests

logger = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com"
_TIMEOUT = 60  # seconds


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _github_headers() -> dict[str, str]:
    token = os.environ["GITHUB_TOKEN"]
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _get(url: str, **params) -> Any:
    resp = requests.get(url, headers=_github_headers(), params=params, timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _get_raw(url: str) -> bytes:
    """Download raw bytes (e.g. for binary .msapp files)."""
    headers = _github_headers()
    headers["Accept"] = "application/vnd.github.raw+json"
    resp = requests.get(url, headers=headers, timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.content


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_repo_info(owner: str, repo: str) -> dict[str, Any]:
    """Return basic repository metadata."""
    return _get(f"{_GITHUB_API}/repos/{owner}/{repo}")


def get_pr_metadata(owner: str, repo: str, pr_number: int) -> dict[str, Any]:
    """
    Return pull-request metadata including base/head SHA and branch names.

    Returned dict keys (subset):
      - number, title, state
      - base.ref, base.sha
      - head.ref, head.sha
    """
    return _get(f"{_GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}")


def list_pr_files(owner: str, repo: str, pr_number: int) -> list[dict[str, Any]]:
    """Return the list of files changed in a PR (paginated, max 3 000 files)."""
    results: list[dict] = []
    page = 1
    while True:
        page_data: list[dict] = _get(
            f"{_GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}/files",
            per_page=100,
            page=page,
        )
        results.extend(page_data)
        if len(page_data) < 100:
            break
        page += 1
    return results


def find_msapp_files(
    owner: str, repo: str, ref: str, path: str = ""
) -> list[str]:
    """
    Recursively list all .msapp files in the repo tree at the given ref.

    Uses the Git tree API (recursive) for efficiency.

    Returns a list of blob paths, e.g.:
        ["apps/MyApp/MyApp.msapp", "apps/OtherApp/OtherApp.msapp"]
    """
    tree_url = f"{_GITHUB_API}/repos/{owner}/{repo}/git/trees/{ref}"
    data = _get(tree_url, recursive=1)

    msapp_paths = [
        item["path"]
        for item in data.get("tree", [])
        if item["type"] == "blob"
        and item["path"].endswith(".msapp")
        and (not path or item["path"].startswith(path))
    ]

    if data.get("truncated"):
        logger.warning(
            "Git tree was truncated for %s/%s@%s — large repo. "
            "Some .msapp files may be missed.",
            owner, repo, ref,
        )

    return msapp_paths


def download_msapp(owner: str, repo: str, ref: str, file_path: str) -> bytes:
    """
    Download the raw bytes of a .msapp file at a specific ref.

    Uses the contents API with raw media type to handle files up to 100 MB.
    For files > 1 MB the API returns a download_url; this function handles
    both cases transparently.
    """
    url = f"{_GITHUB_API}/repos/{owner}/{repo}/contents/{file_path}"
    meta = _get(url, ref=ref)

    if isinstance(meta, list):
        raise ValueError(f"{file_path} is a directory, not a file.")

    # Files > 1 MB: GitHub returns a download_url pointing to raw CDN.
    download_url: str | None = meta.get("download_url")
    if download_url:
        resp = requests.get(download_url, timeout=_TIMEOUT)
        resp.raise_for_status()
        logger.info(
            "Downloaded %s@%s via download_url (%d bytes)",
            file_path, ref, len(resp.content),
        )
        return resp.content

    # Small files: content is base64-encoded in the JSON response.
    import base64
    content_b64: str = meta.get("content", "")
    raw = base64.b64decode(content_b64)
    logger.info(
        "Downloaded %s@%s via contents API (%d bytes)",
        file_path, ref, len(raw),
    )
    return raw


def get_commit_sha(owner: str, repo: str, branch: str) -> str:
    """Return the latest commit SHA for a branch."""
    data = _get(f"{_GITHUB_API}/repos/{owner}/{repo}/commits/{branch}")
    return data["sha"]
