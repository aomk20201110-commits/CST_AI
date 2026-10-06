"""V1.17 tool-surface adapter — the CST_AI tools, as the harness sees them.

This is deliberately a *thin adapter*.  It is the only entry point the DSH tool
surface uses, and it is allowed to call exactly one thing: ``cst_ai_plugin.public``.

It must never:
  * import an internal module (`cst_session_discovery_v19a`, `cst_result_api_v19`,
    `v110c_analysis`, `v19b_real_solve`, `Orchestrator._stage_*`, runstore internals)
  * connect to CST, build, solve, extract or write a CST project itself
  * shell out to `cst_cli.py` (that would add a second, fragile parsing layer)

Those restrictions are enforced by a static check, not by this comment.

Two more contracts live here rather than in the plugin layer:

  errors   every response is a JSON object carrying
           ok / error_type / stage / message / detail / run_id, so a failure
           reaches the caller as data instead of a "tool failed" string.
           The plugin's structured errors are passed through unchanged.

  size     a solve produces 1001-point S-parameter curves and a farfield readback
           produces 2664 points.  Those belong in artifacts, not in a tool
           response, so responses carry summaries plus artifact ids, paths and
           hashes, and INSPECT reads one section at a time.

Nothing in this file runs a solve.
"""

from __future__ import annotations

import base64
import json
import sys
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from cst_ai_plugin import public  # noqa: E402  (the only plugin entry point)

TOOL_SURFACE_VERSION = "1.17.0"

ACTIONS = ("plan", "run", "status", "inspect", "resume")

#: requirement: never dump a 1001-point curve or a 2664-point farfield grid into
#: a tool response.  These cap the *metadata* we return, not the artifacts.
MAX_EVENTS = 40
MAX_LIST = 50

INSPECT_SECTIONS = (
    "summary",
    "plan",
    "artifacts",
    "observables",
    "errors",
    "events",
    "all",
)


# --------------------------------------------------------------------- envelope
def _envelope_ok(**payload) -> dict:
    return {
        "ok": True,
        "tool_surface_version": TOOL_SURFACE_VERSION,
        "public_api_version": public.PUBLIC_API_VERSION,
        "api_revision": getattr(public, "PUBLIC_API_REVISION", None),
        **payload,
    }


def _envelope_error(
    error_type: str,
    *,
    stage: str | None = None,
    message: str = "",
    detail=None,
    run_id: str | None = None,
    traceback_text: str | None = None,
) -> dict:
    """The one error shape every tool returns.

    `traceback_text` is kept out of the response body: it is written to a log
    artifact by the caller instead, because a stack trace is a debugging aid and
    not a normal user-facing result.
    """

    return {
        "ok": False,
        "tool_surface_version": TOOL_SURFACE_VERSION,
        "error_type": error_type,
        "stage": stage,
        "message": message,
        "detail": detail,
        "run_id": run_id,
        "traceback_in_response": False,
        "traceback_artifact": traceback_text,
    }


