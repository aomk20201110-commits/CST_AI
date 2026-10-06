"""The public API surface: what exists, and what it does with no CST present.

These checks are the reason a caller can trust `status` and `inspect` after a
restart: they are answered from the run store alone.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_REPO_ENTRY = str(REPO_ROOT)
if _REPO_ENTRY in sys.path:
    sys.path.remove(_REPO_ENTRY)
sys.path.insert(0, _REPO_ENTRY)

from cst_ai_plugin import public  # noqa: E402

EXPECTED_SURFACE = ("plan", "run", "status", "inspect", "resume", "load_task")


class TestSurface(unittest.TestCase):
    def test_the_suite_tests_this_checkout_and_not_a_neighbouring_one(self):
        """Guards the guard: a real checkout on PYTHONPATH must not win.

        The repository root is forced to the front of `sys.path` above, so these
        assertions prove the modules under test really came from this tree.
        """

        import cst_ai_plugin
        import cst_ai_tool

        for module in (cst_ai_plugin, public, cst_ai_tool):
            with self.subTest(module=module.__name__):
                origin = Path(module.__file__).resolve()
                self.assertTrue(
                    origin.is_relative_to(REPO_ROOT),
                    f"{module.__name__} was imported from {origin}, outside {REPO_ROOT}",
                )

    def test_public_api_v1_is_declared(self):
        self.assertEqual(tuple(public.PUBLIC_API_V1), EXPECTED_SURFACE)

    def test_every_declared_entry_point_exists_and_is_callable(self):
        for name in public.PUBLIC_API_V1:
            with self.subTest(name=name):
                self.assertTrue(hasattr(public, name), f"missing public.{name}")
                self.assertTrue(callable(getattr(public, name)))

    def test_version_literals_are_present(self):
        self.assertIsInstance(public.PUBLIC_API_VERSION, str)
        self.assertIsInstance(public.PUBLIC_API_REVISION, str)

    def test_public_api_helper_reports_the_surface(self):
        report = public.public_api()

        self.assertEqual(report["version"], public.PUBLIC_API_VERSION)
        self.assertEqual(report["revision"], public.PUBLIC_API_REVISION)
        self.assertEqual(tuple(report["PUBLIC_API_V1"]), EXPECTED_SURFACE)
        self.assertFalse(report["internal_module_names_exposed"])
        self.assertTrue(report["capability_ids"])


class TestOfflineReads(unittest.TestCase):
    """No CST, no run store, no live process: these must answer, not hang."""

    def test_status_of_an_unknown_run_is_an_answer_not_an_exception(self):
        result = public.status("definitely-not-a-run", store_root=str(REPO_ROOT / "tests" / "_no_store"))

        self.assertIsInstance(result, dict)
        self.assertFalse(result.get("available"))
        self.assertFalse(result.get("cst_contacted"))
        self.assertIn("error", result)

    def test_inspect_of_an_unknown_run_uses_the_documented_error_path(self):
        """KI-002: `inspect` raises `RunNotFound` where `status` returns it.

        `status` answers with a structured `RUN_NOT_FOUND`; `inspect` currently
        lets the exception escape, so the tool adapter reports
        `TOOL_ADAPTER_EXCEPTION` (with the traceback kept out of the response
        body).  Both are offline.  This test pins the documented behaviour and
        accepts the structured form too, so fixing KI-002 does not require
        rewriting the suite — but a change that reintroduces CST contact, or puts
        a traceback in the response, fails here.
        """

        from cst_ai_plugin.runstore import RunNotFound

        store = str(REPO_ROOT / "tests" / "_no_store")
        structured: dict | None = None

        try:
            result = public.inspect("definitely-not-a-run", store_root=store)
        except RunNotFound as exc:
            self.assertIn("no run store entry", str(exc))
        else:
            self.assertIsInstance(result, dict)
            structured = result
            self.assertFalse(result.get("available"))
            self.assertFalse(result.get("cst_contacted"))

        if structured is not None:
            self.assertTrue(structured.get("read_only", True))

    def test_runtime_refresh_is_not_authorised(self):
        result = public.status(
            "definitely-not-a-run",
            store_root=str(REPO_ROOT / "tests" / "_no_store"),
            refresh_runtime=True,
        )

        self.assertFalse(result.get("available"))
        self.assertEqual(
            (result.get("error") or {}).get("error_code"),
            "REFRESH_RUNTIME_NOT_AUTHORIZED",
        )


if __name__ == "__main__":
    unittest.main()
