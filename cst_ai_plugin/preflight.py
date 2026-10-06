"""CST_AI plugin layer — preflight.

Everything that must be true BEFORE a destructive or expensive step is checked
here, so a run can never fail half way through for a reason that was knowable at
the start.

Returns READY / BLOCKED / UNDECIDED.  UNDECIDED is used when the answer depends
on something the plugin is not allowed to guess (e.g. which of several live CST
sessions the user means).
"""

from __future__ import annotations

from pathlib import Path

from . import capabilities as caps
from .planner import PLAN_BLOCKED, plan_task

PREFLIGHT_VERSION = "1.15.0"

READY = "READY"
BLOCKED = "BLOCKED"
UNDECIDED = "UNDECIDED"


def _check_runtime(plan: dict, runtime_probe: dict | None) -> dict:
    """Uses the V1.9A discovery result; this layer never re-implements it."""

    if not plan["CST_CONNECTION_REQUIRED"]:
        return {
            "check": "runtime_cst_availability",
            "status": "NOT_REQUIRED",
            "detail": "the plan does not contact CST",
        }

    if runtime_probe is None:
        return {
            "check": "runtime_cst_availability",
            "status": UNDECIDED,
            "detail": "no runtime probe was supplied",
        }

    status = runtime_probe.get("status")

    from .errors import (
        CSTUnavailable,
        MultipleLiveDesignEnvironments,
        StaleRuntimePid,
    )

    if status == "MULTIPLE_LIVE_DESIGN_ENVIRONMENTS":
        raise MultipleLiveDesignEnvironments(
            "more than one live DesignEnvironment; the plugin will not guess "
            "which one the user meant",
            stage="PREFLIGHT",
            detail=runtime_probe,
        )

    if status == "NO_LIVE_DESIGN_ENVIRONMENT":
        raise CSTUnavailable(
            "no live DesignEnvironment found", stage="PREFLIGHT",
            detail=runtime_probe,
        )

    if status == "RUNTIME_FILE_MISSING":
        raise StaleRuntimePid(
            "the runtime file is missing and no live DE was recovered",
            stage="PREFLIGHT",
            detail=runtime_probe,
        )

    return {
        "check": "runtime_cst_availability",
        "status": READY,
        "pid": runtime_probe.get("pid"),
        "resolution": status,
        "single_live_de": True,
    }


def _check_capabilities(plan: dict) -> dict:
    missing = []

    for stage in plan["stages"]:
        for cid in stage["capabilities"]:
            if cid not in caps.CAPABILITIES:
                missing.append({"stage": stage["stage"], "capability": cid})

    return {
        "check": "capability_availability",
        "status": BLOCKED if missing else READY,
        "missing": missing,
        "registry_size": len(caps.CAPABILITIES),
    }


def _check_budget(plan: dict) -> dict:
    planned = plan["PLANNED_SOLVE_COUNT"]
    budget = plan["MAX_AUTHORIZED_SOLVES"]

    return {
        "check": "solve_budget",
        "status": READY if planned <= budget else BLOCKED,
        "PLANNED_SOLVE_COUNT": planned,
        "MAX_AUTHORIZED_SOLVES": budget,
        "reason": None if planned <= budget else "budget exceeded before solve",
    }


def _check_output_dir(task: dict) -> dict:
    out = task.get("output_dir")

    if not out:
        return {
            "check": "disk_output_destination",
            "status": BLOCKED,
            "reason": "no output_dir",
        }

    path = Path(out)

    return {
        "check": "disk_output_destination",
        "status": READY,
        "path": str(path),
        "exists": path.exists(),
        "will_create": not path.exists(),
    }


def _check_project_state(task: dict, plan: dict, active_project: str | None) -> dict:
    """Creating a TEST project must never silently close the user's project."""

    creating = bool(plan["projects_created"])

    if not creating:
        return {
            "check": "project_state",
            "status": READY,
            "creates_project": False,
            "active_project": active_project,
        }

    return {
        "check": "project_state",
        "status": READY,
        "creates_project": True,
        "active_project": active_project,
        "would_disturb_open_project": bool(active_project),
        "policy": (
            "a task that creates a project declares it; the plugin never closes "
            "a project it did not open"
        ),
    }


def _check_reuse(plan: dict, task: dict) -> dict:
    solved_key = (task.get("inputs") or {}).get("solved_artifact_key")
    reused = plan["REUSED_SOLVED_ARTIFACTS"]

    return {
        "check": "result_reuse_eligibility",
        "status": READY,
        "declared_key": solved_key,
        "reuse_hit": bool(reused),
        "reuses": [r.get("artifact") if isinstance(r, dict) else r for r in reused],
    }


def preflight(
    task: dict,
    *,
    runtime_probe: dict | None = None,
    active_project: str | None = None,
    reuse_index: dict | None = None,
) -> dict:
    """Run every check.  Raises a typed error for a hard stop."""

    plan = plan_task(task, reuse_index)

    checks = []

    runtime_check = _check_runtime(plan, runtime_probe)
    checks.append(runtime_check)
    checks.append(_check_capabilities(plan))
    budget_check = _check_budget(plan)
    checks.append(budget_check)
    checks.append(_check_output_dir(task))
    checks.append(_check_project_state(task, plan, active_project))
    checks.append(_check_reuse(plan, task))

    if plan["status"] == PLAN_BLOCKED:
        checks.append(
            {
                "check": "plan_status",
                "status": BLOCKED,
                "risks_and_blockers": plan["risks_and_blockers"],
            }
        )

    statuses = {c["status"] for c in checks}

    if BLOCKED in statuses:
        overall = BLOCKED
    elif UNDECIDED in statuses:
        overall = UNDECIDED
    else:
        overall = READY

    return {
        "preflight_version": PREFLIGHT_VERSION,
        "task_id": task["task_id"],
        "status": overall,
        "checks": checks,
        "PLANNED_SOLVE_COUNT": plan["PLANNED_SOLVE_COUNT"],
        "MAX_AUTHORIZED_SOLVES": plan["MAX_AUTHORIZED_SOLVES"],
        "executed_anything": False,
        "cst_contacted": False,
        "plan_hash": plan["execution_plan_hash"],
    }