def _normalise_public_failure(result: dict, *, run_id: str | None = None) -> dict | None:
    """Turn a `{"ok": False}` public-API payload into the tool error envelope.

    `public.plan` reports validation failures under `errors`, `public.resume`
    under `error`, and `public.status` under `error`.  All three are already
    structured; this only relabels them so the caller sees one shape.
    """

    if not isinstance(result, dict):
        return _envelope_error(
            "MALFORMED_PUBLIC_RESPONSE",
            message=f"the public API returned {type(result).__name__}, not a dict",
            detail={"repr": repr(result)[:400]},
            run_id=run_id,
        )

    # STATUS and INSPECT return the durable payload itself, with no `ok` key: a
    # missing `ok` is success, not failure.  Only an explicit ok=False is an error.
    if "ok" not in result:
        return None

    if result.get("ok") is True:
        return None

    if isinstance(result.get("error"), dict):
        err = result["error"]
        return _envelope_error(
            err.get("error_code") or err.get("error_type") or "PUBLIC_API_ERROR",
            stage=err.get("stage"),
            message=err.get("message") or "",
            detail=err.get("detail"),
            run_id=result.get("run_id") or run_id,
        )

    if result.get("error"):
        return _envelope_error(
            "PUBLIC_API_ERROR",
            message=str(result["error"]),
            run_id=result.get("run_id") or run_id,
        )

    errors = result.get("errors")

    if isinstance(errors, list) and errors:
        first = errors[0] if isinstance(errors[0], dict) else {}

        # TaskSpec validation reports its precise code under `reason` (and the
        # offending field under `field`), not `error_code`.  Lifting both into
        # the envelope is what makes the failure self-describing to a user: a
        # bare "PUBLIC_API_ERROR: 1 error(s)" names neither the rule nor the
        # field that broke it.
        reason = first.get("reason") or first.get("error_code")
        field = first.get("field")
        message = first.get("message")

        if not message:
            if reason and field:
                message = f"{reason}: field {field!r}"
            elif reason:
                message = str(reason)
            else:
                message = f"{len(errors)} error(s)"

        return _envelope_error(
            reason or "PUBLIC_API_ERROR",
            stage=first.get("stage") or result.get("stage"),
            message=message,
            detail=errors[:10],
            run_id=result.get("run_id") or run_id,
        )

    # no explicit error field: an unsuccessful response with no reason is itself
    # the finding, so say so rather than inventing a cause
    return _envelope_error(
        "PUBLIC_API_ERROR",
        stage=result.get("stage"),
        message="the public API reported ok=False without an error_code",
        detail={"keys": sorted(result)},
        run_id=result.get("run_id") or run_id,
    )


# ----------------------------------------------------------------- task loading
def _load_task(request: dict) -> dict | None:
    """Accept an inline TaskSpec or a path to one, as required.

    A caller may pass either; nothing here lets a caller name an internal module.
    """

    if isinstance(request.get("task"), dict):
        return request["task"]

    path = request.get("task_path") or request.get("task")

    if isinstance(path, str) and path:
        return public.load_task(path)

    return None


def _task_identity(task: dict | None) -> dict:
    if not isinstance(task, dict):
        return {"present": False}

    return {
        "present": True,
        "task_id": task.get("task_id"),
        "task_kind": task.get("task_kind"),
        "stages": task.get("stages"),
        "solve_policy": task.get("solve_policy"),
        "expected_outputs": task.get("expected_outputs"),
    }


# ------------------------------------------------------------- result shrinking
def _events_view(events) -> dict:
    if not isinstance(events, list):
        return {"count": 0, "returned": 0, "events": [], "truncated": False}

    tail = events[-MAX_EVENTS:]

    return {
        "count": len(events),
        "returned": len(tail),
        "truncated": len(events) > len(tail),
        "events": tail,
    }


def _file_facts(path, known_hash=None) -> dict:
    """size/hash of the file behind an artifact path, when it is readable.

    The registry leaves `hash` null for artifacts that were never written to
    disk, so the digest is only computed for paths that actually resolve.
    """

    facts: dict = {}

    if not isinstance(path, str) or not path:
        return {"size_bytes": None, "sha256": known_hash}

    candidate = Path(path)

    if not candidate.is_absolute():
        candidate = REPO_ROOT / candidate

    if not candidate.is_file():
        return {"size_bytes": None, "sha256": known_hash}

    import hashlib

    facts["size_bytes"] = candidate.stat().st_size
    facts["sha256"] = known_hash or (
        "sha256:" + hashlib.sha256(candidate.read_bytes()).hexdigest()
    )

    return facts


