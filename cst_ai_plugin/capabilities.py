"""CST_AI plugin layer — capability registry.

Every entry names a capability that was ACTUALLY exercised end-to-end in an
earlier round, and points at the module that implements it.  The plugin layer
contains no physics: it only routes.

A capability is never marked VERIFIED on the strength of a version number; the
`verified_stage` field records where the real CST verification happened.
"""

from __future__ import annotations

# verification status vocabulary
VERIFIED_E2E = "VERIFIED_E2E"
VERIFIED_OFFLINE = "VERIFIED_OFFLINE"
NOT_VERIFIED = "NOT_VERIFIED"
OPTIONAL_APPLICATION = "OPTIONAL_APPLICATION"

# solve requirement
NEVER_SOLVES = "NEVER_SOLVES"
MAY_SOLVE = "MAY_SOLVE"


def _cap(
    capability_id: str,
    implementation: str,
    *,
    requires_cst: bool,
    requires_solve: bool,
    requires_existing_results: bool,
    input_schema: dict,
    output_schema: dict,
    verification_status: str,
    verified_stage: str,
    solve_requirement: str,
    notes: str | None = None,
) -> dict:
    return {
        "capability_id": capability_id,
        "implementation": implementation,
        "requires_cst": requires_cst,
        "requires_solve": requires_solve,
        "requires_existing_results": requires_existing_results,
        "input_schema": input_schema,
        "output_schema": output_schema,
        "verification_status": verification_status,
        "verified_stage": verified_stage,
        "solve_requirement": solve_requirement,
        "notes": notes,
    }


