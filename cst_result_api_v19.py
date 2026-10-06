"""V1.9 result access layer: RESULT_TREE_INVENTORY + ResultRef + 1D extraction.

Design rules for this module
----------------------------
1. INVENTORY FIRST.  No result tree path is ever hard-coded here.  The tree is
   enumerated through the vendor API and the paths that actually exist are the
   only paths this module will ever read.

2. DOCUMENTED API.  Every vendor call used below is taken from the official
   cst.results documentation shipped with CST Studio Suite 2026 and re-checked
   against a live installation under the bundled interpreter:

     cst.results.ProjectFile(path, allow_interactive=True)      [harness-proven]
     ProjectFile.get_3d() -> ResultModule
     ResultModule.get_tree_items(filter='0D/1D') -> List[str]
     ResultModule.get_result_item(treepath, run_id=0,
                                  load_impedances=True) -> ResultItem
     ResultModule.get_run_ids(treepath, skip_nonparametric=False) -> List[int]
     ResultModule.get_all_run_ids(max_mesh_passes_only=True) -> List[int]
     ResultModule.get_parameter_combination(run_id) -> dict
     ResultItem.get_xdata() -> object
     ResultItem.get_ydata() -> object
     ResultItem.get_ref_imp_data() -> object
     ResultItem.get_parameter_combination() -> dict
     ResultItem.length / .run_id / .treepath / .title / .xlabel / .ylabel

   Vendor documentation also states: "This module allows reading
   one-dimensional result curves ... No running instance of CST Studio Suite is
   required for data access.  Supported are unpacked and unprotected project
   files generated with CST Studio Suite 2025 or CST Studio Suite 2026."

   The official VBA reference (vendor macros, Library/Macros/Results) confirms
   the same semantics from the CST side:
     ResultTree.GetTreeResults(root, "0D/1D recursive", "filetype0D1D",
                               vTreePaths, vResultTypes, vFileNames, vResultInfo)
     ResultTree.GetResultFromTreeItem(treepath, resultID)
     ResultTree.GetResultIDsFromTreeItem(treepath)

3. S-parameter index semantics follow the vendor definition:
       "S(i,j): i is the OUTPUT (receiving) port, j is the INPUT (excited) port."
   A Floquet port is identified by its FACE (Zmin / Zmax); the mode number of a
   Floquet port is the parenthesised number after the port name.  Mode numbers
   are therefore preserved as explicit fields - they are never collapsed into a
   single "S11" string.

4. NO POWER / NO R-T-A CONVERSION.  This module returns complex modal amplitudes
   and their magnitude / phase only.  It deliberately does not compute
   reflectance, transmittance or absorptance, because the CST normalisation /
   impedance convention is not locked here far enough to justify that
   conversion.  A downstream user who needs R/T/A must first lock that
   convention.

Stdlib only.  No CST import happens at module import time.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

RESULT_REF_SCHEMA_VERSION = "1.9.0"

EVIDENCE_VENDOR_API = "RESULT_READBACK_NATIVE_CST_RESULTS"
EVIDENCE_VENDOR_DOC = "VENDOR_DOCSTRING"

RESULT_KINDS = ("s_parameter", "scalar_0d", "curve_1d")

# comparison protocols (no tolerance is invented anywhere in this module)
PROTOCOL_EXACT = "EXACT_FLOAT64_EQUALITY"
PROTOCOL_INVESTIGATE = "REQUIRES_INVESTIGATION"

# --------------------------------------------------------------- hashing


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(
        canonical_json(value).encode("utf-8")
    ).hexdigest()


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def float_series_token(values: list[float]) -> list[str]:
    """Exact, round-trip-stable textual form of a float series."""

    return [float(value).hex() for value in values]


# ------------------------------------------------- S-parameter identifier

#  S1,1                  -> port "1"  <-> port "1"
#  S1(1),1(1)            -> port 1 mode 1 <-> port 1 mode 1
#  SZmin(1),Zmax(2)      -> Zmax mode 2 excites, Zmin mode 1 receives
#: A CST port name may be a plain integer (discrete ports), a letter name
#: (waveguide ports) or a Floquet face (Zmin / Zmax); the mode number of a
#: multimode port is the parenthesised integer.
_SPARAM_TOKEN = re.compile(
    r"^(?P<name>[A-Za-z0-9_]+?)\s*(?:\((?P<mode>\d+)\))?$"
)


def parse_s_parameter_identifier(identifier: str) -> dict[str, Any]:
    """Split a CST S-parameter identifier into explicit port / mode fields.

    Only the structural convention documented by the vendor is applied.
    Anything that cannot be structurally resolved is reported as UNRESOLVED
    instead of being guessed.
    """

    entry: dict[str, Any] = {
        "raw_identifier": identifier,
        "index_convention": "S(output_index,input_index) - vendor convention",
        "evidence": "official CST result-tree documentation",
        "resolved": False,
        "output": None,
        "input": None,
        "unresolved_reason": None,
    }

    if not identifier.startswith("S"):
        entry["unresolved_reason"] = "NOT_AN_S_PARAMETER_TOKEN"
        return entry

    body = identifier[1:]

    parts = body.split(",")

    if len(parts) != 2:
        entry["unresolved_reason"] = "EXPECTED_TWO_COMMA_SEPARATED_INDICES"
        return entry

    parsed = []

    for part in parts:
        match = _SPARAM_TOKEN.match(part.strip())

        if match is None:
            entry["unresolved_reason"] = "UNPARSABLE_INDEX_TOKEN:" + part
            return entry

        parsed.append(
            {
                "port": match.group("name"),
                "mode": int(match.group("mode"))
                if match.group("mode") is not None
                else None,
                "mode_explicit": match.group("mode") is not None,
            }
        )

    entry["output"] = parsed[0]
    entry["input"] = parsed[1]
    entry["resolved"] = True

    # Floquet faces are named Zmin / Zmax in the CST identifier itself; record
    # the face explicitly so a later R/T/A construction can use it without
    # re-parsing the string.
    for target, node in (("output", parsed[0]), ("input", parsed[1])):
        port = node["port"]

        if port in ("Zmin", "Zmax"):
            entry[target + "_face"] = port
        else:
            entry[target + "_face"] = None

    entry["multimode"] = bool(
        parsed[0]["mode_explicit"] or parsed[1]["mode_explicit"]
    )

    return entry


def classify_result_kind(tree_path: str) -> str:
    leaf = tree_path.split("\\")[-1].strip()

    if leaf.startswith("S") and "," in leaf:
        return "s_parameter"

    if re.match(r"^S[A-Za-z0-9_]*\(", leaf):
        return "s_parameter"

    return "curve_1d"


# ------------------------------------------------------------- ResultRef

RESULT_REF_FIELDS = (
    "result_ref_schema_version",
    "tree_path",
    "cst_result_identifier",
    "result_kind",
    "run_id",
    "title",
    "x_label",
    "y_label",
    "x_unit",
    "y_unit",
    "complex_valued",
    "s_parameter_indices",
    "monitor",
    "frequency",
    "source_project_hash",
    "solve_run_id",
    "evidence",
)


def make_result_ref(
    *,
    tree_path: str,
    title: str | None,
    x_label: str | None,
    y_label: str | None,
    run_id: int | None,
    complex_valued: bool,
    x_unit: str | None = None,
    y_unit: str | None = None,
    monitor: dict | None = None,
    frequency: dict | None = None,
    source_project_hash: str | None = None,
    solve_run_id: str | None = None,
) -> dict[str, Any]:
    """A result reference in the project's existing dict-schema style."""

    kind = classify_result_kind(tree_path)

    ref: dict[str, Any] = {
        "result_ref_schema_version": RESULT_REF_SCHEMA_VERSION,
        "tree_path": tree_path,
        "cst_result_identifier": tree_path,
        "result_kind": kind,
        "run_id": run_id,
        "title": title,
        "x_label": x_label,
        "y_label": y_label,
        "x_unit": x_unit,
        "y_unit": y_unit,
        "complex_valued": bool(complex_valued),
        "s_parameter_indices": (
            parse_s_parameter_identifier(tree_path.split("\\")[-1])
            if kind == "s_parameter"
            else None
        ),
        "monitor": monitor,
        "frequency": frequency,
        "source_project_hash": source_project_hash,
        "solve_run_id": solve_run_id,
        "evidence": EVIDENCE_VENDOR_API,
    }

    return ref


