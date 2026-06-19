"""
models.py — Shared data models (dataclasses / TypedDicts) used across
            all Azure Function activity functions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class IngestionStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class PRContext:
    """Metadata about a GitHub pull request that triggered the pipeline."""
    owner: str
    repo: str
    pr_number: int
    pr_title: str
    base_ref: str
    base_sha: str
    head_ref: str
    head_sha: str
    author: str
    triggered_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class ArtifactRef:
    """
    Reference to a single .msapp file to be fetched from GitHub.

    `branch_type` is either 'base' or 'head' — indicating whether this
    is the before-state or after-state of the PR.
    """
    owner: str
    repo: str
    ref: str                  # branch name or commit SHA
    file_path: str            # e.g. "apps/MyApp/MyApp.msapp"
    branch_type: str          # 'base' | 'head'
    pr_number: int | None = None


@dataclass
class IngestionResult:
    """Result returned from the FetchArtifactActivity function."""
    artifact_ref: ArtifactRef
    blob_path: str            # path within the Azure Blob container
    blob_url: str             # fully-qualified blob URL
    size_bytes: int
    status: IngestionStatus
    error: str | None = None
    ingested_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "owner": self.artifact_ref.owner,
            "repo": self.artifact_ref.repo,
            "ref": self.artifact_ref.ref,
            "file_path": self.artifact_ref.file_path,
            "branch_type": self.artifact_ref.branch_type,
            "pr_number": self.artifact_ref.pr_number,
            "blob_path": self.blob_path,
            "blob_url": self.blob_url,
            "size_bytes": self.size_bytes,
            "status": self.status.value,
            "error": self.error,
            "ingested_at": self.ingested_at.isoformat(),
        }


@dataclass
class OrchestratorInput:
    """
    Payload passed to the Durable orchestrator from the HttpTrigger.

    Defaults are set for the AxleNet/APCMS repository and the known
    CanvasApps directory. All fields can be overridden via the HTTP
    request body or environment variables.
    """
    owner: str
    repo: str
    pr_number: int
    # Subdirectory within the repo to search for .msapp files.
    # Only blobs whose path starts with this prefix are ingested.
    path_prefix: str = "APCMS_PSRIntegration/CanvasApps"
    force_reingest: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OrchestratorInput":
        return cls(
            owner=data["owner"],
            repo=data["repo"],
            pr_number=int(data["pr_number"]),
            path_prefix=data.get(
                "path_prefix", "APCMS_PSRIntegration/CanvasApps"
            ),
            force_reingest=bool(data.get("force_reingest", False)),
        )


@dataclass
class OrchestratorResult:
    """Final result returned by the Durable orchestrator."""
    pr_number: int
    ingested: list[IngestionResult]
    skipped: list[IngestionResult]
    failed: list[IngestionResult]
    duration_seconds: float

    @property
    def total(self) -> int:
        return len(self.ingested) + len(self.skipped) + len(self.failed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pr_number": self.pr_number,
            "total": self.total,
            "ingested_count": len(self.ingested),
            "skipped_count": len(self.skipped),
            "failed_count": len(self.failed),
            "duration_seconds": round(self.duration_seconds, 2),
            "ingested": [r.to_dict() for r in self.ingested],
            "skipped": [r.to_dict() for r in self.skipped],
            "failed": [r.to_dict() for r in self.failed],
        }
