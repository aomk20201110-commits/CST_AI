"""V1.7A CSTModelSpec foundation (frozen schemas, gates, IR, contracts).

CST-free, stdlib-only. Nothing here talks to CST.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

MODELSPEC_SCHEMA_VERSION = "1.7.0"
COMMAND_IR_VERSION = "1.7.0"
MODEL_LEDGER_SCHEMA_VERSION = "1.7.0"
INGESTION_CONTRACT_VERSION = "1.7.0"

# ------------------------------------------------------- evidence status
EXPLICIT_REFERENCE = "EXPLICIT_REFERENCE"
EXPLICIT_SI = "EXPLICIT_SI"
EXPLICIT_FIGURE = "EXPLICIT_FIGURE"
DERIVED_GEOMETRY = "DERIVED_GEOMETRY"
DERIVED_PHYSICS = "DERIVED_PHYSICS"
ASSUMPTION = "ASSUMPTION"
UNKNOWN = "UNKNOWN"

EVIDENCE_STATUSES = (
    EXPLICIT_REFERENCE,
    EXPLICIT_SI,
    EXPLICIT_FIGURE,
    DERIVED_GEOMETRY,
    DERIVED_PHYSICS,
    ASSUMPTION,
    UNKNOWN,
)

# statuses that may feed a production build without extra approval
AUTO_BUILDABLE = (EXPLICIT_REFERENCE, EXPLICIT_SI, EXPLICIT_FIGURE)
CONDITIONAL_BUILDABLE = (DERIVED_GEOMETRY,)

BLOCKING_P0 = "P0"
BLOCKING_P1 = "P1"
BLOCKING_P2 = "P2"

READY = "READY"
CONDITIONAL = "CONDITIONAL"
NO_GO = "NO_GO"

BUILD_LEVELS = ("L0_GEOMETRY_ONLY", "L1_MATERIALS_BOUNDARIES", "L2_SOLVER_READY")

L0_REQUIRED_PATHS = (
    "metadata.units",
    "metadata.coordinate_system",
    "geometry",
)
L1_REQUIRED_PATHS = (
    "materials",
    "boundaries",
    "excitations",
)
L2_REQUIRED_PATHS = (
    "frequency",
    "solver",
    "monitors",
    "results_requested",
)

# ---------------------------------------------------------- command IR
COMMAND_TYPES = (
    "CreateBrick",
    "CreateCylinder",
    "CreatePlanarPolygon",
    "Extrude",
    "Translate",
    "Rotate",
    "BooleanSubtract",
    "BooleanUnion",
    "CreateMaterial",
    "AssignMaterial",
    "SetBoundary",
    "CreatePort",
    "SetFrequencyRange",
    "CreateMonitor",
)

COMMAND_ARG_SCHEMA: dict[str, dict[str, Any]] = {
    "CreateBrick": {
        "required": ("object_id", "component", "material", "xmin", "xmax", "ymin", "ymax", "zmin", "zmax"),
        "numeric": ("xmin", "xmax", "ymin", "ymax", "zmin", "zmax"),
    },
    "CreateCylinder": {
        "required": ("object_id", "component", "material", "axis", "radius", "zmin", "zmax"),
        "numeric": ("radius", "zmin", "zmax"),
        "enum": {"axis": ("x", "y", "z")},
    },
    "CreatePlanarPolygon": {
        "required": ("object_id", "component", "material", "points"),
    },
    "Extrude": {
        "required": ("object_id", "height"),
        "numeric": ("height",),
    },
    "Translate": {
        "required": ("object_id", "dx", "dy", "dz"),
        "numeric": ("dx", "dy", "dz"),
    },
    "Rotate": {
        "required": ("object_id", "axis", "angle_deg"),
        "numeric": ("angle_deg",),
        "enum": {"axis": ("x", "y", "z")},
    },
    "BooleanSubtract": {
        "required": ("target_id", "tool_id"),
    },
    "BooleanUnion": {
        "required": ("target_id", "tool_id"),
    },
    "CreateMaterial": {
        "required": ("material_id", "name", "model_type"),
        "enum": {
            "model_type": (
                "normal",
                "anisotropic",
                "lossy_metal",
                "pec",
                "dielectric",
            )
        },
    },
    "AssignMaterial": {
        "required": ("object_id", "material_id"),
    },
    "SetBoundary": {
        "required": ("face", "kind"),
        "enum": {
            "face": ("x_min", "x_max", "y_min", "y_max", "z_min", "z_max"),
            "kind": (
                "electric",
                "magnetic",
                "open",
                "open_add_space",
                "periodic",
                "unit_cell",
            ),
        },
    },
    "CreatePort": {
        "required": ("port_id", "port_type"),
        "enum": {"port_type": ("waveguide", "discrete", "floquet")},
    },
    "SetFrequencyRange": {
        "required": ("start_ghz", "stop_ghz"),
        "numeric": ("start_ghz", "stop_ghz"),
    },
    "CreateMonitor": {
        "required": ("monitor_id", "monitor_type"),
        "enum": {
            "monitor_type": (
                "e_field",
                "h_field",
                "farfield",
                "s_parameter",
                "power",
            )
        },
    },
}


class ModelSpecError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail

        super().__init__(code + (": " + detail if detail else ""))


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


# ---------------------------------------------------------- ModelSpec

def empty_modelspec(model_id: str) -> dict[str, Any]:
    return {
        "modelspec_schema_version": MODELSPEC_SCHEMA_VERSION,
        "metadata": {
            "model_id": model_id,
            "source_documents": [],
            "source_hashes": [],
            "coordinate_system": None,
            "units": {"length": None, "frequency": None},
        },
        "parameters": [],
        "materials": [],
        "geometry": [],
        "boundaries": {},
        "excitations": [],
        "frequency": {},
        "solver": {},
        "monitors": [],
        "results_requested": [],
        "reference_targets": [],
        "unknowns": [],
        "ambiguities": [],
        "blocking_missing_fields": [],
        "field_evidence": [],
    }


def _norm_unit(unit: str | None) -> str | None:
    if unit is None:
        return None

    text = str(unit).strip().lower()

    aliases = {
        "um": "um",
        "µm": "um",
        "micron": "um",
        "microns": "um",
        "nm": "nm",
        "mm": "mm",
        "cm": "cm",
        "m": "m",
        "thz": "thz",
        "ghz": "ghz",
        "mhz": "mhz",
        "hz": "hz",
        "deg": "deg",
        "degree": "deg",
        "degrees": "deg",
    }

    return aliases.get(text, text)


def normalize_units(spec: dict[str, Any]) -> dict[str, Any]:
    """Length -> mm, frequency -> GHz (canonical build units)."""

    length_factors = {
        "m": 1000.0,
        "cm": 10.0,
        "mm": 1.0,
        "um": 0.001,
        "nm": 1e-6,
    }

    frequency_factors = {
        "hz": 1e-9,
        "mhz": 1e-3,
        "ghz": 1.0,
        "thz": 1e3,
    }

    for parameter in spec.get("parameters", []):
        unit = _norm_unit(parameter.get("unit"))

        if unit in length_factors:
            parameter["value"] = float(parameter["value"]) * length_factors[unit]
            parameter["unit"] = "mm"
        elif unit in frequency_factors:
            parameter["value"] = float(parameter["value"]) * frequency_factors[unit]
            parameter["unit"] = "ghz"

    return spec


def validate_modelspec(spec: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []

    if spec.get("modelspec_schema_version") != MODELSPEC_SCHEMA_VERSION:
        errors.append("MODELSPEC_SCHEMA_VERSION_MISMATCH")

    metadata = spec.get("metadata") or {}

    if not metadata.get("model_id"):
        errors.append("MISSING_MODEL_ID")

    if not metadata.get("source_documents"):
        errors.append("MISSING_SOURCE_DOCUMENTS")

    if not metadata.get("source_hashes"):
        errors.append("MISSING_SOURCE_HASHES")

    object_ids = set()

    for index, obj in enumerate(spec.get("geometry", [])):
        object_id = obj.get("object_id")

        if not object_id:
            errors.append(f"GEOMETRY[{index}]:MISSING_OBJECT_ID")
            continue

        if object_id in object_ids:
            errors.append(f"DUPLICATE_OBJECT_ID:{object_id}")
        object_ids.add(object_id)

        if obj.get("primitive_type") not in (
            "brick",
            "cylinder",
            "planar_polygon",
            "extrusion",
        ):
            errors.append(f"GEOMETRY[{index}]:UNSUPPORTED_PRIMITIVE")

        dimensions = obj.get("dimensions") or {}

        for key, value in dimensions.items():
            if not isinstance(value, (int, float)):
                errors.append(
                    f"GEOMETRY[{index}]:NON_NUMERIC_DIMENSION:{key}"
                )
            elif value <= 0 and key not in ("xmin", "ymin", "zmin"):
                errors.append(
                    f"GEOMETRY[{index}]:INVALID_DIMENSION:{key}={value}"
                )

        if obj.get("evidence_status") not in EVIDENCE_STATUSES:
            errors.append(f"GEOMETRY[{index}]:MISSING_EVIDENCE_STATUS")

        if not obj.get("source_ref"):
            errors.append(f"GEOMETRY[{index}]:MISSING_SOURCE_REF")

    material_ids = {item.get("material_id") for item in spec.get("materials", [])}

    for index, obj in enumerate(spec.get("geometry", [])):
        material = obj.get("material")

        if material and material not in material_ids:
            errors.append(f"GEOMETRY[{index}]:UNKNOWN_MATERIAL:{material}")

    for index, item in enumerate(spec.get("materials", [])):
        if item.get("evidence_status") not in EVIDENCE_STATUSES:
            errors.append(f"MATERIAL[{index}]:MISSING_EVIDENCE_STATUS")

        if not item.get("source_ref"):
            errors.append(f"MATERIAL[{index}]:MISSING_SOURCE_REF")

    boundaries = spec.get("boundaries") or {}

    for face in ("x_min", "x_max", "y_min", "y_max", "z_min", "z_max"):
        if face not in boundaries:
            errors.append(f"BOUNDARY:MISSING:{face}")
        elif boundaries[face] in (None, "", UNKNOWN):
            errors.append(f"BOUNDARY:UNKNOWN:{face}")

    for entry in spec.get("field_evidence", []):
        for key in (
            "field_id",
            "modelspec_path",
            "evidence_status",
            "source_file_sha256",
            "locator",
            "blocking_level",
        ):
            if not entry.get(key):
                errors.append(
                    f"FIELD_EVIDENCE:{entry.get('field_id')}:MISSING:{key}"
                )

        if entry.get("evidence_status") not in EVIDENCE_STATUSES:
            errors.append(
                f"FIELD_EVIDENCE:{entry.get('field_id')}:BAD_STATUS"
            )

    return {"ok": not errors, "errors": errors}


def blocking_paths(spec: dict[str, Any]) -> list[str]:
    """Every P0 field that is UNKNOWN/ASSUMPTION or missing."""

    blocking: list[str] = []

    for entry in spec.get("field_evidence", []):
        if entry.get("blocking_level") != BLOCKING_P0:
            continue

        if entry.get("evidence_status") not in AUTO_BUILDABLE + CONDITIONAL_BUILDABLE:
            blocking.append(entry["modelspec_path"])

    for path in L0_REQUIRED_PATHS:
        node: Any = spec
        for part in path.split("."):
            node = node.get(part) if isinstance(node, dict) else None

        if not node:
            blocking.append(path)

    return sorted(set(blocking))


def readiness(
    spec: dict[str, Any],
    level: str,
) -> dict[str, Any]:
    if level not in BUILD_LEVELS:
        raise ModelSpecError("UNKNOWN_BUILD_LEVEL", level)

    required = {
        "L0_GEOMETRY_ONLY": L0_REQUIRED_PATHS,
        "L1_MATERIALS_BOUNDARIES": L0_REQUIRED_PATHS + L1_REQUIRED_PATHS,
        "L2_SOLVER_READY": L0_REQUIRED_PATHS + L1_REQUIRED_PATHS + L2_REQUIRED_PATHS,
    }[level]

    problems: list[str] = []

    for path in required:
        node: Any = spec
        for part in path.split("."):
            node = node.get(part) if isinstance(node, dict) else None

        if not node:
            problems.append("MISSING:" + path)

    p0_blocking = blocking_paths(spec)

    if p0_blocking:
        problems.extend("BLOCKING_P0:" + item for item in p0_blocking)

    statuses = {
        entry.get("evidence_status")
        for entry in spec.get("field_evidence", [])
    }

    if ASSUMPTION in statuses or UNKNOWN in statuses:
        problems.append("ASSUMPTION_OR_UNKNOWN_PRESENT")

    if DERIVED_PHYSICS in statuses:
        problems.append("DERIVED_PHYSICS_REQUIRES_APPROVAL")

    if not problems:
        verdict = READY
    elif all(problem.startswith(("MISSING:P2", "MISSING:results_requested"))
             for problem in problems):
        verdict = CONDITIONAL
    else:
        verdict = NO_GO

    if problems and verdict != NO_GO:
        if p0_blocking:
            verdict = NO_GO

    return {
        "level": level,
        "readiness": verdict,
        "problems": sorted(set(problems)),
        "blocking_p0": p0_blocking,
        "production_reproduction_allowed": verdict == READY,
    }


# ------------------------------------------------------------- IR build

def validate_command(
    command: dict[str, Any],
    known_objects: set[str],
    known_materials: set[str],
) -> dict[str, Any]:
    errors: list[str] = []

    command_type = command.get("command_type")

    if command_type not in COMMAND_TYPES:
        return {
            "ok": False,
            "errors": ["UNSUPPORTED_COMMAND:" + str(command_type)],
        }

    if not command.get("command_id"):
        errors.append("MISSING_COMMAND_ID")

    args = command.get("validated_args") or {}

    schema = COMMAND_ARG_SCHEMA[command_type]

    for key in schema["required"]:
        if key not in args:
            errors.append(f"MISSING_ARG:{key}")

    for key in schema.get("numeric", ()):
        value = args.get(key)

        if value is not None and not isinstance(value, (int, float)):
            errors.append(f"NON_NUMERIC_ARG:{key}")

    for key, allowed in schema.get("enum", {}).items():
        if key in args and args[key] not in allowed:
            errors.append(f"INVALID_ENUM:{key}")

    if not command.get("source_field_refs"):
        errors.append("MISSING_SOURCE_FIELD_REFS")

    if not command.get("rollback_group"):
        errors.append("MISSING_ROLLBACK_GROUP")

    if command_type in ("BooleanSubtract", "BooleanUnion"):
        for key in ("target_id", "tool_id"):
            if args.get(key) and args[key] not in known_objects:
                errors.append(f"BOOLEAN_REFERENCE_MISSING:{key}")

    if command_type == "AssignMaterial":
        if args.get("material_id") not in known_materials:
            errors.append("MATERIAL_REFERENCE_MISSING")
        if args.get("object_id") not in known_objects:
            errors.append("OBJECT_REFERENCE_MISSING")

    if command_type in ("Translate", "Rotate", "Extrude"):
        if args.get("object_id") not in known_objects:
            errors.append("OBJECT_REFERENCE_MISSING")

    for key, value in args.items():
        if isinstance(value, str) and _looks_like_script(value):
            errors.append("SCRIPT_INJECTION_REJECTED:" + key)

    if command.get("raw_script") or command.get("vba"):
        errors.append("ARBITRARY_SCRIPT_REJECTED")

    return {"ok": not errors, "errors": errors}


SCRIPT_PATTERNS = (
    "add_to_history",
    "vba",
    "Sub Main",
    "End Sub",
    "Execute",
    "eval(",
    "os.system",
    "import os",
    ";\n",
    "`",
)


def _looks_like_script(value: str) -> bool:
    lowered = value.lower()

    return any(pattern.lower() in lowered for pattern in SCRIPT_PATTERNS)


def deterministic_command_order(
    commands: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Stable apply order: creation first, then assignment/transform."""

    phase = {
        "CreateBrick": 0,
        "CreateCylinder": 0,
        "CreatePlanarPolygon": 0,
        "Extrude": 1,
        "Translate": 2,
        "Rotate": 2,
        "BooleanUnion": 3,
        "BooleanSubtract": 3,
        "CreateMaterial": 4,
        "AssignMaterial": 5,
        "SetBoundary": 6,
        "CreatePort": 7,
        "SetFrequencyRange": 8,
        "CreateMonitor": 9,
    }

    return sorted(
        commands,
        key=lambda item: (
            phase.get(item["command_type"], 99),
            item["command_id"],
        ),
    )


