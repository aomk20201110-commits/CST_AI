"""CST CLI — extended with the CST_AI plugin task interface.

Backward compatibility is preserved exactly: every pre-existing command
(`status`, `params`, `get-param`, `set-param`, `tree-has`, `solver-status`,
`messages`, `start-solver`, `abort-solver`) still behaves as it did, and still
goes through `CSTSession`.

The plugin commands are namespaced so they cannot collide with the legacy
`status`:

    task-plan     <task.json>   execution plan          0 CST contact
    task-run      <task.json>   run through the orchestrator
    status-run    <run_id>      offline durable status  0 CST contact
    inspect-run   <run_id>      offline read-only detail 0 CST contact
    resume-run    <run_id>      cross-process resume
    api                         the public API surface

This file is a THIN adapter: it calls `cst_ai_plugin.public` and prints the
result.  It never builds a project, starts a solver or extracts results itself.

`--pid` is an OVERRIDE for the legacy session commands.  The plugin commands
resolve the runtime through the single V1.9A adapter (AUTO), so a user does not
have to supply a pid at all.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "cst_ai_config.json"

#: commands that need a live CST session (the legacy surface)
LEGACY_SESSION_COMMANDS = (
    "status",
    "params",
    "get-param",
    "set-param",
    "tree-has",
    "solver-status",
    "messages",
    "start-solver",
    "abort-solver",
)

#: plugin commands that are fully offline
OFFLINE_PLUGIN_COMMANDS = ("task-plan", "status-run", "inspect-run", "api")


def load_config():
    with CONFIG_PATH.open(
        "r",
        encoding="utf-8-sig",
    ) as f:
        return json.load(f)


def emit(data):
    print(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2,
            default=repr,
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()

    # NOTE: no longer globally required, so the offline plugin commands can run
    # without a pid.  The legacy commands validate it themselves, below.
    parser.add_argument("--pid", type=int, default=None)

    sub = parser.add_subparsers(dest="command", required=True)

    # ------------------------------------------------------- legacy commands
    sub.add_parser("status")
    sub.add_parser("params")
    sub.add_parser("solver-status")
    sub.add_parser("messages")
    sub.add_parser("start-solver")
    sub.add_parser("abort-solver")

    get_parser = sub.add_parser("get-param")
    get_parser.add_argument("name")

    set_parser = sub.add_parser("set-param")
    set_parser.add_argument("name")
    set_parser.add_argument("value", type=float)

    tree_parser = sub.add_parser("tree-has")
    tree_parser.add_argument("path")

    # ------------------------------------------------------- plugin commands
    plan_parser = sub.add_parser("task-plan")
    plan_parser.add_argument("task")
    plan_parser.add_argument("--run-store", default=None)
    plan_parser.add_argument("--config", default=None)

    run_parser = sub.add_parser("task-run")
    run_parser.add_argument("task")
    run_parser.add_argument("--run-store", default=None)
    run_parser.add_argument("--config", default=None)

    status_parser = sub.add_parser("status-run")
    status_parser.add_argument("run_id")
    status_parser.add_argument("--run-store", default=None)
    status_parser.add_argument("--json", action="store_true")
    status_parser.add_argument(
        "--refresh-runtime",
        action="store_true",
        help="NOT implemented; STATUS is offline by default",
    )

    inspect_parser = sub.add_parser("inspect-run")
    inspect_parser.add_argument("run_id")
    inspect_parser.add_argument("--run-store", default=None)
    inspect_parser.add_argument("--json", action="store_true")

    resume_parser = sub.add_parser("resume-run")
    resume_parser.add_argument("run_id")
    resume_parser.add_argument("--run-store", default=None)
    resume_parser.add_argument("--config", default=None)
    resume_parser.add_argument(
        "--task",
        default=None,
        help="cross-check only; never silently replaces the stored task",
    )

    sub.add_parser("api")

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    # ============================================================ plugin surface
    if args.command in ("task-plan", "task-run", "status-run", "inspect-run",
                        "resume-run", "api"):
        # imported lazily so the legacy path never needs the plugin package
        from cst_ai_plugin import public

        if args.command == "api":
            emit({"ok": True, **public.public_api()})
            return 0

        if args.command == "task-plan":
            task = public.load_task(args.task)
            emit(public.plan(task, store_root=args.run_store,
                             config_path=args.config))
            return 0

        if args.command == "task-run":
            task = public.load_task(args.task)
            emit(public.run(task, store_root=args.run_store,
                            config_path=args.config))
            return 0

        if args.command == "status-run":
            result = public.status(
                args.run_id,
                store_root=args.run_store,
                refresh_runtime=args.refresh_runtime,
            )
            emit(result)
            return 0

        if args.command == "inspect-run":
            emit(public.inspect(args.run_id, store_root=args.run_store))
            return 0

        if args.command == "resume-run":
            supplied = public.load_task(args.task) if args.task else None
            emit(public.resume(args.run_id, store_root=args.run_store,
                               task=supplied, config_path=args.config))
            return 0

    # ============================================================ legacy surface
    if args.command in LEGACY_SESSION_COMMANDS and args.pid is None:
        emit(
            {
                "ok": False,
                "error_type": "MissingPid",
                "error": (
                    f"command {args.command!r} needs --pid (or use the plugin "
                    f"commands, which resolve the runtime automatically)"
                ),
            }
        )
        return 2

    from cst_tools import CSTSession

    config = load_config()

    session = CSTSession(
        pid=args.pid,
        expected_project=config["expected_project"],
    )

    if args.command == "status":
        emit({"ok": True, "project": session.project_info()})

    elif args.command == "params":
        emit({"ok": True, "parameters": session.list_parameters()})

    elif args.command == "get-param":
        emit({"ok": True,
              "parameter": session.get_parameter(args.name)})

    elif args.command == "set-param":
        result = session.set_parameter(
            name=args.name,
            value=args.value,
            parameter_rules=config["parameter_rules"],
            rebuild=True,
            save=True,
        )
        emit({"ok": True, "result": result})

    elif args.command == "tree-has":
        emit({"ok": True, "path": args.path,
              "exists": session.tree_has(args.path)})

    elif args.command == "solver-status":
        emit({"ok": True, "solver": session.solver_status()})

    elif args.command == "messages":
        emit({"ok": True, "messages": session.get_messages()})

    elif args.command == "start-solver":
        emit({"ok": True, "solver": session.start_solver()})

    elif args.command == "abort-solver":
        emit({"ok": True, "solver": session.abort_solver()})

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())

    except SystemExit:
        raise

    except Exception as exc:
        emit(
            {
                "ok": False,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        )
        raise SystemExit(3)
