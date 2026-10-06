"""CST_AI plugin layer — planner.

The planner PLANS.  It never executes, never connects to CST, and never triggers
a solve.  It is a pure function of (task, registry snapshot).

It must know the zero-solve paths, otherwise it would schedule a solve for work
that is already finished:

    existing solved project + farfield readback  -> no CST, no solve
    existing sweep result + comparator           -> no CST at all
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from . import capabilities as caps
from .errors import CapabilityMissing, TaskSchemaError
from .task import (
    ALLOW_ONE,
    ALLOW_UP_TO_N,
    FORBID,
    REUSE_ONLY,
    STAGE_CAPABILITIES,
    authorized_solve_budget,
    task_spec_hash,
    validate_task,
)

PLANNER_VERSION = "1.15.0"

PLAN_READY = "READY"
PLAN_BLOCKED = "BLOCKED"
PLAN_UNDECIDED = "UNDECIDED"


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def canonical_hash(value) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(value, sort_keys=True, default=repr).encode("utf-8")
    ).hexdigest()


def execution_plan_hash(plan: dict) -> str:
    return canonical_hash(
        {
            "task_id": plan["task_id"],
            "stages": [s["stage"] for s in plan["stages"]],
            "solve_count": plan["PLANNED_SOLVE_COUNT"],
            "projects_created": plan["projects_created"],
            "reuse": plan["reuse_decisions"],
        }
    )


def _reuse_lookup(reuse_index: dict, key: str) -> dict | None:
    return (reuse_index or {}).get(key)


#: a point may name the fixture signature it was built from; reuse is only
#: granted when the artifact's recorded signature equals it
SIGNATURE_FIELDS = ("reuse_signature", "fixture_signature")


def declared_signature(point: dict, task: dict) -> str | None:
    """The fixture signature a point declares for itself, if any."""

    for container in (point, task.get("inputs") or {}, task.get("spec") or {}):
        if not isinstance(container, dict):
            continue

        for key in SIGNATURE_FIELDS:
            value = container.get(key)

            if isinstance(value, str) and value:
                return value

    return None


def plan_points(task: dict, reuse_index: dict) -> list[dict]:
    """Per-point SOLVE decisions for a sweep-shaped task.

    One global reuse decision cannot describe a sweep that reuses one point and
    solves the others, so the decision is taken per point.

    Reuse is granted only when the caller supplied a matching solved key AND the
    artifact's recorded signature equals the signature the point declares.  A
    signature that is declared but absent on either side is a mismatch: the point
    is never forced onto a possibly-foreign result, it is planned as a solve
    instead.  A point that declares no signature keeps the V1.15 semantics of a
    bare `solved_artifact_key` lookup.
    """

    spec = task.get("spec") or {}
    inputs = task.get("inputs") or {}
    points = spec.get("points") or []
    decisions = []

    for index, raw in enumerate(points):
        point = raw if isinstance(raw, dict) else {}

        key = point.get("solved_artifact_key") or inputs.get("solved_artifact_key")
        hit = _reuse_lookup(reuse_index, key) if key else None

        fresh = bool(
            point.get("require_fresh_solve") or inputs.get("require_fresh_solve")
        )

        expected = declared_signature(point, task)
        actual = hit.get("signature") if isinstance(hit, dict) else None

        if expected is None:
            signature_verified = None
        else:
            signature_verified = bool(hit) and expected == actual

        deny = None
        reuse = None

        if hit and fresh:
            deny = "REQUIRE_FRESH_SOLVE"
        elif hit and signature_verified is False:
            deny = "SIGNATURE_MISMATCH"
        elif hit:
            reuse = hit

        decisions.append(
            {
                "index": index,
                "point_id": point.get("point_id") or f"point_{index}",
                "parameters": point.get("parameters"),
                "project_path": (
                    point.get("project_path") or spec.get("project_path")
                ),
                "solved_artifact_key": key,
                "solve_allowed": reuse is None,
                "reuse": reuse,
                "reuse_denied_reason": deny,
                "signature_expected": expected,
                "signature_actual": actual,
                "signature_verified": signature_verified,
                "declared_parameters_hash": (
                    canonical_hash(point.get("parameters"))
                    if point.get("parameters") is not None else None
                ),
            }
        )

    return decisions


def plan_task(task: dict, reuse_index: dict | None = None) -> dict:
    """Build an execution plan.  Pure: no CST, no filesystem writes, no solve."""

    # a plan is only ever built from a VALID task, so planning can never
    # sidestep the schema checks
    task = validate_task(task)
    reuse_index = reuse_index or {}

    task_id = task["task_id"]
    policy = task["solve_policy"]
    budget = authorized_solve_budget(task)

    stages = []
    planned_solves = 0
    reused = []
    projects_created = []
    projects_touched = []
    blockers = []
    needs_cst = False

    inputs = task.get("inputs") or {}
    existing_project = inputs.get("project_path") or (task.get("spec") or {}).get("project_path")
    existing_results = bool(inputs.get("results_exist"))

    #: a sweep-shaped task decides SOLVE per point, before the stage loop, so
    #: BUILD can tell which points actually mint a project
    point_decisions = plan_points(task, reuse_index)

    # ------------------------------------------------ per-stage planning
    for index, stage in enumerate(task["stages"]):
        stage_caps = list(STAGE_CAPABILITIES.get(stage, ()))
        dependencies = list(task["stages"][:index])

        entry = {
            "stage": stage,
            "capabilities": stage_caps,
            "depends_on": dependencies,
            "executes": False,  # the planner must never run anything
            "solve_allowed": False,
        }

        for cid in stage_caps:
            try:
                cap = caps.get(cid)
            except CapabilityMissing as exc:
                blockers.append({"stage": stage, "reason": exc.message})
                continue

            if cap["requires_cst"]:
                needs_cst = True

        if stage == "BUILD":
            if point_decisions:
                # a reused point already owns a project on disk; only the solved
                # points mint one, and each mints its own
                created = [
                    str(d["project_path"]) for d in point_decisions
                    if d["solve_allowed"] and d["project_path"]
                ]

                entry["creates_projects"] = created
                entry["points_reused_without_building"] = [
                    d["point_id"] for d in point_decisions
                    if not d["solve_allowed"]
                ]
                projects_created.extend(created)
            else:
                target = inputs.get("project_path") or (task.get("spec") or {}).get("project_path")

                if target:
                    entry["creates_project"] = str(target)
                    projects_created.append(str(target))
                else:
                    blockers.append(
                        {"stage": stage, "reason": "no project_path in inputs"}
                    )

        if stage == "SOLVE":
            # the zero-solve branch: an already-solved artifact is reused
            solved_key = inputs.get("solved_artifact_key")

            if point_decisions:
                # a sweep decides per point: one point may be reused while the
                # others are solved, which no single global decision can express
                entry["points"] = point_decisions
                entry["solve_allowed"] = any(
                    d["solve_allowed"] for d in point_decisions
                )
                entry["points_reused"] = [
                    d["point_id"] for d in point_decisions if d["reuse"]
                ]
                entry["points_denied_reuse"] = [
                    {"point_id": d["point_id"], "reason": d["reuse_denied_reason"]}
                    for d in point_decisions if d["reuse_denied_reason"]
                ]

                for decision in point_decisions:
                    if decision["reuse"] is not None:
                        reused.append(decision["reuse"])
                    else:
                        planned_solves += 1

            elif policy == REUSE_ONLY:
                hit = _reuse_lookup(reuse_index, solved_key)

                if hit:
                    entry["solve_allowed"] = False
                    entry["reused_instead_of_solving"] = hit
                    reused.append(hit)
                else:
                    blockers.append(
                        {
                            "stage": stage,
                            "reason": "REUSE_ONLY but no matching solved artifact",
                        }
                    )
            elif policy == FORBID:
                entry["solve_allowed"] = False
                blockers.append(
                    {"stage": stage, "reason": "SOLVE stage under FORBID policy"}
                )
            else:
                solved_key = inputs.get("solved_artifact_key")
                hit = _reuse_lookup(reuse_index, solved_key) if solved_key else None

                if hit and not inputs.get("require_fresh_solve"):
                    entry["solve_allowed"] = False
                    entry["reused_instead_of_solving"] = hit
                    reused.append(hit)
                else:
                    entry["solve_allowed"] = True
                    planned_solves += 1

        if stage in ("EXTRACT", "ANALYZE", "REPORT"):
            # these read stored results; they never need CST themselves
            entry["reads_existing_results"] = True

        stages.append(entry)

    # --------------------------------------- explicit zero-solve paths
    zero_solve_paths = []

    if task["task_kind"] == "LOAD_EXISTING_RESULT":
        zero_solve_paths.append(
            {
                "path": "LOAD_EXISTING_RESULT -> EXTRACT -> REPORT",
                "requires_cst": False,
                "reuses": existing_project,
                "why": "the result already exists; re-solving would be waste",
            }
        )

    if task["task_kind"] == "COMPARE_ONLY":
        zero_solve_paths.append(
            {
                "path": "ANALYZE(comparator) -> REPORT",
                "requires_cst": False,
                "why": "the comparator reads frozen artifacts only",
            }
        )

    if inputs.get("farfield_readback_only") or (
        (task.get("spec") or {}).get("extract_mode") in
        ("radiator", "radiator_farfield")
    ):
        zero_solve_paths.append(
            {
                "path": "CONNECT(select farfield) -> EXTRACT -> REPORT",
                "requires_cst": True,
                "solve_count": 0,
                "why": (
                    "SelectTreeItem activates an EXISTING farfield result; this "
                    "is a read, not a solve (V1.11A)"
                ),
            }
        )

    if point_decisions:
        reused_points = [d for d in point_decisions if d["reuse"]]
        new_points = [d for d in point_decisions if d["solve_allowed"]]

        for decision in reused_points:
            zero_solve_paths.append(
                {
                    "path": f"point {decision['point_id']}: REUSE (no BUILD, no SOLVE)",
                    "requires_cst": False,
                    "solve_count": 0,
                    "reuses": (decision["reuse"] or {}).get("artifact"),
                    "why": (
                        "the point's solved artifact was supplied in the reuse "
                        "index and its declared signature matched the artifact's "
                        "recorded signature; re-solving it would duplicate work"
                        if decision["signature_verified"] else
                        "the point's solved artifact was supplied in the reuse "
                        "index and the point declares no signature to check it "
                        "against, which keeps the V1.15 lookup semantics"
                    ),
                }
            )

    # a plan may not exceed the authorised budget
    if planned_solves > budget:
        blockers.append(
            {
                "stage": "SOLVE",
                "reason": (
                    f"planned {planned_solves} solves exceeds the authorised "
                    f"budget {budget}"
                ),
            }
        )

    plan = {
        "planner_version": PLANNER_VERSION,
        "task_id": task_id,
        "task_spec_hash": task_spec_hash(task),
        "generated_at_utc": utc_now(),
        "stages": stages,
        "stage_order": [s["stage"] for s in stages],
        "dependencies": {s["stage"]: s["depends_on"] for s in stages},
        "CST_CONNECTION_REQUIRED": needs_cst,
        "projects_touched": sorted(
            set(
                projects_touched + projects_created
                + [
                    str(d["project_path"]) for d in point_decisions
                    if d["project_path"]
                ]
                + [
                    str((d["reuse"] or {}).get("artifact"))
                    for d in point_decisions
                    if (d["reuse"] or {}).get("artifact")
                ]
            )
        ),
        "projects_created": sorted(set(projects_created)),
        "PLANNED_SOLVE_COUNT": planned_solves,
        "MAX_AUTHORIZED_SOLVES": budget,
        "REUSED_SOLVED_ARTIFACTS": reused,
        "reuse_decisions": [
            {
                "key": (r.get("key") if isinstance(r, dict) else str(r)),
                "artifact": (r.get("artifact") if isinstance(r, dict) else None),
            }
            for r in reused
        ],
        "expected_outputs": list(task.get("expected_outputs") or []),
        # a sweep states its reuse/new split up front, before any solve happens,
        # so the budget can be checked against what the run intends to do
        "SWEEP": (
            {
                "point_count": len(point_decisions),
                "reused_points": [
                    {
                        "point_id": d["point_id"],
                        "artifact": (d["reuse"] or {}).get("artifact"),
                        "signature_expected": d["signature_expected"],
                        "signature_actual": d["signature_actual"],
                        "signature_verified": d["signature_verified"],
                    }
                    for d in point_decisions if d["reuse"]
                ],
                "new_points": [
                    {
                        "point_id": d["point_id"],
                        "project_path": d["project_path"],
                        "parameters": d["parameters"],
                        "parameters_hash": d["declared_parameters_hash"],
                    }
                    for d in point_decisions if d["solve_allowed"]
                ],
                "reuse_denied": [
                    {"point_id": d["point_id"], "reason": d["reuse_denied_reason"]}
                    for d in point_decisions if d["reuse_denied_reason"]
                ],
                "planned_solve_count": planned_solves,
                "reused_point_count": len(reused_points),
                "new_point_count": len(new_points),
            }
            if point_decisions else None
        ),
        "zero_solve_paths": zero_solve_paths,
        "risks_and_blockers": blockers,
        "solve_policy": policy,
        "within_budget": planned_solves <= budget,
        "executed_anything": False,
        "cst_contacted": False,
    }

    plan["status"] = (
        PLAN_BLOCKED
        if blockers
        else (PLAN_READY if plan["within_budget"] else PLAN_BLOCKED)
    )
    plan["execution_plan_hash"] = execution_plan_hash(plan)

    return plan


def plan_and_refuse_to_execute(task: dict, reuse_index: dict | None = None) -> dict:
    """Convenience wrapper that makes the no-execution contract explicit."""

    plan = plan_task(task, reuse_index)

    if plan["executed_anything"] or plan["cst_contacted"]:
        raise TaskSchemaError("planner violated its no-execution contract")

    return plan