def _artifacts_view(artifacts) -> dict:
    """Artifact metadata only — never the payload behind it.

    The registry names its fields `type` / `hash` / `producer`; the alias
    spellings are accepted as well, so the view survives either shape.
    """

    if isinstance(artifacts, dict):
        items = list(artifacts.values())
    elif isinstance(artifacts, list):
        items = artifacts
    else:
        return {"count": 0, "artifacts": []}

    view = []

    for item in items:
        if not isinstance(item, dict):
            continue

        entry = {
            "artifact_id": item.get("artifact_id"),
            "artifact_type": item.get("type") or item.get("artifact_type"),
            "path": item.get("path"),
            "sha256": item.get("hash") or item.get("sha256"),
            "producer": item.get("producer") or item.get("produced_by_stage"),
            "status": item.get("status"),
            "numerical_status": item.get("numerical_status"),
            "scientific_status": item.get("scientific_status"),
        }
        entry.update(_file_facts(entry["path"], entry["sha256"]))
        view.append(entry)

    return {"count": len(view), "artifacts": view[:MAX_LIST]}


def _observables_view(observables) -> dict:
    """Scalars stay; long arrays are replaced by their shape."""

    if not isinstance(observables, dict):
        return {"count": 0, "observables": {}}

    out = {}

    for key, value in list(observables.items())[:MAX_LIST]:
        out[key] = _shrink(value)

    return {"count": len(observables), "observables": out}


def _shrink(value, depth: int = 0):
    if depth > 3:
        return "<depth-limited>"

    if isinstance(value, dict):
        return {k: _shrink(v, depth + 1) for k, v in list(value.items())[:MAX_LIST]}

    if isinstance(value, (list, tuple)):
        return {
            "length": len(value),
            "first": _shrink(value[0], depth + 1) if value else None,
            "last": _shrink(value[-1], depth + 1) if value else None,
            "elided": True,
        }

    if isinstance(value, str) and len(value) > 300:
        return value[:300] + "…"

    return value


def _bundle_view(bundle) -> dict | None:
    """Artifact pointers, plus the hash of the durable bundle file.

    `write_bundle` returns only the two paths, so the digest is computed here
    rather than reported as an unexplained null.
    """

    if not isinstance(bundle, dict):
        return None

    view = {
        "json": bundle.get("json"),
        "markdown": bundle.get("markdown"),
    }

    path = bundle.get("json")

    if isinstance(path, str) and path:
        candidate = Path(path)

        if not candidate.is_absolute():
            candidate = REPO_ROOT / candidate

        if candidate.is_file():
            import hashlib

            view["sha256"] = "sha256:" + hashlib.sha256(
                candidate.read_bytes()
            ).hexdigest()
            view["size_bytes"] = candidate.stat().st_size

    return view


def _solve_declarations(plan: dict) -> dict:
    """The pre-solve declaration: which points reuse, which are new, how many solves.

    This is the per-point decision the plan makes *before* anything is solved,
    so a user can see the reuse/new split without reading an internal module.
    """

    if not isinstance(plan, dict):
        return {"SWEEP": None, "stage_solve_decisions": {}}

    solve_stages = {
        stage.get("stage"): {
            key: stage.get(key)
            for key in ("solve_allowed", "points", "points_reused",
                        "points_denied_reuse", "reused_instead_of_solving")
            if key in stage
        }
        for stage in plan.get("stages") or []
        if isinstance(stage, dict) and stage.get("stage") == "SOLVE"
    }

    return {
        "SWEEP": plan.get("SWEEP"),
        "stage_solve_decisions": solve_stages,
    }


