"""Zero-solve behaviour: planning and inspecting never touch CST, never solve,
and never write outside the run store.

Everything here runs with no CST session present.  If one of these tests ever
fails on a machine without CST, that is the finding: an action that was supposed
to be offline has grown a dependency on a live Design Environment.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_REPO_ENTRY = str(REPO_ROOT)
if _REPO_ENTRY in sys.path:
    sys.path.remove(_REPO_ENTRY)
sys.path.insert(0, _REPO_ENTRY)

import cst_ai_tool  # noqa: E402
from cst_ai_plugin import public  # noqa: E402


def zero_solve_task(output_dir: str) -> dict:
    """A complete, valid, zero-solve task.  Reading a solved project is enough."""

    return {
        "task_id": "unit_test_zero_solve",
        "task_kind": "LOAD_EXISTING_RESULT",
        "label": "zero solve plan",
        "stages": ["CONNECT", "EXTRACT", "ANALYZE", "REPORT"],
        "solve_policy": "FORBID",
        "spec": {
            "existing_project": "C:/placeholder/never_opened_by_this_test.cst",
            "incident_mode": "Zmin(1)",
            "extract_mode": "floquet_rta",
        },
        "expected_outputs": ["s_parameters", "resonance_table"],
        "output_dir": output_dir,
    }


class TestPlanIsSideEffectFree(unittest.TestCase):
    def test_plan_of_a_forbid_task_declares_zero_solves(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "store"
            task = zero_solve_task(str(Path(tmp) / "out"))

            result = public.plan(task, store_root=str(store))

            self.assertTrue(result["ok"], result)
            self.assertEqual(result["PLANNED_SOLVE_COUNT"], 0)
            self.assertEqual(result["MAX_AUTHORIZED_SOLVES"], 0)
            self.assertFalse(result["cst_contacted"])
            self.assertFalse(result["solved"])

    def test_plan_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "store"
            out = Path(tmp) / "out"

            public.plan(zero_solve_task(str(out)), store_root=str(store))

            self.assertFalse(store.exists(), "PLAN created a run store")
            self.assertFalse(out.exists(), "PLAN created an output directory")

    def test_plan_is_repeatable(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = zero_solve_task(str(Path(tmp) / "out"))

            first = public.plan(task, store_root=str(Path(tmp) / "store"))
            second = public.plan(task, store_root=str(Path(tmp) / "store"))

            self.assertEqual(first["plan_hash"], second["plan_hash"])
            self.assertEqual(first["task_spec_hash"], second["task_spec_hash"])


class TestInvalidTasksNeverReachCst(unittest.TestCase):
    def test_run_rejects_an_invalid_task_before_any_side_effect(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = zero_solve_task(str(Path(tmp) / "out"))
            task["solve_policy"] = "ALLOW_UP_TO_N"  # no max_solves: invalid

            result = public.run(task, store_root=str(Path(tmp) / "store"))

            self.assertFalse(result["ok"])
            self.assertEqual(result["stage"], "VALIDATION")
            self.assertFalse(result["cst_contacted"])
            self.assertFalse(result["solved"])

    def test_plan_reports_validation_errors_as_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = zero_solve_task(str(Path(tmp) / "out"))
            task["unexpected_field"] = True

            result = public.plan(task, store_root=str(Path(tmp) / "store"))

            self.assertFalse(result["ok"])
            self.assertEqual(result["stage"], "VALIDATION")
            self.assertFalse(result["cst_contacted"])


class TestToolAdapterEnvelopes(unittest.TestCase):
    """The adapter is the boundary a harness sees, so it is tested as one."""

    def test_plan_envelope_reports_the_zero_solve_declaration(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = zero_solve_task(str(Path(tmp) / "out"))

            envelope = cst_ai_tool.handle(
                "plan", {"task": task, "store_root": str(Path(tmp) / "store")}
            )

            self.assertTrue(envelope["ok"], envelope)
            self.assertEqual(envelope["action"], "plan")
            self.assertEqual(envelope["PLANNED_SOLVE_COUNT"], 0)
            self.assertEqual(envelope["MAX_AUTHORIZED_SOLVES"], 0)
            self.assertFalse(envelope["cst_contacted"])
            self.assertFalse(envelope["solved"])

    def test_plan_without_a_task_is_a_structured_error(self):
        envelope = cst_ai_tool.handle("plan", {})

        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["error_type"], "TASK_REQUIRED")
        self.assertEqual(envelope["stage"], "VALIDATION")

    def test_unknown_action_is_rejected(self):
        envelope = cst_ai_tool.handle("launch_missiles", {})

        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["error_type"], "UNKNOWN_TOOL_ACTION")

    def test_unknown_inspect_section_is_rejected(self):
        envelope = cst_ai_tool.handle("inspect", {"run_id": "x", "section": "everything"})

        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["error_type"], "UNKNOWN_INSPECT_SECTION")

    def test_status_of_an_unknown_run_is_a_structured_offline_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            envelope = cst_ai_tool.handle(
                "status", {"run_id": "no-such-run", "store_root": str(Path(tmp) / "store")}
            )

            self.assertFalse(envelope["ok"])
            self.assertFalse(envelope["traceback_in_response"])
            self.assertIn(envelope["error_type"], {"RUN_NOT_FOUND", "RUN_NOT_AVAILABLE"})

    def test_inspect_of_an_unknown_run_keeps_the_traceback_out_of_the_response(self):
        """KI-002 as documented: `inspect` still reports the adapter exception.

        `status` returns a structured `RUN_NOT_FOUND`; `inspect` lets
        `RunNotFound` escape, so the adapter answers `TOOL_ADAPTER_EXCEPTION`.
        What must never change is the shape: no CST contact, no traceback in the
        response body, and the offending `run_id` echoed back.
        """

        with tempfile.TemporaryDirectory() as tmp:
            envelope = cst_ai_tool.handle(
                "inspect", {"run_id": "no-such-run", "store_root": str(Path(tmp) / "store")}
            )

            self.assertFalse(envelope["ok"])
            self.assertFalse(envelope["traceback_in_response"])
            self.assertEqual(envelope["run_id"], "no-such-run")
            self.assertIn(
                envelope["error_type"],
                {"RUN_NOT_FOUND", "RUN_NOT_AVAILABLE", "TOOL_ADAPTER_EXCEPTION"},
            )

            if envelope["error_type"] == "TOOL_ADAPTER_EXCEPTION":
                # the documented defect: the traceback is carried beside the
                # response, never inside its message
                self.assertNotIn("Traceback", envelope["message"])
                self.assertIn("RunNotFound", envelope["message"])

    def test_tool_surface_never_inlines_arrays(self):
        """A 1001-point curve must never appear inside an envelope."""

        with tempfile.TemporaryDirectory() as tmp:
            envelope = cst_ai_tool.handle(
                "plan",
                {"task": zero_solve_task(str(Path(tmp) / "out")),
                 "store_root": str(Path(tmp) / "store")},
            )

            text = json.dumps(envelope)

            self.assertLess(len(text), 20000, "the plan envelope is unexpectedly large")


if __name__ == "__main__":
    unittest.main()