CAPABILITIES: dict[str, dict] = {
    c["capability_id"]: c
    for c in (
        _cap(
            "CST_RUNTIME_CONNECT",
            "cst_session_discovery_v19a.discover_design_environments / "
            "resolve_runtime",
            requires_cst=True,
            requires_solve=False,
            requires_existing_results=False,
            input_schema={"runtime_file": "path", "auto_update": "bool"},
            output_schema={
                "status": "REUSED_FROM_RUNTIME | RECOVERED_FROM_DISCOVERY | "
                          "NO_LIVE_DESIGN_ENVIRONMENT | "
                          "MULTIPLE_LIVE_DESIGN_ENVIRONMENTS",
                "pid": "int | null",
            },
            verification_status=VERIFIED_E2E,
            verified_stage="V1.9A",
            solve_requirement=NEVER_SOLVES,
        ),
        _cap(
            "MODEL_BUILD",
            "cst_model_builder_v17.build_plan + cst_model_executor_v17.execute",
            requires_cst=True,
            requires_solve=False,
            requires_existing_results=False,
            input_schema={"spec": "ModelSpec"},
            output_schema={"project_path": "path", "command_hashes": "list"},
            verification_status=VERIFIED_E2E,
            verified_stage="V1.7 / V1.10C / V1.10D",
            solve_requirement=NEVER_SOLVES,
        ),
        _cap(
            "DRY_BUILD",
            "cst_model_executor_v17.snapshot + semantic_projection",
            requires_cst=True,
            requires_solve=False,
            requires_existing_results=False,
            input_schema={"project_path": "path", "expected": "semantic dict"},
            output_schema={"all_checks": "bool", "readback": "dict"},
            verification_status=VERIFIED_E2E,
            verified_stage="V1.10C / V1.10D / V1.12",
            solve_requirement=NEVER_SOLVES,
        ),
        _cap(
            "SOLVE",
            "cst_solver_control_v19a.SolverControlPlane + "
            "v19b_real_solve.FloquetControlBackend",
            requires_cst=True,
            requires_solve=True,
            requires_existing_results=False,
            input_schema={"project_path": "path", "run_id": "str"},
            output_schema={
                "phase": "solver lifecycle phase",
                "native_state": "SUCCESS | FAILED | ...",
            },
            verification_status=VERIFIED_E2E,
            verified_stage="V1.9A / V1.10C / V1.10D / V1.12",
            solve_requirement=MAY_SOLVE,
        ),
        _cap(
            "SOLVER_STATUS",
            "cst_solver_control_v19a.probe_control_methods + "
            "CSTTool.is_solver_running",
            requires_cst=True,
            requires_solve=False,
            requires_existing_results=False,
            input_schema={"pid": "int"},
            output_schema={"solver_running": "bool", "methods": "dict"},
            verification_status=VERIFIED_E2E,
            verified_stage="V1.9A",
            solve_requirement=NEVER_SOLVES,
        ),
        _cap(
            "S_PARAMETERS",
            "cst_result_api_v19.inventory_result_tree / extract_1d_arrays / "
            "select_results",
            requires_cst=False,
            requires_solve=False,
            requires_existing_results=True,
            input_schema={"project_path": "path", "tree_path": "str"},
            output_schema={"x": "list[float]", "y_real": "list[float]",
                           "y_imag": "list[float]"},
            verification_status=VERIFIED_E2E,
            verified_stage="V1.9 / V1.10B / V1.10D",
            solve_requirement=NEVER_SOLVES,
        ),
        _cap(
            "FLOQUET_MULTIMODE",
            "cst_result_api_v19.parse_s_parameter_identifier over SZmin/SZmax "
            "channel names",
            requires_cst=False,
            requires_solve=False,
            requires_existing_results=True,
            input_schema={"project_path": "path"},
            output_schema={"channels": "list[str]", "face_mode_map": "dict"},
            verification_status=VERIFIED_E2E,
            verified_stage="V1.9B",
            solve_requirement=NEVER_SOLVES,
        ),
        _cap(
            "POWER_RTA",
            "V1.9B normalisation over the Floquet S channels (R/T/A)",
            requires_cst=False,
            requires_solve=False,
            requires_existing_results=True,
            input_schema={"project_path": "path", "incident_mode": "str"},
            output_schema={"R_total": "list", "T_total": "list",
                           "A_balance": "list"},
            verification_status=VERIFIED_E2E,
            verified_stage="V1.9B / V1.10C / V1.10D / V1.12",
            solve_requirement=NEVER_SOLVES,
        ),
        _cap(
            "MESH_CONVERGENCE",
            "1D Results/Adaptive Meshing reader (mesh_cells, Delta S)",
            requires_cst=False,
            requires_solve=False,
            requires_existing_results=True,
            input_schema={"project_path": "path"},
            output_schema={"pass_count": "int", "mesh_cells": "list",
                           "delta_all_s": "list"},
            verification_status=VERIFIED_E2E,
            verified_stage="V1.10B / V1.10C / V1.10D",
            solve_requirement=NEVER_SOLVES,
        ),
        _cap(
            "FREQUENCY_CONVERGENCE",
            "1D Results/Convergence/S-Parameters/All S-Parameters reader",
            requires_cst=False,
            requires_solve=False,
            requires_existing_results=True,
            input_schema={"project_path": "path"},
            output_schema={"calculation_count": "int",
                           "final_interpolation_error": "float"},
            verification_status=VERIFIED_E2E,
            verified_stage="V1.10D",
            solve_requirement=NEVER_SOLVES,
        ),
        _cap(
            "RESONANCE_F0",
            "v110c_analysis.extract_resonance (3-point parabolic f0)",
            requires_cst=False,
            requires_solve=False,
            requires_existing_results=True,
            input_schema={"frequencies": "list", "trace": "list"},
            output_schema={"RAW_GRID_F0": "float", "INTERPOLATED_F0": "float"},
            verification_status=VERIFIED_E2E,
            verified_stage="V1.10C / V1.10D / V1.12",
            solve_requirement=NEVER_SOLVES,
        ),
        _cap(
            "FWHM",
            "v110c_analysis.extract_resonance (half-depth crossing "
            "interpolation)",
            requires_cst=False,
            requires_solve=False,
            requires_existing_results=True,
            input_schema={"frequencies": "list", "trace": "list"},
            output_schema={"FWHM": "float", "left_half_frequency": "float",
                           "right_half_frequency": "float"},
            verification_status=VERIFIED_E2E,
            verified_stage="V1.10C / V1.10D / V1.12",
            solve_requirement=NEVER_SOLVES,
        ),
        _cap(
            "Q_LINEWIDTH",
            "v110c_analysis.extract_resonance (Q = f0/FWHM)",
            requires_cst=False,
            requires_solve=False,
            requires_existing_results=True,
            input_schema={"frequencies": "list", "trace": "list"},
            output_schema={"Q_linewidth": "float"},
            verification_status=VERIFIED_E2E,
            verified_stage="V1.10C / V1.10D / V1.12",
            solve_requirement=NEVER_SOLVES,
        ),
        _cap(
            "FARFIELD_COMPLEX_FIELD",
            "SelectTreeItem activation + FarfieldPlot.AddListEvaluationPoint / "
            "CalculateList / GetListItem (vendor-documented chain)",
            requires_cst=True,
            requires_solve=False,
            requires_existing_results=True,
            input_schema={"project_path": "path", "tree_path": "str",
                          "theta": "list", "phi": "list"},
            output_schema={"th_re": "list", "th_im": "list", "ph_re": "list",
                           "ph_im": "list"},
            verification_status=VERIFIED_E2E,
            verified_stage="V1.11A",
            solve_requirement=NEVER_SOLVES,
            notes="requires an existing solved farfield monitor result",
        ),
        _cap(
            "DIRECTIVITY",
            "FarfieldPlot.SetPlotMode('directivity') over the same grid",
            requires_cst=True,
            requires_solve=False,
            requires_existing_results=True,
            input_schema={"project_path": "path", "tree_path": "str"},
            output_schema={"peak_linear": "float", "peak_dBi": "float",
                           "theta_peak": "float"},
            verification_status=VERIFIED_E2E,
            verified_stage="V1.11B",
            solve_requirement=NEVER_SOLVES,
        ),
        _cap(
            "GAIN",
            "FarfieldPlot.SetPlotMode('gain')",
            requires_cst=True,
            requires_solve=False,
            requires_existing_results=True,
            input_schema={"project_path": "path", "tree_path": "str"},
            output_schema={"peak_linear": "float", "peak_dB": "float"},
            verification_status=VERIFIED_E2E,
            verified_stage="V1.11B",
            solve_requirement=NEVER_SOLVES,
        ),
        _cap(
            "REALIZED_GAIN",
            "FarfieldPlot.SetPlotMode('realized gain')  [literal contains a SPACE]",
            requires_cst=True,
            requires_solve=False,
            requires_existing_results=True,
            input_schema={"project_path": "path", "tree_path": "str"},
            output_schema={"peak_linear": "float", "peak_dB": "float"},
            verification_status=VERIFIED_E2E,
            verified_stage="V1.11B",
            solve_requirement=NEVER_SOLVES,
        ),
        _cap(
            "BEAM_DIRECTION",
            "argmax over the read angular grid",
            requires_cst=True,
            requires_solve=False,
            requires_existing_results=True,
            input_schema={"project_path": "path"},
            output_schema={"theta_peak": "float", "phi_peak": "float",
                           "maximum_topology": "str"},
            verification_status=VERIFIED_E2E,
            verified_stage="V1.11A / V1.11B",
            solve_requirement=NEVER_SOLVES,
            notes="may be a NEAR_DEGENERATE_AZIMUTH_RING; no single phi claimed",
        ),
        _cap(
            "HPBW",
            "self-computed -3 dB crossings on a native cut (V1.11A).  The "
            "CST-native getter is API_GAP (V1.11B).",
            requires_cst=True,
            requires_solve=False,
            requires_existing_results=True,
            input_schema={"project_path": "path", "cut": "dict"},
            output_schema={"HPBW_deg": "float", "method": "SELF_COMPUTED"},
            verification_status=VERIFIED_E2E,
            verified_stage="V1.11A",
            solve_requirement=NEVER_SOLVES,
            notes="CST-native GetAngularWidthXdB = API_GAP",
        ),
        _cap(
            "PARAMETER_SWEEP",
            "v112_sweep_engine (manifest/planner) + per-point isolated projects",
            requires_cst=False,
            requires_solve=False,
            requires_existing_results=False,
            input_schema={"parameter": "str", "values": "list", "unit": "str"},
            output_schema={"points": "list", "budget_plan": "dict"},
            verification_status=VERIFIED_E2E,
            verified_stage="V1.12",
            solve_requirement=MAY_SOLVE,
            notes="reuse is signature-checked; duplicates are merged",
        ),
        _cap(
            "REFERENCE_COMPARATOR",
            "v113a_comparator (semantic gate, overlap, scalar/curve/complex/"
            "resonance/sweep comparison)",
            requires_cst=False,
            requires_solve=False,
            requires_existing_results=False,
            input_schema={"reference": "ReferenceDataset",
                          "simulation": "SimulationObservable",
                          "protocol": "dict"},
            output_schema={"comparison_record": "dict"},
            verification_status=VERIFIED_E2E,
            verified_stage="V1.13A",
            solve_requirement=NEVER_SOLVES,
            notes="optional application capability; the core does not depend on it",
        ),
    )
}


def get(capability_id: str) -> dict:
    if capability_id not in CAPABILITIES:
        from .errors import CapabilityMissing

        raise CapabilityMissing(f"unknown capability: {capability_id}")

    return CAPABILITIES[capability_id]


def ids() -> list[str]:
    return sorted(CAPABILITIES)


def requiring_solve() -> list[str]:
    return sorted(
        cid for cid, c in CAPABILITIES.items()
        if c["solve_requirement"] == MAY_SOLVE
    )


def verified_ids() -> list[str]:
    return sorted(
        cid for cid, c in CAPABILITIES.items()
        if c["verification_status"] in (VERIFIED_E2E, VERIFIED_OFFLINE)
    )


#: capabilities that must never appear in a zero-solve plan
ZERO_SOLVE_UNSAFE = tuple(requiring_solve())
