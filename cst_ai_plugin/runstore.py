"""CST_AI plugin layer — durable Run Store.

Filesystem-backed, no database server.  One directory per run:

    <run_store>/<run_id>/
        state.json          atomically replaced checkpoint
        events.jsonl        append-only event log for THIS run
        bundle.json         ResultBundle, once produced
        bundle.md

Responsibilities are kept apart on purpose:

    run store     = current / recoverable EXECUTION state
    plugin ledger = append-only PROVENANCE history

The ledger is not a substitute for the store, and the store is not an audit log.

Integrity: every checkpoint carries a schema version and a payload hash.  A
corrupt checkpoint raises RUN_STORE_CORRUPT; it is never silently rebuilt into an
empty run.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

RUN_STORE_SCHEMA_VERSION = "1.16.0"

#: loaders refuse a MAJOR version they do not understand
SUPPORTED_MAJOR = 1

STATE_FILE = "state.json"
EVENTS_FILE = "events.jsonl"
BUNDLE_JSON = "bundle.json"
BUNDLE_MD = "bundle.md"

# ---------------------------------------------------------------- error codes
RUN_STORE_CORRUPT = "RUN_STORE_CORRUPT"
RUN_NOT_FOUND = "RUN_NOT_FOUND"
UNSUPPORTED_SCHEMA_VERSION = "UNSUPPORTED_SCHEMA_VERSION"


class RunStoreError(Exception):
    code = "RUN_STORE_ERROR"

    def __init__(self, message: str, *, detail=None):
        super().__init__(message)
        self.message = message
        self.detail = detail

    def to_dict(self) -> dict:
        return {"error_code": self.code, "message": self.message,
                "detail": self.detail}


class RunStoreCorrupt(RunStoreError):
    code = RUN_STORE_CORRUPT


class RunNotFound(RunStoreError):
    code = RUN_NOT_FOUND


class UnsupportedSchemaVersion(RunStoreError):
    code = UNSUPPORTED_SCHEMA_VERSION


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def canonical_bytes(payload) -> bytes:
    return json.dumps(
        payload, sort_keys=True, ensure_ascii=False, default=repr,
        separators=(",", ":"),
    ).encode("utf-8")


def payload_hash(payload) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(payload)).hexdigest()


def major_of(version: str) -> int:
    try:
        return int(str(version).split(".")[0])
    except (ValueError, AttributeError, IndexError):
        return -1


def default_run_store_path() -> Path:
    """Repo convention: state lives in a top-level directory, never inside a
    frozen CST project directory."""

    return Path(__file__).resolve().parent.parent / "runs"


def portability_paths(path, root: Path) -> dict:
    """Store a portable form AND the resolved absolute form."""

    p = Path(path).resolve() if path else None

    if p is None:
        return {"absolute": None, "relative": None}

    try:
        relative = str(p.relative_to(Path(root).resolve()))
    except ValueError:
        relative = None

    return {"absolute": str(p), "relative": relative}


class RunStore:
    """Durable per-run state."""

    def __init__(self, root=None):
        self.root = Path(root) if root else default_run_store_path()
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------- locations
    def run_dir(self, run_id: str) -> Path:
        return self.root / run_id

    def state_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / STATE_FILE

    def events_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / EVENTS_FILE

    def exists(self, run_id: str) -> bool:
        return self.state_path(run_id).is_file()

    def list_runs(self) -> list[str]:
        if not self.root.is_dir():
            return []

        return sorted(
            p.name for p in self.root.iterdir()
            if p.is_dir() and (p / STATE_FILE).is_file()
        )

    # -------------------------------------------------------------- atomicity
    @staticmethod
    def _atomic_write_json(path: Path, payload) -> None:
        """temp file -> flush+fsync -> atomic replace.

        A crash mid-write can therefore never leave a half-written checkpoint:
        either the old file or the new one is present, never a partial one.
        """

        path.parent.mkdir(parents=True, exist_ok=True)

        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
        )

        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(canonical_bytes(payload))
                handle.flush()
                os.fsync(handle.fileno())

            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass

            raise

    # ------------------------------------------------------------------ write
    def save_state(self, run_id: str, state: dict) -> dict:
        """Persist a checkpoint, stamping schema version and payload hash."""

        run_dir = self.run_dir(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)

        created = state.get("created_at") or utc_now()

        # The counter comes from DISK, not from the caller's dict: a caller that
        # passes a fresh dict must not be able to rewind the checkpoint number.
        previous = 0
        existing = self.state_path(run_id)

        if existing.is_file():
            try:
                previous = int(
                    json.loads(existing.read_text(encoding="utf-8"))
                    .get("checkpoint_version", 0)
                )
            except Exception:  # noqa: BLE001
                # a damaged prior checkpoint must not stop a new one being
                # written; the damage is reported by load_state, not hidden here
                previous = 0

        payload = {
            "schema_version": state.get("schema_version",
                                        RUN_STORE_SCHEMA_VERSION),
            "checkpoint_version": previous + 1,
            "run_id": run_id,
            "created_at": created,
            "updated_at": utc_now(),
        }

        # everything except the envelope itself
        for key, value in state.items():
            if key not in payload:
                payload[key] = value

        payload["payload_hash"] = payload_hash(
            {k: v for k, v in payload.items() if k != "payload_hash"}
        )

        self._atomic_write_json(self.state_path(run_id), payload)

        return payload

    # ------------------------------------------------------------------- read
    def load_state(self, run_id: str) -> dict:
        path = self.state_path(run_id)

        if not path.is_file():
            raise RunNotFound(f"no run store entry for run_id {run_id!r}",
                              detail={"path": str(path)})

        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise RunStoreCorrupt(
                f"run store for {run_id!r} is not readable JSON: {exc!r}",
                detail={"path": str(path)},
            ) from exc

        version = state.get("schema_version")

        if major_of(version) != SUPPORTED_MAJOR:
            raise UnsupportedSchemaVersion(
                f"run store schema_version {version!r} is not supported by this "
                f"plugin (supported major: {SUPPORTED_MAJOR})",
                detail={"path": str(path), "found": version},
            )

        recorded = state.get("payload_hash")
        actual = payload_hash(
            {k: v for k, v in state.items() if k != "payload_hash"}
        )

        if recorded != actual:
            raise RunStoreCorrupt(
                "run store payload hash mismatch; the checkpoint is damaged",
                detail={
                    "path": str(path),
                    "recorded_hash": recorded,
                    "actual_hash": actual,
                    "hint": (
                        "the store is NOT rebuilt automatically: an empty run "
                        "would be indistinguishable from a real one"
                    ),
                },
            )

        return state

    # ------------------------------------------------------------------ events
    def append_event(self, run_id: str, *, stage, event, severity="INFO",
                     message="", detail=None) -> dict:
        """Append-only event log.  Raw result arrays belong in artifacts, not
        here, and secrets are never written."""

        record = {
            "timestamp_utc": utc_now(),
            "run_id": run_id,
            "stage": stage,
            "event": event,
            "severity": severity,
            "message": message,
            "detail": detail or {},
        }

        path = self.events_path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)

        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=repr))
            handle.write("\n")

        return record

    def read_events(self, run_id: str) -> list[dict]:
        path = self.events_path(run_id)

        if not path.is_file():
            return []

        records = []

        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    records.append(json.loads(line))
                except Exception:  # noqa: BLE001
                    records.append({"event": "UNPARSEABLE_EVENT_LINE"})

        return records

    # ------------------------------------------------------------------ bundle
    def save_bundle(self, run_id: str, bundle: dict, markdown: str) -> dict:
        run_dir = self.run_dir(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)

        json_path = run_dir / BUNDLE_JSON
        md_path = run_dir / BUNDLE_MD

        self._atomic_write_json(json_path, bundle)
        md_path.write_text(markdown, encoding="utf-8")

        return {"json": str(json_path), "markdown": str(md_path)}

    def load_bundle(self, run_id: str) -> dict | None:
        path = self.run_dir(run_id) / BUNDLE_JSON

        if not path.is_file():
            return None

        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise RunStoreCorrupt(f"stored bundle unreadable: {exc!r}",
                                  detail={"path": str(path)}) from exc


# ============================================================ cross-process API


def load_run(run_id: str, *, store_root=None) -> dict:
    """Load a run from disk with NO Orchestrator instance and NO CST contact.

    Usable from a completely fresh Python process.
    """

    store = RunStore(store_root)

    return {
        "run_id": run_id,
        "store_root": str(store.root),
        "state": store.load_state(run_id),
        "events": store.read_events(run_id),
        "bundle": store.load_bundle(run_id),
        "loaded_from_disk": True,
        "orchestrator_instance_required": False,
        "cst_contacted": False,
    }


def run_status(run_id: str, *, store_root=None) -> dict:
    """Offline status.  Never connects to CST, never mutates a project."""

    try:
        loaded = load_run(run_id, store_root=store_root)
    except RunStoreError as exc:
        return {
            "run_id": run_id,
            "available": False,
            "error": exc.to_dict(),
            "cst_contacted": False,
            "mutated_cst_project": False,
        }

    state = loaded["state"]

    return {
        "run_id": run_id,
        "available": True,
        "plugin_lifecycle_state": state.get("plugin_lifecycle_state"),
        "checkpoint": state.get("checkpoint"),
        "current_stage": state.get("current_stage"),
        "completed_stages": state.get("completed_stages"),
        "execution_status": state.get("execution_status"),
        "numerical_status": state.get("numerical_status"),
        "scientific_status": state.get("scientific_status"),
        "provenance_status": state.get("provenance_status"),
        "solver_usage": state.get("solver_usage"),
        "solver_substate": state.get("solver_substate"),
        "created_at": state.get("created_at"),
        "updated_at": state.get("updated_at"),
        "checkpoint_version": state.get("checkpoint_version"),
        "artifact_count": len(state.get("artifacts") or []),
        "bundle_present": loaded["bundle"] is not None,
        "loaded_from_disk": True,
        "cst_contacted": False,
        "mutated_cst_project": False,
    }


def run_inspect(run_id: str, *, store_root=None) -> dict:
    """Offline, read-only inspection.  Never opens a .cst to answer this."""

    loaded = load_run(run_id, store_root=store_root)
    state = loaded["state"]

    return {
        "run_id": run_id,
        "task_spec": state.get("task_spec"),
        "task_spec_hash": state.get("task_spec_hash"),
        "execution_plan": state.get("execution_plan"),
        "execution_plan_hash": state.get("execution_plan_hash"),
        "stages": state.get("completed_stages"),
        "current_stage": state.get("current_stage"),
        "artifacts": state.get("artifacts"),
        "observables": state.get("observables"),
        "warnings": state.get("warnings"),
        "errors": state.get("errors"),
        "unresolved": state.get("unresolved"),
        "solver_usage": state.get("solver_usage"),
        "scientific_labels": {
            "scientific_status": state.get("scientific_status"),
            "valid_for_science": state.get("valid_for_science"),
        },
        "events": loaded["events"],
        "read_only": True,
        "loaded_from_disk": True,
        "cst_contacted": False,
        "mutated_cst_project": False,
    }


#: a persisted plan is untrusted input; a resume must not IndexError on it
REQUIRED_PLAN_KEYS = (
    "stage_order",
    "execution_plan_hash",
    "CST_CONNECTION_REQUIRED",
    "PLANNED_SOLVE_COUNT",
    "MAX_AUTHORIZED_SOLVES",
)


def plan_is_usable(plan) -> dict:
    """Can this stored plan be resumed from as-is?"""

    if not isinstance(plan, dict):
        return {"usable": False, "reason": "PLAN_NOT_A_MAPPING"}

    missing = [k for k in REQUIRED_PLAN_KEYS if k not in plan]

    if missing:
        return {"usable": False, "reason": "PLAN_INCOMPLETE",
                "missing": missing}

    if not isinstance(plan.get("stage_order"), (list, tuple)):
        return {"usable": False, "reason": "PLAN_STAGE_ORDER_NOT_A_LIST"}

    return {"usable": True, "reason": None, "missing": []}


def remaining_solve_budget(state: dict) -> dict:
    """Budget is computed from the ORIGINAL task, never reset on resume."""

    usage = state.get("solver_usage") or {}

    authorized = int(usage.get("authorized", 0))
    performed = int(usage.get("performed", 0))

    return {
        "authorized": authorized,
        "performed": performed,
        "remaining": max(0, authorized - performed),
        "source": "persisted task solve authorization",
        "reset_on_resume": False,
    }