def _plan_view(plan: dict) -> dict:
    if not isinstance(plan, dict):
        return {}

    return {
        "status": plan.get("status"),
        "stage_order": plan.get("stage_order"),
        "stage_count": plan.get("stage_count"),
        "execution_plan_hash": plan.get("execution_plan_hash"),
        "CST_CONNECTION_REQUIRED": plan.get("CST_CONNECTION_REQUIRED"),
        "PLANNED_SOLVE_COUNT": plan.get("PLANNED_SOLVE_COUNT"),
        "MAX_AUTHORIZED_SOLVES": plan.get("MAX_AUTHORIZED_SOLVES"),
        "REUSED_SOLVED_ARTIFACTS": plan.get("REUSED_SOLVED_ARTIFACTS"),
        "projects_created": plan.get("projects_created"),
        "projects_touched": plan.get("projects_touched"),
        "expected_outputs": plan.get("expected_outputs"),
        "zero_solve_paths": plan.get("zero_solve_paths"),
        "risks_and_blockers": plan.get("risks_and_blockers"),
        "reuse_decisions": plan.get("reuse_decisions"),
        **_solve_declarations(plan),
        "capabilities": {
            s.get("stage"): s.get("capabilities")
            for s in plan.get("stages") or []
        },
    }


# --------------------------------------------------------------------- actions
def action_plan(request: dict) -> dict:
    """ZERO SIDE EFFECT: no CST connection, no build, no solve, no project write."""

    task = _load_task(request)

    if task is None:
        return _envelope_error(
            "TASK_REQUIRED",
            stage="VALIDATION",
            message="PLAN needs a TaskSpec: pass `task` (object) or `task_path` (string)",
        )

    reuse_index = request.get("reuse_index")

    result = public.plan(
        task,
        store_root=request.get("store_root"),
        config_path=request.get("config_path"),
        reuse_index=reuse_index,
    )

    failure = _normalise_public_failure(result)

    if failure is not None:
        failure["task_identity"] = _task_identity(task)
        return failure

    plan = result.get("execution_plan") or {}

    return _envelope_ok(
        action="plan",
        task_identity=_task_identity(task),
        task_id=result.get("task_id"),
        task_spec_hash=result.get("task_spec_hash"),
        plan_hash=result.get("plan_hash"),
        plan_status=plan.get("status"),
        stages=result.get("stages"),
        CST_CONNECTION_REQUIRED=result.get("CST_CONNECTION_REQUIRED"),
        PLANNED_SOLVE_COUNT=result.get("PLANNED_SOLVE_COUNT"),
        MAX_AUTHORIZED_SOLVES=result.get("MAX_AUTHORIZED_SOLVES"),
        REUSED_SOLVED_ARTIFACTS=result.get("REUSED_SOLVED_ARTIFACTS"),
        reuse_decisions=result.get("reuse_decisions"),
        projects_created=result.get("projects_created"),
        expected_outputs=result.get("expected_outputs"),
        zero_solve_paths=result.get("zero_solve_paths"),
        risks_and_blockers=result.get("risks_and_blockers"),
        dependency_snapshot_summary=result.get("dependency_snapshot_summary"),
        resolved_config=result.get("resolved_config"),
        cst_contacted=result.get("cst_contacted"),
        solved=result.get("solved"),
        **_solve_declarations(plan),
    )


