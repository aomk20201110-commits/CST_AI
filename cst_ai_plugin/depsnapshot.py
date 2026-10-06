"""CST_AI plugin layer — plan dependency snapshots and full rehydration.

V1.16 could persist a run's ExecutionPlan, but not the *inputs* that produced it.
`plan_task(task, reuse_index)` has exactly one non-TaskSpec dynamic input, and the
run store never wrote it down:

    reuse_index

so a run that planned a cache/reuse hit could not be rebuilt identically in a new
process.  The reconstruction produced a different `PLANNED_SOLVE_COUNT` and
therefore a different `execution_plan_hash`, and resume refused the run with
STALE_OR_FOREIGN_RUN_STORE.  Refusing was correct; being unable to recover was the
gap.

V1.17 closes the gap by persisting the decision inputs, never by relaxing the
hash.  The rules, in order:

    1. a stored, complete, self-consistent ExecutionPlan is used directly;
    2. only when the plan is missing or incomplete is it rebuilt, and then only
       from the STORED TaskSpec + STORED dependency snapshot + STORED resolved
       config -- never from the current filesystem;
    3. a rebuild must reproduce the stored `execution_plan_hash` exactly;
    4. anything that cannot be proven to be the same execution semantics BLOCKS.

Nothing here re-scans the filesystem to invent a plan.  Nothing here solves.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import capabilities as caps
from .planner import PLANNER_VERSION, canonical_hash, execution_plan_hash, plan_task
from .runstore import major_of, plan_is_usable
from .versions import MODULE_VERSIONS

DEPENDENCY_SNAPSHOT_VERSION = "1.17.0"

#: a major change in this format is not silently accepted
SUPPORTED_SNAPSHOT_MAJOR = 1

#: run-store payload marker: its absence means "planned before V1.17"
STATE_SCHEMA_VERSION_KEY = "state_schema_version"
STATE_SCHEMA_VERSION = "1.17.0"

# ------------------------------------------------------------- verdict values
REHYDRATION_OK = "OK"
REHYDRATION_BLOCKED = "BLOCKED"

#: where a usable plan came from
SOURCE_STORED_PLAN = "STORED_PLAN"
SOURCE_REBUILT_FROM_SNAPSHOT = "REBUILT_FROM_DEPENDENCY_SNAPSHOT"

#: a pre-V1.17 run store has no dependency snapshot.  Its plan may still be
#: rebuilt, but ONLY when the rebuilding input is proven correct rather than
#: assumed: `execution_plan_hash` receives the reuse index, so handing the
#: planner an EMPTY reuse index and reproducing the stored hash proves the
#: original plan contained no effective reuse decision.  The hash does the
#: proving; nothing is guessed.  When the proof fails, the run BLOCKS.
SOURCE_LEGACY_PROVEN_EMPTY_REUSE = "REBUILT_FROM_LEGACY_PROVEN_EMPTY_REUSE"

# ---------------------------------------------------------------- error codes
ERR_DEPENDENCY_SNAPSHOT_MISSING = "DEPENDENCY_SNAPSHOT_MISSING"
ERR_DEPENDENCY_SNAPSHOT_CORRUPT = "DEPENDENCY_SNAPSHOT_CORRUPT"
ERR_UNSUPPORTED_DEPENDENCY_SNAPSHOT = "UNSUPPORTED_DEPENDENCY_SNAPSHOT"
ERR_LEGACY_RUN_REHYDRATION_INSUFFICIENT = "LEGACY_RUN_REHYDRATION_INSUFFICIENT"
ERR_STORED_PLAN_INTEGRITY = "STORED_EXECUTION_PLAN_INTEGRITY_FAILED"
ERR_EXECUTION_PLAN_HASH_MISMATCH = "EXECUTION_PLAN_HASH_MISMATCH"
ERR_REUSED_ARTIFACT_MISSING = "REUSED_ARTIFACT_MISSING"
ERR_REUSED_ARTIFACT_HASH_CHANGED = "REUSED_ARTIFACT_HASH_CHANGED"
ERR_CAPABILITY_MISSING = "CAPABILITY_MISSING"
ERR_CAPABILITY_VERSION_MISMATCH = "CAPABILITY_VERSION_MISMATCH"
ERR_CAPABILITY_VERIFICATION_REGRESSED = "CAPABILITY_VERIFICATION_REGRESSED"
ERR_RESOLVED_CONFIG_SNAPSHOT_MISSING = "RESOLVED_CONFIG_SNAPSHOT_MISSING"
ERR_TASK_SPEC_MISSING = "TASK_SPEC_MISSING"

#: verification statuses that count as "this capability was really exercised"
VERIFIED_STATUSES = ("VERIFIED_E2E", "VERIFIED_OFFLINE")


# ===========================================================================
# PLAN_DEPENDENCY_MANIFEST
# ===========================================================================
#
# Audited by reading `planner.plan_task` and `planner.execution_plan_hash` line by
# line, not by reading a report.  The decisive fact is which inputs reach
# `execution_plan_hash`:
#
#     canonical_hash({"task_id", "stages": [names], "solve_count",
#                     "projects_created", "reuse": plan["reuse_decisions"]})
#
# so only the TaskSpec and the reuse index can move the hash.  The capability
# registry changes preflight and `CST_CONNECTION_REQUIRED`, but NOT the hash.

PLAN_DEPENDENCY_MANIFEST = (
    {
        "name": "task_spec",
        "source": "caller-supplied TaskSpec, validated by task.validate_task",
        "deterministic": True,
        "persisted": True,
        "persisted_since": "1.16.0",
        "persisted_as": "run store payload key 'task_spec'",
        "required_for_rebuild": True,
        "may_change_between_processes": False,
        "affects_execution_plan_hash": True,
        "notes": (
            "reaches the hash through task_id, the stage names, and the "
            "solve budget"
        ),
    },
    {
        "name": "reuse_index",
        "source": "second positional argument of planner.plan_task",
        "deterministic": True,
        "persisted": True,
        "persisted_since": "1.17.0",
        "persisted_as": "run store payload key 'dependency_snapshot.reuse_snapshot'",
        "required_for_rebuild": True,
        "may_change_between_processes": True,
        "affects_execution_plan_hash": True,
        "notes": (
            "the ONLY non-TaskSpec input that reaches execution_plan_hash, via "
            "PLANNED_SOLVE_COUNT and reuse_decisions; this was the V1.16 gap"
        ),
    },
    {
        "name": "capability_registry",
        "source": "cst_ai_plugin/capabilities.py module state (CAPABILITIES)",
        "deterministic": True,
        "persisted": True,
        "persisted_since": "1.17.0",
        "persisted_as": "run store payload key 'capability_snapshot'",
        "required_for_rebuild": False,
        "may_change_between_processes": True,
        "affects_execution_plan_hash": False,
        "notes": (
            "reading plan_task shows capabilities.get() only sets needs_cst and "
            "appends blockers; neither enters execution_plan_hash. It is "
            "snapshotted so resume can refuse an incompatible plugin"
        ),
    },
    {
        "name": "resolved_config",
        "source": "cst_ai_plugin/config.resolve_all()",
        "deterministic": True,
        "persisted": True,
        "persisted_since": "1.17.0",
        "persisted_as": "run store payload key 'resolved_config_snapshot'",
        "required_for_rebuild": False,
        "may_change_between_processes": True,
        "affects_execution_plan_hash": False,
        "notes": (
            "plan_task never reads the config, so it cannot move the hash; it is "
            "snapshotted so that editing cst_ai_config.json after a run started "
            "cannot change where that run resumes or writes"
        ),
    },
    {
        "name": "planner_version",
        "source": "planner.PLANNER_VERSION",
        "deterministic": True,
        "persisted": True,
        "persisted_since": "1.17.0",
        "persisted_as": "inside the stored ExecutionPlan ('planner_version')",
        "required_for_rebuild": False,
        "may_change_between_processes": True,
        "affects_execution_plan_hash": False,
        "notes": "recorded for provenance; a planner upgrade is additive here",
    },
    {
        "name": "module_versions",
        "source": "cst_ai_plugin/versions.py MODULE_VERSIONS",
        "deterministic": True,
        "persisted": True,
        "persisted_since": "1.16.0",
        "persisted_as": "run store payload key 'module_versions'",
        "required_for_rebuild": False,
        "may_change_between_processes": True,
        "affects_execution_plan_hash": False,
        "notes": "feeds the capability-compatibility check",
    },
    {
        "name": "generated_at_utc",
        "source": "wall clock inside plan_task",
        "deterministic": False,
        "persisted": True,
        "persisted_since": "1.15.0",
        "persisted_as": "inside the stored ExecutionPlan ('generated_at_utc')",
        "required_for_rebuild": False,
        "may_change_between_processes": True,
        "affects_execution_plan_hash": False,
        "notes": (
            "verified by reading execution_plan_hash: the timestamp is NOT part "
            "of the hash, so a rebuild at a different second still matches"
        ),
    },
)


# ===========================================================================
# small helpers
# ===========================================================================

def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)

    return "sha256:" + digest.hexdigest()


def file_identity(path) -> dict:
    """Identity of an artifact on disk; never raises on absence."""

    if not path:
        return {"kind": "UNRESOLVED", "path": None, "exists": False,
                "sha256": None, "size_bytes": None}

    candidate = Path(str(path))

    if not candidate.is_file():
        return {"kind": "PATH", "path": str(candidate), "exists": False,
                "sha256": None, "size_bytes": None}

    try:
        return {
            "kind": "PATH",
            "path": str(candidate.resolve()),
            "exists": True,
            "sha256": _sha256_file(candidate),
            "size_bytes": candidate.stat().st_size,
        }
    except OSError as exc:  # noqa: BLE001
        return {"kind": "PATH", "path": str(candidate), "exists": True,
                "sha256": None, "size_bytes": None, "read_error": repr(exc)}


def _module_for(implementation: str) -> str | None:
    """Which MODULE_VERSIONS entry implements this capability, if any."""

    if not isinstance(implementation, str):
        return None

    for module in sorted(MODULE_VERSIONS, key=len, reverse=True):
        if module in implementation:
            return module

    return None


# ===========================================================================
# ReuseDecisionSnapshot
# ===========================================================================

def build_reuse_decision_snapshot(reuse_index) -> dict:
    """Snapshot the reuse index exactly as `plan_task` receives it.

    `plan_task` reads `reuse_index[solved_key]` and then only TWO fields of the
    returned dict reach the hash: `key` and `artifact`.  The whole lookup value is
    therefore stored verbatim, so the rebuild hands the planner a byte-identical
    structure -- the hash cannot drift because a field was dropped.
    """

    reuse_index = reuse_index or {}
    candidates = []

    for solved_key in sorted(reuse_index, key=str):
        lookup_value = reuse_index[solved_key]
        hit = lookup_value if isinstance(lookup_value, dict) else {}

        artifact = hit.get("artifact")

        candidates.append(
            {
                "solved_artifact_key": solved_key,
                "selected": True,
                "reason": hit.get("reason", "CALLER_SUPPLIED_REUSE_HIT"),
                "declared_identity": hit.get("key"),
                "artifact": artifact,
                "reuse_eligibility": hit.get(
                    "reuse_eligibility", "DECLARED_BY_CALLER"
                ),
                "signature": hit.get("signature"),
                "source_run_id": hit.get("source_run_id"),
                "source_artifact": hit.get("source_artifact", artifact),
                "artifact_identity": file_identity(artifact),
                # verbatim: this is what the planner is handed back
                "lookup_value": lookup_value,
            }
        )

    return {
        "snapshot_version": DEPENDENCY_SNAPSHOT_VERSION,
        "lookup_keys": sorted(reuse_index, key=str),
        "candidates": candidates,
        "decision_count": len(candidates),
        "selected_count": len(candidates),
        "reuse_eligible_count": len(candidates),
        "snapshot_hash": canonical_hash(candidates),
    }


def reuse_index_from_snapshot(reuse_snapshot) -> dict:
    """Rebuild the exact dict `plan_task` must receive.

    Raises ValueError when the snapshot is not shaped like one; the caller turns
    that into DEPENDENCY_SNAPSHOT_CORRUPT rather than guessing.
    """

    if not isinstance(reuse_snapshot, dict):
        raise ValueError("reuse snapshot is not a dict")

    candidates = reuse_snapshot.get("candidates")

    if not isinstance(candidates, list):
        raise ValueError("reuse snapshot has no candidate list")

    rebuilt = {}

    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValueError("reuse candidate is not a dict")

        if "solved_artifact_key" not in candidate:
            raise ValueError("reuse candidate has no solved_artifact_key")

        if "lookup_value" not in candidate:
            raise ValueError("reuse candidate has no stored lookup_value")

        rebuilt[candidate["solved_artifact_key"]] = candidate["lookup_value"]

    return rebuilt


def verify_reuse_artifacts(reuse_snapshot) -> dict:
    """A reused artifact must still be the same bytes.

    A missing or changed artifact means the reuse decision can no longer be
    proven to mean the same thing, so it BLOCKS.  It never triggers a re-solve.
    """

    if not isinstance(reuse_snapshot, dict):
        return {"status": REHYDRATION_BLOCKED,
                "error_code": ERR_DEPENDENCY_SNAPSHOT_CORRUPT,
                "message": "reuse snapshot is not a dict", "checks": []}

    checks = []
    block = None

    for candidate in reuse_snapshot.get("candidates") or []:
        recorded = candidate.get("artifact_identity") or {}
        current = file_identity(candidate.get("artifact"))

        entry = {
            "solved_artifact_key": candidate.get("solved_artifact_key"),
            "artifact": candidate.get("artifact"),
            "recorded_sha256": recorded.get("sha256"),
            "current_sha256": current.get("sha256"),
            "status": "UNCHANGED",
        }

        if recorded.get("kind") == "PATH" and recorded.get("exists"):
            if not current.get("exists"):
                entry["status"] = ERR_REUSED_ARTIFACT_MISSING
                block = block or {
                    "error_code": ERR_REUSED_ARTIFACT_MISSING,
                    "message": (
                        "a reused solved artifact is gone; the reuse decision "
                        "cannot be proven to mean the same thing"
                    ),
                    "detail": entry,
                }
            elif current.get("sha256") != recorded.get("sha256"):
                entry["status"] = ERR_REUSED_ARTIFACT_HASH_CHANGED
                block = block or {
                    "error_code": ERR_REUSED_ARTIFACT_HASH_CHANGED,
                    "message": (
                        "a reused solved artifact changed on disk; refusing to "
                        "resume against different bytes"
                    ),
                    "detail": entry,
                }
            else:
                entry["status"] = "VERIFIED_UNCHANGED"
        else:
            # nothing was hashable when the run was created; say so instead of
            # implying a verification that never happened
            entry["status"] = "UNVERIFIABLE_NON_PATH_REFERENCE"
            entry["note"] = (
                "the reuse hit did not reference a readable file at snapshot "
                "time, so byte-level verification is not possible"
            )

        checks.append(entry)

    if block is not None:
        return {"status": REHYDRATION_BLOCKED, "checks": checks, **block}

    return {"status": REHYDRATION_OK, "error_code": None, "checks": checks}


# ===========================================================================
# capability snapshot
# ===========================================================================

def build_capability_snapshot() -> dict:
    """What the plan assumed about the plugin.

    `capabilities.py` has no per-capability version field, so none is invented
    here.  Compatibility is judged from the implementing module's MAJOR version
    plus the capability's presence and its recorded verification status.
    """

    entries = {}
    per_capability = {}

    for capability_id in caps.ids():
        capability = caps.get(capability_id)
        implementation = capability.get("implementation")
        module = _module_for(implementation)

        entries[capability_id] = {
            "capability_id": capability_id,
            "implementation": implementation,
            "implementing_module": module,
            "module_version": MODULE_VERSIONS.get(module) if module else None,
            "module_major": (
                major_of(MODULE_VERSIONS.get(module)) if module else None
            ),
            "verification_status": capability.get("verification_status"),
            "solve_requirement": capability.get("solve_requirement"),
            "requires_cst": capability.get("requires_cst"),
        }

        per_capability[capability_id] = canonical_hash(entries[capability_id])

    return {
        "snapshot_version": DEPENDENCY_SNAPSHOT_VERSION,
        "capability_ids": caps.ids(),
        "capability_count": len(entries),
        "entries": entries,
        "capability_fingerprints": per_capability,
        "module_versions": dict(MODULE_VERSIONS),
        "registry_hash": canonical_hash(per_capability),
    }


def compare_capability_snapshots(stored) -> dict:
    """Refuse to resume when the plugin can no longer honour the plan.

    Blocking rules (deliberately narrow, so an additive plugin change does not
    invalidate history):

        capability disappeared                -> CAPABILITY_MISSING
        implementing module MAJOR changed     -> CAPABILITY_VERSION_MISMATCH
        verification regressed to unverified  -> CAPABILITY_VERIFICATION_REGRESSED

    A NEW capability is additive and does not block.
    """

    if not isinstance(stored, dict) or not isinstance(stored.get("entries"), dict):
        return {"status": REHYDRATION_BLOCKED,
                "error_code": ERR_DEPENDENCY_SNAPSHOT_MISSING,
                "message": "capability snapshot is missing or malformed",
                "changes": []}

    current = build_capability_snapshot()
    changes = []
    block = None

    for capability_id, recorded in sorted(stored["entries"].items()):
        live = current["entries"].get(capability_id)

        if live is None:
            changes.append({"capability_id": capability_id,
                            "change": ERR_CAPABILITY_MISSING})
            block = block or {
                "error_code": ERR_CAPABILITY_MISSING,
                "message": (
                    f"capability {capability_id!r} was available when the run was "
                    f"planned and is gone from the registry"
                ),
                "detail": {"capability_id": capability_id},
            }
            continue

        recorded_major = recorded.get("module_major")
        live_major = live.get("module_major")

        if (
            recorded_major is not None
            and live_major is not None
            and recorded_major != live_major
        ):
            changes.append(
                {"capability_id": capability_id,
                 "change": ERR_CAPABILITY_VERSION_MISMATCH,
                 "recorded_major": recorded_major, "current_major": live_major}
            )
            block = block or {
                "error_code": ERR_CAPABILITY_VERSION_MISMATCH,
                "message": (
                    f"capability {capability_id!r} is implemented by "
                    f"{live.get('implementing_module')!r} whose major version "
                    f"changed from {recorded_major} to {live_major}"
                ),
                "detail": {"capability_id": capability_id,
                           "recorded": recorded, "current": live},
            }
            continue

        was_verified = recorded.get("verification_status") in VERIFIED_STATUSES
        is_verified = live.get("verification_status") in VERIFIED_STATUSES

        if was_verified and not is_verified:
            changes.append(
                {"capability_id": capability_id,
                 "change": ERR_CAPABILITY_VERIFICATION_REGRESSED,
                 "recorded": recorded.get("verification_status"),
                 "current": live.get("verification_status")}
            )
            block = block or {
                "error_code": ERR_CAPABILITY_VERIFICATION_REGRESSED,
                "message": (
                    f"capability {capability_id!r} was "
                    f"{recorded.get('verification_status')} and is now "
                    f"{live.get('verification_status')}"
                ),
                "detail": {"capability_id": capability_id},
            }

    added = sorted(set(current["entries"]) - set(stored["entries"]))

    if block is not None:
        return {"status": REHYDRATION_BLOCKED, "changes": changes,
                "added_capabilities": added, **block}

    return {"status": REHYDRATION_OK, "error_code": None, "changes": changes,
            "added_capabilities": added}


# ===========================================================================
# resolved config snapshot
# ===========================================================================

def build_resolved_config_snapshot(resolved: dict) -> dict:
    """Freeze the config a run was created under.

    Editing cst_ai_config.json afterwards must not change an existing run.  A
    deliberate config change is a NEW run, not a resume.
    """

    resolved = resolved or {}

    settings = {}

    for name, entry in (resolved.get("settings") or {}).items():
        if isinstance(entry, dict):
            settings[name] = {
                "resolved_path": entry.get("resolved_path"),
                "value": entry.get("value"),
                "source": entry.get("source"),
            }
        else:
            settings[name] = {"resolved_path": None, "value": entry,
                              "source": None}

    return {
        "snapshot_version": DEPENDENCY_SNAPSHOT_VERSION,
        "config_version": resolved.get("config_version"),
        "config_path": resolved.get("config_path"),
        "config_exists": resolved.get("config_exists"),
        "precedence": resolved.get("precedence"),
        "settings": settings,
        "legacy_keys_preserved": resolved.get("legacy_keys_preserved"),
        "snapshot_hash": canonical_hash(settings),
    }


def config_setting(config_snapshot, key: str):
    """Read one resolved setting out of a frozen snapshot."""

    if not isinstance(config_snapshot, dict):
        return None

    return (config_snapshot.get("settings") or {}).get(key)


# ===========================================================================
# the full dependency snapshot
# ===========================================================================

def build_dependency_snapshot(
    task: dict,
    reuse_index=None,
    *,
    resolved_config: dict | None = None,
) -> dict:
    """Everything needed to rebuild this run's plan without touching the world."""

    reuse_snapshot = build_reuse_decision_snapshot(reuse_index)
    capability_snapshot = build_capability_snapshot()
    config_snapshot = build_resolved_config_snapshot(resolved_config or {})

    body = {
        "snapshot_version": DEPENDENCY_SNAPSHOT_VERSION,
        "planner_version": PLANNER_VERSION,
        "task_id": (task or {}).get("task_id"),
        "task_spec_hash": (task or {}).get("task_spec_hash"),
        "reuse_snapshot": reuse_snapshot,
        "capability_snapshot": capability_snapshot,
        "resolved_config_snapshot": config_snapshot,
        "module_versions": dict(MODULE_VERSIONS),
        "plan_dependency_manifest": list(PLAN_DEPENDENCY_MANIFEST),
    }

    body["snapshot_hash"] = canonical_hash(
        {
            "snapshot_version": body["snapshot_version"],
            "planner_version": body["planner_version"],
            "task_id": body["task_id"],
            "reuse_snapshot_hash": reuse_snapshot.get("snapshot_hash"),
            "capability_registry_hash": capability_snapshot.get("registry_hash"),
            "config_snapshot_hash": config_snapshot.get("snapshot_hash"),
        }
    )

    return body