# ------------------------------------------------------------- inventory


def inventory_result_tree(
    result_module,
    *,
    run_ids: list[int] | None = None,
    probe_items: bool = True,
    max_probe_items: int = 400,
) -> dict[str, Any]:
    """Recursively inventory the real 0D/1D result tree of a solved project.

    Nothing is assumed about which nodes exist.  Folders are separated from
    leaves; every leaf is probed through the vendor result API so that the
    recorded metadata (title / labels / run id / sample count) is read back from
    CST rather than inferred from the path string.
    """

    tree = list(result_module.get_tree_items())

    all_run_ids = []

    try:
        all_run_ids = [
            int(value)
            for value in result_module.get_all_run_ids()
        ]
    except Exception as exc:  # noqa: BLE001
        all_run_ids = []
        all_run_ids_error = repr(exc)
    else:
        all_run_ids_error = None

    nodes: list[dict[str, Any]] = []
    leaves: list[dict[str, Any]] = []
    probe_errors: list[dict[str, Any]] = []

    for path in tree:
        node: dict[str, Any] = {
            "tree_path": str(path),
            "depth": str(path).count("\\"),
            "is_leaf": False,
            "result_kind": classify_result_kind(str(path)),
        }

        nodes.append(node)

    # A path is a leaf when no other inventoried path extends it.  This rule is
    # derived from the tree itself, not from a hard-coded depth.
    path_set = {node["tree_path"] for node in nodes}

    leaf_paths = []

    for node in nodes:
        prefix = node["tree_path"] + "\\"

        if not any(other.startswith(prefix) for other in path_set):
            node["is_leaf"] = True
            leaf_paths.append(node)

    probed = 0

    for node in leaf_paths:
        path = node["tree_path"]

        per_path_run_ids: list[int] = []

        try:
            per_path_run_ids = [
                int(value)
                for value in result_module.get_run_ids(path)
            ]
        except Exception as exc:  # noqa: BLE001
            per_path_run_ids = []
            node["run_ids_error"] = repr(exc)

        node["run_ids"] = per_path_run_ids

        if not probe_items or probed >= max_probe_items:
            leaves.append(node)
            continue

        probed += 1

        try:
            item = result_module.get_result_item(path)

            y_raw = item.get_ydata()

            complex_valued = False
            sample_count = None

            try:
                sample_count = int(item.length)
            except Exception:  # noqa: BLE001
                sample_count = None

            try:
                complex_valued = any(
                    isinstance(value, complex) for value in list(y_raw)[:4]
                )
            except Exception:  # noqa: BLE001
                complex_valued = False

            node.update(
                {
                    "probe_ok": True,
                    "title": repr(getattr(item, "title", None)),
                    "x_label": repr(getattr(item, "xlabel", None)),
                    "y_label": repr(getattr(item, "ylabel", None)),
                    "item_tree_path": repr(getattr(item, "treepath", None)),
                    "item_run_id": _safe_int(getattr(item, "run_id", None)),
                    "sample_count": sample_count,
                    "complex_valued": complex_valued,
                    "has_ref_impedance": _has_ref_impedance(item),
                    "evidence": EVIDENCE_VENDOR_API,
                }
            )

        except Exception as exc:  # noqa: BLE001
            node["probe_ok"] = False
            node["probe_error"] = repr(exc)

            probe_errors.append({"tree_path": path, "error": repr(exc)})

        leaves.append(node)

    return {
        "inventory_schema_version": RESULT_REF_SCHEMA_VERSION,
        "tree_item_count": len(nodes),
        "leaf_count": len(leaf_paths),
        "probed_leaf_count": probed,
        "run_ids": run_ids if run_ids is not None else all_run_ids,
        "all_run_ids": all_run_ids,
        "all_run_ids_error": all_run_ids_error,
        "nodes": nodes,
        "leaves": leaves,
        "probe_errors": probe_errors,
        "inventory_hash": canonical_hash(
            [
                {
                    "tree_path": node["tree_path"],
                    "is_leaf": node["is_leaf"],
                    "sample_count": node.get("sample_count"),
                    "item_run_id": node.get("item_run_id"),
                }
                for node in nodes
            ]
        ),
        "evidence": EVIDENCE_VENDOR_API,
        "note": (
            "Inventory is read back from the CST result database; no path in "
            "this structure was assumed by the caller."
        ),
    }


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except Exception:  # noqa: BLE001
        return None


