"""V1.9A single-threaded CST solver control plane.

Design goal
-----------
Replace the V1.9 execution shape

    worker thread -> blocking run_solver() -> thread completion

with a real control plane built on the officially documented non-blocking entry
point:

    PREPARE -> START -> POLL -> COMPLETE

The blocking ``run_solver()`` path is retained ONLY as an explicitly labelled
``LEGACY_BLOCKING_FALLBACK`` and is never selected silently.

Verified against the official Python API help page shipped with
CST Studio Suite 2026
(``Online Help/Python/source/cst.interface.html``, class ``Model3D``):

    start_solver(timeout: int = None)
        "Starts the currently selected solver asynchronously and gives back
         control to the calling script. It does not wait for the solver to
         finish, use in combination with is_solver_running()."
    run_solver(timeout: int = None)
        "Runs the currently selected solver and subsequent post-processing
         until finished. In case of an error a RunTimeError exception will be
         thrown."
    abort_solver(timeout: int = None)
        "Aborts the currently running (or paused) solver."
    is_solver_running(timeout: int = None)
        "Queries whether the solver is currently running."
    get_solver_run_info(timeout: int = None)
        "Retrieves as dict containing information on the last or current
         solver run."
    pause_solver / resume_solver(timeout: int = None)
        "Pause the currently running solver." / "Resume the currently paused
         solver."

The documented 11 Model3D methods are exactly: abort_solver, add_to_history,
full_history_rebuild, get_active_solver_name, get_solver_run_info,
get_tree_items, is_solver_running, pause_solver, resume_solver, run_solver,
start_solver.

Outcome vocabulary
------------------
    A solver never really started          -> START_NOT_ACKNOWLEDGED
    B solver is running                    -> RUNNING
    C started, stopped, succeeded          -> COMPLETED
    D started, stopped, failed             -> FAILED
    E aborted                              -> ABORTED
    F CST / project disappeared            -> CST_UNAVAILABLE
    G state unknown                        -> UNKNOWN

The terminal phase literals COMPLETED / FAILED / ABORTED / TIMEOUT / UNKNOWN are
shared with the solver lifecycle reader; the START_* / ABORT_* /
TIMEOUT_ABORT_UNCONFIRMED literals are specific to this control plane.

Stdlib only.  No CST import at module import time.
"""

from __future__ import annotations

import time
from typing import Any, Callable

CONTROL_VERSION = "1.9A.0"

# ----------------------------------------------------------- phase literals
PREPARE = "PREPARE"
START_REQUESTED = "START_REQUESTED"
START_ACKNOWLEDGED = "START_ACKNOWLEDGED"
START_NOT_ACKNOWLEDGED = "START_NOT_ACKNOWLEDGED"
RUNNING = "RUNNING"
COMPLETED = "COMPLETED"
FAILED = "FAILED"
ABORT_REQUESTED = "ABORT_REQUESTED"
ABORT_ACKNOWLEDGED = "ABORT_ACKNOWLEDGED"
ABORT_FAILED = "ABORT_FAILED"
ABORTED = "ABORTED"
TIMEOUT = "TIMEOUT"
TIMEOUT_ABORT_UNCONFIRMED = "TIMEOUT_ABORT_UNCONFIRMED"
CST_UNAVAILABLE = "CST_UNAVAILABLE"
UNKNOWN = "UNKNOWN"

CONTROL_PHASES = (
    PREPARE,
    START_REQUESTED,
    START_ACKNOWLEDGED,
    START_NOT_ACKNOWLEDGED,
    RUNNING,
    COMPLETED,
    FAILED,
    ABORT_REQUESTED,
    ABORT_ACKNOWLEDGED,
    ABORT_FAILED,
    ABORTED,
    TIMEOUT,
    TIMEOUT_ABORT_UNCONFIRMED,
    CST_UNAVAILABLE,
    UNKNOWN,
)

TERMINAL_PHASES = (
    START_NOT_ACKNOWLEDGED,
    COMPLETED,
    FAILED,
    ABORTED,
    TIMEOUT,
    TIMEOUT_ABORT_UNCONFIRMED,
    CST_UNAVAILABLE,
    UNKNOWN,
)

#: execution modes - the fallback must always be visible in the record
MODE_NONBLOCKING = "NONBLOCKING_START_SOLVER"
MODE_LEGACY_FALLBACK = "LEGACY_BLOCKING_FALLBACK"