def snapshot_is_supported(snapshot) -> tuple[bool, str | None]:
    if not isinstance(snapshot, dict):
        return False, None

    version = snapshot.get("snapshot_version")

    if major_of(version) != SUPPORTED_SNAPSHOT_MAJOR:
        return False, version

    return True, version


# ===========================================================================
# rehydration
# ===========================================================================

def _blocked(error_code: str, message: str, *, detail=None, **extra) -> dict:
    result = {
        "verdict": REHYDRATION_BLOCKED,
        "plan": None,
        "source": None,
        "error": {"error_code": error_code, "message": message,
                  "detail": detail},
    }
    result.update(extra)
    return result


def is_legacy_state(state: dict) -> bool:
    """A run planned before V1.17 carries no state_schema_version marker."""

    return state.get(STATE_SCHEMA_VERSION_KEY) != STATE_SCHEMA_VERSION


def rehydrate_plan(state: dict) -> dict:
    """Recover the ExecutionPlan for a stored run, or refuse.

    Precedence, exactly as specified:

        A. a stored, usable, self-consistent plan is used DIRECTLY;
        B. only otherwise, rebuild deterministically from the stored TaskSpec +
           stored dependency snapshot.

    A rebuild that reproduces the stored hash is accepted; anything else BLOCKS.
    """

    stored_plan = state.get("execution_plan")
    stored_hash = state.get("execution_plan_hash") or (
        stored_plan or {}
    ).get("execution_plan_hash")

    legacy = is_legacy_state(state)
    snapshot = state.get("dependency_snapshot")

    usability = plan_is_usable(stored_plan)

    # ------------------------------------------------------- A. stored plan
    # NOTE: runstore.plan_is_usable returns a MAPPING, not a bool.  Testing the
    # mapping itself would be truthy for every plan and would silently accept an
    # incomplete one, so the field is read explicitly.
    if usability.get("usable") is True:
        # `plan_is_usable` checks the five keys the orchestrator needs to run a
        # plan, but `execution_plan_hash` reads four others (task_id, stages,
        # projects_created, reuse_decisions).  A checkpoint that satisfies the
        # first set and not the second must BLOCK, not raise.
        try:
            recomputed = execution_plan_hash(stored_plan)
        except (KeyError, TypeError, AttributeError) as exc:
            return _blocked(
                ERR_STORED_PLAN_INTEGRITY,
                "the stored ExecutionPlan passed the usability check but cannot "
                "be hashed, so its integrity cannot be established",
                detail={
                    "unhashable_because": f"{type(exc).__name__}: {exc}"[:200],
                    "present_keys": sorted(stored_plan),
                },
                plan_recovery={"stored_plan_usable": True, "replanned": False,
                               "hash_verified": False},
            )

        if stored_plan.get("execution_plan_hash") not in (None, recomputed):
            return _blocked(
                ERR_STORED_PLAN_INTEGRITY,
                "the stored ExecutionPlan does not hash to its own recorded "
                "execution_plan_hash; the checkpoint is internally inconsistent",
                detail={
                    "recorded": stored_plan.get("execution_plan_hash"),
                    "recomputed": recomputed,
                },
                plan_recovery={"stored_plan_usable": True, "replanned": False,
                               "hash_verified": False},
            )

        return {
            "verdict": REHYDRATION_OK,
            "plan": stored_plan,
            "source": SOURCE_STORED_PLAN,
            "error": None,
            "legacy": legacy,
            "plan_recovery": {
                "stored_plan_usable": True,
                "stored_plan_unusable_reason": usability.get("reason"),
                "replanned": False,
                "hash_verified": True,
                "hash_stored": stored_plan.get("execution_plan_hash"),
                "hash_rebuilt": None,
                "legacy": legacy,
            },
        }

    # --------------------------------------------------- B. deterministic rebuild
    task = state.get("task_spec")

    if not isinstance(task, dict) or not task:
        return _blocked(
            ERR_TASK_SPEC_MISSING,
            "the run store has no usable stored ExecutionPlan and no stored "
            "TaskSpec to rebuild one from",
            plan_recovery={"stored_plan_usable": False, "replanned": False,
                           "hash_verified": False},
        )

    if snapshot is None:
        if legacy:
            # A pre-V1.17 run store.  There is no recorded reuse index, so the
            # only rebuild input available is the empty index.  Rather than
            # assume that is what the original plan used, PROVE it: rebuild and
            # require the stored hash.  `execution_plan_hash` covers
            # PLANNED_SOLVE_COUNT and reuse_decisions, so a match can only happen
            # when the original plan had no effective reuse decision.
            proven = plan_task(task, {})

            if stored_hash and proven["execution_plan_hash"] == stored_hash:
                return {
                    "verdict": REHYDRATION_OK,
                    "plan": proven,
                    "source": SOURCE_LEGACY_PROVEN_EMPTY_REUSE,
                    "error": None,
                    "legacy": True,
                    "reuse_index_source": "PROVEN_EMPTY_BY_HASH_MATCH",
                    "plan_recovery": {
                        "stored_plan_usable": False,
                        "stored_plan_unusable_reason": usability.get("reason"),
                        "replanned": True,
                        "hash_verified": True,
                        "hash_stored": stored_hash,
                        "hash_rebuilt": proven["execution_plan_hash"],
                        "legacy": True,
                        "reuse_proven_empty": True,
                    },
                }

            return _blocked(
                ERR_LEGACY_RUN_REHYDRATION_INSUFFICIENT,
                "this run was planned before dependency snapshots existed, its "
                "stored ExecutionPlan is incomplete, and rebuilding it from the "
                "stored TaskSpec alone does not reproduce the stored "
                "execution_plan_hash -- so the reuse decisions it was planned "
                "with cannot be recovered. V1.17 refuses to guess them.",
                detail={
                    "state_schema_version": state.get(
                        STATE_SCHEMA_VERSION_KEY
                    ),
                    "stored_plan_unusable_reason": usability.get("reason"),
                    "stored": stored_hash,
                    "rebuilt_with_empty_reuse_index": proven.get(
                        "execution_plan_hash"
                    ),
                },
                legacy=True,
                plan_recovery={"stored_plan_usable": False, "replanned": True,
                               "hash_verified": False, "legacy": True,
                               "reuse_proven_empty": False},
            )

        return _blocked(
            ERR_DEPENDENCY_SNAPSHOT_MISSING,
            "a V1.17 run store is missing its dependency snapshot; the "
            "checkpoint is damaged",
            detail={"stored_plan_unusable_reason": usability.get("reason")},
            plan_recovery={"stored_plan_usable": False, "replanned": False,
                           "hash_verified": False},
        )

    supported, version = snapshot_is_supported(snapshot)

    if not supported:
        return _blocked(
            ERR_UNSUPPORTED_DEPENDENCY_SNAPSHOT,
            f"dependency snapshot version {version!r} is not supported",
            detail={"found": version},
            plan_recovery={"stored_plan_usable": False, "replanned": False,
                           "hash_verified": False},
        )

    try:
        rebuilt_index = reuse_index_from_snapshot(snapshot.get("reuse_snapshot"))
    except ValueError as exc:
        return _blocked(
            ERR_DEPENDENCY_SNAPSHOT_CORRUPT,
            f"the stored reuse snapshot cannot be turned back into a reuse "
            f"index: {exc}",
            plan_recovery={"stored_plan_usable": False, "replanned": False,
                           "hash_verified": False},
        )

    rebuilt = plan_task(task, rebuilt_index)

    recovery = {
        "stored_plan_usable": False,
        "replanned": True,
        "reuse_index_rebuilt": True,
        "reuse_entries": len(rebuilt_index),
        "hash_verified": False,
        "hash_stored": stored_hash,
        "hash_rebuilt": rebuilt["execution_plan_hash"],
    }

    if stored_hash and rebuilt["execution_plan_hash"] != stored_hash:
        return _blocked(
            ERR_EXECUTION_PLAN_HASH_MISMATCH,
            "rebuilding the plan from the stored dependency snapshot did not "
            "reproduce the stored execution_plan_hash; refusing to resume "
            "against a different execution semantics",
            detail={"stored": stored_hash,
                    "rebuilt": rebuilt["execution_plan_hash"]},
            plan_recovery=recovery,
        )

    recovery["hash_verified"] = bool(stored_hash)

    return {
        "verdict": REHYDRATION_OK,
        "plan": rebuilt,
        "source": SOURCE_REBUILT_FROM_SNAPSHOT,
        "error": None,
        "plan_recovery": recovery,
    }


