"""CST_AI plugin layer — observable identity and routing.

Two jobs:

  1. IDENTITY: make it impossible for two different quantities to look like the
     same observable later.  An S-parameter carries its (output, input) mode; a
     Floquet channel carries face and mode index; a farfield quantity carries its
     monitor, frequency, quantity and coordinate system.

  2. ROUTING: dispatch to the module that already implements the mathematics.
     Nothing is re-derived here - this file contains no physics.
"""

from __future__ import annotations

import re

from .errors import ObservableIdentityError, ObservableUnresolved

OBSERVABLE_VERSION = "1.15.0"

# observable kinds
S_PARAMETER = "S_PARAMETER"
FLOQUET_CHANNEL = "FLOQUET_CHANNEL"
POWER_RTA = "POWER_RTA"
RESONANCE = "RESONANCE"
FARFIELD_SCALAR = "FARFIELD_SCALAR"
FARFIELD_FIELD = "FARFIELD_FIELD"
CONVERGENCE = "CONVERGENCE"
SWEEP_POINT = "SWEEP_POINT"
COMPARISON = "COMPARISON"

KINDS = (
    S_PARAMETER, FLOQUET_CHANNEL, POWER_RTA, RESONANCE, FARFIELD_SCALAR,
    FARFIELD_FIELD, CONVERGENCE, SWEEP_POINT, COMPARISON,
)

#: required identity keys per kind - an observable missing these is REJECTED
REQUIRED_IDENTITY = {
    S_PARAMETER: ("output", "input"),
    FLOQUET_CHANNEL: ("face", "mode"),
    POWER_RTA: ("incident_mode",),
    RESONANCE: ("source_observable", "incident_mode", "frequency_window"),
    FARFIELD_SCALAR: ("monitor", "frequency_ghz", "quantity", "coordinate_system"),
    FARFIELD_FIELD: ("monitor", "frequency_ghz", "coordinate_system"),
    CONVERGENCE: ("quantity", "axis"),
    SWEEP_POINT: ("parameter", "point_id"),
    COMPARISON: ("reference_id", "simulation_artifact_id"),
}

_SPARAM = re.compile(
    r"^S(?P<out_face>Zmin|Zmax)\((?P<out_mode>\d+)\),"
    r"(?P<in_face>Zmin|Zmax)\((?P<in_mode>\d+)\)$"
)

#: farfield plot-mode literals as documented by the vendor FarfieldPlot reference
FARFIELD_PLOT_MODES = {
    "directivity": "directivity",
    "gain": "gain",
    "realized_gain": "realized gain",  # NOTE: the literal contains a space
    "efield": "efield",
}


def parse_floquet_channel(name: str) -> dict:
    """Name -> identity.  The parser is the V1.9B convention, reused."""

    match = _SPARAM.match(name)

    if not match:
        raise ObservableIdentityError(f"not a Floquet S channel: {name!r}")

    g = match.groupdict()

    return {
        "output": {"face": g["out_face"], "mode": int(g["out_mode"])},
        "input": {"face": g["in_face"], "mode": int(g["in_mode"])},
        "channel_name": name,
    }


def make_observable_spec(kind: str, identity: dict, *, unit: str | None = None,
                         notes: str | None = None) -> dict:
    if kind not in KINDS:
        raise ObservableIdentityError(
            f"unknown observable kind {kind!r}; allowed: {list(KINDS)}"
        )

    missing = [
        key for key in REQUIRED_IDENTITY[kind]
        if key not in (identity or {})
    ]

    if missing:
        raise ObservableIdentityError(
            f"{kind} identity is missing required key(s) {missing}; "
            f"required: {list(REQUIRED_IDENTITY[kind])}"
        )

    return {
        "observable_version": OBSERVABLE_VERSION,
        "kind": kind,
        "identity": dict(identity),
        "unit": unit,
        "notes": notes,
        #: the same name in two places must resolve to the same identity
        "identity_hash": _identity_hash(kind, identity),
    }


def _identity_hash(kind: str, identity: dict) -> str:
    import hashlib
    import json

    return "sha256:" + hashlib.sha256(
        json.dumps({"kind": kind, "identity": identity}, sort_keys=True,
                   default=repr).encode("utf-8")
    ).hexdigest()


# ------------------------------------------------------------------- routing

ROUTES = {
    S_PARAMETER: "cst_result_api_v19.extract_1d_arrays",
    FLOQUET_CHANNEL: "cst_result_api_v19.inventory_result_tree",
    POWER_RTA: "V1.9B normalisation over the Floquet channels",
    RESONANCE: "v110c_analysis.extract_resonance",
    FARFIELD_SCALAR: "FarfieldPlot.SetPlotMode + GetListItem (V1.11B chain)",
    FARFIELD_FIELD: "SelectTreeItem + AddListEvaluationPoint (V1.11A chain)",
    CONVERGENCE: "1D Results readers (Adaptive Meshing / Convergence)",
    SWEEP_POINT: "v112_sweep_engine",
    COMPARISON: "v113a_comparator",
}


def route(observable_spec: dict) -> dict:
    """Say which module would answer this observable - without calling it."""

    kind = observable_spec.get("kind")

    if kind not in ROUTES:
        raise ObservableUnresolved(f"no route for observable kind {kind!r}")

    return {
        "kind": kind,
        "route": ROUTES[kind],
        "identity_hash": observable_spec.get("identity_hash"),
        "dispatched": False,
        "note": "routing only; the target module performs the work",
    }


def assert_distinct(spec_a: dict, spec_b: dict) -> bool:
    """True when two specs are genuinely the same observable."""

    return (
        spec_a.get("kind") == spec_b.get("kind")
        and spec_a.get("identity_hash") == spec_b.get("identity_hash")
    )
