"""CST_AI plugin layer — configuration and precedence.

Extends the EXISTING `cst_ai_config.json` additively.  The keys the old CLI
already relies on (`expected_project`, `parameter_rules`) keep their meaning, so
`cst_cli.py` continues to work unchanged.

Precedence, highest first — one rule, applied everywhere:

    CLI explicit option
  > TaskSpec explicit field
  > config file
  > safe default

The resolved value and the layer it came from are both reported, so there is
never doubt about which of three places won.
"""

from __future__ import annotations

import json
from pathlib import Path

CONFIG_VERSION = "1.16.0"

CONFIG_PATH = Path(__file__).resolve().parent.parent / "cst_ai_config.json"

RUN_STORE_KEY = "run_store_path"
OUTPUT_ROOT_KEY = "default_output_root"
PID_POLICY_KEY = "cst_runtime_pid_policy"

#: safe defaults, used only when nothing else supplies a value
DEFAULTS = {
    RUN_STORE_KEY: "runs",
    OUTPUT_ROOT_KEY: "runs",
    PID_POLICY_KEY: "AUTO",
}

LAYER_CLI = "CLI_EXPLICIT"
LAYER_TASK = "TASKSPEC_EXPLICIT"
LAYER_CONFIG = "CONFIG_FILE"
LAYER_DEFAULT = "SAFE_DEFAULT"

PRECEDENCE = (LAYER_CLI, LAYER_TASK, LAYER_CONFIG, LAYER_DEFAULT)


def load_config(path=None) -> dict:
    """Read the config file.  A missing file is not an error."""

    p = Path(path) if path else CONFIG_PATH

    if not p.is_file():
        return {
            "config_path": str(p),
            "exists": False,
            "raw": {},
            "schema_version": CONFIG_VERSION,
        }

    try:
        raw = json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"config file {p} is not readable JSON: {exc!r}") from exc

    return {
        "config_path": str(p),
        "exists": True,
        "raw": raw,
        "schema_version": CONFIG_VERSION,
        # the two legacy keys the existing CLI depends on
        "legacy_expected_project": raw.get("expected_project"),
        "legacy_parameter_rules": raw.get("parameter_rules"),
    }


def resolve(key: str, *, cli_value=None, task_value=None, config=None,
            path=None) -> dict:
    """Resolve one setting and report which layer supplied it."""

    cfg = config if config is not None else load_config(path)

    if cli_value is not None:
        return {"key": key, "value": cli_value, "layer": LAYER_CLI}

    if task_value is not None:
        return {"key": key, "value": task_value, "layer": LAYER_TASK}

    raw_value = (cfg.get("raw") or {}).get(key)

    if raw_value is not None:
        return {"key": key, "value": raw_value, "layer": LAYER_CONFIG}

    return {"key": key, "value": DEFAULTS.get(key), "layer": LAYER_DEFAULT}


def resolve_all(*, cli=None, task=None, config=None, path=None) -> dict:
    cli = cli or {}
    task = task or {}
    cfg = config if config is not None else load_config(path)

    resolved = {}

    for key in (RUN_STORE_KEY, OUTPUT_ROOT_KEY, PID_POLICY_KEY):
        resolved[key] = resolve(
            key,
            cli_value=cli.get(key),
            task_value=task.get(key),
            config=cfg,
        )

    base = Path(__file__).resolve().parent.parent

    store = Path(resolved[RUN_STORE_KEY]["value"])
    output = Path(resolved[OUTPUT_ROOT_KEY]["value"])

    resolved[RUN_STORE_KEY]["resolved_path"] = str(
        store if store.is_absolute() else (base / store)
    )
    resolved[OUTPUT_ROOT_KEY]["resolved_path"] = str(
        output if output.is_absolute() else (base / output)
    )

    return {
        "config_version": CONFIG_VERSION,
        "config_path": cfg.get("config_path"),
        "config_exists": cfg.get("exists"),
        "precedence": list(PRECEDENCE),
        "settings": resolved,
        "legacy_keys_preserved": [
            "expected_project", "parameter_rules",
        ],
    }


# ------------------------------------------------------------------ PID policy
PID_AUTO = "AUTO"
PID_EXPLICIT = "EXPLICIT"

#: AUTO is the recommendation; the V1.9A adapter is the ONLY discovery path
PID_POLICY_NOTE = (
    "AUTO uses the single V1.9A runtime adapter (communication directory -> "
    "candidate PIDs -> process verification -> endpoint -> connect). --pid is an "
    "OVERRIDE and must still be verified to point at a real CST Design "
    "Environment.  With several live DEs the plugin BLOCKS rather than picking "
    "the first one."
)


def pid_policy(cli_pid=None, task_pid=None, config=None, path=None) -> dict:
    if cli_pid is not None:
        return {"policy": PID_EXPLICIT, "pid": int(cli_pid),
                "layer": LAYER_CLI, "must_verify_pid": True}

    if task_pid is not None:
        return {"policy": PID_EXPLICIT, "pid": int(task_pid),
                "layer": LAYER_TASK, "must_verify_pid": True}

    return {"policy": PID_AUTO, "pid": None, "layer": LAYER_CONFIG
            if config else LAYER_DEFAULT, "must_verify_pid": True,
            "note": PID_POLICY_NOTE}


# ------------------------------------------------------------ dangerous paths

def check_output_path(output_dir, *, protected, repo_root) -> dict:
    """Minimal guard: an output directory may not be empty, may not be the repo
    root, and may not overlap a protected input."""

    problems = []

    raw = str(output_dir or "").strip()

    if not raw:
        problems.append({"reason": "EMPTY_OUTPUT_DIR",
                         "why": "an empty string would resolve to the repo root"})
        return {"ok": False, "problems": problems}

    out = Path(raw)

    if not out.is_absolute():
        out = Path(repo_root) / out

    out = out.resolve()

    if out == Path(repo_root).resolve():
        problems.append({"reason": "OUTPUT_IS_REPO_ROOT",
                         "why": "writing a run into the repository root"})

    for item in protected or []:
        p = Path(item)

        if not p.is_absolute():
            p = Path(repo_root) / p

        p = p.resolve()

        if out == p or out in p.parents or p in out.parents:
            problems.append(
                {"reason": "OUTPUT_OVERLAPS_PROTECTED_INPUT",
                 "output": str(out), "protected": str(p)}
            )

    return {
        "ok": not problems,
        "output_dir_resolved": str(out),
        "problems": problems,
        "sandbox_framework_implemented": False,
    }
