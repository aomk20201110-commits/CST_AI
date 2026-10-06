from __future__ import annotations

import math
from pathlib import Path
from typing import Any


class CSTToolError(RuntimeError):
    pass


def _json_safe(value: Any) -> Any:
    if value is None:
        return None

    if isinstance(value, (str, bool, int, float)):
        return value

    if isinstance(value, dict):
        return {
            str(k): _json_safe(v)
            for k, v in value.items()
        }

    if isinstance(value, (list, tuple)):
        return [
            _json_safe(v)
            for v in value
        ]

    return repr(value)


class CSTSession:
    def __init__(
        self,
        pid: int,
        expected_project: str | Path | None = None,
    ):
        import cst.interface as ci

        self.pid = int(pid)

        try:
            self.de = ci.DesignEnvironment.connect(self.pid)
        except Exception as exc:
            raise CSTToolError(
                f"Failed to connect to CST PID {self.pid}: {exc}"
            ) from exc

        if not self.de.is_connected():
            raise CSTToolError(
                f"CST PID {self.pid} is not connected."
            )

        if not self.de.has_active_project():
            raise CSTToolError(
                "The connected CST instance has no active project."
            )

        self.project = self.de.active_project()
        self.model3d = self.project.model3d

        if self.model3d is None:
            raise CSTToolError(
                "The active project has no Model3D."
            )

        self.expected_project = (
            Path(expected_project).resolve()
            if expected_project is not None
            else None
        )

        self._verify_project()


    def _verify_project(self) -> None:
        actual = Path(self.project.filename()).resolve()

        if (
            self.expected_project is not None
            and actual != self.expected_project
        ):
            raise CSTToolError(
                "Active CST project does not match the locked project.\n"
                f"Expected: {self.expected_project}\n"
                f"Actual:   {actual}"
            )


    def project_info(self) -> dict[str, Any]:
        return {
            "connected": bool(self.de.is_connected()),
            "pid": int(self.de.pid()),
            "filename": self.project.filename(),
            "project_type": str(self.project.project_type()),
        }


    # ========================================================
    # Parameters
    # ========================================================

    def list_parameters(self) -> list[dict[str, Any]]:
        m = self.model3d
        count = int(m.GetNumberOfParameters())

        result = []

        for index in range(count):
            result.append(
                {
                    "index": index,
                    "name": str(m.GetParameterName(index)),
                    "expression": str(m.GetParameterSValue(index)),
                    "numeric": float(m.GetParameterNValue(index)),
                }
            )

        return result


    def get_parameter(self, name: str) -> dict[str, Any]:
        for item in self.list_parameters():
            if item["name"] == name:
                return item

        raise CSTToolError(
            f"Parameter does not exist: {name!r}"
        )


    def set_parameter(
        self,
        name: str,
        value: float,
        parameter_rules: dict[str, dict[str, float]],
        rebuild: bool = True,
        save: bool = True,
    ) -> dict[str, Any]:

        if name not in parameter_rules:
            raise CSTToolError(
                f"Parameter is not AI-writable: {name!r}"
            )

        try:
            numeric_value = float(value)
        except (TypeError, ValueError) as exc:
            raise CSTToolError(
                f"Parameter value is not numeric: {value!r}"
            ) from exc

        if not math.isfinite(numeric_value):
            raise CSTToolError(
                f"Parameter value must be finite: {numeric_value!r}"
            )

        rule = parameter_rules[name]

        min_value = float(rule["min"])
        max_value = float(rule["max"])

        if not (min_value <= numeric_value <= max_value):
            raise CSTToolError(
                f"Parameter {name!r} must be in "
                f"[{min_value}, {max_value}], got {numeric_value}"
            )

        if not self.model3d.DoesParameterExist(name):
            raise CSTToolError(
                f"Refusing to create an unknown parameter: {name!r}"
            )

        before = self.get_parameter(name)
        old_expression = before["expression"]

        try:
            self.model3d.StoreParameter(
                name,
                numeric_value,
            )

            if rebuild:
                self.model3d.Rebuild()

            after = self.get_parameter(name)

            if not math.isclose(
                after["numeric"],
                numeric_value,
                rel_tol=1e-12,
                abs_tol=1e-12,
            ):
                raise CSTToolError(
                    f"CST readback mismatch for {name!r}: "
                    f"requested {numeric_value}, "
                    f"read back {after['numeric']}"
                )

            if save:
                self.project.save()

        except Exception as exc:
            try:
                self.model3d.StoreParameter(
                    name,
                    old_expression,
                )

                if rebuild:
                    self.model3d.Rebuild()

            except Exception as rollback_exc:
                raise CSTToolError(
                    "Parameter update failed and rollback also failed.\n"
                    f"Update error: {exc}\n"
                    f"Rollback error: {rollback_exc}"
                ) from rollback_exc

            raise CSTToolError(
                f"Parameter update failed; previous value was restored: {exc}"
            ) from exc

        return {
            "name": name,
            "before": before,
            "after": after,
            "rule": {
                "min": min_value,
                "max": max_value,
            },
            "rebuilt": rebuild,
            "saved": save,
        }


    # ========================================================
    # Model / tree
    # ========================================================

    def rebuild(self, save: bool = True) -> None:
        self.model3d.Rebuild()

        if save:
            self.project.save()


    def tree_items(self) -> list[str]:
        return list(self.model3d.get_tree_items())


    def tree_has(self, exact_path: str) -> bool:
        return exact_path in self.tree_items()


    # ========================================================
    # Messages
    # ========================================================

    def get_messages(self) -> list[Any]:
        try:
            messages = self.project.get_messages()
        except Exception as exc:
            raise CSTToolError(
                f"Failed to read CST messages: {exc}"
            ) from exc

        return _json_safe(list(messages))


    # ========================================================
    # Solver
    # ========================================================

    def solver_status(self) -> dict[str, Any]:
        try:
            running = bool(
                self.model3d.is_solver_running()
            )
        except Exception as exc:
            raise CSTToolError(
                f"Failed to query solver status: {exc}"
            ) from exc

        return {
            "running": running,
        }


    def start_solver(self) -> dict[str, Any]:
        before = self.solver_status()

        if before["running"]:
            raise CSTToolError(
                "Solver is already running."
            )

        messages_before = self.get_messages()

        try:
            self.model3d.start_solver()
        except Exception as exc:
            raise CSTToolError(
                f"Failed to start solver: {exc}"
            ) from exc

        after = self.solver_status()
        messages_after = self.get_messages()

        return {
            "requested": True,
            "running_before": before["running"],
            "running_after": after["running"],
            "messages_before": messages_before,
            "messages_after": messages_after,
        }


    def abort_solver(self) -> dict[str, Any]:
        before = self.solver_status()

        if not before["running"]:
            raise CSTToolError(
                "Solver is not currently running."
            )

        try:
            self.model3d.abort_solver()
        except Exception as exc:
            raise CSTToolError(
                f"Failed to abort solver: {exc}"
            ) from exc

        after = self.solver_status()

        return {
            "requested": True,
            "running_before": before["running"],
            "running_after": after["running"],
        }