def verify_rehydration(state: dict) -> dict:
    """Environment checks that a stored plan cannot answer by itself.

    Integrity of the plan is not enough: the artifacts the plan depends on must
    still be the same artifacts, and the plugin must still be able to honour the
    plan.  Any failure here BLOCKS.  None of it ever solves.
    """

    snapshot = state.get("dependency_snapshot")
    checks = {}

    if snapshot is None:
        if is_legacy_state(state):
            checks["dependency_snapshot"] = {
                "status": "ABSENT_LEGACY_RUN",
                "note": (
                    "planned before V1.17: usable only while the stored "
                    "ExecutionPlan stays complete"
                ),
            }
            checks["reuse_artifacts"] = {"status": "NOT_VERIFIABLE_NO_SNAPSHOT",
                                         "checks": []}
            checks["capabilities"] = {"status": "NOT_VERIFIABLE_NO_SNAPSHOT",
                                      "changes": []}
            checks["resolved_config"] = {"status": "NOT_VERIFIABLE_NO_SNAPSHOT"}
            return {"status": REHYDRATION_OK, "error_code": None, "checks": checks,
                    "legacy": True}

        return {"status": REHYDRATION_BLOCKED,
                "error_code": ERR_DEPENDENCY_SNAPSHOT_MISSING,
                "message": "a V1.17 run store is missing its dependency snapshot",
                "checks": checks, "legacy": False}

    supported, version = snapshot_is_supported(snapshot)

    if not supported:
        return {"status": REHYDRATION_BLOCKED,
                "error_code": ERR_UNSUPPORTED_DEPENDENCY_SNAPSHOT,
                "message": f"dependency snapshot version {version!r} is not supported",
                "checks": checks, "legacy": False}

    artifacts = verify_reuse_artifacts(snapshot.get("reuse_snapshot"))
    capabilities = compare_capability_snapshots(snapshot.get("capability_snapshot"))

    config_snapshot = snapshot.get("resolved_config_snapshot")
    config_ok = isinstance(config_snapshot, dict) and bool(
        config_snapshot.get("settings") is not None
    )

    checks["reuse_artifacts"] = artifacts
    checks["capabilities"] = capabilities
    checks["resolved_config"] = {
        "status": REHYDRATION_OK if config_ok else "MISSING",
        "snapshot_hash": (config_snapshot or {}).get("snapshot_hash"),
        "config_path": (config_snapshot or {}).get("config_path"),
    }

    if artifacts["status"] != REHYDRATION_OK:
        error = {k: v for k, v in artifacts.items() if k != "checks"}
        return {"status": REHYDRATION_BLOCKED, "checks": checks, "legacy": False,
                **error}

    if capabilities["status"] != REHYDRATION_OK:
        error = {k: v for k, v in capabilities.items()
                 if k not in ("changes", "added_capabilities")}
        return {"status": REHYDRATION_BLOCKED, "checks": checks, "legacy": False,
                **error}

    if not config_ok:
        return {"status": REHYDRATION_BLOCKED,
                "error_code": ERR_RESOLVED_CONFIG_SNAPSHOT_MISSING,
                "message": "the stored resolved-config snapshot is unusable",
                "checks": checks, "legacy": False}

    return {"status": REHYDRATION_OK, "error_code": None, "checks": checks,
            "legacy": False}