def action_run(request: dict) -> dict:
    """Thin adapter: the plugin orchestrates, this only calls it."""

    task = _load_task(request)

    if task is None:
        return _envelope_error(
            "TASK_REQUIRED",
            stage="VALIDATION",
            message="RUN needs a TaskSpec: pass `task` (object) or `task_path` (string)",
        )

    result = public.run(
        task,
        store_root=request.get("store_root"),
        config_path=request.get("config_path"),
        active_project=request.get("active_project"),
        runtime_file=request.get("runtime_file"),
        reuse_index=request.get("reuse_index"),
    )

    run_id = result.get("run_id") if isinstance(result, dict) else None
    failure = _normalise_public_failure(result, run_id=run_id)

    if failure is not None:
        failure["stage_order"] = request.get("stage_order")
        return failure

    bundle = result.get("bundle") or {}

    # `public.run` reports the bundle, not the artifact list; the artifacts are
    # what the caller actually needs, so read them from the bundle when the
    # top-level key is absent, and from the durable store as a last resort
    # rather than reporting an empty registry.
    artifacts = result.get("artifacts")

    if artifacts is None and isinstance(bundle, dict):
        artifacts = bundle.get("artifacts")

    if artifacts is None:
        # The durable bundle file holds the full artifact records (producer,
        # status, registry hash) that the run store's inspect view trims.
        bundle_path = bundle.get("json") if isinstance(bundle, dict) else None

        if isinstance(bundle_path, str) and bundle_path:
            candidate = Path(bundle_path)

            if not candidate.is_absolute():
                candidate = REPO_ROOT / candidate

            if candidate.is_file():
                try:
                    artifacts = json.loads(
                        candidate.read_text(encoding="utf-8")
                    ).get("artifacts")
                except (OSError, ValueError):
                    artifacts = None

    if artifacts is None:
        inspected = public.inspect(run_id, store_root=request.get("store_root"))

        if isinstance(inspected, dict):
            artifacts = inspected.get("artifacts")

    return _envelope_ok(
        action="run",
        run_id=run_id,
        task_identity=_task_identity(task),
        plugin_lifecycle=result.get("plugin_lifecycle"),
        checkpoint=result.get("checkpoint"),
        execution_status=result.get("execution_status"),
        numerical_status=result.get("numerical_status"),
        scientific_status=result.get("scientific_status"),
        solve_usage=result.get("solve_usage"),
        store_root=result.get("store_root"),
        bundle=_bundle_view(bundle),
        artifacts=_artifacts_view(artifacts),
        reuse_decisions=result.get("reuse_decisions"),
        dependency_snapshot_summary=result.get("dependency_snapshot_summary"),
        errors=(result.get("errors") or [])[:10],
        warnings=(result.get("warnings") or [])[:10],
    )


def action_status(request: dict) -> dict:
    """Offline, durable, zero CST contact."""

    run_id = request.get("run_id")

    if not isinstance(run_id, str) or not run_id:
        return _envelope_error(
            "RUN_ID_REQUIRED",
            stage="VALIDATION",
            message="STATUS needs `run_id`",
        )

    result = public.status(
        run_id,
        store_root=request.get("store_root"),
        refresh_runtime=bool(request.get("refresh_runtime")),
    )

    if isinstance(result, dict) and result.get("available") is False:
        err = result.get("error") or {}
        return _envelope_error(
            err.get("error_code") or "RUN_NOT_AVAILABLE",
            stage=err.get("stage"),
            message=err.get("message") or "no durable state for this run_id",
            detail=err.get("detail"),
            run_id=run_id,
        )

    failure = _normalise_public_failure(result, run_id=run_id)

    if failure is not None:
        return failure

    # The durable status payload names these `plugin_lifecycle_state` and
    # `solver_usage`; report them under the tool's own vocabulary.
    solve_usage = result.get("solver_usage") or result.get("solve_usage")

    return _envelope_ok(
        action="status",
        run_id=result.get("run_id") or run_id,
        available=True,
        plugin_lifecycle=result.get("plugin_lifecycle_state")
        or result.get("plugin_lifecycle"),
        checkpoint=result.get("checkpoint"),
        execution_status=result.get("execution_status"),
        numerical_status=result.get("numerical_status"),
        scientific_status=result.get("scientific_status"),
        provenance_status=result.get("provenance_status"),
        current_stage=result.get("current_stage"),
        completed_stages=result.get("completed_stages"),
        solve_usage=solve_usage,
        solve_substate=result.get("solver_substate"),
        artifact_count=result.get("artifact_count"),
        checkpoint_version=result.get("checkpoint_version"),
        bundle_present=result.get("bundle_present"),
        loaded_from_disk=result.get("loaded_from_disk"),
        created_at=result.get("created_at"),
        updated_at=result.get("updated_at"),
        errors=(result.get("errors") or [])[:10],
        warnings=(result.get("warnings") or [])[:10],
        rehydration=result.get("rehydration"),
        cst_contacted=result.get("cst_contacted"),
        mutated_cst_project=result.get("mutated_cst_project"),
    )


