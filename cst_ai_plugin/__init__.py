"""CST_AI plugin layer.

A thin orchestration shell over modules that were already verified against real
CST.  It contains no physics, no CST wrapper of its own, and no document identity.

    runtime      -> the ONE CST session adapter (delegates to V1.9A discovery)
    capabilities -> what the plugin can actually do, and where it is implemented
    task         -> declarative TaskSpec with a mandatory explicit solve_policy
    planner      -> TASK -> EXECUTION PLAN (never executes)
    preflight    -> READY / BLOCKED / UNDECIDED before anything expensive
    artifacts    -> run-scoped artifact registry (references, never rewrites)
    observables  -> observable identity + routing (no mathematics)
    bundle       -> unified ResultBundle (JSON + Markdown), four status layers
    orchestrator -> PLAN / RUN / STATUS / RESUME / INSPECT

Application-specific workflows live OUTSIDE this package.  The core must not
depend on them.
"""

from .artifacts import ArtifactRegistry
from .bundle import make_bundle, write_bundle
from .capabilities import CAPABILITIES, get as get_capability
from .errors import ERROR_CODES, PluginError
from .orchestrator import (
    ANALYSIS_COMPLETE_REPORT_PENDING,
    BLOCKED,
    BUILD_COMPLETE,
    COMPLETE,
    COMPLETED,
    CREATED,
    FAILED,
    PARTIAL,
    PLANNED,
    PLUGIN_LIFECYCLE,
    PREFLIGHT_PASSED,
    RUNNING,
    SOLVE_COMPLETE_ANALYSIS_PENDING,
    Orchestrator,
)
from .planner import plan_task
from .preflight import preflight
from .task import SOLVE_POLICIES, STAGES, authorized_solve_budget, validate_task

PLUGIN_VERSION = "1.15.0"

__all__ = [
    "ArtifactRegistry",
    "Orchestrator",
    "PLUGIN_VERSION",
    "PLUGIN_LIFECYCLE",
    "SOLVE_POLICIES",
    "STAGES",
    "CAPABILITIES",
    "ERROR_CODES",
    "PluginError",
    "authorized_solve_budget",
    "get_capability",
    "make_bundle",
    "plan_task",
    "preflight",
    "validate_task",
    "write_bundle",
    "CREATED",
    "PLANNED",
    "PREFLIGHT_PASSED",
    "RUNNING",
    "COMPLETED",
    "BLOCKED",
    "FAILED",
    "PARTIAL",
    "BUILD_COMPLETE",
    "SOLVE_COMPLETE_ANALYSIS_PENDING",
    "ANALYSIS_COMPLETE_REPORT_PENDING",
    "COMPLETE",
]