def plan_from_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """ModelSpec -> validated command IR (no CST)."""

    commands: list[dict[str, Any]] = []

    for material in spec.get("materials", []):
        commands.append(
            {
                "command_id": "cmd_mat_" + material["material_id"],
                "command_type": "CreateMaterial",
                "validated_args": {
                    "material_id": material["material_id"],
                    "name": material.get("name"),
                    "model_type": material.get("model_type"),
                    "epsilon": material.get("epsilon"),
                    "loss_tangent": material.get("loss_tangent"),
                },
                "source_field_refs": [material.get("source_ref")],
                "preconditions": [],
                "expected_readback": {"material_id": material["material_id"]},
                "rollback_group": "materials",
            }
        )

    for obj in spec.get("geometry", []):
        command_type = {
            "brick": "CreateBrick",
            "cylinder": "CreateCylinder",
            "planar_polygon": "CreatePlanarPolygon",
            "extrusion": "Extrude",
        }[obj["primitive_type"]]

        commands.append(
            {
                "command_id": "cmd_geo_" + obj["object_id"],
                "command_type": command_type,
                "validated_args": {
                    "object_id": obj["object_id"],
                    "component": obj.get("component"),
                    "material": obj.get("material"),
                    **{k: v for k, v in (obj.get("dimensions") or {}).items()},
                },
                "source_field_refs": obj.get("source_refs")
                or [obj.get("source_ref")],
                "preconditions": [],
                "expected_readback": {"object_id": obj["object_id"]},
                "rollback_group": "geometry",
            }
        )

    ordered = deterministic_command_order(commands)

    known_objects = {obj["object_id"] for obj in spec.get("geometry", [])}
    known_materials = {
        item["material_id"] for item in spec.get("materials", [])
    }

    problems = []

    for command in ordered:
        result = validate_command(command, known_objects, known_materials)

        if not result["ok"]:
            problems.append(
                {"command_id": command["command_id"], "errors": result["errors"]}
            )

    return {
        "command_ir_version": COMMAND_IR_VERSION,
        "commands": ordered,
        "validation": {"ok": not problems, "problems": problems},
        "command_plan_hash": "sha256:" + canonical_hash(ordered),
    }