def action_inspect(request: dict) -> dict:
    """Offline, read-only, one section at a time. Never opens a .cst."""

    run_id = request.get("run_id")

    if not isinstance(run_id, str) or not run_id:
        return _envelope_error(
            "RUN_ID_REQUIRED",
            stage="VALIDATION",
            message="INSPECT needs `run_id`",
        )

    section = request.get("section") or "summary"

    if section not in INSPECT_SECTIONS:
        return _envelope_error(
            "UNKNOWN_INSPECT_SECTION",
            stage="VALIDATION",
            message=f"section must be one of {list(INSPECT_SECTIONS)}",
            detail={"requested": section},
            run_id=run_id,
        )

    result = public.inspect(run_id, store_root=request.get("store_root"))

    if isinstance(result, dict) and result.get("available") is False:
        err = result.get("error") or {}
        return _envelope_error(
            err.get("error_code") or "RUN_NOT_AVAILABLE",
            stage=err.get("stage"),
            message=err.get("message") or "no durable state for this run_id",
            detail=err.get("detail"),
            run_id=run_id,
        )

    failure = _normalise_public_failure(result, run_id=run_id)

    if failure is not None:
        return failure

    payload = {
        "action": "inspect",
        "run_id": run_id,
        "section": section,
        "available": result.get("available", True),
        "sections_available": [s for s in INSPECT_SECTIONS if s != "all"],
        "cst_contacted": False,
        "opened_cst_project": False,
    }

    if section in ("summary", "all"):
        # INSPECT reports the durable payload, which carries no `checkpoint` or
        # `completed_stages`; per-stage state lives in `stages`.
        stages = result.get("stages")
        artifacts = result.get("artifacts")
        artifact_count = len(artifacts) if isinstance(artifacts, (list, dict)) else 0

        if isinstance(stages, list):
            stage_view = stages[:MAX_LIST]
        elif isinstance(stages, dict):
            stage_view = {k: stages[k] for k in list(stages)[:MAX_LIST]}
        else:
            stage_view = stages

        payload["summary"] = {
            "task_identity": _task_identity(result.get("task_spec")),
            "task_spec_hash": result.get("task_spec_hash"),
            "execution_plan_hash": result.get("execution_plan_hash"),
            "current_stage": result.get("current_stage"),
            "stages": stage_view,
            "stage_count": len(stages) if isinstance(stages, (list, dict)) else None,
            "scientific_labels": result.get("scientific_labels"),
            "solve_usage": result.get("solver_usage") or result.get("solve_usage"),
            "artifact_count": artifact_count,
            "read_only": result.get("read_only"),
            "loaded_from_disk": result.get("loaded_from_disk"),
            "mutated_cst_project": result.get("mutated_cst_project"),
        }

    if section in ("plan", "all"):
        payload["plan"] = _plan_view(result.get("execution_plan"))
        payload["plan_dependency_snapshot"] = (
            result.get("dependency_snapshot")
            or (result.get("rehydration") or {}).get("dependency_snapshot")
        )

    if section in ("artifacts", "all"):
        payload["artifacts"] = _artifacts_view(result.get("artifacts"))

    if section in ("observables", "all"):
        payload["observables"] = _observables_view(result.get("observables"))

    if section in ("errors", "all"):
        payload["errors"] = {
            "count": len(result.get("errors") or []),
            "errors": (result.get("errors") or [])[:10],
        }
        payload["warnings"] = (result.get("warnings") or [])[:10]
        payload["unresolved"] = (result.get("unresolved") or [])[:10]

    if section in ("events", "all"):
        payload["events"] = _events_view(result.get("events"))

    return _envelope_ok(**payload)


