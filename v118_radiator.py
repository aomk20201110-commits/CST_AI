"""V1.18 radiator extraction adapter - LIBRARY ONLY.

Why this file exists
--------------------
V1.11A proved that a radiator's stored farfield CAN be read back numerically, and
V1.11B proved the directivity/gain/realized-gain quantities can be read too.  Both
rounds proved it from one-off scripts with a `main()`.  V1.18 needs the same
capability on the *public* path, so the verified sequence is packaged here as
importable functions that `cst_ai_plugin.orchestrator._stage_extract` calls.

Hard rules honoured here (V1.11A/V1.11B, unchanged)
--------------------------------------------------
* ZERO solves, ZERO rebuilds, ZERO saves.  `SelectTreeItem` is a VBA GLOBAL
  STATEMENT: it changes which stored result is the active session selection and
  does NOT touch the model history.
* `AddListItem` does not exist in this CST build; the official reference for this
  build documents `AddListEvaluationPoint`.
* `CalculateList(Name)` and `GetListItem(index, fieldComponent)` are the only read
  primitives; the quantity is selected by `SetPlotMode`, never by renaming an
  output.
* Everything read here is LINEAR (`SetScaleLinear "True"`), so power-like
  quantities convert to dB with `10*log10`.
* The native HPBW getter (`GetAngularWidthXdB`) is only defined for
  `.Plottype "Polar"`, so HPBW is SELF-EXTRACTED from the principal cut.  That is
  an upstream API_GAP, not a missing capability.

This module is never reachable from a DSH tool directly: the tool surface calls
`cst_ai_plugin.public`, which drives the orchestrator, which calls this adapter.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import v111a_analysis as v111a_analysis
import v111a_export as v111a_export

V118_RADIATOR_VERSION = "1.18.0"

EVIDENCE_ACTIVATION = "V1.11A SelectTreeItem + FarfieldPlot list evaluation"
EVIDENCE_QUANTITY = "V1.11B SetPlotMode quantity readback"

# the fieldComponent used to read a scalar QUANTITY (V1.11B)
FIELD_COMPONENT_ABS = "spherical abs"

# the complex spherical components used to read the FIELD (V1.11A)
FIELD_COMPONENTS_COMPLEX = ("th_re", "th_im", "ph_re", "ph_im")

# (observable label, native SetPlotMode literal, normalization reference)
QUANTITIES = (
    ("DIRECTIVITY", "directivity", "total radiated power"),
    ("GAIN", "gain", "accepted power"),
    ("REALIZED_GAIN", "realized gain", "stimulated power"),
)

DEGENERACY_REL_TOL = v111a_analysis.DEGENERACY_REL_TOL
AZIMUTH_SYMMETRY_REL_TOL = 0.05

# evaluation-list insertion order, verified in V1.11A: phi is the OUTER loop and
# theta the INNER loop, so index -> (theta fastest, then phi)
EVALUATION_ORDER = "phi outer, theta inner"

CALCULATE_LIST_FIELD_ARGUMENT = "farfield name (V1.11A)"
CALCULATE_LIST_QUANTITY_ARGUMENT = "empty string (V1.11B)"

frange = v111a_export.frange
linear_crossing = v111a_analysis.linear_crossing


def safe(fn, *args) -> dict:
    """Call a COM proxy method and return a JSON-able envelope."""

    try:
        return {"ok": True, "value": fn(*args), "error": None}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "value": None, "error": repr(exc)[:300]}


def is_sentinel(entry: dict) -> bool:
    """A farfield getter that was never activated reports a known sentinel."""

    value = entry.get("value")

    if not entry.get("ok"):
        return True

    if isinstance(value, (int, float)):
        return value in (0.0, -200.0)

    if isinstance(value, (list, tuple)):
        return all(v in (0.0, -200.0) for v in value)

    return False


def _points_to_rows(points: list[dict]) -> list[dict]:
    for row in points:
        th_re, th_im = row.get("th_re"), row.get("th_im")
        ph_re, ph_im = row.get("ph_re"), row.get("ph_im")

        if None in (th_re, th_im, ph_re, ph_im):
            row["E_theta_mag"] = None
            row["E_phi_mag"] = None
            row["E_total_sq"] = None
            continue

        e_theta_sq = th_re * th_re + th_im * th_im
        e_phi_sq = ph_re * ph_re + ph_im * ph_im

        row["E_theta_mag"] = math.sqrt(e_theta_sq)
        row["E_phi_mag"] = math.sqrt(e_phi_sq)
        row["E_total_sq"] = e_theta_sq + e_phi_sq

    return points


# --------------------------------------------------------------------- stages
def activate(model3d, tree_path: str) -> dict:
    """Make an EXISTING stored farfield the active selection.

    This is the step V1.11 was missing.  It is a session/GUI selection, not a
    model change: no history entry, no rebuild, no save.
    """

    return {
        "method": "SelectTreeItem",
        "tree_path": tree_path,
        "present_on_model3d": hasattr(model3d, "SelectTreeItem"),
        "call": safe(model3d.SelectTreeItem, tree_path),
        "modifies_model_history": False,
        "is_gui_or_session_state": True,
        "evidence": EVIDENCE_ACTIVATION,
    }


def configure(farfield_plot, mode: str = "efield") -> dict:
    """The shipped macro's plot configuration, in the verified order."""

    return {
        "Reset": safe(farfield_plot.Reset),
        "SetPlotMode": safe(farfield_plot.SetPlotMode, mode),
        "SetScaleLinear": safe(farfield_plot.SetScaleLinear, "True"),
        "Origin": safe(farfield_plot.Origin, "bbox"),
        "UseFarfieldApproximation": safe(
            farfield_plot.UseFarfieldApproximation, "True"
        ),
        "plot_mode": mode,
        "scale": "LINEAR",
    }


