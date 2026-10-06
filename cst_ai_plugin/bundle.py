"""CST_AI plugin layer — unified ResultBundle (JSON + Markdown).

One task produces one bundle.  It keeps EXECUTION, NUMERICAL, SCIENTIFIC and
PROVENANCE status apart, because a solver SUCCESS is not a CONVERGED result, not
a source-faithful model, and not a reproduced third-party result.

Console text alone is not an output.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

BUNDLE_VERSION = "1.15.0"


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def make_bundle(
    *,
    run_id: str,
    task: dict,
    plan: dict,
    preflight_result: dict,
    plugin_lifecycle: dict,
    solver_lifecycle: dict | None,
    artifacts: list[dict],
    observables: dict,
    solve_usage: dict,
    warnings=None,
    unresolved=None,
    errors=None,
    statuses: dict | None = None,
) -> dict:
    statuses = statuses or {}

    bundle = {
        "bundle_version": BUNDLE_VERSION,
        "run_id": run_id,
        "generated_at_utc": utc_now(),
        "task_summary": {
            "task_id": task.get("task_id"),
            "task_kind": task.get("task_kind"),
            "label": task.get("label"),
            "stages": list(task.get("stages") or []),
            "solve_policy": task.get("solve_policy"),
        },
        "execution_plan": plan,
        "preflight": preflight_result,
        "cst_lifecycle": {
            "plugin_lifecycle": plugin_lifecycle,
            "solver_lifecycle": solver_lifecycle,
            "note": (
                "solver lifecycle is a SUB-state; it is never merged into the "
                "plugin-level lifecycle"
            ),
        },
        "artifacts": artifacts,
        "observables": observables,
        "warnings": list(warnings or []),
        "unresolved": list(unresolved or []),
        "errors": list(errors or []),
        "solve_usage": solve_usage,
        "provenance": {
            "task_spec_hash": plan.get("task_spec_hash"),
            "execution_plan_hash": plan.get("execution_plan_hash"),
            "plugin_version": BUNDLE_VERSION,
            "module_versions": statuses.get("module_versions"),
            "input_artifact_hashes": statuses.get("input_artifact_hashes"),
            "runtime_identity": statuses.get("runtime_identity"),
            "start_time": statuses.get("start_time"),
        },
        #: the four layers, kept apart on purpose
        "execution_status": statuses.get("execution_status", "FAILED"),
        "numerical_status": statuses.get("numerical_status", "NOT_COMPUTED"),
        "scientific_status": statuses.get("scientific_status", "NOT_ASSERTED"),
        "provenance_status": statuses.get("provenance_status", "MISSING"),
        "single_success_flag_used": False,
    }

    return bundle


def write_bundle(bundle: dict, output_dir, *, stem: str = "result_bundle") -> dict:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    json_path = out / f"{stem}.json"
    md_path = out / f"{stem}.md"

    json_path.write_text(
        json.dumps(bundle, ensure_ascii=False, indent=2, default=repr) + "\n",
        encoding="utf-8",
    )

    md_path.write_text(bundle_to_markdown(bundle), encoding="utf-8")

    return {"json": str(json_path), "markdown": str(md_path)}


def bundle_to_markdown(bundle: dict) -> str:
    lines = []
    add = lines.append

    add(f"# ResultBundle — {bundle['task_summary']['task_id']}\n\n")
    add(f"* run_id: `{bundle['run_id']}`\n")
    add(f"* task_kind: `{bundle['task_summary']['task_kind']}`\n")
    add(f"* label: {bundle['task_summary'].get('label')}\n")
    add(f"* generated: {bundle['generated_at_utc']}\n\n")

    add("## Status layers\n\n")
    add("| layer | value |\n|---|---|\n")

    for key in ("execution_status", "numerical_status", "scientific_status",
                "provenance_status"):
        add(f"| `{key}` | **{bundle[key]}** |\n")

    add(
        "\n> These four layers are deliberately separate. A solver SUCCESS does "
        "not imply CONVERGED, SOURCE_FAITHFUL or REPRODUCED.\n\n"
    )

    add("## Solve usage\n\n")
    for key, value in (bundle.get("solve_usage") or {}).items():
        add(f"* `{key}`: {value}\n")

    add("\n## Execution plan\n\n")
    plan = bundle["execution_plan"]
    add(f"* stage order: `{' -> '.join(plan['stage_order'])}`\n")
    add(f"* CST connection required: `{plan['CST_CONNECTION_REQUIRED']}`\n")
    add(f"* PLANNED_SOLVE_COUNT: `{plan['PLANNED_SOLVE_COUNT']}`\n")
    add(f"* MAX_AUTHORIZED_SOLVES: `{plan['MAX_AUTHORIZED_SOLVES']}`\n")
    add(f"* projects created: `{plan['projects_created']}`\n")

    add("\n## CST lifecycle\n\n")
    add(f"* plugin: `{bundle['cst_lifecycle']['plugin_lifecycle']}`\n")
    add(f"* solver: `{bundle['cst_lifecycle']['solver_lifecycle']}`\n")

    add("\n## Observables\n\n")

    for name, value in (bundle.get("observables") or {}).items():
        rendered = json.dumps(value, ensure_ascii=False, default=repr)

        if len(rendered) > 400:
            rendered = rendered[:400] + "…"

        add(f"* **{name}**: `{rendered}`\n")

    add("\n## Artifacts\n\n")
    add("| artifact_id | type | status | scientific | hash |\n|---|---|---|---|---|\n")

    for artifact in bundle.get("artifacts") or []:
        add(
            f"| `{artifact['artifact_id']}` | {artifact['type']} | "
            f"{artifact['status']} | {artifact['scientific_status']} | "
            f"`{artifact['hash']}` |\n"
        )

    if bundle.get("warnings"):
        add("\n## Warnings\n\n")

        for warning in bundle["warnings"]:
            add(f"* {warning}\n")

    if bundle.get("unresolved"):
        add("\n## Unresolved\n\n")

        for item in bundle["unresolved"]:
            add(f"* `{json.dumps(item, ensure_ascii=False, default=repr)}`\n")

    if bundle.get("errors"):
        add("\n## Errors\n\n")

        for error in bundle["errors"]:
            add(f"* `{json.dumps(error, ensure_ascii=False, default=repr)}`\n")

    add("\n## Provenance\n\n")

    for key, value in (bundle.get("provenance") or {}).items():
        add(f"* `{key}`: `{value}`\n")

    return "".join(lines)