def action_resume(request: dict) -> dict:
    """run_id is enough. An optional task may only be integrity-checked."""

    run_id = request.get("run_id")

    if not isinstance(run_id, str) or not run_id:
        return _envelope_error(
            "RUN_ID_REQUIRED",
            stage="VALIDATION",
            message="RESUME needs `run_id`; the stored run store supplies the task, "
                    "plan, dependency snapshot and budget",
        )

    supplied = request.get("task") if isinstance(request.get("task"), dict) else None

    result = public.resume(
        run_id,
        store_root=request.get("store_root"),
        task=supplied,
        config_path=request.get("config_path"),
    )

    failure = _normalise_public_failure(result, run_id=run_id)

    if failure is not None:
        rehydration = result.get("rehydration") if isinstance(result, dict) else None

        if isinstance(rehydration, dict):
            failure["rehydration"] = rehydration

        return failure

    return _envelope_ok(
        action="resume",
        run_id=result.get("run_id") or run_id,
        resume_result=result.get("resume_result"),
        checkpoint_before=result.get("checkpoint_before"),
        checkpoint_after=result.get("checkpoint_after"),
        checkpoint_point_index=result.get("checkpoint_point_index"),
        stages_skipped=result.get("stages_skipped"),
        stages_executed=result.get("stages_executed"),
        stages_resumed_skipped=result.get("stages_resumed_skipped"),
        stages_reuse_skipped=result.get("stages_reuse_skipped"),
        points_completed_before=result.get("points_completed_before"),
        points_completed_after=result.get("points_completed_after"),
        points_total=result.get("points_total"),
        point_states=result.get("point_states"),
        fresh_solve_count=result.get("fresh_solve_count"),
        solve_budget_before=result.get("solve_budget_before"),
        solve_budget_after=result.get("solve_budget_after"),
        budget_reset_on_resume=result.get("budget_reset_on_resume"),
        task_integrity=result.get("task_integrity"),
        plan_recovery=result.get("plan_recovery"),
        rehydration=result.get("rehydration"),
        rehydration_checks=result.get("rehydration_checks"),
        config_source=result.get("config_source"),
        config_snapshot_hash=result.get("config_snapshot_hash"),
        store_root=result.get("store_root"),
    )


DISPATCH = {
    "plan": action_plan,
    "run": action_run,
    "status": action_status,
    "inspect": action_inspect,
    "resume": action_resume,
}


# ------------------------------------------------------------------------ main
def handle(action: str, request: dict) -> dict:
    handler = DISPATCH.get(action)

    if handler is None:
        return _envelope_error(
            "UNKNOWN_TOOL_ACTION",
            stage="VALIDATION",
            message=f"action must be one of {list(ACTIONS)}",
            detail={"requested": action},
        )

    try:
        return handler(request)
    except Exception as exc:  # noqa: BLE001 - the envelope is the contract
        return _envelope_error(
            "TOOL_ADAPTER_EXCEPTION",
            stage=action.upper(),
            message=f"{type(exc).__name__}: {exc}",
            detail={"exception_type": type(exc).__name__},
            run_id=request.get("run_id"),
            traceback_text=traceback.format_exc()[-4000:],
        )


def _read_request(argv: list[str]) -> dict:
    if len(argv) >= 2 and argv[1]:
        decoded = base64.b64decode(argv[1]).decode("utf-8")
        return json.loads(decoded) if decoded.strip() else {}

    raw = sys.stdin.read()

    return json.loads(raw) if raw.strip() else {}


def main(argv: list[str]) -> int:
    action = argv[0] if argv else ""

    try:
        request = _read_request(argv)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps(_envelope_error(
            "MALFORMED_REQUEST",
            stage="VALIDATION",
            message=f"could not decode the request: {type(exc).__name__}: {exc}",
        )))
        return 2

    print(json.dumps(handle(action, request), ensure_ascii=False, default=repr))

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