def add_evaluation_points(farfield_plot, thetas, phis) -> dict:
    """`AddListEvaluationPoint(polar, lateral, radius, coord, tf_type, freq_time)`.

    `tf_type` is only supported by BROADBAND monitor results; this adapter reads a
    single-frequency monitor, so it is left empty and the current frequency
    setting is used.
    """

    added = 0
    error = None

    for phi in phis:
        for theta in thetas:
            entry = safe(
                farfield_plot.AddListEvaluationPoint,
                theta,
                phi,
                0.0,
                "spherical",
                "",
                0.0,
            )

            if not entry.get("ok"):
                error = entry.get("error")
                break

            added += 1

        if error:
            break

    return {
        "points_added": added,
        "error": error,
        "order": EVALUATION_ORDER,
        "signature": (
            "AddListEvaluationPoint(polarAngleInDegree, lateralAngleInDegree, "
            "radius, coordinateSystem, tf_type, freq_time)"
        ),
        "api_name_correction": (
            "the shipped macro calls AddListItem, which does NOT exist in this "
            "CST build; the official reference documents AddListEvaluationPoint"
        ),
    }


def read_field_grid(farfield_plot, thetas, phis) -> dict:
    """Read the complex spherical components over the whole grid."""

    points = []
    error = None
    index = 0

    for phi in phis:
        for theta in thetas:
            row = {"index": index, "theta": theta, "phi": phi}

            for component in FIELD_COMPONENTS_COMPLEX:
                entry = safe(farfield_plot.GetListItem, index, component)

                if not entry.get("ok"):
                    error = entry.get("error")
                    row[component] = None
                else:
                    row[component] = entry.get("value")

            points.append(row)
            index += 1

            if error:
                break

        if error:
            break

    return {
        "ok": error is None and len(points) == len(thetas) * len(phis),
        "points": _points_to_rows(points),
        "points_read": len(points),
        "error": error,
        "components": list(FIELD_COMPONENTS_COMPLEX),
        "derived": {
            "E_theta_mag": "sqrt(th_re^2 + th_im^2)",
            "E_phi_mag": "sqrt(ph_re^2 + ph_im^2)",
            "E_total_sq": "th_re^2 + th_im^2 + ph_re^2 + ph_im^2",
        },
    }


