"""V1.9B real Floquet solve - exactly ONE TEST-ONLY solve.

Purpose: drive the V1.9A non-blocking control plane against the Floquet
unit-cell fixture and then inventory the REAL 0D/1D result tree, recording every
S-parameter identifier verbatim.

Why this module does not reuse CstControlBackend.preflight unchanged
--------------------------------------------------------------------
Solver.GetNumberOfPorts() returns 0 for a unit cell whose open boundaries are
realised as Floquet ports: it counts explicitly defined ports, not the mode
ports implied by the open unit-cell boundary.  The V1.9A preflight rejects
ports <= 0, which is correct for discrete/waveguide fixtures and wrong here.
FloquetControlBackend therefore overrides ONLY the port precondition with the
Floquet mode count that the dry build already read back through the official
mode identity API.  Everything else - start_solver, is_solver_running,
get_solver_run_info, abort_solver, timeout, abort handling, forensics - is
inherited unchanged.

Hard rules:
  * exactly one start request, one solve, no retry, no abort
  * initiated through start_solver (run_solver never called)
  * no CST process launched, closed, killed or elevated
  * no unrelated pre-existing project is ever opened or modified
  * no parameter sweep, no mesh/frequency convergence, no farfield, no 2D/3D

Run under the CST bundled interpreter (adjust the path to your installation):
    "C:\\Program Files\\CST Studio Suite 2026\\Python\\python.exe" v19b_real_solve.py
"""

from __future__ import annotations

import gc
import hashlib
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent

sys.path.insert(0, str(ROOT))

import cst_modelspec_v17 as ms  # noqa: E402
import cst_result_api_v19 as ra  # noqa: E402
import cst_session_discovery_v19a as disc  # noqa: E402
import cst_solver_control_v19a as cp  # noqa: E402

REPORTS = ROOT / "reports"
OUT = REPORTS / "v19b_real_solve_report.json"
RAW_INVENTORY = REPORTS / "v19b_raw_result_inventory.json"
LEDGER = ROOT / "ledger" / "v19b_floquet_ledger.jsonl"
RUNTIME = ROOT / "cst_runtime.json"
DRY_BUILD = REPORTS / "v19b_dry_build_report.json"

VERSION = "1.9B.0"
TOOL = "v19b_floquet_multimode_e2e"

# timing policy - derived, not picked (see timeout_policy in the report)
START_ACK_WINDOW_SECONDS = 180.0
RUNNING_TIMEOUT_SECONDS = 1200.0
POLL_INTERVAL_SECONDS = 2.0
ABORT_CONFIRM_SECONDS = 30.0

TIMEOUT_POLICY = {
    "start_ack_window_seconds": START_ACK_WINDOW_SECONDS,
    "running_timeout_seconds": RUNNING_TIMEOUT_SECONDS,
    "poll_interval_seconds": POLL_INTERVAL_SECONDS,
    "evidence": {
        "v19_td_solve_seconds": 16.328,
        "v19a_td_solve_seconds": 9.344,
        "v19_first_running_observation_seconds": 1.078,
        "v19a_first_running_observation_seconds": 0.047,
        "v19b_fixture_is_a_new_kind": (
            "frequency-domain tetrahedral sweep of a unit cell with two "
            "Floquet modes per face; no historical solve of this fixture exists"
        ),
    },
    "derivation": (
        "The two recorded TD solves took 9.3 s and 16.3 s with the solver "
        "reporting RUNNING within ~1 s.  A frequency-domain tetrahedral sweep "
        "adds per-frequency work and a mode computation for both Floquet faces, "
        "so the acknowledgement window is set to 180 s (~11x the slowest "
        "recorded solve) and the RUNNING timeout to 1200 s (~73x), both measured "
        "from the first RUNNING observation.  These are conservative bounds that "
        "still terminate rather than wait forever."
    ),
}


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def make_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")

    return f"run_{stamp}_{uuid.uuid4().hex}"


def sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None

    digest = hashlib.sha256()

    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)

    return "sha256:" + digest.hexdigest()


def write_out(name: str, payload: dict) -> Path:
    REPORTS.mkdir(parents=True, exist_ok=True)
    path = REPORTS / name
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=repr) + "\n",
        encoding="utf-8",
    )
    return path


