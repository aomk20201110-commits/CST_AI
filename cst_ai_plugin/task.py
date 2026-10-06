"""CST_AI plugin layer — declarative TaskSpec.

Minimal by design: it can only express what the verified capabilities do.  There
is no DSL here, and unknown fields are REJECTED so a typo can never silently
change a run.

Every task that could touch the solver must carry an explicit solve_policy.
There is no hidden "and while we are in there, solve" behaviour.
"""

from __future__ import annotations

import hashlib
import json

from .errors import TaskSchemaError

TASK_SCHEMA_VERSION = "1.15.0"

# ---------------------------------------------------------------- solve policies
FORBID = "FORBID"
ALLOW_ONE = "ALLOW_ONE"
ALLOW_UP_TO_N = "ALLOW_UP_TO_N"
REUSE_ONLY = "REUSE_ONLY"

SOLVE_POLICIES = (FORBID, ALLOW_ONE, ALLOW_UP_TO_N, REUSE_ONLY)

# ---------------------------------------------------------------- stage names
STAGES = (
    "CONNECT",
    "BUILD",
    "VERIFY",
    "SOLVE",
    "EXTRACT",
    "ANALYZE",
    "REPORT",
)

#: which capabilities each stage is allowed to use
STAGE_CAPABILITIES = {
    "CONNECT": ("CST_RUNTIME_CONNECT", "SOLVER_STATUS"),
    "BUILD": ("MODEL_BUILD",),
    "VERIFY": ("DRY_BUILD",),
    "SOLVE": ("SOLVE",),
    "EXTRACT": (
        "S_PARAMETERS",
        "FLOQUET_MULTIMODE",
        "POWER_RTA",
        "MESH_CONVERGENCE",
        "FREQUENCY_CONVERGENCE",
        "FARFIELD_COMPLEX_FIELD",
        "DIRECTIVITY",
        "GAIN",
        "REALIZED_GAIN",
        "BEAM_DIRECTION",
        "HPBW",
    ),
    "ANALYZE": ("RESONANCE_F0", "FWHM", "Q_LINEWIDTH", "PARAMETER_SWEEP",
                "REFERENCE_COMPARATOR"),
    "REPORT": (),
}

TOP_LEVEL_FIELDS = {
    "task_id",
    "task_kind",
    "label",
    "stages",
    "solve_policy",
    "max_solves",
    "spec",
    "inputs",
    "expected_outputs",
    "output_dir",
    "cst",
    "notes",
}

TASK_KINDS = (
    "BUILD_ONLY",
    "BUILD_AND_SOLVE",
    "LOAD_EXISTING_RESULT",
    "PARAMETER_SWEEP",
    "COMPARE_ONLY",
)


def canonical_hash(value) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(value, sort_keys=True, default=repr).encode("utf-8")
    ).hexdigest()


def validate_task(task: dict) -> dict:
    """Reject anything the plugin cannot actually honour."""

    if not isinstance(task, dict):
        raise TaskSchemaError("task must be a mapping")

    unknown = sorted(set(task) - TOP_LEVEL_FIELDS)

    if unknown:
        raise TaskSchemaError(
            f"unknown task field(s): {unknown}; allowed: "
            f"{sorted(TOP_LEVEL_FIELDS)}"
        )

    for required in ("task_id", "task_kind", "stages", "solve_policy"):
        if required not in task:
            raise TaskSchemaError(f"missing required field: {required}")

    if task["task_kind"] not in TASK_KINDS:
        raise TaskSchemaError(
            f"unknown task_kind {task['task_kind']!r}; allowed: {list(TASK_KINDS)}"
        )

    if task["solve_policy"] not in SOLVE_POLICIES:
        raise TaskSchemaError(
            f"unknown solve_policy {task['solve_policy']!r}; allowed: "
            f"{list(SOLVE_POLICIES)}"
        )

    stages = task["stages"]

    if not isinstance(stages, (list, tuple)) or not stages:
        raise TaskSchemaError("stages must be a non-empty list")

    bad = [s for s in stages if s not in STAGES]

    if bad:
        raise TaskSchemaError(f"unknown stage(s): {bad}; allowed: {list(STAGES)}")

    if list(stages) != sorted(set(stages), key=list(STAGES).index):
        raise TaskSchemaError("stages must be unique and in canonical order")

    # an output directory is mandatory: no task may write somewhere implicit
    if not task.get("output_dir"):
        raise TaskSchemaError("output_dir is required")

    if task["solve_policy"] == ALLOW_UP_TO_N:
        n = task.get("max_solves")

        if not isinstance(n, int) or n < 1:
            raise TaskSchemaError("ALLOW_UP_TO_N requires max_solves >= 1")

    if task["solve_policy"] in (FORBID, REUSE_ONLY) and task.get("max_solves"):
        raise TaskSchemaError(
            f"{task['solve_policy']} must not declare max_solves"
        )

    if "SOLVE" in stages and task["solve_policy"] == FORBID:
        # a stage that can never be allowed is a contradiction, not a policy.
        # REUSE_ONLY is NOT a contradiction: the SOLVE stage stays in the plan
        # and the planner converts it into a reuse decision.
        raise TaskSchemaError(
            "stages include SOLVE but solve_policy is FORBID"
        )

    return dict(task)


def authorized_solve_budget(task: dict) -> int:
    """The ONLY place a solve count is authorised."""

    policy = task["solve_policy"]

    if policy in (FORBID, REUSE_ONLY):
        return 0

    if policy == ALLOW_ONE:
        return 1

    if policy == ALLOW_UP_TO_N:
        return int(task["max_solves"])

    raise TaskSchemaError(f"unhandled solve_policy {policy!r}")


def make_task(**kwargs) -> dict:
    task = dict(kwargs)
    task.setdefault("task_schema_version", TASK_SCHEMA_VERSION)
    task.pop("task_schema_version", None)  # not a user field
    return validate_task(task)


def task_spec_hash(task: dict) -> str:
    return canonical_hash(
        {
            "task_id": task["task_id"],
            "task_kind": task["task_kind"],
            "stages": list(task["stages"]),
            "solve_policy": task["solve_policy"],
            "max_solves": task.get("max_solves"),
            "spec": task.get("spec"),
            "inputs": task.get("inputs"),
            "expected_outputs": task.get("expected_outputs"),
        }
    )