# -------------------------------------------------------- model ledger

MODEL_LEDGER_RECORD_TYPES = (
    "build_attempt",
    "source_ingestion",
    "modelspec_snapshot",
    "command_application",
    "model_snapshot",
    "build_failure",
)


def model_ledger_record_hash(record: dict[str, Any]) -> str:
    material = {k: v for k, v in record.items() if k != "record_hash"}

    return "sha256:" + canonical_hash(material)


def append_model_ledger_record(
    path,
    record: dict[str, Any],
    previous_hash: str | None,
) -> dict[str, Any]:
    import json as _json

    body = dict(record)

    body["model_ledger_schema_version"] = MODEL_LEDGER_SCHEMA_VERSION
    body["previous_record_hash"] = previous_hash
    body.pop("record_hash", None)
    body["record_hash"] = model_ledger_record_hash(body)

    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(
            _json.dumps(body, ensure_ascii=False, sort_keys=True) + "\n"
        )

    return body


def verify_model_ledger(path) -> dict[str, Any]:
    import json as _json

    from pathlib import Path as _Path

    target = _Path(path)

    if not target.exists():
        return {"ok": True, "record_count": 0, "head_record_hash": None}

    records = []

    for line in target.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(_json.loads(line))

    previous = None

    for index, record in enumerate(records, start=1):
        if record.get("previous_record_hash") != previous:
            return {
                "ok": False,
                "error": f"chain mismatch at {index}",
                "record_count": len(records),
            }

        if record.get("record_hash") != model_ledger_record_hash(record):
            return {
                "ok": False,
                "error": f"hash mismatch at {index}",
                "record_count": len(records),
            }

        previous = record["record_hash"]

    return {
        "ok": True,
        "record_count": len(records),
        "head_record_hash": previous,
    }


# ------------------------------------------------------------- dry run

def dry_run_readiness_report(spec: dict[str, Any]) -> dict[str, Any]:
    """MODEL_SPEC_ONLY dry run: no CST, no project, no file write."""

    validation = validate_modelspec(spec)

    levels = {
        level: readiness(spec, level) for level in BUILD_LEVELS
    }

    plan = (
        plan_from_spec(spec)
        if validation["ok"]
        else {"commands": [], "validation": {"ok": False}}
    )

    worst = NO_GO

    if any(item["readiness"] == READY for item in levels.values()):
        worst = READY
    elif any(item["readiness"] == CONDITIONAL for item in levels.values()):
        worst = CONDITIONAL

    return {
        "mode": "MODEL_SPEC_ONLY",
        "spec_hash": "sha256:" + canonical_hash(spec),
        "validation": validation,
        "levels": levels,
        "overall_readiness": worst,
        "production_build_allowed": False,
        "command_plan": plan,
        "note": (
            "V1.7A is a foundation phase: no production CST model is "
            "built and no solver runs. A NO_GO result terminates with a "
            "missing-information request instead of a model."
        ),
    }