def read_quantity(farfield_plot, label: str, mode: str, normalization: str,
                  thetas, phis) -> dict:
    """Read one native power-like QUANTITY over the same grid, in linear scale."""

    entry = {
        "observable": label,
        "plot_mode_literal": mode,
        "normalization": normalization,
        "field_component": FIELD_COMPONENT_ABS,
        "linear_or_db": "LINEAR",
        "evidence": EVIDENCE_QUANTITY,
    }

    entry["configuration"] = configure(farfield_plot, mode)
    entry["evaluation_list"] = add_evaluation_points(
        farfield_plot, thetas, phis
    )
    entry["CalculateList"] = safe(
        farfield_plot.CalculateList, ""
    )

    values = []
    error = None
    index = 0

    for phi in phis:
        for theta in thetas:
            r = safe(farfield_plot.GetListItem, index, FIELD_COMPONENT_ABS)

            if not r.get("ok"):
                error = r.get("error")
                break

            values.append(
                {"index": index, "theta": theta, "phi": phi,
                 "value": r.get("value")}
            )
            index += 1

        if error:
            break

    entry["points_read"] = len(values)
    entry["read_error"] = error

    numeric = [v["value"] for v in values
               if isinstance(v["value"], (int, float))]

    if values and numeric:
        peak = max(values, key=lambda v: v["value"]
                   if isinstance(v["value"], (int, float)) else -1)

        # SetScaleLinear was requested, so the reader converted a power-like
        # quantity; dB therefore follows 10*log10 (20*log10 is for field ratios).
        entry["peak_linear"] = peak["value"]
        entry["theta_peak"] = peak["theta"]
        entry["phi_peak"] = peak["phi"]
        entry["peak_dB"] = (
            10.0 * math.log10(peak["value"]) if peak["value"] > 0 else None
        )
        entry["min_linear"] = min(numeric)
        entry["max_linear"] = max(numeric)

        ring = [v for v in values if v["theta"] == peak["theta"]]
        ring_values = [v["value"] for v in ring]

        entry["ring_at_peak_theta"] = {
            "theta": peak["theta"],
            "phi_count": len(ring),
            "min": min(ring_values) if ring_values else None,
            "max": max(ring_values) if ring_values else None,
            "relative_spread": (
                (max(ring_values) - min(ring_values)) / max(ring_values)
                if ring_values and max(ring_values) else None
            ),
        }
        entry["equivalent_peak_count"] = sum(
            1 for v in values
            if isinstance(v["value"], (int, float))
            and v["value"] >= peak["value"] * (1 - DEGENERACY_REL_TOL)
        )
        entry["data_sha256"] = sha256_json(values)

    return entry


def global_quantities(farfield_plot) -> dict:
    """The scalar getters that are sentinels until the result is activated."""

    raw = {
        "GetMax": safe(farfield_plot.GetMax),
        "GetTRP": safe(farfield_plot.GetTRP),
        "GetRadiationEfficiency": safe(farfield_plot.GetRadiationEfficiency),
        "GetTotalEfficiency": safe(farfield_plot.GetTotalEfficiency),
    }

    return {
        "raw": raw,
        "values": {k: v.get("value") for k, v in raw.items()},
        "sentinels": {k: is_sentinel(v) for k, v in raw.items()},
        "activated": not any(is_sentinel(v) for v in raw.values()),
        "native_api_gaps": {
            "GetMainLobeDirection": safe(farfield_plot.GetMainLobeDirection),
            "GetAngularWidthXdB": safe(farfield_plot.GetAngularWidthXdB),
        },
        "api_gap_note": (
            "GetMainLobeDirection / GetAngularWidthXdB are documented for "
            ".Plottype \"Polar\" only; on this result they raise.  Beam direction "
            "and HPBW are therefore derived from the evaluation list."
        ),
    }


# ----------------------------------------------------------------- derivation
def principal_cut(points: list[dict], phi_value: float) -> dict:
    """A theta cut at fixed phi - the cut HPBW is measured on."""

    rows = sorted(
        (r for r in points if abs(r["phi"] - phi_value) < 1e-9),
        key=lambda r: r["theta"],
    )

    return {
        "cut_definition": f"fixed phi = {phi_value} deg, theta varies",
        "phi_deg": phi_value,
        "points": [
            {"theta": r["theta"], "E_total_sq": r["E_total_sq"]}
            for r in rows
        ],
    }