# verdict literals reused from cst_model_executor_v17.py
VERDICT_PASSED = "PASSED"
VERDICT_FAILED = "FAILED"
VERDICT_BLOCKED = "BLOCKED"

#: the CST-native success literal, locked by the V1.6 ledger and V1.9
NATIVE_SUCCESS_STATE = "SUCCESS"

# abort outcome literals
ABORT_NOT_APPLICABLE = "ABORT_NOT_APPLICABLE"
ABORT_REQUESTED_LITERAL = ABORT_REQUESTED
ABORT_CONFIRMED = "ABORT_CONFIRMED"
ABORT_UNCONFIRMED = "ABORT_UNCONFIRMED"


class ControlPlaneError(RuntimeError):
    pass


class PreflightRefused(ControlPlaneError):
    """Nothing was dispatched."""


# --------------------------------------------------------------- backend


class SolverControlBackend:
    """Duck-typed backend contract.  Keeps CST objects out of upper layers."""

    #: MODE_NONBLOCKING or MODE_LEGACY_FALLBACK
    mode = MODE_NONBLOCKING

    def preflight(self) -> dict:
        raise NotImplementedError

    def request_start(self) -> Any:
        raise NotImplementedError

    def is_running(self) -> bool:
        raise NotImplementedError

    def native_run_info(self) -> dict:
        raise NotImplementedError

    def request_abort(self) -> Any:
        raise NotImplementedError

    def cst_alive(self) -> bool:
        raise NotImplementedError

    def project_accessible(self) -> bool:
        raise NotImplementedError

    def result_tree_snapshot(self) -> dict:
        raise NotImplementedError

    def message_tail(self, limit: int = 40) -> list:
        raise NotImplementedError

    def release(self) -> dict:
        return {"released": True}


# ------------------------------------------------------------ forensics


FORENSICS_FIELDS = (
    "run_id",
    "recorded_at_utc",
    "phase",
    "mode",
    "project_path",
    "project_hash",
    "cst_pid",
    "solver_family",
    "solver_config_hash",
    "last_lifecycle_state",
    "last_running_readback",
    "native_solver_state",
    "native_run_info",
    "exception",
    "solver_log_tail",
    "result_inventory_before",
    "result_inventory_after",
    "cst_connection_healthy",
    "project_accessible",
    "evidence_conflicts",
    "lifecycle_timeline",
)


def build_forensics(record: dict) -> dict:
    """Standardised, replayable forensic record for any abnormal termination."""

    evidence = record.get("completion_evidence") or {}

    out = {
        "run_id": record.get("run_id"),
        "recorded_at_utc": record.get("finished_at_utc"),
        "phase": record.get("phase"),
        "mode": record.get("mode"),
        "project_path": record.get("project_path"),
        "project_hash": record.get("project_hash"),
        "cst_pid": record.get("cst_pid"),
        "solver_family": record.get("solver_family"),
        "solver_config_hash": record.get("solver_config_hash"),
        "last_lifecycle_state": record.get("phase"),
        "last_running_readback": evidence.get("last_running_readback"),
        "native_solver_state": evidence.get("native_state"),
        "native_run_info": evidence.get("native_run_info"),
        "exception": record.get("exception"),
        "solver_log_tail": record.get("message_tail"),
        "result_inventory_before": evidence.get("result_tree_before"),
        "result_inventory_after": evidence.get("result_tree_after"),
        "cst_connection_healthy": record.get("cst_alive_after"),
        "project_accessible": record.get("project_accessible_after"),
        "evidence_conflicts": evidence.get("conflicts"),
        "lifecycle_timeline": record.get("timeline"),
    }

    missing = [key for key in FORENSICS_FIELDS if key not in out]

    out["schema_complete"] = not missing
    out["missing_fields"] = missing

    return out


# --------------------------------------------------------- control plane


