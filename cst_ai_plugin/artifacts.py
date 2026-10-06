"""CST_AI plugin layer — artifact registry.

Earlier rounds scattered projects, reports, JSON, ledgers and result hashes
across many directories.  This registry references them without moving or
rewriting anything: frozen files stay exactly where they are.

Identity rule enforced here: an artifact is only VALID_FOR_SCIENCE when its
producer said so.  Execution success never implies scientific validity.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

REGISTRY_VERSION = "1.15.0"

# artifact types
PROJECT = "CST_PROJECT"
RESULT = "RESULT"
REPORT_JSON = "REPORT_JSON"
REPORT_MD = "REPORT_MD"
LEDGER = "LEDGER"
REFERENCE = "REFERENCE"
BUNDLE = "RESULT_BUNDLE"
PLAN = "EXECUTION_PLAN"

ARTIFACT_STATUSES = ("REGISTERED", "MISSING", "STALE")

#: the three layers that must never be collapsed into one "success" flag
EXECUTION_STATUSES = ("CREATED", "RUNNING", "COMPLETED", "FAILED", "BLOCKED",
                      "PARTIAL")
NUMERICAL_STATUSES = ("RESOLVED", "UNRESOLVED", "NOT_COMPUTED", "INVALID")
SCIENTIFIC_STATUSES = (
    "NOT_ASSERTED",
    "VALID_FOR_SCIENCE",
    "TEST_ONLY",
    "SOURCE_FAITHFUL",
    "NOT_SOURCE_FAITHFUL",
)
PROVENANCE_STATUSES = ("COMPLETE", "PARTIAL", "MISSING")


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def sha256_file(path: Path) -> str | None:
    p = Path(path)

    if not p.is_file():
        return None

    digest = hashlib.sha256()

    with p.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)

    return "sha256:" + digest.hexdigest()


def make_artifact(
    *,
    artifact_id: str,
    run_id: str,
    artifact_type: str,
    path,
    producer: str,
    inputs=None,
    status: str = "REGISTERED",
    valid_for_science: bool = False,
    source_faithful: bool = False,
    execution_status: str = "CREATED",
    numerical_status: str = "NOT_COMPUTED",
    scientific_status: str = "NOT_ASSERTED",
    provenance_status: str = "MISSING",
    extra=None,
) -> dict:
    p = Path(path) if path else None

    if artifact_type not in (
        PROJECT, RESULT, REPORT_JSON, REPORT_MD, LEDGER, REFERENCE, BUNDLE, PLAN
    ):
        raise ValueError(f"unknown artifact_type: {artifact_type}")

    if status not in ARTIFACT_STATUSES:
        raise ValueError(f"unknown artifact status: {status}")

    for name, value, allowed in (
        ("execution_status", execution_status, EXECUTION_STATUSES),
        ("numerical_status", numerical_status, NUMERICAL_STATUSES),
        ("scientific_status", scientific_status, SCIENTIFIC_STATUSES),
        ("provenance_status", provenance_status, PROVENANCE_STATUSES),
    ):
        if value not in allowed:
            raise ValueError(f"unknown {name}: {value}")

    return {
        "artifact_id": artifact_id,
        "run_id": run_id,
        "type": artifact_type,
        "path": str(p) if p else None,
        "hash": sha256_file(p) if p else None,
        "producer": producer,
        "inputs": list(inputs or []),
        "status": status,
        "valid_for_science": bool(valid_for_science),
        "source_faithful": bool(source_faithful),
        "created_at": utc_now(),
        "execution_status": execution_status,
        "numerical_status": numerical_status,
        "scientific_status": scientific_status,
        "provenance_status": provenance_status,
        "extra": extra or {},
    }


class ArtifactRegistry:
    """A run-scoped index.  Never rewrites the artifacts it references."""

    def __init__(self, run_id: str):
        self.run_id = run_id
        self._artifacts: dict[str, dict] = {}

    def register(self, **kwargs) -> dict:
        kwargs.setdefault("run_id", self.run_id)
        artifact = make_artifact(**kwargs)
        self._artifacts[artifact["artifact_id"]] = artifact
        return artifact

    def get(self, artifact_id: str) -> dict | None:
        return self._artifacts.get(artifact_id)

    def all(self) -> list[dict]:
        return [self._artifacts[k] for k in sorted(self._artifacts)]

    def by_type(self, artifact_type: str) -> list[dict]:
        return [a for a in self.all() if a["type"] == artifact_type]

    def to_dict(self) -> dict:
        return {
            "registry_version": REGISTRY_VERSION,
            "run_id": self.run_id,
            "artifact_count": len(self._artifacts),
            "artifacts": self.all(),
            "frozen_files_moved_or_rewritten": False,
        }

    @classmethod
    def referencing_existing(cls, run_id: str, entries: list[dict]) -> "ArtifactRegistry":
        """Index pre-existing files from earlier rounds without touching them."""

        registry = cls(run_id)

        for entry in entries:
            registry.register(**entry)

        return registry


def hashes_of(artifacts: list[dict]) -> dict:
    return {a["artifact_id"]: a["hash"] for a in artifacts}
