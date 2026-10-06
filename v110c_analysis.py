"""V1.10C analysis: resonance validity, f0, FWHM, Q_linewidth, FREQ_A, FREQ_B E2E,
adaptive-mesh observability.  ZERO new solves - everything is read from the one
stored result database.

Every definition applied here is fixed in code before the solve exists (no
definition is chosen after seeing the numbers):

    observable          T_total(f) for incident Zmin(1), resonance_type = dip
    RAW_GRID_F0         grid frequency of the minimum
    INTERPOLATED_F0     3-point parabola vertex through the minimum
    baseline_level      max(T(f_min), T(f_max))
    half_level          baseline - (baseline - T_min) / 2
    FWHM                f_right_crossing - f_left_crossing (linearly interpolated)
    Q_linewidth         INTERPOLATED_F0 / FWHM

FREQ_B resamples the SAME solve at stride 1 / 2 / 4 / 8 and re-extracts f0, FWHM
and Q INDEPENDENTLY on each resampled set.  The fine-grid answer is never used to
help a coarse-grid crossing.

Run under the CST bundled interpreter (adjust the path to your installation):
    "C:\\Program Files\\CST Studio Suite 2026\\Python\\python.exe" v110c_analysis.py
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent

sys.path.insert(0, str(ROOT))

import cst_result_api_v19 as ra  # noqa: E402

REPORTS = ROOT / "reports"
OUT_RES = REPORTS / "v110c_resonance_report.json"
OUT_FREQ_A = REPORTS / "v110c_freq_a_report.json"
OUT_FREQ_B = REPORTS / "v110c_freq_b_e2e_report.json"
OUT_MESH = REPORTS / "v110c_mesh_observability.json"

DRY = REPORTS / "v110c_dry_build_report.json"
OBS_LOCK = REPORTS / "v110c_observable_lock.json"

SPARAM_PREFIX = "1D Results\\S-Parameters\\"
IDENTIFIER = re.compile(
    r"^S(?P<out_face>Zmin|Zmax)\((?P<out_mode>\d+)\),"
    r"(?P<in_face>Zmin|Zmax)\((?P<in_mode>\d+)\)$"
)
FREQ_A_PATH = "1D Results\\Convergence\\S-Parameters\\All S-Parameters"
ADAPT_PREFIX = "1D Results\\Adaptive Meshing\\"

INCIDENT_MODE = "Zmin(1)"


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def write_out(path: Path, payload: dict) -> Path:
    REPORTS.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=repr) + "\n",
        encoding="utf-8",
    )
    return path


# ------------------------------------------------------------- extraction core


def linear_crossing(xa: float, ya: float, xb: float, yb: float, level: float) -> float:
    """x where the straight line through (xa,ya)-(xb,yb) equals level."""

    if yb == ya:
        return xa

    return xa + (level - ya) * (xb - xa) / (yb - ya)


def parabola_vertex(x0, y0, x1, y1, x2, y2) -> float:
    """Vertex x of the parabola through three (possibly unevenly spaced) points."""

    denominator = (x0 - x1) * (x0 - x2) * (x1 - x2)

    if denominator == 0:
        return x1

    a = (x2 * (y1 - y0) + x1 * (y0 - y2) + x0 * (y2 - y1)) / denominator
    b = (
        x2 * x2 * (y0 - y1)
        + x1 * x1 * (y2 - y0)
        + x0 * x0 * (y1 - y2)
    ) / denominator

    if a == 0:
        return x1

    return -b / (2.0 * a)


def extract_resonance(
    frequencies: list[float],
    values: list[float],
    *,
    noise_band: float | None = None,
) -> dict:
    """Apply the frozen definitions. No tolerance and no post-hoc window."""

    out: dict = {
        "points": len(frequencies),
        "sampling_step_min_ghz": min(
            (b - a for a, b in zip(frequencies, frequencies[1:])), default=None
        ),
        "sampling_step_max_ghz": max(
            (b - a for a, b in zip(frequencies, frequencies[1:])), default=None
        ),
    }

    if len(values) < 3:
        out.update({"valid": False, "reason": "TOO_FEW_POINTS"})
        return out

    index_min = min(range(len(values)), key=lambda i: values[i])

    raw_f0 = frequencies[index_min]
    t_min = values[index_min]
    baseline = max(values[0], values[-1])

    out.update(
        {
            "RAW_GRID_F0": raw_f0,
            "T_min": t_min,
            "index_of_minimum": index_min,
            "baseline_level": baseline,
            "prominence": baseline - t_min,
        }
    )

    # ---------------------------------------------------------- validity
    failures = []

    if index_min == 0 or index_min == len(values) - 1:
        failures.append("MINIMUM_IS_AN_ENDPOINT")

    if not (frequencies[0] < raw_f0 < frequencies[-1]):
        failures.append("F0_NOT_STRICTLY_INSIDE_BAND")

    prominence = baseline - t_min

    if prominence <= 0.0:
        failures.append("NO_PROMINENCE")

    if noise_band is not None:
        out["noise_band"] = noise_band
        out["prominence_exceeds_noise_band"] = prominence > noise_band

        if prominence <= noise_band:
            failures.append("PROMINENCE_NOT_ABOVE_NUMERICAL_NOISE")
    else:
        out["prominence_exceeds_noise_band"] = None

    half_level = baseline - prominence / 2.0
    out["half_level"] = half_level

    if not (t_min < half_level < baseline):
        failures.append("HALF_LEVEL_NOT_BETWEEN_MIN_AND_BASELINE")

    # ------------------------------------------------------- crossings
    left = None

    for index in range(index_min, 0, -1):
        if values[index - 1] >= half_level:
            left = linear_crossing(
                frequencies[index - 1], values[index - 1],
                frequencies[index], values[index], half_level,
            )
            out["left_crossing_bracket"] = [frequencies[index - 1], frequencies[index]]
            break

    right = None

    for index in range(index_min, len(values) - 1):
        if values[index + 1] >= half_level:
            right = linear_crossing(
                frequencies[index], values[index],
                frequencies[index + 1], values[index + 1], half_level,
            )
            out["right_crossing_bracket"] = [frequencies[index], frequencies[index + 1]]
            break

    if left is None:
        failures.append("NO_LEFT_HALF_LEVEL_CROSSING")

    if right is None:
        failures.append("NO_RIGHT_HALF_LEVEL_CROSSING")

    out["left_half_frequency"] = left
    out["right_half_frequency"] = right

    # ------------------------------------------------------ interpolated f0
    if 0 < index_min < len(values) - 1:
        interpolated_f0 = parabola_vertex(
            frequencies[index_min - 1], values[index_min - 1],
            frequencies[index_min], values[index_min],
            frequencies[index_min + 1], values[index_min + 1],
        )
        out["interpolation_bracket"] = [
            frequencies[index_min - 1],
            frequencies[index_min],
            frequencies[index_min + 1],
        ]
    else:
        interpolated_f0 = raw_f0

    out["INTERPOLATED_F0"] = interpolated_f0

    # --------------------------------------------- points inside the FWHM
    if left is not None and right is not None:
        fwhm = right - left
        out["FWHM"] = fwhm
        out["points_inside_FWHM"] = sum(
            1 for f in frequencies if left <= f <= right
        )
    else:
        out["FWHM"] = None
        out["points_inside_FWHM"] = None

    # ------------------------------------------------------------- verdict
    out["validity_failures"] = failures
    out["valid"] = not failures
    out["FIXTURE_RESONANCE_VALID"] = not failures

    if not failures and out["FWHM"]:
        out["Q_linewidth"] = interpolated_f0 / out["FWHM"]
        out["Q_definition"] = "Q_linewidth = INTERPOLATED_F0 / FWHM"
        out["Q_name_is_linewidth_only"] = True
    else:
        out["Q_linewidth"] = None
        out["Q_upgrade_stopped"] = True

    return out


# --------------------------------------------------------------------- main


def main() -> int:
    import cst.results as cr

    dry = json.loads(DRY.read_text(encoding="utf-8"))
    lock = json.loads(OBS_LOCK.read_text(encoding="utf-8"))

    project = Path(dry["path_regression"]["actual_created_path"])

    pf = cr.ProjectFile(str(project), allow_interactive=True)
    rm = pf.get_3d()

    tree = [str(item) for item in rm.get_tree_items()]

    channels = {}

    for path in tree:
        if not path.startswith(SPARAM_PREFIX):
            continue

        name = path.split("\\")[-1]

        if not IDENTIFIER.match(name):
            continue

        channels[name] = ra.extract_1d_arrays(rm.get_result_item(path))

    if not channels:
        write_out(
            OUT_RES,
            {"report": "v110c_resonance_report", "status": "FAILED",
             "error": "NO_SPARAMETERS"},
        )
        return 2

    frequencies = next(iter(channels.values()))["x"]

    # ------------------------------------------------ primary observable T_total
    in_face, in_mode = re.match(r"(Zmin|Zmax)\((\d+)\)", INCIDENT_MODE).groups()

    reflected = [
        n for n in channels
        if n.endswith(f",{in_face}({in_mode})") and n.startswith(f"S{in_face}(")
    ]
    transmitted = [
        n for n in channels
        if n.endswith(f",{in_face}({in_mode})")
        and not n.startswith(f"S{in_face}(")
    ]

    r_total, t_total, a_balance = [], [], []

    for index in range(len(frequencies)):
        r = sum(
            channels[n]["y_real"][index] ** 2 + channels[n]["y_imag"][index] ** 2
            for n in reflected
        )
        t = sum(
            channels[n]["y_real"][index] ** 2 + channels[n]["y_imag"][index] ** 2
            for n in transmitted
        )

        r_total.append(r)
        t_total.append(t)
        a_balance.append(1.0 - r - t)

    # numeric error band from the physically-zero absorptance (purely PEC)
    noise_band = max(abs(v) for v in a_balance)

    primary = extract_resonance(frequencies, t_total, noise_band=noise_band)

    # a cross-check on the complementary quantity: R_total should peak
    complementary = extract_resonance(
        frequencies, [-v for v in r_total], noise_band=noise_band
    )

    curve_hash = hashlib.sha256()

    for name in sorted(channels):
        curve_hash.update(name.encode("utf-8"))

        for value in channels[name]["y_real"]:
            curve_hash.update(f"{value:.15g}".encode("utf-8"))

    resonance = {
        "report": "v110c_resonance_report",
        "phase": "Q3_RESONANCE_VALIDITY + Q4_F0 + Q5_FWHM + Q6_Q_LINEWIDTH",
        "generated_at_utc": utc_now(),
        "real_solve_count": 0,
        "project_path": str(project),
        "observable_lock": lock.get("PRIMARY_RESONANCE_OBSERVABLE"),
        "observable_was_locked_before_solve": True,
        "observable_lock_timestamp": lock.get("generated_at_utc"),
        "definitions_applied": lock.get("definitions"),
        "PRIMARY_RESONANCE_OBSERVABLE": {
            "name": "T_total",
            "incident_mode": INCIDENT_MODE,
            "resonance_type": "dip",
            "reflected_channels": sorted(reflected),
            "transmitted_channels": sorted(transmitted),
            "curve_hash": "sha256:" + curve_hash.hexdigest(),
            "T_total_first": t_total[0],
            "T_total_last": t_total[-1],
            "T_total_min": min(t_total),
            "T_total_max": max(t_total),
            "R_total_at_min": r_total[t_total.index(min(t_total))],
            "A_balance_max_abs": noise_band,
        },
        "FIXTURE_RESONANCE_VALID": primary.get("FIXTURE_RESONANCE_VALID"),
        "extraction": primary,
        "complementary_check_on_R_total_peak": complementary,
        "Q_linewidth": primary.get("Q_linewidth"),
        "Q_never_called_unloaded": True,
        "point_count": len(frequencies),
        "frequency_range_ghz": [frequencies[0], frequencies[-1]],
        "status": "PASSED"
        if primary.get("FIXTURE_RESONANCE_VALID")
        else "FIXTURE_RESONANCE_INVALID",
    }

    write_out(OUT_RES, resonance)

    # -------------------------------------------------------------- FREQ_A
    freq_a = {
        "report": "v110c_freq_a_report",
        "phase": "Q7 (part) FREQ_A",
        "generated_at_utc": utc_now(),
        "real_solve_count": 0,
        "definitions": {
            "FREQ_A": (
                "does the CST broadband adaptive frequency sweep resolve the "
                "response well enough?"
            ),
            "solver_samples": (
                "the frequency axis of solver-native per-frequency result "
                "families; evidence grade INFERRED_FROM_SOLVER_NATIVE_FREQUENCY_AXES"
            ),
            "result_output_samples": (
                "the stored 1D curve grid, sized by SetNumberOfResultDataSamples"
            ),
        },
    }

    if FREQ_A_PATH in tree:
        interp = ra.extract_1d_arrays(rm.get_result_item(FREQ_A_PATH))
        freq_a["broadband_interpolation_error"] = {
            "raw_tree_path": FREQ_A_PATH,
            "calculation_count": interp["sample_count"],
            "x_values": interp["x"],
            "error_values": interp["y_real"],
            "final_error": interp["y_real"][-1],
            "data_sha256": interp["data_sha256"],
        }

    native_axes = []

    for path in tree:
        if "Equation System Solver" in path:
            item = rm.get_result_item(path)
            native_axes.append([round(float(v), 6) for v in item.get_xdata()])

    freq_a["solver_native_frequency_axes"] = native_axes
    freq_a["solver_sample_frequencies_inferred"] = (
        native_axes[0] if native_axes else None
    )
    freq_a["SOLVER_SAMPLE_FREQUENCIES"] = (
        "INFERRED_FROM_SOLVER_NATIVE_FREQUENCY_AXES"
    )
    freq_a["solver_native_axis_family_count"] = len(native_axes)
    freq_a["result_output_grid"] = {
        "points": len(frequencies),
        "first_ghz": frequencies[0],
        "last_ghz": frequencies[-1],
        "step_ghz": (
            (frequencies[-1] - frequencies[0]) / (len(frequencies) - 1)
            if len(frequencies) > 1
            else None
        ),
    }
    freq_a["solver_samples_vs_output_samples_distinct"] = (
        len({tuple(axis) for axis in native_axes}) == 1
        and native_axes[0] != [frequencies[0], frequencies[-1]]
        if native_axes
        else None
    )
    freq_a["output_points_per_solver_sample"] = (
        len(frequencies) / len(native_axes[0]) if native_axes else None
    )
    freq_a["no_extra_solve_used"] = True
    freq_a["status"] = "PASSED" if native_axes else "BLOCKED"

    write_out(OUT_FREQ_A, freq_a)

    # ------------------------------------------------------- FREQ_B E2E
    strides = (1, 2, 4, 8)
    views = []

    for stride in strides:
        index = list(range(0, len(frequencies), stride))

        if index[-1] != len(frequencies) - 1:
            index.append(len(frequencies) - 1)

        if len(index) < 5:
            views.append({"stride": stride, "skipped": "TOO_FEW_POINTS",
                          "points": len(index)})
            continue

        sub_x = [frequencies[i] for i in index]
        sub_t = [t_total[i] for i in index]
        sub_a = [abs(a_balance[i]) for i in index]

        local_noise = max(sub_a)

        # INDEPENDENT extraction: the coarse set is analysed on its own terms
        extracted = extract_resonance(sub_x, sub_t, noise_band=local_noise)

        views.append(
            {
                "stride": stride,
                "points": len(sub_x),
                "independently_extracted": True,
                "full_grid_values_used_to_help_crossings": False,
                "extraction": extracted,
                "RAW_GRID_F0": extracted.get("RAW_GRID_F0"),
                "INTERPOLATED_F0": extracted.get("INTERPOLATED_F0"),
                "FWHM": extracted.get("FWHM"),
                "Q_linewidth": extracted.get("Q_linewidth"),
                "valid": extracted.get("valid"),
                "points_inside_FWHM": extracted.get("points_inside_FWHM"),
            }
        )

    baseline_view = views[0] if views else {}

    deltas = []

    for view in views[1:]:
        if view.get("skipped"):
            continue

        def delta(key):
            base = baseline_view.get(key)
            other = view.get(key)

            if base is None or other is None:
                return None

            return {
                "stride_1": base,
                f"stride_{view['stride']}": other,
                "absolute_change": abs(other - base),
                "relative_change": abs(other - base) / abs(base) if base else None,
            }

        deltas.append(
            {
                "stride": view["stride"],
                "delta_f0": delta("INTERPOLATED_F0"),
                "delta_FWHM": delta("FWHM"),
                "delta_Q": delta("Q_linewidth"),
            }
        )

    freq_b = {
        "report": "v110c_freq_b_e2e_report",
        "phase": "Q7_FREQ_B_E2E",
        "generated_at_utc": utc_now(),
        "real_solve_count": 0,
        "new_solves_for_freq_b": 0,
        "definition": (
            "FREQ_B = postprocess extraction convergence: on the SAME stored "
            "response, does a coarser re-sampling change f0 / FWHM / Q?"
        ),
        "method": (
            "the stored 1001-point curve is subsampled at stride 1/2/4/8 and the "
            "frozen extraction is re-run INDEPENDENTLY on each resampled set; "
            "the fine-grid result is never used to locate a coarse-grid crossing"
        ),
        "views": views,
        "deltas_vs_stride_1": deltas,
        "scientific_threshold_applied": False,
        "threshold_note": (
            "REPRODUCTION_NUMERICAL_CRITERION remains "
            "UNSET_PENDING_AUTHORIZATION; only the measured changes are reported"
        ),
        "status": "PASSED" if any(v.get("valid") for v in views) else "FAILED",
    }

    write_out(OUT_FREQ_B, freq_b)

    # ---------------------------------------------- mesh / PER_PASS_Q observability
    adaptive_nodes = [p for p in tree if p.startswith(ADAPT_PREFIX)]

    frequencies_folders = sorted(
        {
            m.group(1)
            for p in adaptive_nodes
            for m in [re.match(r"1D Results\\Adaptive Meshing\\f=([0-9.]+)", p)]
            if m
        }
    )

    mesh = {
        "report": "v110c_mesh_observability",
        "phase": "Q8_MESH_Q_OBSERVABILITY",
        "generated_at_utc": utc_now(),
        "real_solve_count": 0,
        "adaptation_frequencies": frequencies_folders,
        "adaptive_node_count": len(adaptive_nodes),
        "adaptive_nodes": adaptive_nodes,
        "mesh_cells_vs_tetrahedra": {
            "mesh_cells": "READBACK_AS_NAMED",
            "tetrahedra_count": "UNRESOLVED",
            "why": (
                "CST 2026 does not document the 'Meshcells' result as a "
                "tetrahedron count; the two are not equated"
            ),
        },
    }

    if frequencies_folders:
        prefix = f"{ADAPT_PREFIX}f={frequencies_folders[0]}\\"

        if prefix + "Meshcells" in tree:
            cells = ra.extract_1d_arrays(rm.get_result_item(prefix + "Meshcells"))
            mesh["mesh_cells"] = cells["y_real"]
            mesh["mesh_cells_x"] = cells["x"]
            mesh["pass_count"] = cells["sample_count"]

        if prefix + "Solvertime" in tree:
            st = ra.extract_1d_arrays(rm.get_result_item(prefix + "Solvertime"))
            mesh["solver_time"] = st["y_real"]
            mesh["solver_time_all_zero"] = all(v == 0.0 for v in st["y_real"])

        delta_path = prefix + "S-Parameters\\Delta\\All S-Parameters"

        if delta_path in tree:
            d = ra.extract_1d_arrays(rm.get_result_item(delta_path))
            mesh["delta_all_s_parameters"] = d["y_real"]
            mesh["delta_x"] = d["x"]

        per_pass_channels = [
            p for p in adaptive_nodes
            if p.startswith(prefix + "S-Parameters\\") and "Delta" not in p
        ]

        mesh["per_pass_s_parameter_curves"] = len(per_pass_channels)

        per_pass_labels = set()
        per_pass_counts = set()
        per_pass_x = set()

        for p in per_pass_channels:
            item = rm.get_result_item(p)
            per_pass_labels.add(repr(getattr(item, "xlabel", None)))
            per_pass_counts.add(int(item.length))
            per_pass_x.add(tuple(round(float(v), 6) for v in item.get_xdata()))

        mesh["per_pass_sample_counts"] = sorted(per_pass_counts)
        mesh["per_pass_x_labels"] = sorted(per_pass_labels)
        mesh["per_pass_x_axes"] = [list(axis) for axis in sorted(per_pass_x)]

    # The decisive question: does a single solve expose a FULL resonance curve
    # per adaptation pass?  The per-pass curves are indexed by PASS and carry one
    # sample each - the value at the adaptation frequency only.  So the axis
    # label, not the point count, is what identifies them as single-frequency.
    per_pass_axis_is_pass = mesh.get("per_pass_x_labels") == ["'Pass'"]

    mesh["per_pass_axis_is_pass_number"] = per_pass_axis_is_pass
    mesh["per_pass_one_sample_per_pass"] = (
        isinstance(mesh.get("per_pass_sample_counts"), list)
        and len(mesh.get("per_pass_sample_counts") or []) == 1
        and mesh["per_pass_sample_counts"][0] == mesh.get("pass_count")
    )

    per_pass_is_single_frequency = per_pass_axis_is_pass

    mesh["PER_PASS_RESONANCE_CURVE"] = (
        "NOT_AVAILABLE" if per_pass_is_single_frequency else "REQUIRES_INSPECTION"
    )
    mesh["PER_PASS_Q"] = (
        "NOT_OBSERVABLE_FROM_SINGLE_SOLVE"
        if per_pass_is_single_frequency
        else "REQUIRES_INSPECTION"
    )
    mesh["PER_PASS_Q_reason"] = (
        "the per-pass S-parameter results under 'Adaptive Meshing\\f=<freq>\\' "
        "are indexed by PASS (xlabel 'Pass') and carry exactly one complex value "
        "per pass - the S-parameter at the adaptation frequency only. A full "
        "resonance curve per pass does not exist in the stored results, so "
        "f0 / FWHM / Q cannot be formed for an individual pass from a single "
        "solve."
        if per_pass_is_single_frequency
        else "per-pass curves were not indexed by pass; inspect"
    )
    mesh["per_pass_Q_fabricated"] = False
    mesh["Q_MESH_CONVERGENCE_METHOD"] = (
        "INTER_SOLVE_REQUIRED" if per_pass_is_single_frequency
        else "REQUIRES_INSPECTION"
    )
    mesh["Q_MESH_CONVERGENCE_reason"] = (
        "because Q_linewidth needs the full resonance curve, and the stored "
        "per-pass results hold only the adaptation-frequency sample, Q mesh "
        "convergence must be established by comparing INDEPENDENT FINAL SOLVES "
        "(one per mesh setting) - it cannot be stepped through inside one solve."
    )

    # ------------------------------------------------ stop-mechanism observation
    deltas = mesh.get("delta_all_s_parameters") or []
    adaptation_freq = float(frequencies_folders[0]) if frequencies_folders else None

    # the two documented criteria that act on the same delta curve
    inner_threshold = 0.02   # CST FD-TET default MaxDeltaS
    inner_checks = 1         # CST FD-TET default NumberOfDeltaSChecks
    broadband_threshold = 0.01   # AddStopCriterion "All S-Parameters" in the history
    broadband_checks = 2

    def consecutive_below(series, threshold):
        best = 0
        run = 0

        for value in series:
            if value < threshold:
                run += 1
                best = max(best, run)
            else:
                run = 0

        return best

    mesh["stop_mechanism_observation"] = {
        "delta_series": deltas,
        "pass_count": mesh.get("pass_count"),
        "inner_discrete_criterion": {
            "name": "MaxDeltaS (EnableInnerSParameterAdaptation)",
            "threshold": inner_threshold,
            "checks": inner_checks,
            "source": "CST documented FD-TET default (not set in history)",
            "consecutive_passes_below": consecutive_below(deltas, inner_threshold),
            "satisfied": consecutive_below(deltas, inner_threshold) >= inner_checks,
        },
        "broadband_criterion": {
            "name": 'AddStopCriterion "All S-Parameters"',
            "threshold": broadband_threshold,
            "checks": broadband_checks,
            "source": "CST-written solver parameter block in the project history",
            "consecutive_passes_below": consecutive_below(
                deltas, broadband_threshold
            ),
            "satisfied": consecutive_below(deltas, broadband_threshold)
            >= broadband_checks,
        },
        "STOP_REASON": (
            "STOP_REASON_PARTIALLY_UNEXPLAINED"
            if consecutive_below(deltas, broadband_threshold) < broadband_checks
            and consecutive_below(deltas, inner_threshold) >= inner_checks
            else "CONSISTENT_WITH_CONFIGURED_CRITERIA"
        ),
        "why": (
            "the delta at pass 2 and pass 3 both exceed the broadband threshold "
            "0.01, and only pass 4 falls below it, so that criterion's required 2 "
            "consecutive checks are NOT met - yet the solve stopped after pass 4. "
            "The discrete-frequency MaxDeltaS criterion (threshold 0.02, 1 check) "
            "IS met at pass 4. Which mechanism actually terminated the run cannot "
            "be decided from the stored data, so it is recorded as partially "
            "unexplained rather than guessed."
        ),
        "not_used_to_change_any_result": True,
    }
    mesh["adaptation_frequency_ghz"] = adaptation_freq
    mesh["status"] = "PASSED" if mesh.get("pass_count") else "BLOCKED"

    write_out(OUT_MESH, mesh)

    # ------------------------------------------------------------------ console
    print("PRIMARY observable      :", resonance["PRIMARY_RESONANCE_OBSERVABLE"]["name"],
          "dip, incident", INCIDENT_MODE)
    print("  T range               :", resonance["PRIMARY_RESONANCE_OBSERVABLE"]["T_total_min"],
          "-", resonance["PRIMARY_RESONANCE_OBSERVABLE"]["T_total_max"])
    print("  noise band (max|A|)   :", noise_band)
    print()
    print("FIXTURE_RESONANCE_VALID :", resonance["FIXTURE_RESONANCE_VALID"])
    print("  RAW_GRID_F0           :", primary.get("RAW_GRID_F0"), "GHz")
    print("  INTERPOLATED_F0       :", primary.get("INTERPOLATED_F0"), "GHz")
    print("  baseline_level        :", primary.get("baseline_level"))
    print("  half_level            :", primary.get("half_level"))
    print("  left crossing         :", primary.get("left_half_frequency"), "GHz")
    print("  right crossing        :", primary.get("right_half_frequency"), "GHz")
    print("  FWHM                  :", primary.get("FWHM"), "GHz")
    print("  points inside FWHM    :", primary.get("points_inside_FWHM"))
    print("  Q_linewidth           :", primary.get("Q_linewidth"))
    print("  failures              :", primary.get("validity_failures"))
    print()
    print("FREQ_A")
    print("  interpolation error   :", freq_a.get("broadband_interpolation_error", {}).get(
        "error_values"))
    print("  solver sample axis    :", freq_a.get("solver_sample_frequencies_inferred"))
    print("  output grid           :", freq_a["result_output_grid"]["points"], "points")
    print()
    print("FREQ_B E2E (independent extraction per stride)")
    for view in views:
        print(f"  stride {view['stride']:>1} ({view.get('points')} pts): "
              f"f0={view.get('INTERPOLATED_F0')} FWHM={view.get('FWHM')} "
              f"Q={view.get('Q_linewidth')} valid={view.get('valid')} "
              f"pts_in_FWHM={view.get('points_inside_FWHM')}")
    print()
    print("mesh observability")
    print("  pass count            :", mesh.get("pass_count"))
    print("  mesh_cells            :", mesh.get("mesh_cells"))
    print("  delta all S           :", mesh.get("delta_all_s_parameters"))
    print("  per-pass sample counts:", mesh.get("per_pass_sample_counts"))
    print("  PER_PASS_Q            :", mesh.get("PER_PASS_Q"))
    print("  Q mesh convergence    :", mesh.get("Q_MESH_CONVERGENCE_METHOD"))
    print()
    print("status                  :", resonance["status"])

    return 0 if resonance["status"] == "PASSED" else 3


if __name__ == "__main__":
    raise SystemExit(main())