def summarise_rehydration(rehydration: dict) -> dict:
    """Small, tool-safe projection of a rehydration result."""

    plan = rehydration.get("plan") or {}

    return {
        "verdict": rehydration.get("verdict"),
        "source": rehydration.get("source"),
        "error_code": (rehydration.get("error") or {}).get("error_code"),
        "plan_recovery": rehydration.get("plan_recovery"),
        "plan_stages": plan.get("stage_order"),
        "planned_solve_count": plan.get("PLANNED_SOLVE_COUNT"),
        "execution_plan_hash": plan.get("execution_plan_hash"),
        "reuse_decisions": plan.get("reuse_decisions"),
    }


def dependency_snapshot_summary(snapshot) -> dict:
    if not isinstance(snapshot, dict):
        return {"present": False}

    reuse = snapshot.get("reuse_snapshot") or {}
    capability = snapshot.get("capability_snapshot") or {}
    config = snapshot.get("resolved_config_snapshot") or {}

    return {
        "present": True,
        "snapshot_version": snapshot.get("snapshot_version"),
        "snapshot_hash": snapshot.get("snapshot_hash"),
        "planner_version": snapshot.get("planner_version"),
        "reuse_decision_count": reuse.get("decision_count", 0),
        "reuse_lookup_keys": reuse.get("lookup_keys", []),
        "reuse_snapshot_hash": reuse.get("snapshot_hash"),
        "capability_count": capability.get("capability_count"),
        "capability_registry_hash": capability.get("registry_hash"),
        "config_snapshot_hash": config.get("snapshot_hash"),
        "manifest_entries": len(snapshot.get("plan_dependency_manifest") or []),
    }


__all__ = [
    "DEPENDENCY_SNAPSHOT_VERSION",
    "SUPPORTED_SNAPSHOT_MAJOR",
    "STATE_SCHEMA_VERSION",
    "STATE_SCHEMA_VERSION_KEY",
    "REHYDRATION_OK",
    "REHYDRATION_BLOCKED",
    "SOURCE_STORED_PLAN",
    "SOURCE_REBUILT_FROM_SNAPSHOT",
    "SOURCE_LEGACY_PROVEN_EMPTY_REUSE",
    "PLAN_DEPENDENCY_MANIFEST",
    "VERIFIED_STATUSES",
    "file_identity",
    "build_reuse_decision_snapshot",
    "reuse_index_from_snapshot",
    "verify_reuse_artifacts",
    "build_capability_snapshot",
    "compare_capability_snapshots",
    "build_resolved_config_snapshot",
    "config_setting",
    "build_dependency_snapshot",
    "snapshot_is_supported",
    "is_legacy_state",
    "rehydrate_plan",
    "verify_rehydration",
    "summarise_rehydration",
    "dependency_snapshot_summary",
]
