"""CST_AI plugin layer — runtime validation.

V1.15 declared capability input/output schemas as documentation.  V1.16 actually
enforces them at the plugin's public boundary.

Lightweight on purpose: no validation framework is introduced.  The schema
vocabulary is the small one already used by the capability registry
(`"str"`, `"int"`, `"float"`, `"bool"`, `"list"`, `"dict"`, `"path"`, and
`"a | b"` unions).

Underlying modules keep their own type systems; where a legacy return value does
not match the unified schema, the ADAPTER normalises it.  Frozen modules are not
modified.
"""

from __future__ import annotations

from pathlib import Path

VALIDATION_VERSION = "1.16.0"

SCHEMA_TYPES = ("str", "int", "float", "bool", "list", "dict", "path", "none")

TASK_KINDS = (
    "BUILD_ONLY", "BUILD_AND_SOLVE", "LOAD_EXISTING_RESULT", "PARAMETER_SWEEP",
    "COMPARE_ONLY",
)

#: which stages each task kind is allowed to contain
TASK_KIND_STAGES = {
    "BUILD_ONLY": ("CONNECT", "BUILD", "VERIFY", "REPORT"),
    "BUILD_AND_SOLVE": ("CONNECT", "BUILD", "VERIFY", "SOLVE", "EXTRACT",
                        "ANALYZE", "REPORT"),
    "LOAD_EXISTING_RESULT": ("CONNECT", "EXTRACT", "ANALYZE", "REPORT"),
    "PARAMETER_SWEEP": ("ANALYZE", "REPORT"),
    "COMPARE_ONLY": ("ANALYZE", "REPORT"),
}

#: task kinds that can never contain a SOLVE stage
SOLVE_FORBIDDEN_KINDS = ("LOAD_EXISTING_RESULT", "COMPARE_ONLY",
                         "PARAMETER_SWEEP", "BUILD_ONLY")

STAGE_SOLVE = "SOLVE"

FIELD_TYPES = {
    "task_id": "str",
    "task_kind": "str",
    "label": "str | none",
    "stages": "list",
    "solve_policy": "str",
    "max_solves": "int | none",
    "spec": "dict",
    "inputs": "dict",
    "expected_outputs": "list",
    "output_dir": "path",
    "cst": "dict | none",
    "notes": "str | none",
    "schema_version": "str | none",
    "run_store_path": "path | none",
    "observables": "list | none",
}

REQUIRED_FIELDS = ("task_id", "task_kind", "stages", "solve_policy",
                   "output_dir")

PATH_FIELDS = ("output_dir", "run_store_path")


def _type_ok(value, spec: str) -> bool:
    spec = spec.replace(" ", "")

    if value is None:
        return "none" in spec.split("|")

    for option in spec.split("|"):
        if option == "none":
            continue

        if option == "str" and isinstance(value, str):
            return True

        if option == "int" and isinstance(value, int) and not isinstance(value, bool):
            return True

        if option == "float" and isinstance(value, (int, float)) and not isinstance(
            value, bool
        ):
            return True

        if option == "bool" and isinstance(value, bool):
            return True

        if option == "list" and isinstance(value, (list, tuple)):
            return True

        if option == "dict" and isinstance(value, dict):
            return True

        if option == "path" and isinstance(value, (str, Path)):
            return True

    return False