def _has_ref_impedance(item) -> bool:
    try:
        item.get_ref_imp_data()
        return True
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------------- extraction


def to_complex(value: Any) -> complex:
    if isinstance(value, complex):
        return value

    if isinstance(value, bool):
        raise TypeError("boolean is not a valid result sample")

    if isinstance(value, (int, float)):
        return complex(float(value), 0.0)

    # vendor quantity objects expose __complex__ / __float__
    try:
        return complex(value)
    except Exception:  # noqa: BLE001
        pass

    try:
        return complex(float(value), 0.0)
    except Exception as exc:  # noqa: BLE001
        raise TypeError(
            f"cannot convert result sample to complex: {value!r} "
            f"type={type(value).__name__}"
        ) from exc


def extract_1d_arrays(
    result_item,
    *,
    complex_valued: bool | None = None,
) -> dict[str, Any]:
    """Fetch x / real y / imaginary y / magnitude / phase from one 1D item."""

    x_raw = list(result_item.get_xdata())
    y_raw = list(result_item.get_ydata())

    if len(x_raw) != len(y_raw):
        raise ValueError(
            f"X/Y sample count mismatch: {len(x_raw)} != {len(y_raw)}"
        )

    if not x_raw:
        raise ValueError("result item contains zero samples")

    x = [float(value) for value in x_raw]
    y = [to_complex(value) for value in y_raw]

    if complex_valued is None:
        complex_valued = any(abs(value.imag) > 0.0 for value in y)

    y_real = [value.real for value in y]
    y_imag = [value.imag for value in y]
    magnitude = [abs(value) for value in y]
    phase_rad = [
        math.atan2(value.imag, value.real) for value in y
    ]

    payload = {
        "sample_count": len(x),
        "x": x,
        "y_real": y_real,
        "y_imag": y_imag,
        "magnitude": magnitude,
        "phase_rad": phase_rad,
        "complex_valued": bool(complex_valued),
    }

    payload["x_sha256"] = sha256_text(canonical_json(float_series_token(x)))
    payload["y_sha256"] = sha256_text(
        canonical_json(
            {
                "real": float_series_token(y_real),
                "imag": float_series_token(y_imag),
            }
        )
    )
    payload["data_sha256"] = sha256_text(
        canonical_json(
            {
                "x": float_series_token(x),
                "real": float_series_token(y_real),
                "imag": float_series_token(y_imag),
            }
        )
    )

    return payload