def append_ledger(record: dict) -> dict:
    head = None

    if LEDGER.exists():
        for line in LEDGER.read_text(encoding="utf-8").splitlines():
            if line.strip():
                head = json.loads(line).get("record_hash")

    LEDGER.parent.mkdir(parents=True, exist_ok=True)

    return ms.append_model_ledger_record(LEDGER, record, head)


class FloquetControlBackend(cp.CstControlBackend):
    """CstControlBackend whose port precondition understands Floquet ports."""

    mode = cp.MODE_NONBLOCKING

    def __init__(self, *, expected_floquet_modes_per_face: int = 2, **kwargs):
        super().__init__(**kwargs)
        self.expected_floquet_modes_per_face = int(
            expected_floquet_modes_per_face
        )
        self.floquet_readback: dict | None = None

    def _read_floquet(self) -> dict:
        model3d = self.model3d

        model3d.allow_history_commands()

        try:
            floquet = model3d.FloquetPort

            per_face = {}

            for face in ("Zmin", "Zmax"):
                floquet.Port(face)

                per_face[face] = {
                    "number_of_modes_considered": int(
                        floquet.GetNumberOfModesConsidered()
                    ),
                    "number_of_modes_specified": int(floquet.GetNumberOfModes()),
                    "mode_number_by_name": {
                        name: floquet.GetModeNumberByName(name)[1]
                        for name in ("TE(0,0)", "TM(0,0)")
                    },
                }
        finally:
            model3d.disallow_history_commands()

        return per_face

    def preflight(self) -> dict:
        """Full preflight for a Floquet unit cell.

        cst_solver_control_v19a.CstControlBackend.preflight() is NOT called:
        it rejects Solver.GetNumberOfPorts() <= 0 before any subclass hook could
        run, which is exactly the criterion that does not apply to a Floquet
        unit cell.  The V1.9A module is left untouched (it is part of the frozen
        V1.9A surface); the Floquet variant is implemented here in full.
        """

        present = self.method_probe.get("present", {})

        if not present.get("start_solver"):
            raise cp.PreflightRefused(
                "start_solver is not present on this CST build"
            )

        if not present.get("abort_solver"):
            raise cp.PreflightRefused("abort_solver is not present on this build")

        if not self.de.is_connected():
            raise cp.PreflightRefused("CST DesignEnvironment is not connected")

        live_pid = int(self.de.pid())

        if live_pid != self.expected_pid:
            raise cp.PreflightRefused(
                f"CST PID changed: expected {self.expected_pid}, got {live_pid}"
            )

        if not self.de.has_active_project():
            raise cp.PreflightRefused("CST has no active project")

        actual = str(self.project.filename())

        if actual != self.expected_project_path:
            raise cp.PreflightRefused(
                "active project does not match the control lock: "
                f"expected {self.expected_project_path}, got {actual}"
            )

        fmin = float(self.model3d.Solver.GetFmin())
        fmax = float(self.model3d.Solver.GetFmax())

        if fmin <= 0.0:
            raise cp.PreflightRefused(f"solver fmin must be > 0 (got {fmin})")

        if fmax <= fmin:
            raise cp.PreflightRefused(f"invalid frequency range ({fmin} .. {fmax})")

        running = bool(self.model3d.is_solver_running())

        # ---- Floquet-specific precondition (replaces the port count check)
        floquet = self._read_floquet()
        self.floquet_readback = floquet

        expected_modes = self.expected_floquet_modes_per_face

        for face in ("Zmin", "Zmax"):
            considered = floquet[face]["number_of_modes_considered"]

            if considered != expected_modes:
                raise cp.PreflightRefused(
                    f"{face} considers {considered} Floquet modes, expected "
                    f"{expected_modes}"
                )

            mapping = floquet[face]["mode_number_by_name"]

            if mapping.get("TE(0,0)") != 1 or mapping.get("TM(0,0)") != 2:
                raise cp.PreflightRefused(
                    f"{face} mode identity is not TE(0,0)=1 / TM(0,0)=2: "
                    f"{mapping}"
                )

        import hashlib as _hashlib

        digest = _hashlib.sha256()

        with open(self.expected_project_path, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)

        project_hash = "sha256:" + digest.hexdigest()

        solver_family = str(self.model3d.get_active_solver_name())

        config = {
            "solver_family": solver_family,
            "fmin": fmin,
            "fmax": fmax,
            "floquet_modes_zmin": floquet["Zmin"]["number_of_modes_considered"],
            "floquet_modes_zmax": floquet["Zmax"]["number_of_modes_considered"],
        }

        ports_declared = int(self.model3d.Solver.GetNumberOfPorts())

        return {
            "ok": not running,
            "already_running": running,
            "project_path": actual,
            "project_hash": project_hash,
            "cst_pid": live_pid,
            "solver_family": solver_family,
            "solver_config_hash": _hashlib.sha256(
                repr(sorted(config.items())).encode("utf-8")
            ).hexdigest(),
            "fmin": fmin,
            "fmax": fmax,
            "port_count": ports_declared,
            "project_type": str(self.project.project_type()),
            "run_info_before": cp._json_safe(self.model3d.get_solver_run_info()),
            "method_probe": present,
            "floquet": floquet,
            "floquet_precondition_used": (
                "Floquet mode count per face; Solver.GetNumberOfPorts returns "
                f"{ports_declared} for this unit cell and is NOT used as a "
                "precondition"
            ),
            "get_number_of_ports_raw": ports_declared,
        }


