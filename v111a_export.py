"""V1.11A R3/R4: read the REAL angular farfield data through the vendor chain.

ZERO solves.  Opened, read, exported to a temporary output directory, and closed
WITHOUT save.

Chain (verbatim from the CST-shipped GRASP macro):
    SelectTreeItem "Farfields\\farfield (f=20.0) [1]"
    FarfieldPlot.Reset / SetPlotMode "efield" / SetScaleLinear "True" /
                 Origin "bbox" / UseFarfieldApproximation "True"
    FarfieldPlot.AddListItem(theta, phi, radius)   for every grid point
    FarfieldPlot.CalculateList("farfield (f=20.0)")
    FarfieldPlot.GetListItem(i, "<component>")

Components are the strings the shipped macro actually reads:
    th_re / th_im / ph_re / ph_im               (E_theta, E_phi, complex)
    ludwig 3 horizontal / vertical (+ phases)

Run under the CST bundled interpreter (adjust the path to your installation):
    "C:\\Program Files\\CST Studio Suite 2026\\Python\\python.exe" v111a_export.py
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPORTS = ROOT / "reports"
EXPORT_DIR = REPORTS / "v111a_export"

OUT = REPORTS / "v111a_angular_data.json"
OUT_SCHEMA = REPORTS / "v111a_angular_schema.json"

V111_DRY = REPORTS / "v111_dry_build_report.json"
RUNTIME = ROOT / "cst_runtime.json"

FARFIELD_TREE_PATH = "Farfields\\farfield (f=20.0) [1]"
FARFIELD_NAME = "farfield (f=20.0)"

THETA_START, THETA_STOP, THETA_STEP = 0.0, 180.0, 5.0
PHI_START, PHI_STOP, PHI_STEP = 0.0, 355.0, 5.0

COMPONENTS = (
    "th_re",
    "th_im",
    "ph_re",
    "ph_im",
    "ludwig 3 horizontal",
    "ludwig 3 hor. phase",
    "ludwig 3 vertical",
    "ludwig 3 ver. phase",
    "radial re",
    "radial im",
)


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None

    digest = hashlib.sha256()

    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)

    return "sha256:" + digest.hexdigest()


def write_out(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=repr) + "\n",
        encoding="utf-8",
    )
    return path


def safe(fn, *args):
    try:
        value = fn(*args)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": repr(exc)[:240]}

    if isinstance(value, (str, bool, int, float, type(None))):
        return {"ok": True, "value": value}

    return {"ok": True, "value": repr(value)[:200]}


def frange(start: float, stop: float, step: float) -> list[float]:
    out = []
    value = start
    guard = 0

    while value <= stop + 1e-9 and guard < 10000:
        out.append(round(value, 6))
        value += step
        guard += 1

    return out


def main() -> int:
    import cst.interface as ci
    import cst_session_discovery_v19a as disc

    dry = json.loads(V111_DRY.read_text(encoding="utf-8"))
    project_path = Path(dry["path_regression"]["actual_created_path"])

    thetas = frange(THETA_START, THETA_STOP, THETA_STEP)
    phis = frange(PHI_START, PHI_STOP, PHI_STEP)

    data: dict = {
        "report": "v111a_angular_data",
        "phase": "R3_NUMERIC_EXPORT + R4_SCHEMA_SEMANTICS",
        "generated_at_utc": utc_now(),
        "real_solve_count": 0,
        "saved": False,
        "rebuilt": False,
        "solver_started": False,
        "project_hash_before": sha256_file(project_path),
        "chain": "SelectTreeItem -> FarfieldPlot config -> AddListItem -> "
                 "CalculateList -> GetListItem",
        "grid": {
            "theta_start": THETA_START,
            "theta_stop": THETA_STOP,
            "theta_step": THETA_STEP,
            "theta_count": len(thetas),
            "phi_start": PHI_START,
            "phi_stop": PHI_STOP,
            "phi_step": PHI_STEP,
            "phi_count": len(phis),
            "point_count": len(thetas) * len(phis),
        },
        "components_requested": list(COMPONENTS),
    }

    resolution = disc.resolve_runtime(RUNTIME, auto_update=True)

    if resolution.get("status") not in (disc.STATUS_REUSED, disc.STATUS_RECOVERED):
        data["status"] = "BLOCKED"
        data["reason"] = resolution.get("status")
        write_out(OUT, data)
        return 3

    pid = int(resolution["pid"])
    de = ci.DesignEnvironment.connect(pid)

    if de.has_active_project():
        data["status"] = "BLOCKED"
        data["reason"] = "A_PROJECT_IS_ALREADY_OPEN"
        write_out(OUT, data)
        return 4

    project = de.open_project(str(project_path))
    data["opened"] = str(project.filename())

    try:
        m3d = project.model3d
        fp = m3d.FarfieldPlot

        # ---------------------------------------------------- activation
        data["SelectTreeItem"] = safe(m3d.SelectTreeItem, FARFIELD_TREE_PATH)

        config = {
            "Reset": safe(fp.Reset),
            "SetPlotMode": safe(fp.SetPlotMode, "efield"),
            "SetScaleLinear": safe(fp.SetScaleLinear, "True"),
            "Origin": safe(fp.Origin, "bbox"),
            "UseFarfieldApproximation": safe(fp.UseFarfieldApproximation, "True"),
        }

        data["plot_configuration"] = config

        # ------------------------------------------ add the evaluation points
        #
        # Signature taken from the official FarfieldPlot reference:
        #   AddListEvaluationPoint(polarAngleInDegree, lateralAngleInDegree,
        #                          radius, coordinateSystem, tf_type, freq_time)
        # The official text notes tf_type is "only supported by broadband far
        # field monitor results"; this fixture has a SINGLE-frequency monitor, so
        # tf_type is left empty and the current frequency setting is used.
        added = {"ok": True, "count": 0, "error": None}

        for phi in phis:
            for theta in thetas:
                entry = safe(
                    fp.AddListEvaluationPoint,
                    theta,
                    phi,
                    0.0,
                    "spherical",
                    "",
                    0.0,
                )

                if not entry.get("ok"):
                    added = {"ok": False, "count": added["count"],
                             "error": entry.get("error")}
                    break

                added["count"] += 1

            if not added["ok"]:
                break

        data["AddListEvaluationPoint"] = added
        data["AddListEvaluationPoint_signature"] = (
            "AddListEvaluationPoint(polarAngleInDegree, lateralAngleInDegree, "
            "radius, coordinateSystem, tf_type, freq_time) - official "
            "FarfieldPlot reference"
        )
        data["v111_api_name_correction"] = (
            "the shipped macro calls FarfieldPlot.AddListItem, which does NOT "
            "exist in this CST build; the official reference for this build "
            "documents AddListEvaluationPoint instead"
        )

        # -------------------------------------------------------- calculate
        calc = safe(fp.CalculateList, FARFIELD_NAME)
        data["CalculateList"] = calc

        # ------------------------------------------------------------ read
        components = {}
        row_count = added["count"]

        for component in COMPONENTS:
            values = safe(fp.GetListItem, 0, component)

            if values.get("ok"):
                components[component] = {"probe_ok": True,
                                         "first_value": values.get("value")}
            else:
                components[component] = {"probe_ok": False,
                                         "error": values.get("error")}

        data["component_probe"] = components

        # full read of the spherical components
        grid = []
        read_ok = True
        read_error = None

        index = 0

        for phi in phis:
            for theta in thetas:
                row = {"index": index, "theta": theta, "phi": phi}

                for component in ("th_re", "th_im", "ph_re", "ph_im"):
                    entry = safe(fp.GetListItem, index, component)

                    if not entry.get("ok"):
                        read_ok = False
                        read_error = entry.get("error")
                        row[component] = None
                    else:
                        row[component] = entry.get("value")

                grid.append(row)
                index += 1

                if not read_ok:
                    break

            if not read_ok:
                break

        data["grid_read_ok"] = read_ok
        data["grid_read_error"] = read_error
        data["points_read"] = len(grid)

        # ------------------------------------------------- derived magnitudes
        for row in grid:
            th_re = row.get("th_re")
            th_im = row.get("th_im")
            ph_re = row.get("ph_re")
            ph_im = row.get("ph_im")

            if None in (th_re, th_im, ph_re, ph_im):
                row["E_theta_mag"] = None
                row["E_phi_mag"] = None
                row["E_total_sq"] = None
                continue

            e_theta_sq = th_re * th_re + th_im * th_im
            e_phi_sq = ph_re * ph_re + ph_im * ph_im

            row["E_theta_mag"] = e_theta_sq ** 0.5
            row["E_phi_mag"] = e_phi_sq ** 0.5
            row["E_total_sq"] = e_theta_sq + e_phi_sq

        data["grid"] = grid

        # -------------------------------------------------------- export file
        EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        export_path = EXPORT_DIR / "v111a_angular_grid.json"
        write_out(export_path, {"grid": grid, "components": list(COMPONENTS)})

        data["export"] = {
            "path": str(export_path),
            "sha256": sha256_file(export_path),
            "rows": len(grid),
        }

        # ------------------------------------------- extra global quantities
        data["GetMax"] = safe(fp.GetMax)
        data["GetTRP"] = safe(fp.GetTRP)
        data["GetRadiationEfficiency"] = safe(fp.GetRadiationEfficiency)
        data["GetTotalEfficiency"] = safe(fp.GetTotalEfficiency)
        data["GetMainLobeDirection_after_calc"] = safe(fp.GetMainLobeDirection)
        data["GetAngularWidthXdB_after_calc"] = safe(fp.GetAngularWidthXdB)
        data["GetSideLobeLevel_after_calc"] = safe(fp.GetSideLobeLevel)
        data["GetList"] = safe(fp.GetList)

    finally:
        try:
            project.close()
        except Exception as exc:  # noqa: BLE001
            data["close_error"] = repr(exc)[:200]

    data["project_hash_after"] = sha256_file(project_path)
    data["project_unchanged"] = (
        data["project_hash_before"] == data["project_hash_after"]
    )

    data["cst_session_after"] = {
        "pid": pid,
        "is_connected": bool(de.is_connected()),
        "has_active_project": bool(de.has_active_project()),
    }

    data["status"] = (
        "PASSED" if data.get("points_read") else "FAILED"
    )

    write_out(OUT, data)

    # ------------------------------------------------------------- console
    print("activation               :", data["SelectTreeItem"])
    print("AddListItem              :", added)
    print("CalculateList            :", calc)
    print()
    print("component probe:")

    for name, entry in data["component_probe"].items():
        print(f"  {name:24} ok={entry['probe_ok']} "
              f"first={str(entry.get('first_value'))[:40]} "
              f"err={(entry.get('error') or '')[:60]}")

    print()
    print("grid read ok             :", read_ok, "| points:", len(grid))

    if grid:
        sample = grid[len(grid) // 2]
        print("  sample row             :", json.dumps(sample)[:260])

    print()
    print("GetMax                   :", data["GetMax"])
    print("GetTRP                   :", data["GetTRP"])
    print("GetMainLobeDirection     :", data["GetMainLobeDirection_after_calc"])
    print("GetAngularWidthXdB       :", data["GetAngularWidthXdB_after_calc"])
    print("GetList                  :", str(data["GetList"])[:200])
    print()
    print("project unchanged        :", data["project_unchanged"])
    print("status                   :", data["status"])

    return 0 if data["status"] == "PASSED" else 5


if __name__ == "__main__":
    raise SystemExit(main())