def azimuth_cut(points: list[dict], theta_value: float) -> dict:
    """A phi cut at fixed theta - shows whether the maximum is a ring."""

    rows = sorted(
        (r for r in points if abs(r["theta"] - theta_value) < 1e-9),
        key=lambda r: r["phi"],
    )

    return {
        "cut_definition": f"fixed theta = {theta_value} deg, phi varies",
        "theta_deg": theta_value,
        "points": [{"phi": r["phi"], "E_total_sq": r["E_total_sq"]} for r in rows],
    }


def hpbw(cut: dict) -> dict:
    """Half-power beamwidth from a theta cut, by piecewise linear interpolation.

    The native getter is an upstream API_GAP (see `global_quantities`), so this
    self-extracted value is the reported HPBW.
    """

    result = {
        "cut_used": cut.get("cut_definition"),
        "HPBW": "UNRESOLVED",
        "HPBW_deg": None,
        "interpolation": "piecewise linear on E_total_sq, -3 dB == half power",
    }

    pts = [p for p in cut.get("points") or () if p.get("E_total_sq") is not None]

    if not pts:
        result["reason"] = "cut carries no numeric samples"
        return result

    values = [p["E_total_sq"] for p in pts]
    angles = [p["theta"] for p in pts]

    peak_index = values.index(max(values))
    peak = values[peak_index]
    half = peak / 2.0

    left = None
    right = None

    for i in range(peak_index, 0, -1):
        if values[i - 1] <= half <= values[i]:
            left = linear_crossing(
                angles[i - 1], values[i - 1], angles[i], values[i], half
            )
            break

    for i in range(peak_index, len(values) - 1):
        if values[i] >= half >= values[i + 1]:
            right = linear_crossing(
                angles[i], values[i], angles[i + 1], values[i + 1], half
            )
            break

    result.update({
        "peak_angle_deg": angles[peak_index],
        "peak_value": peak,
        "half_level": half,
        "left_3db_angle_deg": left,
        "right_3db_angle_deg": right,
        "crossings_found": left is not None and right is not None,
        "wraps_0_360": False,
        "multiple_equal_peaks_in_cut": (
            sum(1 for v in values if v >= peak * (1 - DEGENERACY_REL_TOL)) > 1
        ),
    })

    if left is not None and right is not None:
        result["HPBW_deg"] = right - left
        result["HPBW"] = right - left
    else:
        result["reason"] = (
            "the half-power level is not crossed on both sides inside the "
            "measured theta range"
        )

    return result


