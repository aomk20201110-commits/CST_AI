"""CST_AI plugin layer — PUBLIC API (v1).

This is the stable surface a user or another tool is meant to consume:

    plan(task)          -> execution plan          (0 CST, 0 solve)
    run(task)           -> durable run, persisted  (side effects per plan)
    status(run_id)      -> offline status          (0 CST)
    inspect(run_id)     -> offline detail          (0 CST, read-only)
    resume(run_id)      -> continue a run          (never re-solves a solved stage)
    load_task(path)     -> TaskSpec from JSON

Users ask for a CAPABILITY ("Q_LINEWIDTH"), never for an internal module
("v110c_analysis.py").  The internal version names are provenance, and they are
deliberately NOT part of this API.

Everything is persisted through the durable Run Store, so STATUS / INSPECT /
RESUME work from a completely fresh process.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import capabilities as caps
from . import config as cfg
from . import depsnapshot as ds
from . import runstore as rs
from . import validation as val
from .bundle import bundle_to_markdown
from .errors import PluginError
from .orchestrator import (
    ANALYSIS_COMPLETE_REPORT_PENDING,
    COMPLETE,
    Orchestrator,
    SOLVE_COMPLETE_ANALYSIS_PENDING,
)
from .planner import plan_task
from .task import task_spec_hash, validate_task

#: the API SURFACE version — frozen at V1.16 and unchanged by this round
PUBLIC_API_VERSION = "1.16.0"

#: the implementation revision behind that surface.  V1.17 adds optional
#: inputs (reuse_index, dependency snapshots); it does not add, remove or rename
#: any PUBLIC_API_V1 entry.
PUBLIC_API_REVISION = "1.17.0"

#: the complete public surface.  Nothing else is promised to users.
PUBLIC_API_V1 = (
    "plan",
    "run",
    "status",
    "inspect",
    "resume",
    "load_task",
)

ALREADY_COMPLETE = "ALREADY_COMPLETE"


def load_task(path) -> dict:
    """Load and validate a TaskSpec from JSON."""

    p = Path(path)

    if not p.is_file():
        raise FileNotFoundError(f"task spec not found: {p}")

    task = json.loads(p.read_text(encoding="utf-8-sig"))

    report = val.validate_task_runtime(task)

    if not report["ok"]:
        from .errors import TaskSchemaError

        raise TaskSchemaError(
            "task spec failed runtime validation",
            stage="LOAD_TASK",
            detail=report["errors"],
        )

    return validate_task(task)


# ------------------------------------------------------------------------ PLAN


def plan(task: dict, *, store_root=None, config_path=None,
         reuse_index=None) -> dict:
    """Plan only.  Never contacts CST, never solves, never writes a run.

    `reuse_index` is the only non-TaskSpec input the planner reads, so it is
    echoed back inside a dependency snapshot the caller can persist alongside the
    plan.  PLAN still writes nothing.
    """

    report = val.validate_task_runtime(task)

    if not report["ok"]:
        return {
            "ok": False,
            "stage": "VALIDATION",
            "errors": report["errors"],
            "cst_contacted": False,
            "solved": False,
        }

    task = validate_task(task)

    resolved = cfg.resolve_all(
        cli={"run_store_path": store_root} if store_root else None,
        task={"run_store_path": task.get("run_store_path")},
        path=config_path,
    )

    orche = Orchestrator()
    execution_plan = orche.plan_only(task, reuse_index)

    dependency_snapshot = ds.build_dependency_snapshot(
        task, reuse_index, resolved_config=resolved
    )

    return {
        "ok": True,
        "api_version": PUBLIC_API_VERSION,
        "api_revision": PUBLIC_API_REVISION,
        "task_id": task["task_id"],
        "task_spec_hash": task_spec_hash(task),
        "plan_hash": execution_plan["execution_plan_hash"],
        "stages": execution_plan["stage_order"],
        "CST_CONNECTION_REQUIRED": execution_plan["CST_CONNECTION_REQUIRED"],
        "PLANNED_SOLVE_COUNT": execution_plan["PLANNED_SOLVE_COUNT"],
        "MAX_AUTHORIZED_SOLVES": execution_plan["MAX_AUTHORIZED_SOLVES"],
        "REUSED_SOLVED_ARTIFACTS": execution_plan["REUSED_SOLVED_ARTIFACTS"],
        "projects_created": execution_plan["projects_created"],
        "expected_outputs": execution_plan["expected_outputs"],
        "risks_and_blockers": execution_plan["risks_and_blockers"],
        "zero_solve_paths": execution_plan["zero_solve_paths"],
        "resolved_config": resolved,
        "reuse_decisions": execution_plan["reuse_decisions"],
        "dependency_snapshot": dependency_snapshot,
        "dependency_snapshot_summary": ds.dependency_snapshot_summary(
            dependency_snapshot
        ),
        "cst_contacted": False,
        "solved": False,
        "execution_plan": execution_plan,
    }


# ------------------------------------------------------------------------- RUN


def _state_from_orchestrator(orche: Orchestrator, task: dict,
                             result: dict | None, *, current_stage=None,
                             completed=None, rehydration=None) -> dict:
    """Project live orchestrator state into the durable run-store payload.

    `rehydration` is the V1.17 addition: the plan *inputs* that V1.16 never wrote
    down.  It is optional so every V1.16 call site keeps its exact behaviour.
    """

    state = {
        "run_id": orche.run_id,
        "task_spec": task,
        "task_spec_hash": task_spec_hash(task),
        "execution_plan": orche.plan,
        "execution_plan_hash": (orche.plan or {}).get("execution_plan_hash"),
        "plugin_version": orche.plugin_version,
        "module_versions": {
            k: v for k, v in (
                __import__("cst_ai_plugin.versions", fromlist=["MODULE_VERSIONS"])
                .MODULE_VERSIONS.items()
            )
        },
        "plugin_lifecycle_state": orche.lifecycle,
        "checkpoint": orche.checkpoint,
        # V1.18: which point the checkpoint describes.  A resumed process needs
        # it to refuse to apply point N's checkpoint to point N+1.
        "checkpoint_point_index": getattr(orche, "checkpoint_point_index", None),
        "current_stage": current_stage,
        "completed_stages": list(completed or []),
        "solver_usage": orche.solve_usage,
        "solver_substate": orche.solver_lifecycle,
        "artifacts": [
            {
                "artifact_id": a["artifact_id"],
                "type": a["type"],
                "path": a["path"],
                "hash": a["hash"],
                "scientific_status": a["scientific_status"],
            }
            for a in (orche.registry.all() if orche.registry else [])
        ],
        "observables": {
            k: v for k, v in (orche.observables or {}).items()
        },
        "errors": result.get("errors") if result else list(orche.errors),
        "warnings": list(orche.warnings),
        "unresolved": list(orche.unresolved),
        "execution_status": (result or {}).get("execution_status", "FAILED"),
        "numerical_status": (result or {}).get("numerical_status",
                                               "NOT_COMPUTED"),
        "scientific_status": (result or {}).get("scientific_status",
                                                "NOT_ASSERTED"),
        "provenance_status": "COMPLETE" if orche.registry else "MISSING",
        "valid_for_science": False,
        "stages_completed_at": {},
        # V1.18: a sweep-shaped run persists how far it got, point by point.
        # `points_completed` is what a later process reads to know which points
        # must never be solved again.
        "points_total": getattr(orche, "points_total", 0) or 0,
        "points_completed": getattr(orche, "points_completed", 0) or 0,
        "point_states": [
            dict(p) for p in (getattr(orche, "point_states", None) or [])
        ],
    }

    if rehydration:
        state.update(rehydration)

    return state


def run(task: dict, *, store_root=None, config_path=None,
        active_project=None, runtime_file=None, reuse_index=None) -> dict:
    """Execute a task through the ONE orchestrator, persisting state as it goes.

    `reuse_index` is the planner's only non-TaskSpec input.  It is persisted in a
    dependency snapshot BEFORE the plan is executed, so a later process can
    rebuild this exact plan instead of guessing it.
    """

    report = val.validate_task_runtime(task)

    if not report["ok"]:
        return {"ok": False, "stage": "VALIDATION",
                "errors": report["errors"], "cst_contacted": False,
                "solved": False}

    task = validate_task(task)

    resolved = cfg.resolve_all(
        cli={"run_store_path": store_root} if store_root else None,
        task={"run_store_path": task.get("run_store_path")},
        path=config_path,
    )

    store_path = resolved["settings"][cfg.RUN_STORE_KEY]["resolved_path"]
    store = rs.RunStore(store_path)

    # the plan inputs, frozen before anything executes
    dependency_snapshot = ds.build_dependency_snapshot(
        task, reuse_index, resolved_config=resolved
    )
    rehydration = {
        ds.STATE_SCHEMA_VERSION_KEY: ds.STATE_SCHEMA_VERSION,
        "api_revision": PUBLIC_API_REVISION,
        "reuse_index": reuse_index or {},
        "dependency_snapshot": dependency_snapshot,
        "capability_snapshot": dependency_snapshot["capability_snapshot"],
        "resolved_config_snapshot": dependency_snapshot[
            "resolved_config_snapshot"
        ],
    }

    orche = Orchestrator()
    orche.plan_only(task, reuse_index)

    run_id = orche.run_id
    store.save_state(
        run_id, _state_from_orchestrator(orche, task, None,
                                         rehydration=rehydration)
    )
    store.append_event(run_id, stage="PLAN", event="PLANNED",
                       message=f"plan {orche.plan['execution_plan_hash']}")

    orche.persist_hook = _make_persist_hook(store, task, run_id, rehydration)

    result = orche.run(task, runtime_file=runtime_file,
                       active_project=active_project,
                       reuse_index=reuse_index)

    state = _state_from_orchestrator(
        orche, task, result,
        current_stage=orche.checkpoint,
        completed=[t["state"] for t in orche.transitions],
        rehydration=rehydration,
    )

    store.save_state(run_id, state)
    store.append_event(
        run_id,
        stage="FINISH",
        event=orche.lifecycle,
        severity="INFO" if orche.lifecycle == "COMPLETED" else "ERROR",
        message=f"plugin lifecycle {orche.lifecycle}",
    )

    bundle = result.get("bundle_object")

    if bundle:
        store.save_bundle(run_id, bundle, bundle_to_markdown(bundle))

    return {
        "ok": orche.lifecycle == "COMPLETED",
        "api_version": PUBLIC_API_VERSION,
        "api_revision": PUBLIC_API_REVISION,
        "run_id": run_id,
        "task_id": task["task_id"],
        "plugin_lifecycle": orche.lifecycle,
        "checkpoint": orche.checkpoint,
        "execution_status": result.get("execution_status"),
        "numerical_status": result.get("numerical_status"),
        "scientific_status": result.get("scientific_status"),
        "solve_usage": orche.solve_usage,
        "store_root": str(store.root),
        "bundle": result.get("bundle"),
        "bundles": result.get("bundle"),
        "errors": result.get("errors"),
        "warnings": result.get("warnings"),
        "resolved_config": resolved,
        "reuse_decisions": (orche.plan or {}).get("reuse_decisions"),
        "dependency_snapshot_summary": ds.dependency_snapshot_summary(
            dependency_snapshot
        ),
    }


def _make_persist_hook(store: rs.RunStore, task: dict, run_id: str,
                       rehydration=None):
    """Checkpoint after every stage, atomically."""

    completed: list[str] = []

    def hook(orche, stage):
        completed.append(stage)

        state = _state_from_orchestrator(
            orche, task, None,
            current_stage=stage,
            completed=list(completed),
            rehydration=rehydration,
        )

        store.save_state(run_id, state)
        store.append_event(
            run_id, stage=stage, event="STAGE_COMPLETE",
            message=f"{stage} completed (plugin state {orche.lifecycle})",
        )

    return hook


# --------------------------------------------------------- status / inspect


def status(run_id: str, *, store_root=None, refresh_runtime=False) -> dict:
    """Offline by default.  `refresh_runtime` is NOT implemented on purpose."""

    if refresh_runtime:
        return {
            "run_id": run_id,
            "available": False,
            "error": {
                "error_code": "REFRESH_RUNTIME_NOT_AUTHORIZED",
                "message": (
                    "--refresh-runtime is a separate, future authorization; "
                    "default STATUS is fully offline"
                ),
            },
            "cst_contacted": False,
            "mutated_cst_project": False,
        }

    return rs.run_status(run_id, store_root=store_root)


def inspect(run_id: str, *, store_root=None) -> dict:
    return rs.run_inspect(run_id, store_root=store_root)


# ---------------------------------------------------------------------- RESUME


def resume(run_id: str, *, store_root=None, task=None, config_path=None) -> dict:
    """Cross-process resume.

    A stage that already succeeded is never re-executed, and the solve budget is
    recomputed from the ORIGINAL persisted authorization - never reset.
    """

    store = rs.RunStore(store_root)

    try:
        state = store.load_state(run_id)
    except rs.RunStoreError as exc:
        return {"ok": False, "run_id": run_id, "error": exc.to_dict(),
                "cst_contacted": False, "solved": False}

    checkpoint = state.get("checkpoint")
    lifecycle = state.get("plugin_lifecycle_state")
    stored_task = state.get("task_spec")

    # Task integrity is checked BEFORE the COMPLETED early-return: a caller who
    # hands us the wrong task is pointing at the wrong run, and needs to be told
    # even when the run they actually named is already finished.
    integrity = {"checked": task is not None, "matches": None}

    if task is not None:
        integrity["matches"] = task_spec_hash(task) == state.get("task_spec_hash")
        integrity["stored_hash"] = state.get("task_spec_hash")
        integrity["supplied_hash"] = task_spec_hash(task)

        if not integrity["matches"]:
            return {
                "ok": False,
                "run_id": run_id,
                "error": {
                    "error_code": "TASK_INTEGRITY_MISMATCH",
                    "message": (
                        "the supplied task does not match the stored task; it is "
                        "used for cross-check only and never silently replaces it"
                    ),
                    "detail": integrity,
                },
                "cst_contacted": False,
                "solved": False,
            }

    if checkpoint == COMPLETE or lifecycle == "COMPLETED":
        return {
            "ok": True,
            "run_id": run_id,
            "resume_result": ALREADY_COMPLETE,
            "stages_skipped": ["CONNECT", "BUILD", "VERIFY", "SOLVE", "EXTRACT",
                               "ANALYZE", "REPORT"],
            "stages_executed": [],
            "fresh_solve_count": 0,
            "regenerated_report": False,
            "solve_budget": rs.remaining_solve_budget(state),
            "task_integrity": integrity,
            "note": (
                "a COMPLETED run is a no-op; regenerating its report would need "
                "an explicit regenerate_report flag, which is not implemented"
            ),
        }

    if not stored_task:
        return {
            "ok": False,
            "run_id": run_id,
            "error": {"error_code": "RUN_STORE_MISSING_TASK_SPEC",
                      "message": "no stored task spec to resume from"},
            "cst_contacted": False,
            "solved": False,
        }

    budget_before = rs.remaining_solve_budget(state)

    task = validate_task(stored_task)

    # A persisted plan is untrusted input.  V1.17 recovers it through
    # depsnapshot.rehydrate_plan, which prefers the stored plan and otherwise
    # rebuilds from the STORED dependency snapshot - never from the current
    # filesystem - and requires the rebuild to reproduce the stored hash.
    rehydration = ds.rehydrate_plan(state)
    plan_recovery = rehydration.get("plan_recovery") or {}

    if rehydration["verdict"] != ds.REHYDRATION_OK:
        error_code = (rehydration.get("error") or {}).get("error_code")
        legacy = bool(rehydration.get("legacy"))

        # V1.16 contract, kept for run stores written before dependency
        # snapshots existed: an incomplete legacy plan that cannot be rebuilt to
        # the same hash is reported as STALE_OR_FOREIGN_RUN_STORE.  The precise
        # V1.17 reason is carried both in the detail and in a top-level
        # `rehydration` block, so the specific code is never hidden.
        outer_code = (
            "STALE_OR_FOREIGN_RUN_STORE" if legacy else error_code
        )

        return {
            "ok": False,
            "run_id": run_id,
            "error": {
                "error_code": outer_code,
                "message": (rehydration.get("error") or {}).get("message"),
                "detail": {
                    **plan_recovery,
                    "rehydration_error_code": error_code,
                    "legacy_run_store": legacy,
                },
            },
            "rehydration": {
                "verdict": rehydration["verdict"],
                "source": rehydration.get("source"),
                "error_code": error_code,
                "legacy_run_store": legacy,
                "detail": (rehydration.get("error") or {}).get("detail"),
            },
            "plan_recovery": plan_recovery,
            "cst_contacted": False,
            "solved": False,
        }

    plan_for_resume = rehydration["plan"]

    # Environment checks the stored plan cannot answer by itself: are the reused
    # artifacts still the same bytes, and can this plugin still honour the plan?
    # A failure BLOCKS; it never re-solves.
    rehydration_check = ds.verify_rehydration(state)
    rehydration_summary = ds.summarise_rehydration(rehydration)

    if rehydration_check["status"] != ds.REHYDRATION_OK:
        check_legacy = bool(rehydration_check.get("legacy"))
        check_code = rehydration_check.get("error_code")

        return {
            "ok": False,
            "run_id": run_id,
            "error": {
                "error_code": check_code,
                "message": rehydration_check.get("message"),
                "detail": rehydration_check.get("checks"),
            },
            "rehydration": {
                "verdict": rehydration_check["status"],
                "source": rehydration.get("source"),
                "error_code": check_code,
                "legacy_run_store": check_legacy,
                "detail": rehydration_check.get("checks"),
            },
            "plan_recovery": plan_recovery,
            "cst_contacted": False,
            "solved": False,
        }

    # The config a run was created under is frozen with it.  Editing
    # cst_ai_config.json afterwards must not move an existing run; a deliberate
    # config change is a NEW run, not a resume.
    config_snapshot = state.get("resolved_config_snapshot")
    snapshot_store = ds.config_setting(config_snapshot, cfg.RUN_STORE_KEY)
    snapshot_store_path = (
        snapshot_store.get("resolved_path")
        if isinstance(snapshot_store, dict) else None
    )
    config_source = (
        "STORED_RESOLVED_CONFIG_SNAPSHOT"
        if snapshot_store_path
        else "CURRENT_CONFIG_LEGACY_RUN"
    )

    orche = Orchestrator()
    resume_state = {
        "run_id": run_id,
        "checkpoint": checkpoint,
        "checkpoint_point_index": state.get("checkpoint_point_index"),
        "plan": plan_for_resume,
        "solve_usage": dict(state.get("solver_usage") or {}),
        # V1.18: a sweep resumes at the first point that was NOT persisted as
        # complete.  Without these the resumed process would restart the sweep
        # from point 0 and spend a solve on a point that is already solved.
        "points_completed": int(state.get("points_completed") or 0),
        "point_states": list(state.get("point_states") or []),
    }

    performed_before = int(
        (state.get("solver_usage") or {}).get("performed") or 0
    )
    points_before = int(state.get("points_completed") or 0)

    # V1.18: a resumed task that needs CST must probe the SAME runtime the
    # original run declared.  With no probe at all the preflight answers
    # UNDECIDED for `runtime_cst_availability`, the orchestrator blocks before it
    # dispatches a single stage, and a sweep resume silently does nothing -
    # acceptance C caught exactly that.  The probe is V1.9A read-only discovery;
    # it starts nothing and is the same call an ordinary `run` makes.
    resume_runtime_file = None

    if isinstance(stored_task.get("cst"), dict):
        declared_runtime = stored_task["cst"].get("runtime_file")

        if declared_runtime:
            resume_runtime_file = Path(declared_runtime)

    result = orche.run(
        task,
        resume_from=resume_state,
        execute_stages=True,
        runtime_file=resume_runtime_file,
        reuse_index=state.get("reuse_index"),
    )

    plan_stages = (plan_for_resume or {}).get("stage_order") or []

    if orche.points_total:
        # a sweep's checkpoint describes one point, so a stage-level guess would
        # misreport what this process did; the orchestrator recorded it instead
        skipped = list(orche.stages_resumed_skipped)
        executed = list(orche.stages_dispatched)
    else:
        skipped = [s for s in plan_stages if orche._skip_for_resume(s)]
        executed = [s for s in plan_stages if not orche._skip_for_resume(s)]

    # a resume that re-solves an already-solved point is the one failure this
    # round must never report as success, so the count is measured, not asserted
    fresh_solve_count = (
        int(orche.solve_usage.get("performed") or 0) - performed_before
    )

    new_state = _state_from_orchestrator(
        orche, task, result,
        current_stage=orche.checkpoint,
        completed=list(state.get("completed_stages") or []) + executed,
        rehydration={
            k: state[k] for k in (
                ds.STATE_SCHEMA_VERSION_KEY, "api_revision", "reuse_index",
                "dependency_snapshot", "capability_snapshot",
                "resolved_config_snapshot",
            ) if k in state
        } or None,
    )
    new_state["solver_usage"] = orche.solve_usage

    store.save_state(run_id, new_state)
    store.append_event(
        run_id, stage="RESUME", event="RESUMED",
        message=f"skipped {skipped}, executed {executed}",
        detail={"checkpoint": checkpoint, "skipped": skipped,
                "executed": executed,
                "rehydration_source": rehydration.get("source")},
    )

    budget_after = rs.remaining_solve_budget(new_state)

    return {
        "ok": True,
        "api_version": PUBLIC_API_VERSION,
        "api_revision": PUBLIC_API_REVISION,
        "run_id": run_id,
        "resume_result": "RESUMED",
        "checkpoint_before": checkpoint,
        "checkpoint_after": orche.checkpoint,
        "stages_skipped": skipped,
        "stages_executed": executed,
        "stages_reuse_skipped": list(orche.stages_reuse_skipped),
        "stages_resumed_skipped": list(orche.stages_resumed_skipped),
        "fresh_solve_count": fresh_solve_count,
        "points_completed_before": points_before,
        "points_completed_after": int(orche.points_completed or 0),
        "points_total": int(orche.points_total or 0),
        "point_states": [dict(p) for p in (orche.point_states or [])],
        "solve_budget_before": budget_before,
        "solve_budget_after": budget_after,
        "budget_reset_on_resume": (
            budget_after.get("authorized") != budget_before.get("authorized")
        ),
        "task_integrity": integrity,
        "plan_recovery": plan_recovery,
        "rehydration": rehydration_summary,
        "rehydration_checks": rehydration_check.get("checks"),
        "config_source": config_source,
        "config_snapshot_store_path": snapshot_store_path,
        "config_snapshot_hash": (
            config_snapshot or {}
        ).get("snapshot_hash") if isinstance(config_snapshot, dict) else None,
        "store_root": str(store.root),
        "cst_contacted": False,
        "solved": False,
    }


# --------------------------------------------------------------- API surface


def public_api() -> dict:
    """The documented public surface, for the ledger and the gate."""

    return {
        "PUBLIC_API_V1": list(PUBLIC_API_V1),
        "version": PUBLIC_API_VERSION,
        "revision": PUBLIC_API_REVISION,
        "surface_unchanged_since": "1.16.0",
        "internal_module_names_exposed": False,
        "users_request_capabilities_not_modules": True,
        "capability_ids": caps.ids(),
        "durable_rehydration": {
            "dependency_snapshot_version": ds.DEPENDENCY_SNAPSHOT_VERSION,
            "state_schema_version": ds.STATE_SCHEMA_VERSION,
            "plan_dependency_manifest_entries": len(ds.PLAN_DEPENDENCY_MANIFEST),
            "hash_relaxation_used": False,
            "solves_on_rehydration": 0,
        },
        "error": (
            "internal implementation names such as the round tags are provenance "
            "only and are not part of this API"
        ),
    }