class SolverControlPlane:
    """Single-threaded solver control plane.

    One instance owns exactly one run identity.  ``start`` may be called at
    most once per instance; the control plane refuses every further start
    request, including after a terminal phase (that would be a NEW solve
    request and must not be auto-executed).
    """

    def __init__(
        self,
        backend: SolverControlBackend,
        *,
        run_id: str,
        start_ack_window_seconds: float = 60.0,
        running_timeout_seconds: float = 600.0,
        poll_interval_seconds: float = 1.0,
        abort_confirm_seconds: float = 30.0,
        max_consecutive_ipc_failures: int = 3,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if start_ack_window_seconds <= 0:
            raise ControlPlaneError("start_ack_window_seconds must be > 0")

        if running_timeout_seconds <= 0:
            raise ControlPlaneError("running_timeout_seconds must be > 0")

        self.backend = backend
        self.run_id = run_id
        self.start_ack_window_seconds = float(start_ack_window_seconds)
        self.running_timeout_seconds = float(running_timeout_seconds)
        self.poll_interval_seconds = float(poll_interval_seconds)
        self.abort_confirm_seconds = float(abort_confirm_seconds)
        self.max_consecutive_ipc_failures = int(max_consecutive_ipc_failures)
        self.clock = clock
        self.sleep = sleep

        self._start_requests = 0
        self._phase = PREPARE
        self._running_seen = False
        self._running_first_at: float | None = None
        self._running_last_at: float | None = None
        self._running_observations = 0
        self._consecutive_ipc_failures = 0
        self._t0 = self.clock()
        self._timeline: list[dict] = []
        self._start_requested_at: float | None = None
        self._abort_state: dict | None = None
        self._conflicts: list[dict] = []

    # ------------------------------------------------------------ helpers

    def _now(self) -> float:
        return self.clock() - self._t0

    def _transition(self, phase: str, **detail) -> None:
        self._phase = phase

        entry = {"phase": phase, "at_seconds": round(self._now(), 6)}
        entry.update(detail)

        self._timeline.append(entry)

    @property
    def phase(self) -> str:
        return self._phase

    @property
    def start_requests(self) -> int:
        return self._start_requests

    # ------------------------------------------------------------ PREPARE

    def prepare(self) -> dict:
        """PREPARE: never dispatch on an unusable project."""

        record: dict[str, Any] = {
            "control_version": CONTROL_VERSION,
            "run_id": self.run_id,
            "mode": self.backend.mode,
            "phases": list(CONTROL_PHASES),
            "terminal_phases": list(TERMINAL_PHASES),
            "preflight": None,
            "start_request": None,
            "completion_evidence": None,
            "abort": None,
            "exception": None,
            "cst_alive_after": None,
            "project_accessible_after": None,
            "message_tail": None,
        }

        self._transition(PREPARE)

        try:
            preflight = self.backend.preflight()
        except Exception as exc:  # noqa: BLE001
            self._transition(FAILED, reason="PREFLIGHT_REFUSED", error=repr(exc))
            record["preflight"] = {"ok": False, "error": repr(exc)}
            record["phase"] = FAILED
            record["reason"] = "PREFLIGHT_REFUSED"
            record["verdict"] = VERDICT_FAILED
            record["timeline"] = list(self._timeline)
            record["exception"] = repr(exc)
            return record

        record["preflight"] = preflight

        for key in (
            "project_path",
            "project_hash",
            "cst_pid",
            "solver_family",
            "solver_config_hash",
        ):
            if key in preflight:
                record[key] = preflight[key]

        if not preflight.get("ok", True):
            self._transition(FAILED, reason="PREFLIGHT_NOT_OK", dispatched=False)
            record["phase"] = FAILED
            record["reason"] = "PREFLIGHT_NOT_OK"
            record["verdict"] = VERDICT_FAILED
            record["timeline"] = list(self._timeline)
            return record

        # a live solve already in progress must never be adopted
        if preflight.get("already_running"):
            self._transition(
                FAILED, reason="PREFLIGHT_ALREADY_RUNNING", dispatched=False
            )
            record["phase"] = FAILED
            record["reason"] = "PREFLIGHT_ALREADY_RUNNING"
            record["verdict"] = VERDICT_FAILED
            record["timeline"] = list(self._timeline)
            return record

        record["phase"] = PREPARE
        record["reason"] = None
        record["verdict"] = None
        record["timeline"] = list(self._timeline)

        return record

    # -------------------------------------------------------------- START

    def start(self) -> dict:
        """START: one non-blocking start_solver() per run identity, at most."""

        if self._start_requests >= 1:
            self._transition(
                FAILED,
                reason="SECOND_START_REFUSED",
                start_requests=self._start_requests,
            )
            return {
                "ok": False,
                "reason": "SECOND_START_REFUSED",
                "start_requests": self._start_requests,
                "note": (
                    "one run identity corresponds to at most one start "
                    "request; a further start would be a NEW solve request"
                ),
            }

        self._start_requests += 1
        self._start_requested_at = self._now()

        self._transition(START_REQUESTED, start_requests=self._start_requests)

        try:
            returned = self.backend.request_start()
        except Exception as exc:  # noqa: BLE001
            self._transition(
                START_NOT_ACKNOWLEDGED,
                reason="START_CALL_RAISED",
                error=repr(exc),
            )
            return {
                "ok": False,
                "reason": "START_CALL_RAISED",
                "exception": repr(exc),
                "start_requests": self._start_requests,
            }

        return {
            "ok": True,
            "reason": None,
            "return_value": repr(returned),
            "mode": self.backend.mode,
            "start_requests": self._start_requests,
            "requested_at_seconds": round(self._start_requested_at, 6),
        }

    # --------------------------------------------------------------- POLL

    def poll(self) -> dict:
        """One authoritative poll step.

        Returns the observation and, when a terminal phase is reached, the
        completion evidence.
        """

        observation: dict[str, Any] = {
            "at_seconds": round(self._now(), 6),
            "phase_before": self._phase,
            "running": None,
            "running_error": None,
            "native_state": None,
            "ipc_ok": True,
        }

        try:
            running = bool(self.backend.is_running())
            self._consecutive_ipc_failures = 0
        except Exception as exc:  # noqa: BLE001
            self._consecutive_ipc_failures += 1
            observation["ipc_ok"] = False
            observation["running_error"] = repr(exc)
            observation["consecutive_ipc_failures"] = (
                self._consecutive_ipc_failures
            )

            if (
                self._consecutive_ipc_failures
                >= self.max_consecutive_ipc_failures
            ):
                self._transition(
                    CST_UNAVAILABLE,
                    reason="IPC_FAILURE_LIMIT_REACHED",
                    failures=self._consecutive_ipc_failures,
                )
                observation["phase_after"] = self._phase
                observation["terminal"] = True
            else:
                observation["phase_after"] = self._phase
                observation["terminal"] = False

            return observation

        observation["running"] = running

        if running:
            self._running_seen = True
            self._running_observations += 1
            self._running_last_at = self._now()

            if self._running_first_at is None:
                self._running_first_at = self._now()

                if self._phase == START_REQUESTED:
                    self._transition(
                        START_ACKNOWLEDGED,
                        ack_after_seconds=round(
                            self._running_first_at - (self._start_requested_at or 0),
                            6,
                        ),
                    )

            if self._phase != RUNNING:
                self._transition(RUNNING)

            observation["phase_after"] = self._phase
            observation["terminal"] = False
            observation["running_first_at"] = round(self._running_first_at, 6)

            return observation

        # ---- not running
        if self._running_seen:
            native = self._read_native(observation)

            state = native.get("state") if isinstance(native, dict) else None

            observation["native_state"] = state

            if str(state) == NATIVE_SUCCESS_STATE:
                self._transition(COMPLETED, native_state=state)
            else:
                self._transition(FAILED, native_state=state)

            observation["phase_after"] = self._phase
            observation["terminal"] = True

            return observation

        # never ran yet: is the acknowledgement window still open?
        waited = self._now() - (self._start_requested_at or 0.0)

        if waited > self.start_ack_window_seconds:
            self._transition(
                START_NOT_ACKNOWLEDGED,
                waited_seconds=round(waited, 6),
                ack_window_seconds=self.start_ack_window_seconds,
            )
            observation["phase_after"] = self._phase
            observation["terminal"] = True
            return observation

        observation["phase_after"] = self._phase
        observation["terminal"] = False
        observation["ack_window_remaining"] = round(
            self.start_ack_window_seconds - waited, 6
        )

        return observation

    def _read_native(self, observation: dict) -> dict:
        try:
            native = self.backend.native_run_info()
        except Exception as exc:  # noqa: BLE001
            observation["native_error"] = repr(exc)
            return {}

        if isinstance(native, dict):
            return native

        observation["native_error"] = "non-dict run info"
        return {}

    # --------------------------------------------------------------- WAIT

    def wait(self, max_seconds: float | None = None) -> dict:
        """POLL until a terminal phase, applying RUNNING-based timeout.

        The timeout clock starts when RUNNING is first observed, NOT when the
        script started - a slow mesh/port preparation must not eat the solver
        budget, and a never-started solver must not be reported as a timeout.
        """

        polls: list[dict] = []

        while self._phase not in TERMINAL_PHASES:
            observation = self.poll()
            polls.append(observation)

            if observation.get("terminal"):
                break

            # timeout is measured on RUNNING wall-clock
            if self._running_first_at is not None:
                running_elapsed = self._now() - self._running_first_at

                if running_elapsed > self.running_timeout_seconds:
                    self._transition(
                        TIMEOUT,
                        running_elapsed_seconds=round(running_elapsed, 6),
                        running_timeout_seconds=self.running_timeout_seconds,
                    )
                    break

            if max_seconds is not None and self._now() > max_seconds:
                self._transition(
                    UNKNOWN,
                    reason="WAIT_WALL_CLOCK_EXCEEDED",
                    waited_seconds=round(self._now(), 6),
                )
                break

            self.sleep(self.poll_interval_seconds)

        return {"polls": polls, "poll_count": len(polls), "phase": self._phase}

    # -------------------------------------------------------------- ABORT

    def abort(self) -> dict:
        """ABORT: only ever requested from a state where a solver may run."""

        outcome: dict[str, Any] = {
            "requested": False,
            "called": False,
            "outcome": None,
            "confirmed_stopped": False,
            "confirm_polls": 0,
            "cst_alive": None,
            "project_accessible": None,
            "kill_used": False,
            "note": "the CST process is never killed",
        }

        # refuse to abort from states where nothing can be running
        if self._phase in (
            PREPARE,
            COMPLETED,
            FAILED,
            START_NOT_ACKNOWLEDGED,
            ABORTED,
            TIMEOUT_ABORT_UNCONFIRMED,
            CST_UNAVAILABLE,
            UNKNOWN,
        ):
            outcome["outcome"] = ABORT_NOT_APPLICABLE
            outcome["phase_at_request"] = self._phase
            self._abort_state = outcome
            return outcome

        # probe first: abort is only issued against a live solver
        try:
            running = bool(self.backend.is_running())
        except Exception as exc:  # noqa: BLE001
            outcome["outcome"] = ABORT_UNCONFIRMED
            outcome["phase_at_request"] = self._phase
            outcome["probe_error"] = repr(exc)
            self._abort_state = outcome
            return outcome

        if not running:
            # it stopped between polls: the native state decides, not abort
            outcome["outcome"] = ABORT_NOT_APPLICABLE
            outcome["phase_at_request"] = self._phase
            outcome["note"] = "solver already stopped when abort was requested"
            self._abort_state = outcome
            return outcome

        outcome["requested"] = True
        outcome["phase_at_request"] = self._phase

        self._transition(ABORT_REQUESTED)

        try:
            outcome["called"] = True
            outcome["return_value"] = repr(self.backend.request_abort())
        except Exception as exc:  # noqa: BLE001
            outcome["call_error"] = repr(exc)

        deadline = self.clock() + self.abort_confirm_seconds

        while self.clock() < deadline:
            outcome["confirm_polls"] += 1

            try:
                still = bool(self.backend.is_running())
            except Exception as exc:  # noqa: BLE001
                outcome["confirm_error"] = repr(exc)
                break

            if not still:
                outcome["confirmed_stopped"] = True
                break

            self.sleep(self.poll_interval_seconds)

        if outcome["confirmed_stopped"]:
            outcome["outcome"] = ABORT_CONFIRMED
            self._transition(ABORT_ACKNOWLEDGED, confirmed_stopped=True)
            self._transition(ABORTED, native="abort_confirmed")
        else:
            outcome["outcome"] = ABORT_UNCONFIRMED
            self._transition(ABORT_FAILED, confirmed_stopped=False)

        try:
            outcome["cst_alive"] = bool(self.backend.cst_alive())
        except Exception as exc:  # noqa: BLE001
            outcome["cst_alive_error"] = repr(exc)

        try:
            outcome["project_accessible"] = bool(
                self.backend.project_accessible()
            )
        except Exception as exc:  # noqa: BLE001
            outcome["project_accessible_error"] = repr(exc)

        self._abort_state = outcome

        return outcome

    # ---------------------------------------------------------- COMPLETE

    def completion_evidence(self, *, result_tree_before: dict | None = None) -> dict:
        """Assemble the multi-source completion evidence (never a single bool)."""

        native: dict = {}
        native_error = None

        try:
            value = self.backend.native_run_info()
            native = value if isinstance(value, dict) else {}
        except Exception as exc:  # noqa: BLE001
            native_error = repr(exc)

        state = native.get("state")

        try:
            tree_after = self.backend.result_tree_snapshot()
        except Exception as exc:  # noqa: BLE001
            tree_after = {"error": repr(exc)}

        try:
            messages = self.backend.message_tail()
        except Exception as exc:  # noqa: BLE001
            messages = [{"error": repr(exc)}]

        conflicts: list[dict] = list(self._conflicts)

        native_ok = str(state) == NATIVE_SUCCESS_STATE

        if native_ok and not self._running_seen:
            conflicts.append(
                {
                    "kind": "NATIVE_SUCCESS_WITHOUT_RUNNING_OBSERVATION",
                    "detail": (
                        "native state is SUCCESS but the control plane never "
                        "observed is_solver_running() == True"
                    ),
                }
            )

        if self._phase == COMPLETED and not native_ok:
            conflicts.append(
                {
                    "kind": "COMPLETED_WITHOUT_NATIVE_SUCCESS",
                    "detail": f"phase COMPLETED but native state is {state!r}",
                }
            )

        before_paths = set((result_tree_before or {}).get("paths") or [])
        after_paths = set(tree_after.get("paths") or [])

        evidence = {
            "running_seen": self._running_seen,
            "running_observations": self._running_observations,
            "running_first_at_seconds": (
                round(self._running_first_at, 6)
                if self._running_first_at is not None
                else None
            ),
            "running_last_at_seconds": (
                round(self._running_last_at, 6)
                if self._running_last_at is not None
                else None
            ),
            "last_running_readback": (
                self._running_last_at is not None
            ),
            "native_state": state,
            "native_run_info": native,
            "native_error": native_error,
            "result_tree_before": result_tree_before,
            "result_tree_after": tree_after,
            "result_tree_delta": {
                "before_count": (result_tree_before or {}).get("tree_item_count"),
                "after_count": tree_after.get("tree_item_count"),
                "new_paths": sorted(after_paths - before_paths),
            },
            "message_tail": messages,
            "conflicts": conflicts,
            "evidence_class": (
                "COMPLETION_CONFIRMED"
                if (native_ok and self._running_seen)
                else (
                    "COMPLETION_NATIVE_ONLY"
                    if native_ok
                    else "COMPLETION_NOT_CONFIRMED"
                )
            ),
        }

        return evidence

    def finalize(self, record: dict, evidence: dict) -> dict:
        """Attach terminal bookkeeping to a record."""

        record["completion_evidence"] = evidence
        record["phase"] = self._phase
        record["timeline"] = list(self._timeline)
        record["start_requests"] = self._start_requests
        record["abort"] = self._abort_state

        try:
            record["cst_alive_after"] = bool(self.backend.cst_alive())
        except Exception as exc:  # noqa: BLE001
            record["cst_alive_after"] = None
            record["cst_alive_error"] = repr(exc)

        try:
            record["project_accessible_after"] = bool(
                self.backend.project_accessible()
            )
        except Exception as exc:  # noqa: BLE001
            record["project_accessible_after"] = None
            record["project_accessible_error"] = repr(exc)

        try:
            record["message_tail"] = self.backend.message_tail()
        except Exception as exc:  # noqa: BLE001
            record["message_tail"] = [{"error": repr(exc)}]

        record["finished_at_utc"] = _utc_now()
        record["release"] = _safe(self.backend.release)

        if self._phase == COMPLETED:
            record["reason"] = "NATIVE_STATE:" + str(
                (evidence or {}).get("native_state")
            )
            record["verdict"] = VERDICT_PASSED
        elif self._phase == TIMEOUT:
            record["reason"] = "RUNNING_TIMEOUT"
            record["verdict"] = VERDICT_FAILED
        elif self._phase == TIMEOUT_ABORT_UNCONFIRMED:
            record["reason"] = "TIMEOUT_ABORT_UNCONFIRMED"
            record["verdict"] = VERDICT_FAILED
        else:
            record["reason"] = record.get("reason") or self._phase
            record["verdict"] = VERDICT_FAILED

        if self._phase in TERMINAL_PHASES and self._phase != COMPLETED:
            record["forensics"] = build_forensics(record)

        return record

    # ------------------------------------------------------ full pipeline

    def execute(
        self,
        *,
        wait_max_seconds: float | None = None,
    ) -> dict:
        """PREPARE -> START -> (POLL until terminal) -> evidence -> finalize."""

        record = self.prepare()

        if record.get("phase") != PREPARE:
            return record

        try:
            before = self.backend.result_tree_snapshot()
        except Exception as exc:  # noqa: BLE001
            before = {"error": repr(exc)}

        record["result_tree_before"] = before

        start = self.start()

        record["start_request"] = start

        if not start.get("ok"):
            evidence = self.completion_evidence(result_tree_before=before)
            record["exception"] = start.get("exception")
            return self.finalize(record, evidence)

        self.wait(max_seconds=wait_max_seconds)

        # timeout handling: record -> abort -> confirm -> health checks
        if self._phase == TIMEOUT:
            abort = self.abort()

            if abort.get("outcome") == ABORT_CONFIRMED:
                self._phase = TIMEOUT
            else:
                self._transition(
                    TIMEOUT_ABORT_UNCONFIRMED,
                    abort_outcome=abort.get("outcome"),
                )

        evidence = self.completion_evidence(result_tree_before=before)

        return self.finalize(record, evidence)


def _safe(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        return {"error": repr(exc)}


def _utc_now() -> str:
    from datetime import datetime, timezone

    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


# ------------------------------------------------------- CST-bound backends


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value

    if isinstance(value, complex):
        return {"real": value.real, "imag": value.imag}

    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}

    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]

    return repr(value)


