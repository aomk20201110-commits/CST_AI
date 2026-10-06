"""TaskSpec validation: the schema is a gate, not a suggestion.

Every rejection below is a rule a user can hit, and each one is asserted by the
message it raises, so a silent behaviour change fails this test.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_REPO_ENTRY = str(REPO_ROOT)
if _REPO_ENTRY in sys.path:
    sys.path.remove(_REPO_ENTRY)
sys.path.insert(0, _REPO_ENTRY)

from cst_ai_plugin.errors import TaskSchemaError  # noqa: E402
from cst_ai_plugin.task import (  # noqa: E402
    ALLOW_ONE,
    ALLOW_UP_TO_N,
    FORBID,
    REUSE_ONLY,
    TASK_KINDS,
    authorized_solve_budget,
    canonical_hash,
    task_spec_hash,
    validate_task,
)

EXAMPLES = sorted((REPO_ROOT / "examples").glob("*.json"))


def base_task(**overrides) -> dict:
    task = {
        "task_id": "unit_test_task",
        "task_kind": "LOAD_EXISTING_RESULT",
        "stages": ["CONNECT", "EXTRACT", "ANALYZE", "REPORT"],
        "solve_policy": FORBID,
        "spec": {"existing_project": "C:/placeholder/model.cst"},
        "expected_outputs": ["resonance_table"],
        "output_dir": "C:/placeholder/out",
    }
    task.update(overrides)
    return task


class TestAcceptance(unittest.TestCase):
    def test_minimal_task_is_accepted(self):
        self.assertEqual(validate_task(base_task())["task_id"], "unit_test_task")

    def test_every_declared_kind_is_accepted(self):
        for kind in TASK_KINDS:
            with self.subTest(kind=kind):
                validate_task(base_task(task_kind=kind))

    def test_optional_fields_are_accepted(self):
        validate_task(
            base_task(
                label="demo",
                inputs={"incident_mode": "Zmin(1)"},
                cst={"runtime_file": "C:/placeholder/cst_runtime.json"},
                notes="free text",
                spec={"existing_project": "C:/placeholder/model.cst"},
            )
        )

    def test_examples_all_validate(self):
        self.assertEqual(len(EXAMPLES), 3, f"expected 3 examples, found {EXAMPLES}")

        for path in EXAMPLES:
            with self.subTest(example=path.name):
                task = json.loads(path.read_text(encoding="utf-8-sig"))
                validate_task(task)


class TestRejections(unittest.TestCase):
    def assert_rejected(self, task: dict, fragment: str):
        with self.assertRaises(TaskSchemaError) as ctx:
            validate_task(task)
        self.assertIn(fragment, str(ctx.exception))

    def test_not_a_mapping(self):
        with self.assertRaises(TaskSchemaError):
            validate_task(["not", "a", "task"])

    def test_unknown_field_is_rejected(self):
        self.assert_rejected(base_task(stage="EXTRACT"), "unknown task field")

    def test_missing_required_fields(self):
        for field in ("task_id", "task_kind", "stages", "solve_policy"):
            with self.subTest(field=field):
                task = base_task()
                task.pop(field)
                self.assert_rejected(task, f"missing required field: {field}")

    def test_unknown_kind_and_policy(self):
        self.assert_rejected(base_task(task_kind="DO_EVERYTHING"), "unknown task_kind")
        self.assert_rejected(base_task(solve_policy="MAYBE"), "unknown solve_policy")

    def test_empty_or_unknown_stages(self):
        self.assert_rejected(base_task(stages=[]), "stages must be a non-empty list")
        self.assert_rejected(base_task(stages=["CONNECT", "TELEPORT"]), "unknown stage")

    def test_stages_must_be_unique_and_ordered(self):
        self.assert_rejected(
            base_task(stages=["ANALYZE", "CONNECT"]),
            "stages must be unique and in canonical order",
        )
        self.assert_rejected(
            base_task(stages=["CONNECT", "CONNECT", "REPORT"]),
            "stages must be unique and in canonical order",
        )

    def test_output_dir_is_mandatory(self):
        self.assert_rejected(base_task(output_dir=""), "output_dir is required")

    def test_output_dir_cannot_be_dropped(self):
        task = base_task()
        task.pop("output_dir")
        self.assert_rejected(task, "output_dir is required")

    def test_allow_up_to_n_needs_a_positive_budget(self):
        self.assert_rejected(
            base_task(solve_policy=ALLOW_UP_TO_N),
            "ALLOW_UP_TO_N requires max_solves >= 1",
        )
        self.assert_rejected(
            base_task(solve_policy=ALLOW_UP_TO_N, max_solves=0),
            "ALLOW_UP_TO_N requires max_solves >= 1",
        )

    def test_zero_budget_policies_must_not_declare_max_solves(self):
        for policy in (FORBID, REUSE_ONLY):
            with self.subTest(policy=policy):
                self.assert_rejected(
                    base_task(solve_policy=policy, max_solves=2),
                    f"{policy} must not declare max_solves",
                )

    def test_solve_stage_with_forbid_is_contradictory(self):
        self.assert_rejected(
            base_task(
                stages=["CONNECT", "BUILD", "VERIFY", "SOLVE", "REPORT"],
                solve_policy=FORBID,
            ),
            "stages include SOLVE but solve_policy is FORBID",
        )

    def test_solve_stage_with_reuse_only_is_allowed(self):
        """REUSE_ONLY is not a contradiction: the stage becomes a reuse decision."""

        task = validate_task(
            base_task(
                stages=["CONNECT", "BUILD", "VERIFY", "SOLVE", "REPORT"],
                solve_policy=REUSE_ONLY,
            )
        )
        self.assertEqual(authorized_solve_budget(task), 0)


class TestBudget(unittest.TestCase):
    def test_budget_per_policy(self):
        self.assertEqual(authorized_solve_budget(base_task(solve_policy=FORBID)), 0)
        self.assertEqual(authorized_solve_budget(base_task(solve_policy=REUSE_ONLY)), 0)
        self.assertEqual(authorized_solve_budget(base_task(solve_policy=ALLOW_ONE)), 1)
        self.assertEqual(
            authorized_solve_budget(
                base_task(solve_policy=ALLOW_UP_TO_N, max_solves=4)
            ),
            4,
        )


class TestIdentity(unittest.TestCase):
    def test_hash_is_stable_and_order_insensitive(self):
        a = canonical_hash({"x": 1, "y": [2, 3]})
        b = canonical_hash({"y": [2, 3], "x": 1})

        self.assertEqual(a, b)
        self.assertTrue(a.startswith("sha256:"))

    def test_task_spec_hash_changes_with_the_task(self):
        task = base_task()
        original = task_spec_hash(task)

        self.assertEqual(original, task_spec_hash(base_task()))

        changed = base_task(stages=["CONNECT", "REPORT"])
        self.assertNotEqual(original, task_spec_hash(changed))

    def test_task_spec_hash_ignores_presentation_fields(self):
        """A label or note is not part of the task's identity."""

        original = task_spec_hash(base_task())
        self.assertEqual(
            original,
            task_spec_hash(base_task(label="renamed", notes="typo fixed")),
        )


if __name__ == "__main__":
    unittest.main()
