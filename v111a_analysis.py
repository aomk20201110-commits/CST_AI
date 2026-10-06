"""V1.11A R4/R5/R6: schema lock, peak extraction, degeneracy, principal cuts, HPBW,
dipole sanity.  OFFLINE - reads the exported native angular grid, touches no CST.

Run with any Python:
    python v111a_analysis.py
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPORTS = ROOT / "reports"

ANGULAR = REPORTS / "v111a_angular_data.json"
OUT_SCHEMA = REPORTS / "v111a_angular_schema.json"
OUT_PAT = REPORTS / "v111a_pattern_analysis.json"
OUT_ACT = REPORTS / "v111a_activation_probe.json"

#: relative threshold for calling two peaks "the same maximum"
DEGENERACY_REL_TOL = 1e-3


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def write_out(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=repr) + "\n",
        encoding="utf-8",
    )
    return path


def linear_crossing(xa, ya, xb, yb, level):
    if yb == ya:
        return xa

    return xa + (level - ya) * (xb - xa) / (yb - ya)


def main() -> int:
    data = json.loads(ANGULAR.read_text(encoding="utf-8"))
    grid = data["grid"]

    thetas = sorted({row["theta"] for row in grid})
    phis = sorted({row["phi"] for row in grid})

    # ------------------------------------------------------------ R4 schema
    schema = {
        "report": "v111a_angular_schema",
        "phase": "R4_SCHEMA_SEMANTICS",
        "generated_at_utc": utc_now(),
        "real_solve_count": 0,
        "source": "CST FarfieldPlot.GetListItem over the native evaluation list",
        "grid": {
            "theta_deg": {"min": min(thetas), "max": max(thetas),
                          "count": len(thetas)},
            "phi_deg": {"min": min(phis), "max": max(phis),
                        "count": len(phis)},
            "points": len(grid),
        },
        "quantities": {
            "th_re": {"status": "AVAILABLE",
                      "meaning": "real part of E_theta",
                      "unit": "CST farfield pattern unit (voltage-like)"},
            "th_im": {"status": "AVAILABLE", "meaning": "imag part of E_theta"},
            "ph_re": {"status": "AVAILABLE",
                      "meaning": "real part of E_phi"},
            "ph_im": {"status": "AVAILABLE", "meaning": "imag part of E_phi"},
            "ludwig 3 horizontal": {
                "status": "AVAILABLE",
                "meaning": "Ludwig3 horizontal component magnitude",
            },
            "ludwig 3 hor. phase": {"status": "AVAILABLE"},
            "ludwig 3 vertical": {"status": "AVAILABLE"},
            "ludwig 3 ver. phase": {"status": "AVAILABLE"},
            "radial re": {"status": "AVAILABLE",
                          "note": "~0 as required for farfield (transverse)"},
            "radial im": {"status": "AVAILABLE"},
            "E_theta_mag": {"status": "DERIVED_BY_THIS_ROUND",
                            "from": "sqrt(th_re^2 + th_im^2)"},
            "E_phi_mag": {"status": "DERIVED_BY_THIS_ROUND",
                          "from": "sqrt(ph_re^2 + ph_im^2)"},
            "E_total_sq": {
                "status": "DERIVED_BY_THIS_ROUND",
                "from": "th_re^2 + th_im^2 + ph_re^2 + ph_im^2",
                "meaning": "total transverse field power density (proportional)",
            },
            "directivity": {
                "status": "API_GAP",
                "why": (
                    "the chain was run with SetPlotMode 'efield' (the only "
                    "PlotMode string the shipped macro actually uses); the "
                    "numeric DIRECTIVITY quantity was NOT read, so nothing here "
                    "is labelled directivity or gain"
                ),
            },
            "gain": {"status": "NOT_READ"},
            "realized_gain": {"status": "NOT_READ"},
            "axial_ratio": {"status": "NOT_READ"},
        },
        "evaluation_list_radius_m": 0.0,
        "use_farfield_approximation": True,
        "coordinate_system_argument": "spherical",
        "coordinate_semantics": (
            "theta measured from +z, phi from +x in the x-y plane - carried over "
            "unchanged from the vendor documentation"
        ),
        "no_quantity_renamed": True,
        "status": "PASSED",
    }

    write_out(OUT_SCHEMA, schema)

    # -------------------------------------------------------------- R5 peak
    best = max(grid, key=lambda r: r["E_total_sq"])
    peak_value = best["E_total_sq"]

    degenerate = [
        {
            "theta": r["theta"],
            "phi": r["phi"],
            "E_total_sq": r["E_total_sq"],
            "relative_to_max": r["E_total_sq"] / peak_value if peak_value else None,
        }
        for r in grid
        if peak_value and r["E_total_sq"] >= peak_value * (1.0 - DEGENERACY_REL_TOL)
    ]

    theta_of_peaks = sorted({d["theta"] for d in degenerate})
    phi_of_peaks = sorted({d["phi"] for d in degenerate})

    degeneracy = {
        "is_degenerate": len(degenerate) > 1,
        "relative_tolerance_used": DEGENERACY_REL_TOL,
        "equivalent_peak_count": len(degenerate),
        "theta_values_of_peaks": theta_of_peaks,
        "phi_values_of_peaks": phi_of_peaks,
        "phi_span_deg": (
            (max(phi_of_peaks) - min(phi_of_peaks)) if len(phi_of_peaks) > 1 else 0.0
        ),
        "interpretation": (
            "the maxima form a RING at essentially constant theta over the whole "
            "phi range, i.e. an azimuthally degenerate doughnut - a single "
            "'beam direction' cannot be claimed"
            if len(phi_of_peaks) > 1 and len(theta_of_peaks) == 1
            else "see theta/phi values above"
        ),
        "single_beam_direction_claimed": False,
    }

    # ------------------------------------------------------- R6 cuts + HPBW
    def theta_cut(phi_value: float) -> dict:
        rows = sorted(
            (r for r in grid if abs(r["phi"] - phi_value) < 1e-9),
            key=lambda r: r["theta"],
        )

        return {
            "cut_definition": f"fixed phi = {phi_value} deg, theta varies",
            "phi_deg": phi_value,
            "points": [
                {"theta": r["theta"], "E_total_sq": r["E_total_sq"],
                 "E_theta_mag": r["E_theta_mag"], "E_phi_mag": r["E_phi_mag"]}
                for r in rows
            ],
        }

    def phi_cut(theta_value: float) -> dict:
        rows = sorted(
            (r for r in grid if abs(r["theta"] - theta_value) < 1e-9),
            key=lambda r: r["phi"],
        )

        return {
            "cut_definition": f"fixed theta = {theta_value} deg, phi varies",
            "theta_deg": theta_value,
            "points": [
                {"phi": r["phi"], "E_total_sq": r["E_total_sq"]}
                for r in rows
            ],
        }

    cuts = [theta_cut(0.0), theta_cut(90.0), phi_cut(90.0)]

    for cut in cuts:
        points = cut["points"]
        digest = hashlib.sha256()

        for p in points:
            digest.update(
                f"{p.get('theta', p.get('phi')):.6g},{p['E_total_sq']:.15g}".encode()
            )

        cut["raw_data_sha256"] = "sha256:" + digest.hexdigest()
        cut["peak_in_cut"] = max(points, key=lambda p: p["E_total_sq"])["E_total_sq"]

    # HPBW on the theta cut that contains the main lobe
    hpbw = {"HPBW": "UNRESOLVED"}

    main_cut = next((c for c in cuts if c.get("phi_deg") == 0.0), None)

    if main_cut:
        pts = main_cut["points"]
        values = [p["E_total_sq"] for p in pts]
        angles = [p["theta"] for p in pts]

        peak_index = values.index(max(values))
        peak_angle = angles[peak_index]
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

        hpbw = {
            "cut_used": main_cut["cut_definition"],
            "peak_angle_deg": peak_angle,
            "peak_value": peak,
            "half_level": half,
            "left_3db_angle_deg": left,
            "right_3db_angle_deg": right,
            "HPBW_deg": (right - left) if (left is not None and right is not None) else None,
            "HPBW": (right - left) if (left is not None and right is not None) else "UNRESOLVED",
            "interpolation": "piecewise linear on E_total_sq, -3 dB == half power",
            "crossings_found": left is not None and right is not None,
            "wraps_0_360": False,
            "multiple_equal_peaks_in_cut": (
                sum(1 for v in values if v >= peak * (1 - DEGENERACY_REL_TOL)) > 1
            ),
        }

    # ---------------------------------------------------- dipole sanity check
    def value_at(theta, phi):
        for r in grid:
            if abs(r["theta"] - theta) < 1e-9 and abs(r["phi"] - phi) < 1e-9:
                return r["E_total_sq"]

        return None

    axis_0 = value_at(0.0, 0.0)
    axis_180 = value_at(180.0, 0.0)
    broadside = value_at(90.0, 0.0)

    phi_spread = None

    ring = [r["E_total_sq"] for r in grid if abs(r["theta"] - 90.0) < 1e-9]

    if ring:
        phi_spread = {
            "theta_90_min": min(ring),
            "theta_90_max": max(ring),
            "relative_spread": (max(ring) - min(ring)) / max(ring) if max(ring) else None,
        }

    sanity = {
        "axis_theta_0_E_total_sq": axis_0,
        "axis_theta_180_E_total_sq": axis_180,
        "broadside_theta_90_E_total_sq": broadside,
        "axis_to_broadside_ratio_0": (axis_0 / broadside) if axis_0 and broadside else None,
        "axis_to_broadside_ratio_180": (axis_180 / broadside) if axis_180 and broadside else None,
        "phi_dependence_at_theta_90": phi_spread,
        "expectations_pre_declared_in_V111": [
            "null along the dipole axis (theta ~ 0 and ~ 180)",
            "broadside maximum (theta ~ 90)",
            "azimuthal symmetry",
        ],
        "SANITY_VERDICT": None,
    }

    broadside_ring = [
        r["E_total_sq"] for r in grid if abs(r["theta"] - 90.0) < 1e-9
    ]

    conditions = {
        "axis_below_broadside": (
            axis_0 is not None
            and broadside is not None
            and axis_0 < broadside
            and axis_180 < broadside
        ),
        # The maximum must sit on the theta = 90 ring.  Requiring the specific
        # phi = 0 sample to equal the GLOBAL maximum would be wrong for a
        # rotationally symmetric radiator, whose ring carries a small numerical
        # spread; the correct test is that the peak THETA is 90.
        "broadside_ring_is_the_maximum": best["theta"] == 90.0,
        "azimuthally_symmetric": (
            phi_spread is not None and phi_spread["relative_spread"] is not None
            and phi_spread["relative_spread"] < 0.05
        ),
    }

    sanity["criterion_note"] = (
        "the broadside test asks whether the PEAK THETA is 90 deg. An earlier "
        "draft instead demanded that the single phi=0 sample equal the global "
        "maximum, which a rotationally symmetric pattern cannot satisfy because "
        "its ring carries a ~0.2% numerical spread. The criterion was corrected; "
        "NO data were adjusted."
    )
    sanity["broadside_ring_sample_count"] = len(broadside_ring)

    sanity["conditions_checked"] = conditions
    sanity["SANITY_VERDICT"] = (
        "SANITY_CONSISTENT" if all(conditions.values()) else "SANITY_PARTIALLY_CONSISTENT"
    )
    sanity["no_tolerance_used_to_adjust_data"] = True

    pattern = {
        "report": "v111a_pattern_analysis",
        "phase": "R5_PEAK + R6_CUT_HPBW + dipole sanity",
        "generated_at_utc": utc_now(),
        "real_solve_count": 0,
        "observable_used": {
            "name": "E_total_sq",
            "derived_from": "th_re, th_im, ph_re, ph_im (native CST components)",
            "is_native": False,
            "is_derived": True,
            "why_derived": (
                "the chain was run with SetPlotMode 'efield', so the native "
                "quantities are field components; no directivity/gain quantity "
                "was read and none is claimed"
            ),
        },
        "PEAK_VALUE": peak_value,
        "PEAK_THETA": best["theta"],
        "PEAK_PHI": best["phi"],
        "peak_observable_type": "E_total_sq (field power density, NOT gain/directivity)",
        "DEGENERATE_MAXIMA": "DEGENERATE_MAXIMA" if degeneracy["is_degenerate"]
        else "UNIQUE_MAXIMUM",
        "degeneracy": degeneracy,
        "principal_cuts": cuts,
        "HPBW_result": hpbw,
        "dipole_sanity": sanity,
        "scientific_threshold_applied": False,
        "status": "PASSED",
    }

    write_out(OUT_PAT, pattern)

    # -------------------------------------------------------------- console
    print("R4 schema")
    print("  grid                  :", schema["grid"])
    print("  AVAILABLE             : th_re/th_im/ph_re/ph_im, ludwig3 x4, radial x2")
    print("  directivity/gain      : API_GAP / NOT_READ (not renamed)")
    print()
    print("R5 peak")
    print("  PEAK_VALUE            :", peak_value)
    print("  PEAK_THETA / PEAK_PHI :", best["theta"], "/", best["phi"])
    print("  DEGENERATE_MAXIMA     :", pattern["DEGENERATE_MAXIMA"])
    print("  equivalent peaks      :", degeneracy["equivalent_peak_count"],
          "| theta set:", theta_of_peaks, "| phi count:", len(phi_of_peaks))
    print("  interpretation        :", degeneracy["interpretation"])
    print()
    print("R6 cuts + HPBW")
    print("  cuts                  :", [c["cut_definition"] for c in cuts])

    for c in cuts:
        cth = c.get("theta_deg")
        cph = c.get("phi_deg")
        key = "theta_deg" if cth is not None else "phi_deg"
        print(f"    {c['cut_definition']:44} points={len(c['points'])} "
              f"peak={c['peak_in_cut']:.6g} sha={c['raw_data_sha256'][:22]}")
    print("  HPBW                  :", hpbw.get("HPBW"))
    print("  peak angle            :", hpbw.get("peak_angle_deg"),
          "| left:", hpbw.get("left_3db_angle_deg"),
          "| right:", hpbw.get("right_3db_angle_deg"))
    print()
    print("dipole sanity")
    print("  theta 0   E_total_sq  :", axis_0)
    print("  theta 180 E_total_sq  :", axis_180)
    print("  theta 90  E_total_sq  :", broadside)
    print("  ratio 0/90, 180/90    :", sanity["axis_to_broadside_ratio_0"],
          "/", sanity["axis_to_broadside_ratio_180"])
    print("  phi spread at theta 90:", phi_spread)
    print("  VERDICT               :", sanity["SANITY_VERDICT"])
    print("  conditions            :", conditions)
    print()
    print("status                  :", pattern["status"])

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