def pattern_metrics(points: list[dict]) -> dict:
    """Peak, maximum topology, principal cuts and HPBW - all self-derived."""

    numeric = [r for r in points if r.get("E_total_sq") is not None]

    if not numeric:
        return {"PEAK_VALUE": None, "reason": "no numeric samples"}

    best = max(numeric, key=lambda r: r["E_total_sq"])
    peak_value = best["E_total_sq"]

    equivalent = [
        r for r in numeric
        if peak_value and r["E_total_sq"] >= peak_value * (1 - DEGENERACY_REL_TOL)
    ]

    theta_of_peaks = sorted({r["theta"] for r in equivalent})
    phi_of_peaks = sorted({r["phi"] for r in equivalent})

    if len(equivalent) > 1 and len(theta_of_peaks) == 1:
        topology = "NEAR_DEGENERATE_AZIMUTH_RING"
    elif len(equivalent) > 1:
        topology = "MULTIPLE_EQUIVALENT_MAXIMA"
    else:
        topology = "SINGLE_MAXIMUM"

    # the rotationally symmetric case: the ring is the maximum, and demanding one
    # particular phi sample be the global maximum would be wrong
    ring = [r["E_total_sq"] for r in numeric if abs(r["theta"] - best["theta"]) < 1e-9]

    ring_spread = None

    if ring and max(ring):
        ring_spread = (max(ring) - min(ring)) / max(ring)

    main_cut = principal_cut(points, phi_of_peaks[0] if phi_of_peaks else 0.0)
    az_cut = azimuth_cut(points, best["theta"])

    for cut in (main_cut, az_cut):
        digest = hashlib.sha256()

        for p in cut["points"]:
            key = p.get("theta", p.get("phi"))
            digest.update(f"{key:.6g},{p['E_total_sq']:.15g}".encode())

        cut["raw_data_sha256"] = "sha256:" + digest.hexdigest()
        cut["peak_in_cut"] = max(
            (p["E_total_sq"] for p in cut["points"] if p["E_total_sq"] is not None),
            default=None,
        )

    beam = {
        "peak_value_E_total_sq": peak_value,
        "peak_theta_deg": best["theta"],
        "peak_phi_deg": best["phi"],
        "maximum_topology": topology,
        "equivalent_peak_count": len(equivalent),
        "relative_tolerance_used": DEGENERACY_REL_TOL,
        "theta_values_of_peaks": theta_of_peaks,
        "phi_values_of_peaks": phi_of_peaks,
        "phi_span_deg": (
            (max(phi_of_peaks) - min(phi_of_peaks))
            if len(phi_of_peaks) > 1 else 0.0
        ),
        "ring_at_peak_theta_relative_spread": ring_spread,
        "single_beam_direction_claimed": topology == "SINGLE_MAXIMUM",
    }

    return {
        "PEAK_VALUE": peak_value,
        "PEAK_THETA": best["theta"],
        "PEAK_PHI": best["phi"],
        "beam_direction": beam,
        "principal_cut": main_cut,
        "azimuth_cut": az_cut,
        "HPBW_result": hpbw(main_cut),
        "derivation": (
            "self-extracted from the native evaluation list; the native HPBW "
            "getter is an upstream API_GAP on this result type"
        ),
    }


def radiator_sanity(points: list[dict], metrics: dict) -> dict:
    """The pre-declared V1.11 qualitative sanity checks for a z-directed dipole.

    Deliberately qualitative: no absolute gain target is asserted, and NO data is
    adjusted to satisfy a criterion.
    """

    by_key = {
        (r["theta"], r["phi"]): r["E_total_sq"]
        for r in points if r.get("E_total_sq") is not None
    }

    axis_0 = by_key.get((0.0, 0.0))
    axis_180 = by_key.get((180.0, 0.0))
    broadside = by_key.get((90.0, 0.0))
    peak_theta = metrics.get("PEAK_THETA")

    ring = [r["E_total_sq"] for r in points
            if r.get("E_total_sq") is not None and abs(r["theta"] - 90.0) < 1e-9]

    ring_spread = (
        (max(ring) - min(ring)) / max(ring) if ring and max(ring) else None
    )

    conditions = {
        "axis_below_broadside": (
            axis_0 is not None and broadside is not None
            and axis_0 < broadside
            and axis_180 is not None and axis_180 < broadside
        ),
        "broadside_ring_is_the_maximum": peak_theta == 90.0,
        "azimuthally_symmetric": (
            ring_spread is not None and ring_spread < AZIMUTH_SYMMETRY_REL_TOL
        ),
    }

    return {
        "axis_theta_0_E_total_sq": axis_0,
        "axis_theta_180_E_total_sq": axis_180,
        "broadside_theta_90_E_total_sq": broadside,
        "axis_to_broadside_ratio_0": (
            axis_0 / broadside if axis_0 and broadside else None
        ),
        "axis_to_broadside_ratio_180": (
            axis_180 / broadside if axis_180 and broadside else None
        ),
        "broadside_ring_sample_count": len(ring),
        "broadside_ring_relative_spread": ring_spread,
        "expectations_pre_declared": [
            "null along the dipole axis (theta ~ 0 and ~ 180)",
            "broadside maximum (theta ~ 90)",
            "azimuthal symmetry",
        ],
        "conditions_checked": conditions,
        "SANITY_VERDICT": (
            "SANITY_CONSISTENT" if all(conditions.values())
            else "SANITY_PARTIALLY_CONSISTENT"
        ),
        "absolute_gain_target_asserted": False,
        "no_tolerance_used_to_adjust_data": True,
    }


# -------------------------------------------------------------------- helpers
def sha256_json(payload) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=repr).encode()
    ).hexdigest()


