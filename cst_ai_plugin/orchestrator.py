"""CST_AI plugin layer — orchestrator.

The single entry point.  PLAN / RUN / STATUS / RESUME / INSPECT all live here.

Safety contract, enforced by static test:
    PLAN     never solves
    INSPECT  never mutates a CST project
    STATUS   never mutates a CST project
    RUN      performs only the side effects the ExecutionPlan lists
    RESUME   only continues unfinished stages, and never re-solves a point that
             already succeeded

The orchestrator contains no physics and no document identity.  Every real step
is delegated to a module that was already verified against CST.
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import capabilities as caps
from . import observables as obs
from . import runtime as rt
from .artifacts import ArtifactRegistry
from .bundle import make_bundle, write_bundle
from .errors import (
    ExistingResultNotFound,
    PluginError,
    StagePrerequisiteMissing,
)
from .planner import canonical_hash, execution_plan_hash, plan_task
from .preflight import preflight as run_preflight
from .task import authorized_solve_budget, task_spec_hash, validate_task

ORCHESTRATOR_VERSION = "1.15.0"

ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "ledger" / "v115_plugin_run_ledger.jsonl"

# ------------------------------------------------------------------- lifecycle
CREATED = "CREATED"
PLANNED = "PLANNED"
PREFLIGHT_PASSED = "PREFLIGHT_PASSED"
RUNNING = "RUNNING"
COMPLETED = "COMPLETED"
BLOCKED = "BLOCKED"
FAILED = "FAILED"
PARTIAL = "PARTIAL"

PLUGIN_LIFECYCLE = (
    CREATED, PLANNED, PREFLIGHT_PASSED, RUNNING, COMPLETED, BLOCKED, FAILED,
    PARTIAL,
)

#: resume checkpoint states - distinct from the plugin lifecycle on purpose
BUILD_COMPLETE = "BUILD_COMPLETE"
SOLVE_COMPLETE_ANALYSIS_PENDING = "SOLVE_COMPLETE_ANALYSIS_PENDING"
ANALYSIS_COMPLETE_REPORT_PENDING = "ANALYSIS_COMPLETE_REPORT_PENDING"
COMPLETE = "COMPLETE"

#: V1.18: a multi-point run (spec.points) checkpoints once per finished point.
#: Its meaning is "point N is durable, point N+1 has not started", which none of
#: the per-stage checkpoints above can express.
POINT_COMPLETE = "POINT_COMPLETE"

RESUME_STATES = (
    BUILD_COMPLETE, SOLVE_COMPLETE_ANALYSIS_PENDING,
    ANALYSIS_COMPLETE_REPORT_PENDING, COMPLETE, POINT_COMPLETE,
)

#: The durable checkpoint of a stage that SUCCEEDED, in the sense a later process
#: needs: "everything this state covers is on disk, so it must not be repeated".
#:
#: V1.15 only advanced the checkpoint when the `stop_after_stage` test hook fired,
#: so a real run that died after a successful SOLVE left no durable proof of it
#: and a resume would have re-solved that point.  Resuming must never spend a
#: second solve, so the checkpoint now advances by itself, and only forward.
#: CONNECT deliberately maps to None: a session is not durable progress.
_DURABLE_CHECKPOINT_FOR_STAGE = {
    "CONNECT": None,
    "BUILD": BUILD_COMPLETE,
    "VERIFY": BUILD_COMPLETE,
    "SOLVE": SOLVE_COMPLETE_ANALYSIS_PENDING,
    "EXTRACT": SOLVE_COMPLETE_ANALYSIS_PENDING,
    "ANALYZE": ANALYSIS_COMPLETE_REPORT_PENDING,
    "REPORT": ANALYSIS_COMPLETE_REPORT_PENDING,
}

#: Monotone order, so a late stage can never talk the checkpoint backwards.
_CHECKPOINT_RANK = {
    None: 0,
    BUILD_COMPLETE: 1,
    SOLVE_COMPLETE_ANALYSIS_PENDING: 2,
    ANALYSIS_COMPLETE_REPORT_PENDING: 3,
    COMPLETE: 4,
}

from .versions import MODULE_VERSIONS


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def make_run_id() -> str:
    return "run_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + \
        "_" + uuid.uuid4().hex[:8]


def _append_ledger(record: dict) -> dict:
    import cst_modelspec_v17 as ms

    head = None

    if LEDGER.exists():
        for line in LEDGER.read_text(encoding="utf-8").splitlines():
            if line.strip():
                head = json.loads(line).get("record_hash")

    LEDGER.parent.mkdir(parents=True, exist_ok=True)

    return ms.append_model_ledger_record(LEDGER, record, head)


class Orchestrator:
    """One orchestrator invocation == one run."""

    def __init__(self, *, plugin_version: str = ORCHESTRATOR_VERSION):
        self.plugin_version = plugin_version
        self.run_id: str | None = None
        self.lifecycle = CREATED
        self.checkpoint = None
        #: which point the durable checkpoint describes.  Without this, the
        #: checkpoint left by a finished point would be applied to the next,
        #: not-yet-built point and silently skip its build and solve.
        self.checkpoint_point_index: int | None = None
        self.transitions: list[dict] = []
        self.de = None
        self.pid = None
        self.plan = None
        #: set by BUILD, or resolved by EXTRACT for LOAD_EXISTING_RESULT.
        #: Declared here so a stage that runs before its producer fails with a
        #: typed error instead of an AttributeError.
        self.project = None
        self.project_path = None
        self.preflight_result = None
        self.registry: ArtifactRegistry | None = None
        self.observables: dict = {}
        self.solver_lifecycle = None
        self.solve_usage = {"planned": 0, "authorized": 0, "performed": 0,
                            "reused": 0}
        self.warnings: list[str] = []
        self.unresolved: list[str] = []
        self.errors: list[dict] = []
        self.started_at = None
        #: test hook: stop right after a named stage transition
        self.stop_after_stage: str | None = None
        #: V1.18 multi-point state.  `spec.points` turns one run into a sweep of
        #: isolated points, each with its own project/artifact identity.  The
        #: counters are the durable ones: `points_completed` is what a resumed
        #: process reads to know which points must NOT be solved again.
        self.current_point: dict | None = None
        self.current_point_index: int | None = None
        self.points_total = 0
        self.points_completed = 0
        self.point_states: list[dict] = []
        #: the point whose progress the resumed checkpoint describes
        self._resume_point_index: int | None = None
        #: per-invocation fact: which stages this process actually ran, skipped
        #: because the checkpoint already covered them, or skipped because a
        #: point's PLAN decided a cache hit
        self.stages_dispatched: list[str] = []
        self.stages_resumed_skipped: list[str] = []
        self.stages_reuse_skipped: list[str] = []
        #: test hook: stop right after a named point index transition
        self.stop_after_point: int | None = None
        #: OPTIONAL checkpoint observer.  Additive: when unset, the orchestrator
        #: behaves exactly as it did in V1.15.
        self.persist_hook = None

    # ------------------------------------------------------------ lifecycle
    def _transition(self, state: str, detail: dict | None = None) -> None:
        if state not in PLUGIN_LIFECYCLE:
            raise ValueError(f"unknown plugin lifecycle state {state!r}")

        self.lifecycle = state
        self.transitions.append(
            {"state": state, "at_utc": utc_now(), "seq": len(self.transitions),
             "detail": detail or {}}
        )

    def status(self) -> dict:
        """Report.  Never mutates anything."""

        return {
            "run_id": self.run_id,
            "plugin_lifecycle": self.lifecycle,
            "checkpoint": self.checkpoint,
            "solver_lifecycle": self.solver_lifecycle,
            "transitions": self.transitions,
            "solve_usage": self.solve_usage,
            "cst_contacted": self.de is not None,
            "mutated_cst_project": False,
            "artifacts": len(self.registry.all()) if self.registry else 0,
        }

    # ------------------------------------------------------------------ PLAN
    def plan_only(self, task: dict, reuse_index: dict | None = None) -> dict:
        """PLAN.  No CST, no solve, no filesystem write."""

        task = validate_task(task)
        self.run_id = make_run_id()
        self._transition(CREATED, {"task_id": task["task_id"]})

        plan = plan_task(task, reuse_index)

        self.plan = plan
        self.solve_usage["planned"] = plan["PLANNED_SOLVE_COUNT"]
        self.solve_usage["authorized"] = plan["MAX_AUTHORIZED_SOLVES"]

        self._transition(PLANNED, {"plan_hash": plan["execution_plan_hash"]})

        return plan

    # -------------------------------------------------------------- INSPECT
    def inspect(self, target: str) -> dict:
        """INSPECT.  Read-only.  Never mutates a CST project."""

        import cst.results as cr

        path = Path(target)

        if not path.is_file():
            return {"target": str(path), "exists": False, "read_only": True,
                    "mutated_cst_project": False, "cst_contacted": False}

        try:
            pf = cr.ProjectFile(str(path), allow_interactive=True)
            rm = pf.get_3d()
            tree = [str(t) for t in rm.get_tree_items()]
        except Exception as exc:  # noqa: BLE001
            # INSPECT must degrade, not crash: not every path is a CST project
            return {
                "target": str(path),
                "exists": True,
                "readable_as_cst_project": False,
                "reason": repr(exc)[:200],
                "read_only": True,
                "mutated_cst_project": False,
                "cst_contacted": False,
            }

        return {
            "target": str(path),
            "exists": True,
            "readable_as_cst_project": True,
            "tree_item_count": len(tree),
            "s_parameter_results": [
                t for t in tree if t.startswith("1D Results\\S-Parameters\\")
            ],
            "adaptive_meshing_results": [
                t for t in tree if t.startswith("1D Results\\Adaptive Meshing\\")
            ],
            "convergence_results": [
                t for t in tree if t.startswith("1D Results\\Convergence\\")
            ],
            "read_only": True,
            "mutated_cst_project": False,
            "cst_contacted": False,
        }

    # ------------------------------------------------------------ RUN / RESUME
    def run(self, task: dict, *, reuse_index: dict | None = None,
            runtime_file: Path | None = None, active_project: str | None = None,
            resume_from: dict | None = None,
            execute_stages: bool = True) -> dict:
        """The only method that performs side effects."""

        task = validate_task(task)

        if resume_from:
            self.run_id = resume_from.get("run_id") or make_run_id()
            self.checkpoint = resume_from.get("checkpoint")
            self.plan = resume_from.get("plan")
            self.registry = ArtifactRegistry(self.run_id)
            self.solve_usage = resume_from.get(
                "solve_usage", self.solve_usage
            )
            # A multi-point run resumes at the first point that was not persisted
            # as complete, and the checkpoint describes THAT point's progress.
            # The index is kept so stage-level skipping cannot leak onto the
            # points that come after it.
            self.points_completed = int(resume_from.get("points_completed") or 0)
            self.point_states = list(resume_from.get("point_states") or [])
            self._resume_point_index = self.points_completed
            self.checkpoint_point_index = resume_from.get(
                "checkpoint_point_index"
            )
        else:
            # Re-planning here minted a SECOND run id and split the run's identity:
            # `public.run()` plans first, keys the durable run store by that run_id,
            # then called this method, which planned again.  The run store ended up
            # under one id while the ArtifactRegistry (and therefore every artifact
            # and the ResultBundle) was stamped with the other, so the artifacts no
            # longer belonged to the run that reported them.  A caller that has
            # already planned keeps its identity; only an unplanned orchestrator
            # plans here.
            if self.plan is None:
                self.plan_only(task, reuse_index)

        if self.plan is None:
            self.plan = plan_task(task, reuse_index)

        self.started_at = utc_now()

        if self.registry is None:
            self.registry = ArtifactRegistry(self.run_id)

        # ---------------------------------------------------------- preflight
        probe_result = None

        if self.plan["CST_CONNECTION_REQUIRED"] and execute_stages:
            probe_result = rt.probe(runtime_file)

        self.preflight_result = run_preflight(
            task,
            runtime_probe=probe_result,
            active_project=active_project,
            reuse_index=reuse_index,
        )

        if self.preflight_result["status"] != "READY":
            self._transition(
                BLOCKED, {"preflight": self.preflight_result["status"]}
            )
            return self._finish(task, blocked=True)

        self._transition(PREFLIGHT_PASSED,
                         {"pid": (probe_result or {}).get("pid")})

        # -------------------------------------------------------------- stages
        self._transition(RUNNING)

        points = self._points(task)

        try:
            if not points:
                # the V1.15/V1.16/V1.17 single-subject path, unchanged
                for stage in self.plan["stage_order"]:
                    if self._dispatch_stage(task, stage):
                        self._transition(PARTIAL,
                                         {"stopped_after_stage": stage})
                        return self._finish(task, partial=True)

                self.checkpoint = COMPLETE
                self._transition(COMPLETED)
            else:
                for index in range(self.points_completed, len(points)):
                    self.current_point = points[index]
                    self.current_point_index = index
                    stopped_stage = None

                    for stage in self.plan["stage_order"]:
                        if self._dispatch_stage(task, stage):
                            stopped_stage = stage
                            break

                    if stopped_stage is not None:
                        self._transition(
                            PARTIAL,
                            {"stopped_after_stage": stopped_stage,
                             "point_index": index,
                             "point_id": (points[index] or {}).get("point_id")},
                        )
                        return self._finish(task, partial=True)

                    # one point = one isolated project identity: the next point
                    # must never inherit this point's open project
                    self._close_point_project()
                    self.points_completed = index + 1
                    self.point_states.append(self._point_state(index))
                    self.observables["sweep_points"] = [
                        dict(p) for p in self.point_states
                    ]

                    # durable checkpoint: POINT_<n>_COMPLETE only exists after the
                    # run store has this point's state on disk.  The checkpoint
                    # belongs to THIS point, so the resumed process will not apply
                    # it to the next one.
                    self.checkpoint_point_index = index

                    if self.persist_hook is not None:
                        self.persist_hook(self, f"POINT_{index + 1}_COMPLETE")

                    if self.stop_after_point == index:
                        self.checkpoint = POINT_COMPLETE
                        self._transition(
                            PARTIAL,
                            {"stopped_after_point": index,
                             "point_id": (points[index] or {}).get("point_id"),
                             "points_completed": self.points_completed},
                        )
                        return self._finish(task, partial=True)

                self.checkpoint = COMPLETE
                self._transition(COMPLETED)

        except PluginError as exc:
            self.errors.append(exc.to_dict())
            self._transition(FAILED, {"error": exc.code})
        except Exception as exc:  # noqa: BLE001
            self.errors.append(
                {"error_code": "UNEXPECTED", "message": repr(exc)[:300],
                 "stage": None}
            )
            self._transition(FAILED, {"error": "UNEXPECTED"})

        return self._finish(task)

    def _dispatch_stage(self, task: dict, stage: str) -> bool:
        """Run ONE stage.  Returns True when the run must stop after it.

        Factored out of the V1.15 loop so the single-subject path and the V1.18
        multi-point path cannot drift apart.
        """

        if self.checkpoint in (
            SOLVE_COMPLETE_ANALYSIS_PENDING,
            ANALYSIS_COMPLETE_REPORT_PENDING,
            COMPLETE,
        ) and self._skip_for_resume(stage):
            self.stages_resumed_skipped.append(stage)
            return False

        # A point whose PLAN decided a cache hit is not built, not verified and
        # not solved - it is read.  Building it would mint a second project
        # identity for a point that already has one, so the reuse decision is
        # honoured for BUILD and VERIFY as well as SOLVE.
        if self._point_is_reused():
            if stage == "BUILD":
                self._adopt_reused_project(self._solve_reuse_hit())
                self.stages_reuse_skipped.append(stage)
                return False

            if stage == "VERIFY":
                self.warnings.append(
                    "VERIFY skipped: this point reuses an existing solved "
                    "artifact and was not rebuilt"
                )
                self.stages_reuse_skipped.append(stage)
                return False

            if stage == "SOLVE":
                self.solve_usage["reused"] += 1
                self.observables["solve_reused"] = self._solve_reuse_hit()
                self.stages_reuse_skipped.append(stage)
                return False

        # A reused solve must not be re-solved.  V1.16 recorded the
        # reuse decision in the plan (PLANNED_SOLVE_COUNT=0,
        # solve_allowed=False, reused_instead_of_solving=<hit>) but the
        # loop dispatched every stage unconditionally, so a REUSE_ONLY
        # run with a cache hit still spent a real solve.  The decision is
        # honoured here; `executes` cannot be used for this because it is
        # also False for ANALYZE and REPORT, which must still run.
        if stage == "SOLVE" and self._solve_is_reused():
            hit = self._solve_reuse_hit()
            self.solve_usage["reused"] += 1
            self.observables["solve_reused"] = hit
            self._adopt_reused_project(hit)
            self.stages_reuse_skipped.append(stage)
            return False

        self.stages_dispatched.append(stage)
        getattr(self, f"_stage_{stage.lower()}")(task)

        # The stage returned, so it succeeded: record the furthest durable
        # progress BEFORE the checkpoint is written, so the run store can never
        # claim less than is true and a resume can never repeat a solve.
        candidate = _DURABLE_CHECKPOINT_FOR_STAGE.get(stage)

        if candidate is not None and (
            _CHECKPOINT_RANK.get(candidate, 0)
            > _CHECKPOINT_RANK.get(self.checkpoint, 0)
        ):
            self.checkpoint = candidate
            self.checkpoint_point_index = self.current_point_index

        # durable checkpoint: the run store writes atomically here, so a
        # crash between stages can never lose a completed stage
        if self.persist_hook is not None:
            self.persist_hook(self, stage)

        if self.stop_after_stage == stage:
            self.checkpoint = self._checkpoint_for(stage)
            return True

        return False

    def _points(self, task: dict) -> list[dict]:
        """`spec.points` when the task is a multi-point run, else [].

        An empty list means "the V1.15 single-subject path", so every existing
        task keeps its exact behaviour.
        """

        raw = (task.get("spec") or {}).get("points")

        if not isinstance(raw, list) or not raw:
            return []

        points = []

        for index, point in enumerate(raw):
            point = dict(point) if isinstance(point, dict) else {}
            point.setdefault("point_id", f"point_{index}")
            point["index"] = index
            points.append(point)

        self.points_total = len(points)

        return points

    def _close_point_project(self) -> None:
        """A finished point's project is closed before the next one is built."""

        if getattr(self, "project", None) is None:
            return

        try:
            self.project.save()
            self.project.close()
        except Exception as exc:  # noqa: BLE001
            self.warnings.append(f"point project close warning: {exc!r}"[:200])

        self.project = None

    def _point_state(self, index: int) -> dict:
        point = self.current_point or {}
        solve_entry = self._point_solve_entry() or {}

        point_id = point.get("point_id")

        artifacts = []

        for artifact in self.registry.all():
            artifact_id = artifact.get("artifact_id") or ""
            if point_id and not artifact_id.endswith("@" + str(point_id)):
                continue
            artifacts.append(
                {
                    "artifact_id": artifact_id,
                    "type": artifact.get("type"),
                    "path": artifact.get("path"),
                    "hash": artifact.get("hash"),
                    "status": artifact.get("status"),
                }
            )

        return {
            "index": index,
            "point_id": point_id,
            "parameters": point.get("parameters"),
            "project_path": str(self.project_path) if self.project_path else None,
            "solved": bool(solve_entry.get("solve_allowed")),
            "reused": bool(solve_entry.get("reuse")),
            "reused_artifact": (
                (solve_entry.get("reuse") or {}).get("artifact")
                if isinstance(solve_entry.get("reuse"), dict) else None
            ),
            "solve_usage_after": dict(self.solve_usage),
            # The unified per-point table needs the mesh summary and the
            # frequency-sweep status next to the resonance numbers, so both are
            # carried here instead of only living in the flat observable map.
            "observables": {
                k: v for k, v in self.observables.items()
                if k in (
                    "resonance", "power_rta", "sweep_point",
                    "mesh_timeline", "frequency_sweep_state",
                )
            },
            "artifacts": artifacts,
        }

    def _skip_for_resume(self, stage: str) -> bool:
        """A solved point is never solved again."""

        if self.checkpoint == COMPLETE:
            return True

        # In a multi-point run the checkpoint records the progress of ONE point.
        # Once that point is behind us, every later point must run its whole
        # stage order - otherwise the checkpoint left by point N would silently
        # skip point N+1's build, verify and solve.  A checkpoint that does not
        # carry a point index is likewise not evidence about this point.
        if self.points_total:
            if self.current_point_index != self._resume_point_index:
                return False

            if self.checkpoint_point_index != self.current_point_index:
                return False

        if self.checkpoint == POINT_COMPLETE:
            return True

        if self.checkpoint == SOLVE_COMPLETE_ANALYSIS_PENDING:
            return stage in ("CONNECT", "BUILD", "VERIFY", "SOLVE")

        if self.checkpoint == ANALYSIS_COMPLETE_REPORT_PENDING:
            return stage != "REPORT"

        if self.checkpoint == BUILD_COMPLETE:
            return stage in ("CONNECT", "BUILD", "VERIFY")

        return False

    @staticmethod
    def _checkpoint_for(stage: str) -> str:
        return {
            "BUILD": BUILD_COMPLETE,
            "SOLVE": SOLVE_COMPLETE_ANALYSIS_PENDING,
            "ANALYZE": ANALYSIS_COMPLETE_REPORT_PENDING,
        }.get(stage, BUILD_COMPLETE)

    # ------------------------------------------------------------- the stages
    def _stage_connect(self, task: dict) -> None:
        self.de, self.pid, probe_result = rt.connect(
            Path(task["cst"]["runtime_file"]) if task.get("cst") else None
        )
        self.session = rt.session_safety(self.de)

    def _stage_build(self, task: dict) -> None:
        spec = task["spec"]
        point = self.current_point or {}
        out = Path(task["output_dir"])
        out.mkdir(parents=True, exist_ok=True)

        # the fixture definition supplies the numbers; the orchestrator only
        # schedules.  A multi-point run names them per point, so one point = one
        # isolated project/artifact identity.
        fixture_path = Path(
            point.get("fixture_definition") or spec["fixture_definition"]
        )
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        blocks = fixture["command_blocks"]

        # Absolute, for two independent reasons: `cst.results.ProjectFile`
        # cannot read the results of a relatively named project, and the solver
        # control plane compares `str(project.filename())` against this path
        # before it is allowed to start a solver.
        project_path = Path(
            point.get("project_path") or spec["project_path"]
        ).resolve()

        import cst.interface as ci

        project = self.de.new_project(ci.ProjectType.MWS)
        project.save(str(project_path))

        for name, block in blocks.items():
            project.model3d.add_to_history("v115_cmd_" + name, block)

        project.model3d.Rebuild()
        project.save()

        self.project = project
        self.project_path = project_path

        self.registry.register(
            artifact_id=self._artifact_id("project"),
            artifact_type="CST_PROJECT",
            path=project_path,
            producer="cst_ai_plugin.orchestrator._stage_build",
            inputs=[str(fixture_path)],
            valid_for_science=False,
            execution_status="COMPLETED",
            scientific_status="TEST_ONLY",
            provenance_status="PARTIAL",
        )

    def _stage_verify(self, task: dict) -> None:
        m3d = self.project.model3d

        point = self.current_point or {}

        readback = {
            "solver_type": str(m3d.GetSolverType()),
            "fmin": float(m3d.Solver.GetFmin()),
            "fmax": float(m3d.Solver.GetFmax()),
        }

        # A unit-cell boundary is a Floquet-fixture property, not a universal
        # one.  A radiator (dipole) fixture uses expanded-open boundaries, where
        # these getters are not meaningful; V1.18 records the absence instead of
        # failing a project that is perfectly well defined.
        for key, getter in (
            ("unit_cell_ds1", lambda: float(m3d.Boundary.GetUnitCellDs1())),
            ("unit_cell_ds2", lambda: float(m3d.Boundary.GetUnitCellDs2())),
        ):
            try:
                readback[key] = getter()
            except Exception as exc:  # noqa: BLE001
                readback[key] = None
                self.warnings.append(
                    f"VERIFY: {key} not readable for this boundary ({exc!r})"[:200]
                )

        expected = point.get("expected_readback") or task["spec"].get(
            "expected_readback"
        ) or {}
        failures = []

        for key, value in expected.items():
            actual = readback.get(key)

            if actual is None and key in ("unit_cell_ds1", "unit_cell_ds2"):
                # explicitly not applicable to this fixture: report it, do not
                # invent a value and do not fail
                self.warnings.append(
                    f"VERIFY: {key} expected {value!r} but is not applicable "
                    "to this boundary"
                )
                continue

            if isinstance(value, (int, float)) and isinstance(actual, (int, float)):
                if abs(actual - value) > 1e-6:
                    failures.append({"key": key, "expected": value,
                                     "actual": actual})
            elif actual != value:
                failures.append({"key": key, "expected": value, "actual": actual})

        self.verify_readback = readback
        self.verify_failures = failures

        self.registry.register(
            artifact_id=self._artifact_id("semantic_readback"),
            artifact_type="REPORT_JSON",
            path=None,
            producer="cst_ai_plugin.orchestrator._stage_verify",
            valid_for_science=False,
            execution_status="COMPLETED" if not failures else "FAILED",
            scientific_status="TEST_ONLY",
            extra={"readback": readback, "failures": failures},
        )

        if failures:
            from .errors import SemanticReadbackFailed

            raise SemanticReadbackFailed(
                f"semantic readback mismatches: {failures}", stage="VERIFY",
                detail=readback,
            )

    def _solve_entry(self) -> dict | None:
        """The plan's own SOLVE entry, or None when the plan has no SOLVE stage."""

        for entry in (self.plan or {}).get("stages") or ():
            if entry.get("stage") == "SOLVE":
                return entry

        return None

    def _point_solve_entry(self) -> dict | None:
        """The plan's SOLVE decision for the point currently in flight.

        A multi-point plan cannot express reuse in a single global decision: the
        V1.18 sweep reuses one point and solves the others, so the decision is
        planned per point and read per point here.
        """

        entry = self._solve_entry()

        if not entry or self.current_point_index is None:
            return None

        for candidate in entry.get("points") or ():
            if candidate.get("index") == self.current_point_index:
                return candidate

        return None

    def _solve_reuse_hit(self) -> dict | None:
        point_entry = self._point_solve_entry()

        if point_entry is not None:
            hit = point_entry.get("reuse")
            return hit if isinstance(hit, dict) else None

        entry = self._solve_entry()

        if not entry:
            return None

        hit = entry.get("reused_instead_of_solving")

        return hit if isinstance(hit, dict) else None

    def _point_is_reused(self) -> bool:
        """The point in flight already has a solved artifact.

        A per-point reuse decision is stronger than the global one: the point has
        an identity on disk, so BUILD, VERIFY and SOLVE are all satisfied by it.
        """

        entry = self._point_solve_entry()

        if entry is None:
            return False

        return (
            entry.get("solve_allowed") is False
            and isinstance(entry.get("reuse"), dict)
        )

    def _solve_is_reused(self) -> bool:
        """True when the plan decided a cache hit satisfies the SOLVE stage.

        The planner already refused to authorise a solve in that case
        (`PLANNED_SOLVE_COUNT` excludes it and `solve_allowed` is False), so
        executing the stage would spend a solve the plan never budgeted and
        contradict the reuse decision persisted in the run store.  Requirement is
        explicit: a selected reusable solved artifact must never be re-solved.
        """

        if self._point_is_reused():
            return True

        entry = self._solve_entry()

        if not entry or self._solve_reuse_hit() is None:
            return False

        return entry.get("solve_allowed") is False

    def _adopt_reused_project(self, hit: dict | None) -> None:
        """A reused point reads the artifact that already exists.

        Nothing is built, nothing is solved and nothing is written to it: the
        stored solved project is opened read-only by EXTRACT.
        """

        if not isinstance(hit, dict):
            return

        artifact = hit.get("artifact")

        if artifact:
            self.project_path = Path(artifact).resolve()

        self.registry.register(
            artifact_id=self._artifact_id("solved_project"),
            artifact_type="CST_PROJECT",
            path=self.project_path,
            producer="cst_ai_plugin.orchestrator._adopt_reused_project",
            valid_for_science=False,
            execution_status="COMPLETED",
            numerical_status="RESOLVED",
            scientific_status="TEST_ONLY",
            provenance_status="COMPLETE",
            extra={"reused": True, "reuse_key": hit.get("key")},
        )

    def _artifact_id(self, base: str) -> str:
        """Per-point artifact ids, so points cannot overwrite each other."""

        if not self.points_total:
            return base

        point = self.current_point or {}

        return f"{base}@{point.get('point_id', self.current_point_index)}"

    def _stage_solve(self, task: dict) -> None:
        import cst_solver_control_v19a as cp
        import v19b_real_solve as v19b

        spec = task["spec"]
        point = self.current_point or {}
        budget = authorized_solve_budget(task)

        if self.solve_usage["performed"] >= budget:
            from .errors import SolveBudgetExceeded

            raise SolveBudgetExceeded(
                f"already performed {self.solve_usage['performed']} solves, "
                f"budget {budget}",
                stage="SOLVE",
            )

        method_probe = cp.probe_control_methods(self.project.model3d)

        # V1.18: which solver control backend fits this fixture is a property of
        # the fixture, not of the orchestrator.  A Floquet unit cell needs the
        # Floquet backend (which preflights the mode table); a radiator with a
        # discrete port needs the plain port-based backend.  Default stays
        # "floquet" so every pre-V1.18 task keeps its exact behaviour.
        backend_kind = str(
            point.get("solver_backend") or spec.get("solver_backend") or "floquet"
        ).lower()

        if backend_kind == "plain":
            backend = cp.CstControlBackend(
                design_environment=self.de,
                project=self.project,
                expected_project_path=str(self.project_path),
                expected_pid=self.pid,
                method_probe=method_probe,
            )
        elif backend_kind == "floquet":
            backend = v19b.FloquetControlBackend(
                design_environment=self.de,
                project=self.project,
                expected_project_path=str(self.project_path),
                expected_pid=self.pid,
                method_probe=method_probe,
                expected_floquet_modes_per_face=int(
                    point.get("floquet_modes_per_face")
                    or spec.get("floquet_modes_per_face", 2)
                ),
            )
        else:
            from .errors import TaskSchemaError

            raise TaskSchemaError(
                f"unknown spec.solver_backend {backend_kind!r}; expected "
                "'floquet' or 'plain'",
                stage="SOLVE",
            )

        plane = cp.SolverControlPlane(
            backend,
            run_id=self.run_id,
            start_ack_window_seconds=300.0,
            running_timeout_seconds=2400.0,
            poll_interval_seconds=2.0,
            abort_confirm_seconds=30.0,
            max_consecutive_ipc_failures=3,
        )

        record = plane.execute()

        if (record.get("start_request") or {}).get("ok") is not True:
            from .errors import StartNotAcknowledged

            raise StartNotAcknowledged(
                "solver start was not acknowledged", stage="SOLVE",
                detail=record.get("reason"),
            )

        self.solve_usage["performed"] += 1
        self.solver_lifecycle = {
            "phase": record.get("phase"),
            "native_state": (record.get("completion_evidence") or {}).get(
                "native_state"
            ),
            "mode": record.get("mode"),
            "start_requests": record.get("start_requests"),
            "backend": backend_kind,
        }

        self.project.save()

        if record.get("phase") != cp.COMPLETED or self.solver_lifecycle[
            "native_state"
        ] != "SUCCESS":
            from .errors import SolverFailed

            raise SolverFailed(
                "solver did not complete successfully", stage="SOLVE",
                detail=self.solver_lifecycle,
            )

        self.registry.register(
            artifact_id=self._artifact_id("solved_project"),
            artifact_type="CST_PROJECT",
            path=self.project_path,
            producer="cst_ai_plugin.orchestrator._stage_solve",
            valid_for_science=False,
            execution_status="COMPLETED",
            numerical_status="RESOLVED",
            scientific_status="TEST_ONLY",
            provenance_status="COMPLETE",
            extra={"reused": False, "backend": backend_kind},
        )

    def _resolve_existing_project(self, spec: dict) -> Path:
        """Wire LOAD_EXISTING_RESULT to the project it names.

        V1.15/V1.16 declared LOAD_EXISTING_RESULT as a task kind and advertised
        the zero-solve path `LOAD_EXISTING_RESULT -> EXTRACT -> REPORT`, but no
        stage ever resolved the existing project: `project_path` was only ever
        set by BUILD, and BUILD is not part of this kind's declared stages.  The
        kind therefore could not execute.  The resolution is read-only.
        """

        declared = spec.get("existing_project") or spec.get("project_path")

        if not declared:
            raise ExistingResultNotFound(
                "this is a LOAD_EXISTING_RESULT task but spec.existing_project "
                "(or spec.project_path) does not name the solved project to read",
                stage="EXTRACT",
                detail={"spec_keys": sorted(spec)},
            )

        path = Path(declared)

        if not path.is_file():
            raise ExistingResultNotFound(
                f"the existing solved project {str(path)!r} does not exist",
                stage="EXTRACT",
                detail={"existing_project": str(path)},
            )

        # `cst.results.ProjectFile` is handed this path verbatim.  Given a
        # relative path it cannot build its storage ("Cannot create storage
        # with non-existent base path") and then answers every tree lookup with
        # `ResultItem does not exist for run id=0`.  V1.15 always passed an
        # absolute path, which is why the bug only showed up once a task
        # declared the project relatively.
        return path.resolve()

    def _stage_extract(self, task: dict) -> None:
        """Dispatch EXTRACT to the extractor this fixture actually needs.

        A Floquet unit cell and a radiator have nothing in common past the
        solver: one is described by multimode reflection/transmission, the other
        by a port reflectance plus a stored farfield.  Which one applies is a
        property of the fixture, so it is read from the spec instead of being
        assumed here.
        """

        spec = task["spec"]
        mode = str(
            (self.current_point or {}).get("extract_mode")
            or spec.get("extract_mode")
            or "floquet_rta"
        ).lower()

        if mode in ("radiator", "radiator_farfield"):
            return self._extract_radiator(task)

        if mode != "floquet_rta":
            from .errors import TaskSchemaError

            raise TaskSchemaError(
                f"unknown extract_mode {mode!r}; expected 'floquet_rta' or "
                "'radiator'",
                stage="EXTRACT",
            )

        return self._extract_floquet_rta(task)

    def _extract_floquet_rta(self, task: dict) -> None:
        import cst.results as cr
        import cst_result_api_v19 as ra

        if self.project_path is None:
            # LOAD_EXISTING_RESULT: reuse a project that was solved earlier
            # rather than building one.  Nothing is written to it.
            self.project_path = self._resolve_existing_project(task["spec"])

        pf = cr.ProjectFile(str(self.project_path), allow_interactive=True)
        rm = pf.get_3d()
        tree = [str(t) for t in rm.get_tree_items()]

        channels = {}
        identities = {}

        for path in tree:
            if not path.startswith("1D Results\\S-Parameters\\"):
                continue

            name = path.split("\\")[-1]
            identity = obs.parse_floquet_channel(name)

            channels[name] = ra.extract_1d_arrays(rm.get_result_item(path))
            identities[name] = identity

        self.channels = channels
        self.channel_identities = identities

        incident = task["spec"].get("incident_mode", "Zmin(1)")
        in_face, in_mode = incident.replace(")", "").split("(")

        reflected = [
            n for n in channels
            if n.endswith(f",{in_face}({in_mode})")
            and n.startswith(f"S{in_face}(")
        ]
        transmitted = [
            n for n in channels
            if n.endswith(f",{in_face}({in_mode})")
            and not n.startswith(f"S{in_face}(")
        ]

        frequencies = next(iter(channels.values()))["x"]

        r_total, t_total, a_balance = [], [], []

        for index in range(len(frequencies)):
            r = sum(
                channels[n]["y_real"][index] ** 2
                + channels[n]["y_imag"][index] ** 2
                for n in reflected
            )
            t = sum(
                channels[n]["y_real"][index] ** 2
                + channels[n]["y_imag"][index] ** 2
                for n in transmitted
            )

            r_total.append(r)
            t_total.append(t)
            a_balance.append(1.0 - r - t)

        self.frequencies = frequencies
        self.rta = {"R_total": r_total, "T_total": t_total, "A_balance": a_balance}

        # A caller asking "what are the main S-parameters and how many points is
        # the curve" must not be handed the arrays: the tool surface shrinks
        # them anyway.  So summarise each channel here - length, range and the
        # minimum - and let the arrays stay in memory for ANALYZE.
        s_parameter_summary = {}

        for name in sorted(channels):
            sample = channels[name]
            magnitudes = [
                (re_ * re_ + im_ * im_) ** 0.5
                for re_, im_ in zip(sample["y_real"], sample["y_imag"])
            ]
            s_parameter_summary[name] = {
                "sample_count": int(sample["sample_count"]),
                "min_magnitude": min(magnitudes) if magnitudes else None,
                "max_magnitude": max(magnitudes) if magnitudes else None,
                "magnitude_at_first_frequency": magnitudes[0] if magnitudes else None,
                "magnitude_at_last_frequency": magnitudes[-1] if magnitudes else None,
            }

        self.observables["s_parameter_summary"] = s_parameter_summary

        self.observables["floquet_channels"] = {
            "count": len(channels),
            "names": sorted(channels),
            "identities": identities,
            "sample_counts": {
                name: int(channels[name]["sample_count"]) for name in sorted(channels)
            },
            "frequency_point_count": len(frequencies),
            "frequency_range_ghz": [
                min(frequencies), max(frequencies),
            ] if frequencies else None,
        }
        self.observables["power_rta"] = {
            "incident_mode": incident,
            "reflected_channels": sorted(reflected),
            "transmitted_channels": sorted(transmitted),
            "R_total_range": [min(r_total), max(r_total)],
            "T_total_range": [min(t_total), max(t_total)],
        }

        # mesh timeline
        adaptive = [t for t in tree if t.startswith(
            "1D Results\\Adaptive Meshing\\")]
        mesh = {"adaptation_result_count": len(adaptive)}

        meshcells = [
            t for t in adaptive if t.endswith("Meshcells")
        ]

        if meshcells:
            cells = ra.extract_1d_arrays(rm.get_result_item(meshcells[0]))
            mesh["pass_count"] = cells["sample_count"]
            mesh["final_mesh_cells"] = cells["y_real"][-1]

        self.observables["mesh_timeline"] = mesh

        # frequency sweep state
        freq_a_path = "1D Results\\Convergence\\S-Parameters\\All S-Parameters"
        freq_state = {"present": freq_a_path in tree}

        if freq_state["present"]:
            interp = ra.extract_1d_arrays(rm.get_result_item(freq_a_path))
            freq_state["calculation_count"] = interp["sample_count"]
            freq_state["final_interpolation_error"] = interp["y_real"][-1]

        self.observables["frequency_sweep_state"] = freq_state

    def _extract_radiator(self, task: dict) -> None:
        """Radiator EXTRACT: inventory + return loss + efficiencies + farfield.

        Every step is a READ.  Activating the stored farfield (`SelectTreeItem`)
        changes which result is the active selection - session state, not model
        history - and no solve, rebuild or save happens anywhere in here.
        """

        import math
        import re

        import cst.results as cr
        import cst_result_api_v19 as ra
        import v118_radiator as radiator

        spec = task["spec"]
        point = self.current_point or {}
        out = Path(task["output_dir"])

        farfield_spec = point.get("farfield") or spec.get("farfield")

        if not isinstance(farfield_spec, dict):
            from .errors import TaskSchemaError

            raise TaskSchemaError(
                "a radiator task must declare spec.farfield (tree_path, name, "
                "theta, phi, monitor_frequency_ghz)",
                stage="EXTRACT",
            )

        required = ("tree_path", "name", "theta", "phi", "monitor_frequency_ghz")

        for key in required:
            if key not in farfield_spec:
                from .errors import TaskSchemaError

                raise TaskSchemaError(
                    f"spec.farfield.{key} is required for a radiator task",
                    stage="EXTRACT",
                    detail={"farfield_keys": sorted(farfield_spec)},
                )

        if self.project_path is None or not Path(self.project_path).is_file():
            raise ExistingResultNotFound(
                f"the solved project {str(self.project_path)!r} is not readable",
                stage="EXTRACT",
                detail={"project_path": str(self.project_path)},
            )

        # ------------------------------------------------- 1. result inventory
        pf = cr.ProjectFile(str(self.project_path), allow_interactive=True)
        rm = pf.get_3d()
        tree = [str(t) for t in rm.get_tree_items()]

        channels = {}
        sample_counts = {}

        for path in tree:
            if not path.startswith("1D Results\\S-Parameters\\"):
                continue

            name = path.split("\\")[-1]
            arrays = ra.extract_1d_arrays(rm.get_result_item(path))
            channels[name] = arrays
            sample_counts[name] = arrays["sample_count"]

        reflection = sorted(n for n in channels if re.match(r"^S(.+),\1$", n))
        s11_name = (
            reflection[0] if reflection
            else (sorted(channels)[0] if channels else None)
        )

        self.observables["result_inventory"] = {
            "tree_item_count": len(tree),
            "s_parameter_channels": sorted(channels),
            "s_parameter_sample_counts": sample_counts,
            "farfield_results": [t for t in tree if t.startswith("Farfields\\")],
            "adaptive_meshing_results": [
                t for t in tree if t.startswith("1D Results\\Adaptive Meshing\\")
            ],
            "convergence_results": [
                t for t in tree if t.startswith("1D Results\\Convergence\\")
            ],
            "reflection_channel": s11_name,
            "reflection_channel_rule": (
                "^S<port>,<port>$ - the diagonal of the S matrix is the port "
                "reflectance"
            ),
            "read_only": True,
            "mutated_cst_project": False,
        }

        # ------------------------------------------------------- 2. return loss
        monitor_ghz = float(farfield_spec["monitor_frequency_ghz"])
        spectra = None

        if s11_name:
            channel = channels[s11_name]
            freqs = [float(v) for v in channel["x"]]
            re_part = [float(v) for v in channel["y_real"]]
            im_part = [float(v) for v in channel["y_imag"]]
            magnitudes = [
                (re * re + im * im) ** 0.5 for re, im in zip(re_part, im_part)
            ]

            brackets = [
                i for i in range(len(freqs) - 1)
                if freqs[i] <= monitor_ghz <= freqs[i + 1]
            ]

            if brackets:
                i = brackets[0]
                span = freqs[i + 1] - freqs[i]
                w = 0.0 if span == 0 else (monitor_ghz - freqs[i]) / span
                re_value = re_part[i] + w * (re_part[i + 1] - re_part[i])
                im_value = im_part[i] + w * (im_part[i + 1] - im_part[i])
            else:
                nearest = min(
                    range(len(freqs)), key=lambda k: abs(freqs[k] - monitor_ghz)
                )
                re_value = re_part[nearest]
                im_value = im_part[nearest]

            magnitude = (re_value * re_value + im_value * im_value) ** 0.5
            best_index = min(range(len(magnitudes)), key=lambda k: magnitudes[k])

            self.observables["s_parameters"] = {
                "channel": s11_name,
                "sample_count": channel["sample_count"],
                "frequency_range_ghz": [freqs[0], freqs[-1]],
                "monitor_frequency_ghz": monitor_ghz,
                "at_monitor_frequency": {
                    "real": re_value,
                    "imag": im_value,
                    "magnitude_linear": magnitude,
                    "magnitude_db": (
                        20.0 * math.log10(magnitude) if magnitude > 0 else None
                    ),
                    "db_convention": (
                        "20*log10|S11| - a FIELD ratio, so the factor is 20 and "
                        "not the 10 used for the power-like farfield quantities"
                    ),
                    "interpolation": "piecewise linear on the complex samples",
                },
                "minimum_in_band": {
                    "frequency_ghz": freqs[best_index],
                    "magnitude_linear": magnitudes[best_index],
                    "magnitude_db": (
                        20.0 * math.log10(magnitudes[best_index])
                        if magnitudes[best_index] > 0 else None
                    ),
                },
                "read_only": True,
            }

            spectra = {
                "channel": s11_name,
                "frequencies_ghz": freqs,
                "return_loss_magnitude": magnitudes,
            }

        else:
            self.observables["s_parameters"] = {
                "status": "NO_REFLECTION_CHANNEL",
                "s_parameter_channels": sorted(channels),
            }

        # -------------------------------------------- 3. farfield activation
        thetas = radiator.frange(*[float(v) for v in farfield_spec["theta"]])
        phis = radiator.frange(*[float(v) for v in farfield_spec["phi"]])

        opened_here = False

        if getattr(self, "project", None) is not None:
            model3d = self.project.model3d
        else:
            # a reused point was never built, so the stored project is opened
            # here purely to be read and is closed again right after
            self.project = self.de.open_project(str(self.project_path))
            opened_here = True
            model3d = self.project.model3d

        try:
            evidence = radiator.extract(
                model3d,
                tree_path=str(farfield_spec["tree_path"]),
                farfield_name=str(farfield_spec["name"]),
                thetas=thetas,
                phis=phis,
            )
        finally:
            if opened_here:
                try:
                    self.project.close()
                except Exception as exc:  # noqa: BLE001
                    self.warnings.append(
                        f"read-only close warning: {exc!r}"[:200]
                    )
                self.project = None

        points = evidence.pop("_points")
        pattern = evidence["pattern"]
        cuts = evidence["cuts"]
        after = evidence["AFTER_ACTIVATION"]

        # large arrays never travel inside a tool response: they land in an
        # artifact, and the response only carries the hash and a summary
        artifact = radiator.write_angular_artifact(
            out / "radiator_angular_grid.json",
            points,
            {
                "run_id": self.run_id,
                "point_id": point.get("point_id"),
                "tree_path": str(farfield_spec["tree_path"]),
                "farfield_name": str(farfield_spec["name"]),
                "monitor_frequency_ghz": monitor_ghz,
                "theta_count": len(thetas),
                "phi_count": len(phis),
                "components": list(radiator.FIELD_COMPONENTS_COMPLEX),
            },
        )

        self.observables["farfield_activation"] = {
            "farfield_name": evidence["farfield_name"],
            "tree_path": evidence["tree_path"],
            "activation": evidence["activation"],
            "sentinel_transition": evidence["sentinel_transition"],
            "BEFORE_ACTIVATION": evidence["BEFORE_ACTIVATION"]["values"],
            "AFTER_ACTIVATION": after["values"],
            "api_gap_note": after["api_gap_note"],
            "side_effects": evidence["side_effects"],
        }

        self.observables["radiator_efficiencies"] = {
            "GetMax": after["values"].get("GetMax"),
            "GetTRP": after["values"].get("GetTRP"),
            "GetRadiationEfficiency": after["values"].get(
                "GetRadiationEfficiency"
            ),
            "GetTotalEfficiency": after["values"].get("GetTotalEfficiency"),
            "source": (
                "native CST FarfieldPlot getters, read after SelectTreeItem "
                "activated the stored farfield"
            ),
        }

        self.observables["farfield_quantities"] = {
            label: {
                "plot_mode_literal": entry["plot_mode_literal"],
                "normalization": entry["normalization"],
                "linear_or_db": entry["linear_or_db"],
                "peak_linear": entry.get("peak_linear"),
                "peak_dB": entry.get("peak_dB"),
                "theta_peak": entry.get("theta_peak"),
                "phi_peak": entry.get("phi_peak"),
                "equivalent_peak_count": entry.get("equivalent_peak_count"),
                "ring_at_peak_theta": entry.get("ring_at_peak_theta"),
                "points_read": entry["points_read"],
                "read_error": entry["read_error"],
                "data_sha256": entry.get("data_sha256"),
            }
            for label, entry in evidence["quantities"].items()
        }

        self.observables["angular_readback"] = {
            "artifact": artifact,
            "grid": evidence["grid"],
            "evaluation_list": evidence["evaluation_list"],
            "field_ok": evidence["field"]["ok"],
            "field_points_read": evidence["field"]["points_read"],
            "field_error": evidence["field"]["error"],
            "components": evidence["field"]["components"],
            "derived": evidence["field"]["derived"],
            "beam_direction": pattern.get("beam_direction"),
            "PEAK_VALUE": pattern.get("PEAK_VALUE"),
            "PEAK_THETA": pattern.get("PEAK_THETA"),
            "PEAK_PHI": pattern.get("PEAK_PHI"),
            "HPBW_result": pattern.get("HPBW_result"),
            "principal_cut_sha256": cuts["principal"].get("raw_data_sha256"),
            "azimuth_cut_sha256": cuts["azimuth"].get("raw_data_sha256"),
            "sanity": evidence["sanity"],
            "native_hpbw_gap": after["api_gap_note"],
        }

        self.registry.register(
            artifact_id=self._artifact_id("farfield_angular"),
            artifact_type="RESULT",
            path=Path(artifact["path"]),
            producer="cst_ai_plugin.orchestrator._extract_radiator",
            valid_for_science=False,
            execution_status="COMPLETED",
            numerical_status="RESOLVED",
            scientific_status="TEST_ONLY",
            provenance_status="COMPLETE",
            extra={
                "sha256": artifact["sha256"],
                "point_count": artifact["point_count"],
            },
        )

        self.radiator_readback = {
            "evidence": evidence,
            "points": points,
            "cuts": cuts,
            "artifact": artifact,
        }
        self.spectra = spectra

        if spectra is not None:
            self.frequencies = spectra["frequencies_ghz"]

    def _stage_analyze(self, task: dict) -> None:
        """Dispatch ANALYZE to the analysis this fixture needs."""

        spec = task["spec"]
        mode = str(
            (self.current_point or {}).get("analyze_mode")
            or spec.get("analyze_mode")
            or "floquet_resonance"
        ).lower()

        if mode == "return_loss":
            return self._analyze_return_loss(task)

        if mode != "floquet_resonance":
            from .errors import TaskSchemaError

            raise TaskSchemaError(
                f"unknown analyze_mode {mode!r}; expected 'floquet_resonance' "
                "or 'return_loss'",
                stage="ANALYZE",
            )

        return self._analyze_floquet_resonance(task)

    def _analyze_return_loss(self, task: dict) -> None:
        """Resonance of a PORT reflectance dip - the radiator's own definition."""

        import v110c_analysis as extractor

        spectra = getattr(self, "spectra", None)

        if not spectra:
            raise StagePrerequisiteMissing(
                "ANALYZE needs the return-loss trace that EXTRACT reads, but this "
                "plan never produced it",
                stage="ANALYZE",
                detail={
                    "missing": ["self.spectra"],
                    "declared_stages": (self.plan or {}).get("stage_order"),
                    "required_stage": "EXTRACT",
                },
            )

        extraction = extractor.extract_resonance(
            spectra["frequencies_ghz"],
            spectra["return_loss_magnitude"],
            noise_band=None,
        )

        self.extraction = extraction

        valid = bool(extraction.get("FIXTURE_RESONANCE_VALID"))

        self.observables["resonance"] = {
            "TRACE": f"|{spectra['channel']}|",
            "FIXTURE_RESONANCE_VALID": valid,
            "RAW_GRID_F0": extraction.get("RAW_GRID_F0"),
            "INTERPOLATED_F0": extraction.get("INTERPOLATED_F0") if valid else None,
            "FWHM": extraction.get("FWHM") if valid else None,
            "Q_linewidth": extraction.get("Q_linewidth") if valid else None,
            "T_min": extraction.get("T_min"),
            "validity_failures": extraction.get("validity_failures"),
            "noise_band_argument": None,
            "noise_band_note": (
                "a port reflectance has no absorption-balance residual to bound "
                "the numerical noise, so no noise band is asserted.  The "
                "prominence criterion is reported but not thresholded, and that "
                "is recorded rather than silently defaulted."
            ),
            "metrics_invented_when_invalid": False,
        }

        if not valid:
            self.unresolved.append(
                "resonance metrics: "
                f"{extraction.get('validity_failures')}"
            )

        self.registry.register(
            artifact_id=self._artifact_id("extraction"),
            artifact_type="REPORT_JSON",
            path=None,
            producer="cst_ai_plugin.orchestrator._analyze_return_loss",
            valid_for_science=False,
            execution_status="COMPLETED",
            numerical_status="RESOLVED" if valid else "UNRESOLVED",
            scientific_status="TEST_ONLY",
            extra=extraction,
        )

    def _analyze_floquet_resonance(self, task: dict) -> None:
        import v110c_analysis as extractor

        # ANALYZE consumes what EXTRACT produced.  When a task declares ANALYZE
        # without EXTRACT (the planner records dependencies positionally, so it
        # does not catch this), V1.16 raised a bare AttributeError that the run
        # reported only as `UNEXPECTED`.  Name the missing producer instead.
        missing = [
            name for name in ("frequencies", "rta")
            if not hasattr(self, name)
        ]

        if missing:
            raise StagePrerequisiteMissing(
                "ANALYZE needs the output of EXTRACT, but this plan never ran "
                "it: " + ", ".join(f"self.{n} is unset" for n in missing),
                stage="ANALYZE",
                detail={
                    "missing": missing,
                    "declared_stages": (self.plan or {}).get("stage_order"),
                    "required_stage": "EXTRACT",
                },
            )

        extraction = extractor.extract_resonance(
            self.frequencies,
            self.rta["T_total"],
            noise_band=max(abs(v) for v in self.rta["A_balance"]),
        )

        self.extraction = extraction

        valid = bool(extraction.get("FIXTURE_RESONANCE_VALID"))

        self.observables["resonance"] = {
            "FIXTURE_RESONANCE_VALID": valid,
            "RAW_GRID_F0": extraction.get("RAW_GRID_F0"),
            "INTERPOLATED_F0": extraction.get("INTERPOLATED_F0") if valid else None,
            "FWHM": extraction.get("FWHM") if valid else None,
            "Q_linewidth": extraction.get("Q_linewidth") if valid else None,
            "T_min": extraction.get("T_min"),
            "validity_failures": extraction.get("validity_failures"),
            "metrics_invented_when_invalid": False,
        }

        if not valid:
            self.unresolved.append(
                "resonance metrics: "
                f"{extraction.get('validity_failures')}"
            )

        self.registry.register(
            artifact_id="extraction",
            artifact_type="REPORT_JSON",
            path=None,
            producer="cst_ai_plugin.orchestrator._stage_analyze",
            valid_for_science=False,
            execution_status="COMPLETED",
            numerical_status="RESOLVED" if valid else "UNRESOLVED",
            scientific_status="TEST_ONLY",
            extra=extraction,
        )

    def _stage_report(self, task: dict) -> None:
        out = Path(task["output_dir"])
        out.mkdir(parents=True, exist_ok=True)

        payload = {
            "run_id": self.run_id,
            "observables": self.observables,
            "semantic_readback": getattr(self, "verify_readback", None),
            "extraction": getattr(self, "extraction", None),
            "points_total": self.points_total,
            "points_completed": self.points_completed,
            "point_states": self.point_states,
        }

        json_path = out / "plugin_observables.json"
        json_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=repr) + "\n",
            encoding="utf-8",
        )

        self.registry.register(
            artifact_id=self._artifact_id("observables_report"),
            artifact_type="REPORT_JSON",
            path=json_path,
            producer="cst_ai_plugin.orchestrator._stage_report",
            valid_for_science=False,
            scientific_status="TEST_ONLY",
            provenance_status="COMPLETE",
        )

    # ---------------------------------------------------------------- finish
    def _finish(self, task: dict, *, blocked: bool = False,
                partial: bool = False) -> dict:
        out = Path(task["output_dir"])
        out.mkdir(parents=True, exist_ok=True)

        # the solved project could not be closed by us; report, do not hide it
        if getattr(self, "project", None) is not None:
            try:
                self.project.save()
                self.project.close()
            except Exception as exc:  # noqa: BLE001
                self.warnings.append(f"project close warning: {exc!r}"[:200])

        numerical = "NOT_COMPUTED"

        resonance = (self.observables.get("resonance") or {})

        if resonance:
            numerical = (
                "RESOLVED" if resonance.get("FIXTURE_RESONANCE_VALID")
                else "UNRESOLVED"
            )

        execution_status = (
            BLOCKED if blocked else (PARTIAL if partial else
                                     (COMPLETED if self.lifecycle == COMPLETED
                                      else FAILED))
        )

        # a sweep is only honest about its isolation if each point owns a
        # distinct on-disk project identity; say so explicitly rather than
        # leaving the reader to infer it from the point list
        point_paths = [
            s.get("project_path") for s in self.point_states
            if s.get("project_path")
        ]

        points_summary = {
            "points_total": self.points_total,
            "points_completed": self.points_completed,
            "point_states": self.point_states,
            "one_project_identity_per_point": (
                bool(point_paths) and len(set(point_paths)) == len(point_paths)
            ),
            "distinct_project_paths": len(set(point_paths)),
        }

        statuses = {
            "execution_status": execution_status,
            "numerical_status": numerical,
            "scientific_status": "TEST_ONLY",
            "provenance_status": "COMPLETE" if self.registry else "MISSING",
            "module_versions": MODULE_VERSIONS,
            "points": points_summary,
            "input_artifact_hashes": {
                a["artifact_id"]: a["hash"] for a in self.registry.all()
            },
            "runtime_identity": {"pid": self.pid, "plugin": self.plugin_version},
            "start_time": self.started_at,
        }

        bundle = make_bundle(
            run_id=self.run_id,
            task=task,
            plan=self.plan,
            preflight_result=self.preflight_result,
            plugin_lifecycle={"state": self.lifecycle,
                              "transitions": self.transitions,
                              "checkpoint": self.checkpoint},
            solver_lifecycle=self.solver_lifecycle,
            artifacts=self.registry.all(),
            observables=self.observables,
            solve_usage=self.solve_usage,
            warnings=self.warnings,
            unresolved=self.unresolved,
            errors=self.errors,
            statuses=statuses,
        )

        # a run that did not COMPLETE must never overwrite the bundle of one
        # that did; the stem carries the execution status in that case
        stem = (
            "result_bundle"
            if execution_status == COMPLETED
            else f"result_bundle_{execution_status.lower()}"
        )

        paths = write_bundle(bundle, out, stem=stem)

        self.registry.register(
            artifact_id="result_bundle",
            artifact_type="RESULT_BUNDLE",
            path=Path(paths["json"]),
            producer="cst_ai_plugin.orchestrator._finish",
            valid_for_science=False,
            scientific_status="TEST_ONLY",
            provenance_status="COMPLETE",
        )

        _append_ledger(
            {
                "record_type": "plugin_run",
                "ledger_schema_version": ORCHESTRATOR_VERSION,
                "tool_name": "cst_ai_plugin.orchestrator",
                "run_id": self.run_id,
                "timestamp_utc": utc_now(),
                "task_id": task["task_id"],
                "task_kind": task["task_kind"],
                "task_spec_hash": self.plan["task_spec_hash"],
                "execution_plan_hash": self.plan["execution_plan_hash"],
                "stage_transitions": self.transitions,
                "plugin_lifecycle": self.lifecycle,
                "checkpoint": self.checkpoint,
                "solver_lifecycle": self.solver_lifecycle,
                "solve_usage": self.solve_usage,
                "points": points_summary,
                "artifact_refs": [
                    {"artifact_id": a["artifact_id"], "hash": a["hash"],
                     "path": a["path"]}
                    for a in self.registry.all()
                ],
                "execution_status": execution_status,
                "numerical_status": numerical,
                "scientific_status": "TEST_ONLY",
                "valid_for_science": False,
            }
        )

        return {
            "run_id": self.run_id,
            "plugin_lifecycle": self.lifecycle,
            "checkpoint": self.checkpoint,
            "execution_status": execution_status,
            "numerical_status": numerical,
            "scientific_status": "TEST_ONLY",
            "solve_usage": self.solve_usage,
            "bundle": paths,
            "bundle_object": bundle,
            "artifacts": self.registry.to_dict(),
            "points": points_summary,
            "errors": self.errors,
            "warnings": self.warnings,
            "unresolved": self.unresolved,
        }
