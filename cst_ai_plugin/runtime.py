"""CST_AI plugin layer — the ONE CST runtime adapter.

Every other plugin module asks this file for a CST session.  Nothing else in the
plugin may call DesignEnvironment.connect or read the communication directory.

The actual discovery is V1.9A's verified implementation; this is an adapter, not
a second implementation.
"""

from __future__ import annotations

from pathlib import Path

from .errors import (
    CSTUnavailable,
    MultipleLiveDesignEnvironments,
    StaleRuntimePid,
)

RUNTIME_VERSION = "1.15.0"

RUNTIME_FILE = Path(__file__).resolve().parent.parent / "cst_runtime.json"


def _discovery():
    """Import V1.9A lazily so offline use never needs the CST python."""

    import sys

    root = str(Path(__file__).resolve().parent.parent)

    if root not in sys.path:
        sys.path.insert(0, root)

    import cst_session_discovery_v19a as disc

    return disc


def probe(runtime_file: Path | None = None, *, auto_update: bool = True) -> dict:
    """Resolve the live DesignEnvironment through the V1.9A mechanism."""

    disc = _discovery()
    path = Path(runtime_file) if runtime_file else RUNTIME_FILE

    resolution = disc.resolve_runtime(path, auto_update=auto_update)

    return {
        "runtime_version": RUNTIME_VERSION,
        "resolution": resolution,
        "status": resolution.get("status"),
        "pid": resolution.get("pid"),
        "runtime_file": str(path),
        "delegated_to": "cst_session_discovery_v19a.resolve_runtime",
        "second_discovery_implemented": False,
    }


def connect(runtime_file: Path | None = None):
    """Return (DesignEnvironment, pid) or raise a typed plugin error."""

    result = probe(runtime_file)
    status = result.get("status")

    disc = _discovery()

    if status == disc.STATUS_MULTIPLE:
        raise MultipleLiveDesignEnvironments(
            "multiple live DesignEnvironments; refusing to guess",
            stage="CONNECT",
            detail=result,
        )

    if status in (disc.STATUS_NO_LIVE, disc.STATUS_RUNTIME_MISSING):
        if status == disc.STATUS_RUNTIME_MISSING:
            raise StaleRuntimePid(
                "runtime file missing and no live DE recovered",
                stage="CONNECT",
                detail=result,
            )

        raise CSTUnavailable("no live DesignEnvironment", stage="CONNECT",
                             detail=result)

    if status not in (disc.STATUS_REUSED, disc.STATUS_RECOVERED):
        raise CSTUnavailable(
            f"unhandled runtime status {status!r}", stage="CONNECT", detail=result
        )

    import cst.interface as ci

    pid = int(result["pid"])
    de = ci.DesignEnvironment.connect(pid)

    if not de.is_connected():
        raise CSTUnavailable(
            f"connected object for pid {pid} reports not connected",
            stage="CONNECT",
            detail=result,
        )

    return de, pid, result


def session_safety(de) -> dict:
    """Report session state WITHOUT changing it."""

    return {
        "has_active_project": bool(de.has_active_project()),
        "active_project": de.active_project().filename()
        if de.has_active_project()
        else None,
        "policy": (
            "the plugin never closes a project it did not open; a task that "
            "needs a TEST project declares that in its plan"
        ),
        "mutated_session": False,
    }
