"""CST_AI plugin layer — error taxonomy.

A single RuntimeError for every failure destroys provenance.  These types keep
the failure class, the stage it happened in, and the original exception.

Nothing here contacts CST.
"""

from __future__ import annotations

from typing import Any


class PluginError(Exception):
    """Base class.  Carries a machine-readable code and the stage."""

    code = "PLUGIN_ERROR"

    def __init__(
        self,
        message: str,
        *,
        stage: str | None = None,
        detail: Any = None,
        cause: BaseException | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.stage = stage
        self.detail = detail
        self.cause = cause

    def to_dict(self) -> dict:
        return {
            "error_code": self.code,
            "message": self.message,
            "stage": self.stage,
            "detail": self.detail,
            "cause_type": type(self.cause).__name__ if self.cause else None,
            "cause_repr": repr(self.cause)[:400] if self.cause else None,
        }


# ---------------------------------------------------------------- runtime layer
class CSTUnavailable(PluginError):
    code = "CST_UNAVAILABLE"


class MultipleLiveDesignEnvironments(PluginError):
    code = "MULTIPLE_LIVE_DES"


class StaleRuntimePid(PluginError):
    code = "STALE_RUNTIME_PID"


# ------------------------------------------------------------------ build layer
class BuildFailed(PluginError):
    code = "BUILD_FAILED"


class SemanticReadbackFailed(PluginError):
    code = "SEMANTIC_READBACK_FAILED"


# ----------------------------------------------------------------- solver layer
class StartNotAcknowledged(PluginError):
    code = "START_NOT_ACKNOWLEDGED"


class SolverFailed(PluginError):
    code = "SOLVER_FAILED"


class SolverTimeout(PluginError):
    code = "SOLVER_TIMEOUT"


class SolveBudgetExceeded(PluginError):
    code = "SOLVE_BUDGET_EXCEEDED"


# ----------------------------------------------------------------- result layer
class ResultNotFound(PluginError):
    code = "RESULT_NOT_FOUND"


class ResultActivationFailed(PluginError):
    code = "RESULT_ACTIVATION_FAILED"


class ObservableUnresolved(PluginError):
    code = "OBSERVABLE_UNRESOLVED"


class ResonanceInvalid(PluginError):
    code = "RESONANCE_INVALID"


# ------------------------------------------------------------------ cache layer
class SignatureMismatch(PluginError):
    code = "SIGNATURE_MISMATCH"


class CacheInvalid(PluginError):
    code = "CACHE_INVALID"


# ---------------------------------------------------------------- compare layer
class SemanticComparisonMismatch(PluginError):
    code = "SEMANTIC_COMPARISON_MISMATCH"


# ----------------------------------------------------------------- circuit layer
class CapabilityMissing(PluginError):
    code = "CAPABILITY_MISSING"


class PreflightBlocked(PluginError):
    code = "PREFLIGHT_BLOCKED"


class TaskSchemaError(PluginError):
    code = "TASK_SCHEMA_ERROR"


class ObservableIdentityError(PluginError):
    code = "OBSERVABLE_IDENTITY_ERROR"


# ------------------------------------------------------- stage wiring (V1.17)
class StagePrerequisiteMissing(PluginError):
    """A stage ran without the output an earlier stage was supposed to produce.

    V1.16 let this surface as a raw AttributeError, which the orchestrator
    reported as `UNEXPECTED`.  A plan whose stages cannot feed each other is a
    task-authoring error, so it gets its own code and names the missing producer.
    """

    code = "STAGE_PREREQUISITE_MISSING"


class ExistingResultNotFound(PluginError):
    """A LOAD_EXISTING_RESULT task named a project that is not there.

    V1.16 declared LOAD_EXISTING_RESULT as a task kind but never wired a stage to
    resolve the existing project, so the kind could not execute at all.
    """

    code = "EXISTING_RESULT_NOT_FOUND"


#: every code the plugin can emit, for the error taxonomy report
ERROR_CODES = {
    cls.code: cls.__name__
    for cls in (
        PluginError,
        CSTUnavailable,
        MultipleLiveDesignEnvironments,
        StaleRuntimePid,
        BuildFailed,
        SemanticReadbackFailed,
        StartNotAcknowledged,
        SolverFailed,
        SolverTimeout,
        SolveBudgetExceeded,
        ResultNotFound,
        ResultActivationFailed,
        ObservableUnresolved,
        ResonanceInvalid,
        SignatureMismatch,
        CacheInvalid,
        SemanticComparisonMismatch,
        CapabilityMissing,
        PreflightBlocked,
        TaskSchemaError,
        ObservableIdentityError,
        StagePrerequisiteMissing,
        ExistingResultNotFound,
    )
}