def validate_task_runtime(task: dict) -> dict:
    """Deep runtime validation of a TaskSpec.

    Returns the validated task plus a report.  Raises nothing itself; the caller
    decides.  Unknown fields are reported as errors, never ignored.
    """

    errors = []
    warnings = []

    if not isinstance(task, dict):
        return {"ok": False, "errors": [{"reason": "TASK_NOT_A_MAPPING"}],
                "warnings": [], "task": None}

    # ---- required fields
    for field in REQUIRED_FIELDS:
        if field not in task or task[field] in (None, "", [], {}):
            errors.append({"reason": "MISSING_REQUIRED_FIELD", "field": field})

    # ---- unknown fields
    unknown = sorted(set(task) - set(FIELD_TYPES))

    for field in unknown:
        errors.append({"reason": "UNKNOWN_FIELD", "field": field})

    # ---- field types
    for field, spec in FIELD_TYPES.items():
        if field in task and not _type_ok(task[field], spec):
            errors.append(
                {
                    "reason": "WRONG_TYPE",
                    "field": field,
                    "expected": spec,
                    "actual": type(task[field]).__name__,
                }
            )

    # ---- enums
    kind = task.get("task_kind")

    if kind is not None and kind not in TASK_KINDS:
        errors.append({"reason": "UNKNOWN_TASK_KIND", "value": kind,
                       "allowed": list(TASK_KINDS)})

    # ---- path fields
    for field in PATH_FIELDS:
        value = task.get(field)

        if value is None:
            continue

        if isinstance(value, str) and not value.strip():
            errors.append({"reason": "EMPTY_PATH_FIELD", "field": field})

    # ---- stages
    stages = task.get("stages")

    if isinstance(stages, (list, tuple)):
        from .task import STAGES as CANONICAL_STAGES

        bad = [s for s in stages if s not in CANONICAL_STAGES]

        for stage in bad:
            errors.append({"reason": "UNKNOWN_STAGE", "stage": stage})

        if list(stages) != sorted(set(stages), key=list(CANONICAL_STAGES).index):
            errors.append({"reason": "STAGES_NOT_CANONICAL_UNIQUE_ORDER"})

        # stage / task-kind compatibility
        if kind in TASK_KIND_STAGES:
            allowed = TASK_KIND_STAGES[kind]
            extra = [s for s in stages if s not in allowed]

            if extra:
                errors.append(
                    {
                        "reason": "STAGE_NOT_ALLOWED_FOR_TASK_KIND",
                        "task_kind": kind,
                        "extra_stages": extra,
                        "allowed": list(allowed),
                    }
                )

        if kind in SOLVE_FORBIDDEN_KINDS and STAGE_SOLVE in stages:
            errors.append(
                {
                    "reason": "SOLVE_STAGE_FORBIDDEN_FOR_TASK_KIND",
                    "task_kind": kind,
                }
            )

    # ---- solve-policy compatibility
    policy = task.get("solve_policy")

    from .task import SOLVE_POLICIES, FORBID, REUSE_ONLY, ALLOW_UP_TO_N

    if policy is not None and policy not in SOLVE_POLICIES:
        errors.append({"reason": "UNKNOWN_SOLVE_POLICY", "value": policy,
                       "allowed": list(SOLVE_POLICIES)})

    if policy == FORBID and isinstance(stages, (list, tuple)) and STAGE_SOLVE in stages:
        errors.append({"reason": "FORBID_WITH_SOLVE_STAGE"})

    if policy == ALLOW_UP_TO_N and not isinstance(task.get("max_solves"), int):
        errors.append({"reason": "ALLOW_UP_TO_N_WITHOUT_MAX_SOLVES"})

    if policy in (FORBID, REUSE_ONLY) and task.get("max_solves") is not None:
        errors.append({"reason": "MAX_SOLVES_WITH_NON_SOLVING_POLICY"})

    # ---- observable specs
    from . import observables as obs

    for index, spec in enumerate(task.get("observables") or []):
        if not isinstance(spec, dict):
            errors.append({"reason": "OBSERVABLE_NOT_A_MAPPING",
                           "index": index})
            continue

        kind_name = spec.get("kind")

        if kind_name not in obs.KINDS:
            errors.append({"reason": "UNKNOWN_OBSERVABLE_KIND",
                           "index": index, "value": kind_name})
            continue

        missing = [
            key for key in obs.REQUIRED_IDENTITY[kind_name]
            if key not in (spec.get("identity") or {})
        ]

        if missing:
            errors.append(
                {
                    "reason": "OBSERVABLE_IDENTITY_INCOMPLETE",
                    "index": index,
                    "kind": kind_name,
                    "missing": missing,
                }
            )

    # ---- capability requirements
    from . import capabilities as caps
    from .task import STAGE_CAPABILITIES

    if isinstance(stages, (list, tuple)) and not bad:
        for stage in stages:
            for capability_id in STAGE_CAPABILITIES.get(stage, ()):
                if capability_id not in caps.CAPABILITIES:
                    errors.append(
                        {
                            "reason": "CAPABILITY_NOT_REGISTERED",
                            "stage": stage,
                            "capability": capability_id,
                        }
                    )

    return {
        "validation_version": VALIDATION_VERSION,
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "task": task if not errors else None,
    }


# ------------------------------------------------------- capability boundary


def validate_capability_input(capability_id: str, payload: dict) -> dict:
    """Validate what the plugin is about to hand a capability."""

    from . import capabilities as caps
    from .errors import CapabilityMissing

    if capability_id not in caps.CAPABILITIES:
        raise CapabilityMissing(f"unknown capability: {capability_id}")

    schema = caps.CAPABILITIES[capability_id]["input_schema"]
    errors = []
    normalised = {}

    for key, spec in schema.items():
        if key not in payload:
            errors.append({"reason": "MISSING_INPUT", "key": key,
                           "expected": spec})
            continue

        value = payload[key]

        if not _type_ok(value, spec):
            errors.append({"reason": "WRONG_INPUT_TYPE", "key": key,
                           "expected": spec,
                           "actual": type(value).__name__})
            continue

        normalised[key] = value

    return {
        "capability_id": capability_id,
        "ok": not errors,
        "errors": errors,
        "normalised_input": normalised,
        "schema_checked": True,
    }


def validate_capability_output(capability_id: str, result) -> dict:
    """Validate what a capability returned, and normalise legacy shapes.

    A legacy module returning something slightly different is normalised HERE,
    in the adapter.  The frozen module is never edited.
    """

    from . import capabilities as caps

    schema = caps.CAPABILITIES.get(capability_id, {}).get("output_schema", {})

    errors = []
    normalised = {}
    normalisations = []

    if result is None:
        return {
            "capability_id": capability_id,
            "ok": False,
            "errors": [{"reason": "NULL_OUTPUT"}],
            "normalised_output": None,
            "normalisations_applied": [],
        }

    for key, spec in schema.items():
        if key in result:
            value = result[key]
        else:
            # a legacy shape may nest the value; normalise it rather than fail
            nested = None

            if isinstance(result, dict):
                for candidate in result.values():
                    if isinstance(candidate, dict) and key in candidate:
                        nested = candidate[key]
                        break

            if nested is None:
                errors.append({"reason": "MISSING_OUTPUT", "key": key,
                               "expected": spec})
                continue

            value = nested
            normalisations.append({"key": key, "from": "nested",
                                   "to": "top_level"})

        if not _type_ok(value, spec):
            # bool/int drift is a normalisation, not an error
            if spec.strip() == "bool" and isinstance(value, (int, float)):
                value = bool(value)
                normalisations.append({"key": key, "from": "number",
                                       "to": "bool"})
            else:
                errors.append({"reason": "WRONG_OUTPUT_TYPE", "key": key,
                               "expected": spec,
                               "actual": type(value).__name__})
                continue

        normalised[key] = value

    return {
        "capability_id": capability_id,
        "ok": not errors,
        "errors": errors,
        "normalised_output": normalised,
        "normalisations_applied": normalisations,
        "legacy_module_modified": False,
        "schema_checked": True,
    }