def probe_control_methods(model3d) -> dict:
    """Run-time presence probe for the official non-blocking entry points.

    cst.interface.Model3D is a dynamic COM proxy: its static dir() exposes only
    allow_history_commands / disallow_history_commands, so existence can only be
    established against a live model.  Absence is reported, never worked around
    silently.
    """

    names = (
        "start_solver",
        "abort_solver",
        "is_solver_running",
        "get_solver_run_info",
        "pause_solver",
        "resume_solver",
        "run_solver",
        "get_active_solver_name",
        "get_tree_items",
    )

    present: dict[str, bool] = {}

    for name in names:
        try:
            member = getattr(model3d, name, None)
        except Exception:  # noqa: BLE001
            present[name] = False
            continue

        present[name] = member is not None

    return {
        "present": present,
        "nonblocking_control_available": bool(
            present.get("start_solver")
            and present.get("is_solver_running")
            and present.get("abort_solver")
        ),
        "evidence": "RUNTIME_PROBE_ON_LIVE_MODEL3D",
    }


class CstControlBackend(SolverControlBackend):
    """Non-blocking control backend (the default and intended path)."""

    mode = MODE_NONBLOCKING

    def __init__(
        self,
        *,
        design_environment,
        project,
        expected_project_path: str,
        expected_pid: int,
        method_probe: dict | None = None,
    ) -> None:
        self.de = design_environment
        self.project = project
        self.model3d = project.model3d
        self.expected_project_path = str(expected_project_path)
        self.expected_pid = int(expected_pid)
        self.method_probe = method_probe or probe_control_methods(self.model3d)
        self.started = False

    # ---------------------------------------------------------- PREPARE

    def preflight(self) -> dict:
        present = self.method_probe.get("present", {})

        if not present.get("start_solver"):
            raise PreflightRefused(
                "start_solver is not present on this CST build; the "
                "non-blocking control plane cannot run and the legacy blocking "
                "fallback must be selected explicitly"
            )

        if not present.get("abort_solver"):
            raise PreflightRefused("abort_solver is not present on this build")

        if not self.de.is_connected():
            raise PreflightRefused("CST DesignEnvironment is not connected")

        live_pid = int(self.de.pid())

        if live_pid != self.expected_pid:
            raise PreflightRefused(
                f"CST PID changed: expected {self.expected_pid}, got {live_pid}"
            )

        if not self.de.has_active_project():
            raise PreflightRefused("CST has no active project")

        actual = str(self.project.filename())

        if actual != self.expected_project_path:
            raise PreflightRefused(
                "active project does not match the control lock: "
                f"expected {self.expected_project_path}, got {actual}"
            )

        fmin = float(self.model3d.Solver.GetFmin())
        fmax = float(self.model3d.Solver.GetFmax())
        ports = int(self.model3d.Solver.GetNumberOfPorts())
        running = bool(self.model3d.is_solver_running())

        if fmin <= 0.0:
            raise PreflightRefused(f"solver fmin must be > 0 (got {fmin})")

        if fmax <= fmin:
            raise PreflightRefused(f"invalid frequency range ({fmin} .. {fmax})")

        if ports <= 0:
            raise PreflightRefused("project contains no solver ports")

        import hashlib

        digest = hashlib.sha256()

        with open(self.expected_project_path, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)

        project_hash = "sha256:" + digest.hexdigest()

        solver_family = str(self.model3d.get_active_solver_name())

        config = {
            "solver_family": solver_family,
            "fmin": fmin,
            "fmax": fmax,
            "port_count": ports,
        }

        return {
            "ok": not running,
            "already_running": running,
            "project_path": actual,
            "project_hash": project_hash,
            "cst_pid": live_pid,
            "solver_family": solver_family,
            "solver_config_hash": hashlib.sha256(
                repr(sorted(config.items())).encode("utf-8")
            ).hexdigest(),
            "fmin": fmin,
            "fmax": fmax,
            "port_count": ports,
            "project_type": str(self.project.project_type()),
            "run_info_before": _json_safe(self.model3d.get_solver_run_info()),
            "method_probe": self.method_probe.get("present"),
        }

    # --------------------------------------------------------- control ops

    def request_start(self) -> Any:
        self.started = True
        return _json_safe(self.model3d.start_solver())

    def is_running(self) -> bool:
        return bool(self.model3d.is_solver_running())

    def native_run_info(self) -> dict:
        value = self.model3d.get_solver_run_info()
        return _json_safe(value) if isinstance(value, dict) else {"raw": repr(value)}

    def request_abort(self) -> Any:
        return _json_safe(self.model3d.abort_solver())

    # --------------------------------------------------------- health ops

    def cst_alive(self) -> bool:
        return bool(self.de.is_connected()) and bool(self.de.has_active_project())

    def project_accessible(self) -> bool:
        try:
            return bool(str(self.project.filename()))
        except Exception:  # noqa: BLE001
            return False

    def result_tree_snapshot(self) -> dict:
        paths = [str(item) for item in self.model3d.get_tree_items()]

        return {
            "tree_item_count": len(paths),
            "paths": paths,
            "result_like_paths": [
                path
                for path in paths
                if path.startswith("1D Results") or path.startswith("2D/3D Results")
            ],
        }

    def message_tail(self, limit: int = 40) -> list:
        messages = [str(item) for item in self.project.get_messages()]
        return messages[-limit:]

    def release(self) -> dict:
        return {"released": True, "started": self.started}