def main() -> int:
    import cst.interface as ci

    wall_t0 = time.perf_counter()
    run_id = make_run_id()

    dry = json.loads(DRY_BUILD.read_text(encoding="utf-8"))
    project_path = Path(dry["path_regression"]["actual_created_path"])

    report: dict = {
        "report": "v19b_real_solve_report",
        "tool_name": TOOL,
        "version": VERSION,
        "phase": "F3_REAL_SOLVE + F4_RESULT_IDENTIFIER_READBACK",
        "run_id": run_id,
        "started_at_utc": utc_now(),
        "test_only": True,
        "valid_for_science": False,
        "fresh_cst_solves": 0,
        "solve_initiated_by": "start_solver (non-blocking control plane)",
        "run_solver_called": False,
        "timeout_policy": TIMEOUT_POLICY,
        "unrelated_projects_touched": False,
        "cst_process_killed": False,
        "launched_anything": False,
        "project_path": str(project_path),
        "project_hash_before_solve": sha256_file(project_path),
    }

    resolution = disc.resolve_runtime(RUNTIME, auto_update=True)

    report["session_resolution"] = {
        "status": resolution.get("status"),
        "pid": resolution.get("pid"),
    }

    if resolution.get("status") not in (
        disc.STATUS_REUSED,
        disc.STATUS_RECOVERED,
    ):
        report["status"] = "BLOCKED"
        report["reason"] = resolution.get("status")
        write_out("v19b_real_solve_report.json", report)
        print(json.dumps(report, ensure_ascii=False, indent=2, default=repr))
        return 3

    if not project_path.is_file():
        report["status"] = "BLOCKED"
        report["reason"] = "DRY_BUILD_PROJECT_MISSING"
        write_out("v19b_real_solve_report.json", report)
        print(json.dumps(report, ensure_ascii=False, indent=2, default=repr))
        return 4

    pid = int(resolution["pid"])
    de = ci.DesignEnvironment.connect(pid)

    if de.has_active_project():
        report["status"] = "BLOCKED"
        report["reason"] = "A_PROJECT_IS_ALREADY_OPEN"
        write_out("v19b_real_solve_report.json", report)
        print(json.dumps(report, ensure_ascii=False, indent=2, default=repr))
        return 5

    project = de.open_project(str(project_path))

    report["opened_project"] = str(project.filename())

    if str(project.filename()) != str(project_path):
        report["status"] = "FAILED"
        report["reason"] = "OPENED_PROJECT_PATH_MISMATCH"
        write_out("v19b_real_solve_report.json", report)
        print(json.dumps(report, ensure_ascii=False, indent=2, default=repr))
        return 6

    method_probe = cp.probe_control_methods(project.model3d)

    report["runtime_method_probe"] = method_probe

    if not method_probe["nonblocking_control_available"]:
        report["status"] = "BLOCKED"
        report["reason"] = "NONBLOCKING_CONTROL_UNAVAILABLE"
        write_out("v19b_real_solve_report.json", report)
        print(json.dumps(report, ensure_ascii=False, indent=2, default=repr))
        return 7

    backend = FloquetControlBackend(
        design_environment=de,
        project=project,
        expected_project_path=str(project_path),
        expected_pid=pid,
        method_probe=method_probe,
        expected_floquet_modes_per_face=2,
    )

    plane = cp.SolverControlPlane(
        backend,
        run_id=run_id,
        start_ack_window_seconds=START_ACK_WINDOW_SECONDS,
        running_timeout_seconds=RUNNING_TIMEOUT_SECONDS,
        poll_interval_seconds=POLL_INTERVAL_SECONDS,
        abort_confirm_seconds=ABORT_CONFIRM_SECONDS,
        max_consecutive_ipc_failures=3,
    )

    record = plane.execute()

    # the solve budget is only consumed if start_solver was actually dispatched
    dispatched = (record.get("start_request") or {}).get("ok") is True

    report["solve_dispatched"] = dispatched
    report["fresh_cst_solves"] = 1 if dispatched else 0
    report["control_plane"] = record
    report["floquet_preflight"] = backend.floquet_readback
    report["mode"] = record.get("mode")

    if not dispatched:
        # nothing was started, so nothing may be saved or interpreted
        report["status"] = "FAILED"
        report["reason"] = record.get("reason") or "NOT_DISPATCHED"
        report["note"] = (
            "start_solver was never dispatched; no solve authorisation was "
            "consumed"
        )
        write_out("v19b_real_solve_report.json", report)
        print(json.dumps(report, ensure_ascii=False, indent=2, default=repr))
        return 9

    project.save()

    report["project_hash_after_solve"] = sha256_file(project_path)

    # ------------------------------------------------- minimal solve gate
    evidence = record.get("completion_evidence") or {}

    if (
        record.get("phase") != cp.COMPLETED
        or evidence.get("native_state") != "SUCCESS"
    ):
        report["status"] = "FAILED"
        report["reason"] = "SOLVE_DID_NOT_COMPLETE_SUCCESSFULLY"
        write_out("v19b_real_solve_report.json", report)
        print(json.dumps(report, ensure_ascii=False, indent=2, default=repr))
        return 8

    # ------------------------------------------- raw result inventory
    inventory_report: dict = {
        "report": "v19b_raw_result_inventory",
        "phase": "F4_RESULT_IDENTIFIER_READBACK",
        "run_id": run_id,
        "project_path": str(project_path),
        "hard_coded_paths": [],
        "note": (
            "every identifier below is read back verbatim from the CST result "
            "database; no result path or S-parameter name is assumed"
        ),
    }

    try:
        module = ra.open_result_module(str(project_path))

        inventory = ra.inventory_result_tree(module)

        inventory_report["inventory"] = inventory
        inventory_report["tree_item_count"] = inventory["tree_item_count"]
        inventory_report["leaf_count"] = inventory["leaf_count"]
        inventory_report["tree_paths"] = [
            node["tree_path"] for node in inventory["nodes"]
        ]

        # every S-parameter-like leaf, recorded verbatim
        s_like = []

        for leaf in inventory.get("leaves", []):
            path = leaf.get("tree_path", "")
            leaf_name = path.split("\\")[-1]

            if not path.startswith("1D Results"):
                continue

            if "S-Parameter" not in path and not leaf_name.startswith("S"):
                continue

            entry = {
                "raw_tree_path": path,
                "raw_identifier": leaf_name,
                "sample_count": leaf.get("sample_count"),
                "item_run_id": leaf.get("item_run_id"),
                "complex_valued": leaf.get("complex_valued"),
                "title": leaf.get("title"),
                "x_label": leaf.get("x_label"),
                "y_label": leaf.get("y_label"),
                "has_ref_impedance": leaf.get("has_ref_impedance"),
            }

            s_like.append(entry)

        inventory_report["s_parameter_like_results"] = s_like
        inventory_report["s_parameter_like_count"] = len(s_like)

        # all other 0D/1D results (power / balance / energy / ...)
        inventory_report["non_s_results"] = [
            {
                "raw_tree_path": leaf.get("tree_path"),
                "sample_count": leaf.get("sample_count"),
                "complex_valued": leaf.get("complex_valued"),
                "title": leaf.get("title"),
                "x_label": leaf.get("x_label"),
                "y_label": leaf.get("y_label"),
            }
            for leaf in inventory.get("leaves", [])
            if not any(
                item["raw_tree_path"] == leaf.get("tree_path") for item in s_like
            )
        ]

        del module
        gc.collect()
    except Exception as exc:  # noqa: BLE001
        inventory_report["error"] = repr(exc)

    write_out("v19b_raw_result_inventory.json", inventory_report)

    report["raw_result_inventory"] = {
        "tree_item_count": inventory_report.get("tree_item_count"),
        "s_parameter_like_count": inventory_report.get("s_parameter_like_count"),
        "error": inventory_report.get("error"),
    }

    # ------------------------------------------------------ ledger
    ledger_record = {
        "record_type": "floquet_solve_run",
        "ledger_schema_version": VERSION,
        "tool_name": TOOL,
        "tool_version": VERSION,
        "run_id": run_id,
        "solve_index_within_operation": 1,
        "operation_id": "v19b_floquet_" + uuid.uuid4().hex[:8],
        "timestamp_utc": utc_now(),
        "test_only": True,
        "valid_for_science": False,
        "cst_pid": pid,
        "project_path": str(project_path),
        "project_hash_after_solve": report.get("project_hash_after_solve"),
        "control_mode": record.get("mode"),
        "start_requests": record.get("start_requests"),
        "phase": record.get("phase"),
        "native_state": evidence.get("native_state"),
        "running_observations": evidence.get("running_observations"),
        "abort": record.get("abort"),
        "result_tree_paths": inventory_report.get("tree_paths"),
        "s_parameter_identifiers": [
            item["raw_identifier"]
            for item in inventory_report.get("s_parameter_like_results", [])
        ],
        "fresh_cst_solves": 1,
        "integrity": {
            "exactly_one_start_request": record.get("start_requests") == 1,
            "start_solver_used": record.get("mode") == cp.MODE_NONBLOCKING,
            "run_solver_not_called": True,
            "no_abort_issued": (record.get("abort") or {}).get("called") in (None, False),
            "cst_process_killed": False,
            "unrelated_projects_touched": False,
        },
    }

    appended = append_ledger(ledger_record)

    report["ledger"] = {
        "path": str(LEDGER),
        "record_hash": appended.get("record_hash"),
        "verification": ms.verify_model_ledger(LEDGER),
    }

    project.close()
    gc.collect()

    report["cst_session_after"] = {
        "pid": pid,
        "is_connected": bool(de.is_connected()),
        "has_active_project": bool(de.has_active_project()),
        "open_projects": [str(x) for x in de.list_open_projects()],
    }

    report["finished_at_utc"] = utc_now()
    report["wall_seconds"] = round(time.perf_counter() - wall_t0, 3)

    checks = {
        "exactly_one_solve": report["fresh_cst_solves"] == 1,
        "nonblocking_start_solver": record.get("mode") == cp.MODE_NONBLOCKING,
        "one_start_request": record.get("start_requests") == 1,
        "start_acknowledged": any(
            item.get("phase") == cp.START_ACKNOWLEDGED
            for item in record.get("timeline", [])
        ),
        "running_observed": (evidence.get("running_observations") or 0) > 0,
        "completed": record.get("phase") == cp.COMPLETED,
        "native_success": evidence.get("native_state") == "SUCCESS",
        "no_abort": (record.get("abort") or {}).get("called") in (None, False),
        "result_tree_non_empty": bool(inventory_report.get("tree_item_count")),
        "s_parameters_present": bool(inventory_report.get("s_parameter_like_count")),
        "session_clean_after": report["cst_session_after"]["has_active_project"]
        is False,
        "cst_connected": report["cst_session_after"]["is_connected"],
    }

    report["checks"] = checks
    report["status"] = "PASSED" if all(checks.values()) else "FAILED"

    write_out("v19b_real_solve_report.json", report)

    print("run id                 :", run_id)
    print("project                :", project_path)
    print("control phase          :", record.get("phase"), "| mode:", record.get("mode"))
    print("native state           :", evidence.get("native_state"))
    print("running observations   :", evidence.get("running_observations"))
    print("timeline               :")
    for item in record.get("timeline", []):
        print(f"   {item.get('phase'):22} @ {item.get('at_seconds')}s")
    print()
    print("result tree items      :", inventory_report.get("tree_item_count"))
    print("S-like results         :", inventory_report.get("s_parameter_like_count"))
    for item in inventory_report.get("s_parameter_like_results", []):
        print("   RAW:", item["raw_tree_path"], "| id:", item["raw_identifier"],
              "| n:", item.get("sample_count"))
    print()
    print("checks:")

    for key, value in checks.items():
        print(f"  {key:28} {value}")

    print()
    print("status                 :", report["status"])

    return 0 if report["status"] == "PASSED" else 10


if __name__ == "__main__":
    raise SystemExit(main())
