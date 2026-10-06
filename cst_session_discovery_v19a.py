"""V1.9A reusable CST session discovery and runtime stale-PID recovery.

Why this module exists
----------------------
Plain process enumeration is UNRELIABLE on this machine and must never be used
to decide whether CST is running:

  * ``Get-Process`` finds no explorer.exe / svchost.exe / csrss.exe / System
  * ``psapi.EnumProcesses`` returned 25 pids while a live CST Design
    Environment (pid 41896) was demonstrably running - ``OpenProcess(41896)``
    succeeded immediately afterwards
  * ``tasklist`` -> Access denied; ``Get-CimInstance Win32_Process`` -> HRESULT
    0x80041003

The route proven in V1.9 is therefore the only supported one:

    %LOCALAPPDATA%\\CST AG\\<version>\\Communication\\<pid>
        -> file name IS the Design Environment PID
        -> file content IS its TCP endpoint ("tcp:host=127.0.0.1,port=NNNN")
    -> OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION) + QueryFullProcessImageNameW
        -> image must be CST DESIGN ENVIRONMENT_AMD64.exe
    -> TCP connect to the recorded endpoint
    -> cst.interface.DesignEnvironment.connect(pid)

Stale communication entries are tolerated: they are reported, never deleted,
and are not a precondition for discovery.

Run with any Python (cst.interface is imported lazily inside the functions that
need it, so the pure-file part is testable without CST).
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import json
import os
import socket
from pathlib import Path
from typing import Any

VERSION = "1.9A.0"

CST_IMAGE_NAME = "CST DESIGN ENVIRONMENT_AMD64.exe"

#: the process image suffix that identifies a CST Studio Suite front end whose
#: windows belong to the same product family (diagnostics only, never a
#: substitute for the DesignEnvironment image check)
CST_FAMILY_HINTS = ("CST", "SchematicEditor_AMD64")

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
MAX_PATH = 32768

# --------------------------------------------------------------- literals
STATUS_REUSED = "REUSED_FROM_RUNTIME"
STATUS_RECOVERED = "RECOVERED_FROM_DISCOVERY"
STATUS_NO_LIVE = "NO_LIVE_DESIGN_ENVIRONMENT"
STATUS_MULTIPLE = "MULTIPLE_LIVE_DESIGN_ENVIRONMENTS"
STATUS_RUNTIME_MISSING = "RUNTIME_FILE_MISSING"

ENTRY_LIVE = "LIVE"
ENTRY_STALE = "STALE"
ENTRY_NOT_CST_IMAGE = "NOT_CST_IMAGE"
ENTRY_IMAGE_UNREADABLE = "IMAGE_UNREADABLE"
ENTRY_TCP_UNREACHABLE = "TCP_UNREACHABLE"
ENTRY_ATTACH_FAILED = "ATTACH_FAILED"
ENTRY_ATTACH_OK = "ATTACH_OK"


class DiscoveryError(RuntimeError):
    pass


# ------------------------------------------------------------- pure helpers


def communication_dir() -> Path:
    return (
        Path(os.environ["LOCALAPPDATA"]) / "CST AG" / "2026" / "Communication"
    )


def image_name_of(pid: int) -> dict:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    handle = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
    )

    if not handle:
        return {"open": False, "win32_error": ctypes.get_last_error()}

    try:
        buffer = ctypes.create_unicode_buffer(MAX_PATH)
        size = wintypes.DWORD(MAX_PATH)

        ok = kernel32.QueryFullProcessImageNameW(
            handle, 0, buffer, ctypes.byref(size)
        )

        return {
            "open": True,
            "image": buffer.value if ok else None,
            "query_error": None if ok else ctypes.get_last_error(),
        }
    finally:
        kernel32.CloseHandle(handle)


def parse_endpoint(content: str) -> dict:
    text = content.strip().strip('"')

    try:
        host = text.split("host=")[1].split(",")[0].strip()
        port = int(text.split("port=")[1].strip().strip('"'))
    except Exception as exc:  # noqa: BLE001
        return {"parsed": False, "error": repr(exc), "raw": content}

    return {"parsed": True, "host": host, "port": port, "raw": content}


def tcp_probe(endpoint: dict, timeout: float = 3.0) -> dict:
    if not endpoint.get("parsed"):
        return {"reachable": False, "error": "unparsed_endpoint"}

    sock = socket.socket()
    sock.settimeout(timeout)

    try:
        sock.connect((endpoint["host"], endpoint["port"]))
        return {"reachable": True}
    except Exception as exc:  # noqa: BLE001
        return {"reachable": False, "error": repr(exc)}
    finally:
        sock.close()


# ------------------------------------------------------------ entry probing


def probe_entry(path: Path, attach: bool = True) -> dict:
    """Verify one communication entry through the five proven checks."""

    name = path.name

    entry: dict[str, Any] = {
        "file": name,
        "communication_file_exists": path.is_file(),
    }

    try:
        pid = int(name)
    except ValueError:
        entry["is_pid"] = False
        entry["status"] = ENTRY_NOT_CST_IMAGE
        entry["reason"] = "file name is not a pid"
        return entry

    entry["is_pid"] = True
    entry["pid"] = pid

    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        content = ""
        entry["read_error"] = repr(exc)

    entry["content"] = content.strip()

    # 2. OpenProcess + image verification
    image = image_name_of(pid)
    entry["process_query"] = image

    if not image.get("open"):
        entry["status"] = ENTRY_STALE
        entry["reason"] = "OpenProcess failed (pid not alive)"
        return entry

    if not image.get("image"):
        entry["status"] = ENTRY_IMAGE_UNREADABLE
        entry["reason"] = "image name unreadable"
        return entry

    entry["image"] = image["image"]
    entry["image_is_cst_de"] = (
        os.path.basename(image["image"]) == CST_IMAGE_NAME
    )

    if not entry["image_is_cst_de"]:
        entry["status"] = ENTRY_NOT_CST_IMAGE
        entry["reason"] = "image is not " + CST_IMAGE_NAME
        return entry

    # 3. TCP endpoint
    endpoint = parse_endpoint(content)
    entry["endpoint"] = endpoint
    entry["tcp"] = tcp_probe(endpoint)

    if not entry["tcp"].get("reachable"):
        entry["status"] = ENTRY_TCP_UNREACHABLE
        entry["reason"] = "recorded TCP endpoint is not reachable"
        return entry

    # 4/5. vendor attach
    if not attach:
        entry["status"] = ENTRY_LIVE
        entry["attach"] = {"skipped": True}
        return entry

    entry["attach"] = attach_probe(pid)

    if entry["attach"].get("connect") == "OK":
        entry["status"] = ENTRY_ATTACH_OK
    else:
        entry["status"] = ENTRY_ATTACH_FAILED

    return entry


def attach_probe(pid: int) -> dict:
    """cst.interface.DesignEnvironment.connect(pid) readback (read-only)."""

    out: dict[str, Any] = {"pid": int(pid)}

    try:
        import cst.interface as ci
    except Exception as exc:  # noqa: BLE001
        out["connect"] = "FAILED"
        out["import_error"] = repr(exc)
        return out

    try:
        de = ci.DesignEnvironment.connect(int(pid))
    except Exception as exc:  # noqa: BLE001
        out["connect"] = "FAILED"
        out["error"] = repr(exc)
        return out

    out["connect"] = "OK"
    out["reported_pid"] = int(de.pid())

    try:
        out["is_connected"] = bool(de.is_connected())
    except Exception as exc:  # noqa: BLE001
        out["is_connected"] = None
        out["is_connected_error"] = repr(exc)

    try:
        out["has_active_project"] = bool(de.has_active_project())
    except Exception as exc:  # noqa: BLE001
        out["has_active_project"] = None
        out["has_active_project_error"] = repr(exc)

    try:
        out["open_projects"] = [str(item) for item in de.list_open_projects()]
    except Exception as exc:  # noqa: BLE001
        out["open_projects"] = None
        out["open_projects_error"] = repr(exc)

    out["handle"] = de

    return out


def visible_window_titles(pid: int) -> dict:
    """Top-level window titles of a pid.

    Window enumeration is NOT sandbox-filtered on this machine (246 top-level
    windows across many pids are visible), so the window title is a reliable
    operator-facing confirmation of which project a session has open.
    """

    user32 = ctypes.WinDLL("user32", use_last_error=True)

    found: list[dict] = []
    callback_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
    )

    def _callback(hwnd, _lparam):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))

        if int(owner.value) != int(pid):
            return True

        length = user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)

        found.append(
            {
                "hwnd": int(hwnd),
                "title": buffer.value,
                "visible": bool(user32.IsWindowVisible(hwnd)),
            }
        )

        return True

    user32.EnumWindows(callback_type(_callback), 0)

    titled = [item for item in found if item["title"]]

    return {
        "window_count": len(found),
        "titled_windows": titled,
        "visible_titled_windows": [item for item in titled if item["visible"]],
    }


# ----------------------------------------------------------- discovery API


def discover_design_environments(attach: bool = True) -> dict:
    """Enumerate every communication entry and verify each one."""

    directory = communication_dir()

    entries: list[dict] = []

    if directory.is_dir():
        for path in sorted(directory.glob("*")):
            entries.append(probe_entry(path, attach=attach))

    live = [entry for entry in entries if entry.get("status") == ENTRY_ATTACH_OK]

    return {
        "discovery_version": VERSION,
        "communication_dir": str(directory),
        "entry_count": len(entries),
        "entries": entries,
        "live_pids": [entry["pid"] for entry in live],
        "live_count": len(live),
        "stale_entries": [
            {"pid": entry.get("pid"), "file": entry["file"], "status": entry["status"]}
            for entry in entries
            if entry.get("status") != ENTRY_ATTACH_OK
        ],
        "note": (
            "stale entries are reported, never deleted; a stale entry is not a "
            "precondition for discovery"
        ),
    }


def read_runtime_pid(runtime_path: Path) -> dict:
    """Read cst_runtime.json.  Schema (verified in the repo) is a single 'pid'."""

    if not runtime_path.is_file():
        return {"exists": False, "pid": None}

    try:
        data = json.loads(runtime_path.read_text(encoding="utf-8-sig"))
    except Exception as exc:  # noqa: BLE001
        return {"exists": True, "pid": None, "parse_error": repr(exc)}

    return {"exists": True, "pid": data.get("pid"), "raw": data}


def write_runtime_pid(runtime_path: Path, pid: int) -> dict:
    """Write cst_runtime.json in the same byte shape the plugin reads.

    The runtime file is a single-key JSON object, ``{"pid": <int>}``, written as
    UTF-8 WITHOUT BOM.  This reproduces that byte shape exactly.
    """

    payload = json.dumps({"pid": int(pid)}, indent=4)
    # PowerShell ConvertTo-Json renders `"pid":  41896` (two spaces after the
    # colon); match it so the file stays byte-compatible with the reader
    payload = payload.replace('": ', '":  ')

    runtime_path.write_text(payload + "\n", encoding="utf-8", newline="\n")

    return read_runtime_pid(runtime_path)


def resolve_runtime(
    runtime_path: Path,
    *,
    auto_update: bool = True,
) -> dict:
    """Resolve a live DesignEnvironment, recovering from a stale runtime PID.

    Order of preference:
      1. the PID recorded in cst_runtime.json, if it still attaches
      2. a full communication-directory discovery

    If discovery finds MORE THAN ONE live Design Environment the function
    REFUSES to guess and returns MULTIPLE_LIVE_DESIGN_ENVIRONMENTS with the
    candidate list, so the caller can ask for an explicit selection.  Recency
    is never used as a tie breaker.
    """

    result: dict[str, Any] = {
        "resolution_version": VERSION,
        "runtime_path": str(runtime_path),
        "runtime_before": read_runtime_pid(runtime_path),
    }

    recorded = result["runtime_before"].get("pid")

    if recorded is not None:
        probe = probe_entry_for_pid(int(recorded))

        result["runtime_pid_probe"] = probe

        if probe.get("status") == ENTRY_ATTACH_OK:
            result["status"] = STATUS_REUSED
            result["pid"] = int(recorded)
            result["design_environment"] = probe
            return result

        result["runtime_pid_stale_reason"] = probe.get(
            "reason", probe.get("status")
        )
    else:
        result["runtime_pid_probe"] = None

    discovery = discover_design_environments(attach=True)
    result["discovery"] = discovery

    live = [entry for entry in discovery["entries"] if entry.get("status") == ENTRY_ATTACH_OK]

    if len(live) == 0:
        result["status"] = STATUS_NO_LIVE
        return result

    if len(live) > 1:
        result["status"] = STATUS_MULTIPLE
        result["candidates"] = [
            {
                "pid": entry["pid"],
                "image": entry.get("image"),
                "endpoint": entry.get("endpoint"),
                "has_active_project": entry.get("attach", {}).get(
                    "has_active_project"
                ),
                "open_projects": entry.get("attach", {}).get("open_projects"),
            }
            for entry in live
        ]
        result["note"] = (
            "more than one live Design Environment: refusing to choose, "
            "recency is not used as a tie breaker"
        )
        return result

    chosen = live[0]

    result["status"] = STATUS_RECOVERED
    result["pid"] = chosen["pid"]
    result["design_environment"] = chosen

    if auto_update:
        result["runtime_after"] = write_runtime_pid(runtime_path, chosen["pid"])
    else:
        result["runtime_after"] = None

    return result


def probe_entry_for_pid(pid: int) -> dict:
    """Probe a pid that did not necessarily come from the communication dir."""

    entry: dict[str, Any] = {"pid": int(pid), "source": "runtime_file"}

    image = image_name_of(pid)
    entry["process_query"] = image

    if not image.get("open"):
        entry["status"] = ENTRY_STALE
        entry["reason"] = "OpenProcess failed (pid not alive)"
        return entry

    if not image.get("image"):
        entry["status"] = ENTRY_IMAGE_UNREADABLE
        entry["reason"] = "image name unreadable"
        return entry

    entry["image"] = image["image"]
    entry["image_is_cst_de"] = (
        os.path.basename(image["image"]) == CST_IMAGE_NAME
    )

    if not entry["image_is_cst_de"]:
        entry["status"] = ENTRY_NOT_CST_IMAGE
        entry["reason"] = "image is not " + CST_IMAGE_NAME
        return entry

    # cross-check the communication file (informational; a missing file is
    # tolerated, the connection probe decides)
    comm_file = communication_dir() / str(int(pid))
    entry["communication_file_exists"] = comm_file.is_file()

    if comm_file.is_file():
        endpoint = parse_endpoint(
            comm_file.read_text(encoding="utf-8", errors="replace")
        )
        entry["endpoint"] = endpoint
        entry["tcp"] = tcp_probe(endpoint)

    entry["attach"] = attach_probe(int(pid))

    if entry["attach"].get("connect") == "OK":
        entry["status"] = ENTRY_ATTACH_OK
    else:
        entry["status"] = ENTRY_ATTACH_FAILED
        entry["reason"] = "vendor attach failed"

    return entry