def sha256_file(path) -> str | None:
    p = Path(path)

    if not p.is_file():
        return None

    digest = hashlib.sha256()

    with p.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)

    return "sha256:" + digest.hexdigest()


def write_angular_artifact(path, points: list[dict], meta: dict) -> dict:
    """Large arrays never travel inside a tool response - they land here."""

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "artifact": "v118_radiator_angular_grid",
        "adapter_version": V118_RADIATOR_VERSION,
        "meta": meta,
        "point_count": len(points),
        "points": points,
    }

    p.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1, default=repr) + "\n",
        encoding="utf-8",
    )

    return {
        "path": str(p),
        "bytes": p.stat().st_size,
        "sha256": sha256_file(p),
        "point_count": len(points),
        "summary": {
            "theta_values": sorted({r["theta"] for r in points}),
            "phi_values": sorted({r["phi"] for r in points}),
        },
    }


def extract(model3d, *, tree_path: str, farfield_name: str, thetas, phis,
            quantities=QUANTITIES) -> dict:
    """The whole verified read chain for one already-solved radiator result."""

    farfield_plot = model3d.FarfieldPlot

    evidence = {
        "adapter": "v118_radiator",
        "adapter_version": V118_RADIATOR_VERSION,
        "farfield_name": farfield_name,
        "tree_path": tree_path,
        "grid": {
            "theta_count": len(thetas),
            "phi_count": len(phis),
            "point_count": len(thetas) * len(phis),
            "theta_range": [min(thetas), max(thetas)] if thetas else None,
            "phi_range": [min(phis), max(phis)] if phis else None,
        },
        "evaluation_order": EVALUATION_ORDER,
        "used_native_modules": [
            "v111a_export.frange",
            "v111a_analysis.linear_crossing",
            "v111a_analysis.DEGENERACY_REL_TOL",
        ],
    }

    # sentinel baseline first: it proves the getters were meaningless BEFORE the
    # selection, so the change afterwards is attributable to activation
    before = global_quantities(farfield_plot)

    evidence["activation"] = activate(model3d, tree_path)
    evidence["configuration"] = configure(farfield_plot, "efield")

    added = add_evaluation_points(farfield_plot, thetas, phis)
    evidence["evaluation_list"] = added
    evidence["CalculateList"] = safe(farfield_plot.CalculateList, farfield_name)
    evidence["CalculateList_argument"] = CALCULATE_LIST_FIELD_ARGUMENT

    field = read_field_grid(farfield_plot, thetas, phis)

    evidence["field"] = {
        "ok": field["ok"],
        "points_read": field["points_read"],
        "error": field["error"],
        "components": field["components"],
        "derived": field["derived"],
    }

    after = global_quantities(farfield_plot)

    evidence["BEFORE_ACTIVATION"] = before
    evidence["AFTER_ACTIVATION"] = after
    evidence["sentinel_transition"] = {
        "before_all_sentinels": all(before["sentinels"].values()),
        "after_any_sentinel": any(after["sentinels"].values()),
        "proves_activation": (
            all(before["sentinels"].values())
            and not any(after["sentinels"].values())
        ),
    }

    quantities_read = {}

    for label, mode, normalization in quantities:
        quantities_read[label] = read_quantity(
            farfield_plot, label, mode, normalization, thetas, phis
        )

    evidence["quantities"] = quantities_read

    # leave the plot in the field mode the activation chain used
    evidence["restore_plot_mode"] = safe(farfield_plot.SetPlotMode, "efield")

    points = field["points"]
    metrics = pattern_metrics(points)

    evidence["pattern"] = {
        k: v for k, v in metrics.items()
        if k not in ("principal_cut", "azimuth_cut")
    }
    evidence["cuts"] = {
        "principal": metrics.get("principal_cut"),
        "azimuth": metrics.get("azimuth_cut"),
    }
    evidence["sanity"] = radiator_sanity(points, metrics)

    evidence["side_effects"] = {
        "solves": 0,
        "rebuilds": 0,
        "saves": 0,
        "model_history_modified": False,
        "reads_only": True,
    }

    evidence["_points"] = points

    return evidence