class CstLegacyBlockingBackend(CstControlBackend):
    """EXPLICIT legacy fallback: blocking run_solver() on a worker thread.

    This class exists only so that the old execution shape remains available
    where the non-blocking entry point is unusable.  It must be selected
    explicitly by the caller; the control plane never switches to it silently,
    and every record it produces carries MODE_LEGACY_FALLBACK.
    """

    mode = MODE_LEGACY_FALLBACK

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)

        import threading

        self._thread = None
        self._exception: list[BaseException] = []
        self._return: list[Any] = []

    def preflight(self) -> dict:
        result = super().preflight()
        result["fallback_warning"] = (
            "LEGACY_BLOCKING_FALLBACK: run_solver() runs on a worker thread; "
            "abort cannot be issued while the blocking call holds the proxy"
        )
        return result

    def request_start(self) -> Any:
        import threading

        self.started = True

        def _run() -> None:
            try:
                self._return.append(self.model3d.run_solver())
            except BaseException as exc:  # noqa: BLE001
                self._exception.append(exc)

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()

        return "LEGACY_BLOCKING_FALLBACK_DISPATCHED"

    def is_running(self) -> bool:
        return bool(self.model3d.is_solver_running())

    def release(self) -> dict:
        alive = bool(self._thread is not None and self._thread.is_alive())

        return {
            "released": True,
            "mode": self.mode,
            "worker_thread_still_alive": alive,
            "worker_exception": repr(self._exception[0])
            if self._exception
            else None,
        }