# ------------------------------------------------ numeric persistence check


def compare_numeric_series(
    first: list[float],
    second: list[float],
) -> dict[str, Any]:
    """Exact comparison.  No tolerance is invented.

    The two series are read from the same result database entry through the same
    vendor API, so they are expected to be the identical serialised array.  The
    primary verdict is therefore exact equality; a mismatch is reported for
    investigation and never silently relaxed into a pass.
    """

    result: dict[str, Any] = {
        "protocol": PROTOCOL_EXACT,
        "length_first": len(first),
        "length_second": len(second),
        "length_equal": len(first) == len(second),
        "equal": False,
        "mismatch_count": None,
        "first_mismatch_index": None,
        "max_abs_diff": None,
        "bitwise_equal": False,
    }

    if len(first) != len(second):
        result["protocol"] = PROTOCOL_INVESTIGATE
        return result

    mismatch_indices = []
    max_abs_diff = 0.0

    for index, (left, right) in enumerate(zip(first, second)):
        if left == right and math.copysign(1.0, left) == math.copysign(1.0, right):
            continue

        if left == right:
            # +0.0 vs -0.0 : numerically identical but not bit-identical
            mismatch_indices.append(index)
            continue

        mismatch_indices.append(index)
        max_abs_diff = max(max_abs_diff, abs(left - right))

    result["mismatch_count"] = len(mismatch_indices)
    result["bitwise_equal"] = not mismatch_indices
    result["equal"] = not mismatch_indices
    result["max_abs_diff"] = max_abs_diff if mismatch_indices else 0.0

    if mismatch_indices:
        result["first_mismatch_index"] = mismatch_indices[0]
        result["protocol"] = PROTOCOL_INVESTIGATE

    return result


def compare_extractions(
    before: dict[str, Any],
    after: dict[str, Any],
) -> dict[str, Any]:
    """Compare two extractions of the same result item."""

    checks = {
        "x": compare_numeric_series(before["x"], after["x"]),
        "y_real": compare_numeric_series(before["y_real"], after["y_real"]),
        "y_imag": compare_numeric_series(before["y_imag"], after["y_imag"]),
    }

    unchanged = (
        before["data_sha256"] == after["data_sha256"]
        and before["x_sha256"] == after["x_sha256"]
        and before["y_sha256"] == after["y_sha256"]
    )

    return {
        "comparison_protocol": PROTOCOL_EXACT,
        "rationale": (
            "Both arrays are read from the same treepath / run_id of the same "
            "unpacked CST project through cst.results.get_xdata/get_ydata. "
            "The same serialised array is expected, so exact float64 equality "
            "is the primary criterion; no tolerance is applied."
        ),
        "sample_count_before": before["sample_count"],
        "sample_count_after": after["sample_count"],
        "sample_count_equal": (
            before["sample_count"] == after["sample_count"]
        ),
        "data_sha256_before": before["data_sha256"],
        "data_sha256_after": after["data_sha256"],
        "data_sha256_equal": unchanged,
        "series": checks,
        "numeric_persisted": bool(
            unchanged
            and all(check["equal"] for check in checks.values())
        ),
    }


# ------------------------------------------------------------- CST-bound


def open_result_module(project_path: str):
    """Open the CST result database of an unpacked project (vendor API)."""

    import cst.results as cr

    project_file = cr.ProjectFile(
        str(project_path),
        allow_interactive=True,
    )

    return project_file.get_3d()


def read_result_ref(result_module, tree_path: str, run_id: int = 0) -> dict[str, Any]:
    """Read one tree item into a ResultRef + raw metadata."""

    item = result_module.get_result_item(tree_path, run_id)

    ref = make_result_ref(
        tree_path=tree_path,
        title=repr(getattr(item, "title", None)),
        x_label=repr(getattr(item, "xlabel", None)),
        y_label=repr(getattr(item, "ylabel", None)),
        run_id=_safe_int(getattr(item, "run_id", None)),
        complex_valued=False,
        monitor=None,
        frequency=None,
    )

    return {"ref": ref, "item": item}


def select_results(
    inventory: dict[str, Any],
    *,
    kinds: tuple[str, ...] = ("s_parameter",),
    require_samples: bool = True,
) -> list[dict[str, Any]]:
    """Pick inventory leaves that satisfy the caller's requirement."""

    selected = []

    for leaf in inventory.get("leaves", []):
        if not leaf.get("is_leaf"):
            continue

        if kinds and leaf.get("result_kind") not in kinds:
            continue

        if require_samples and not leaf.get("sample_count"):
            continue

        selected.append(leaf)

    return selected
