"""Static checks on the agent-facing adapter: the five tools, their timeouts, and
the absence of machine-specific paths.

These are text/AST checks on purpose.  They run anywhere, they need no Node
runtime, and they fail on exactly the regressions that would break a user's setup
silently: a renamed tool, a dropped timeout, or a hard-coded path from the machine
the file was written on.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_REPO_ENTRY = str(REPO_ROOT)
if _REPO_ENTRY in sys.path:
    sys.path.remove(_REPO_ENTRY)
sys.path.insert(0, _REPO_ENTRY)

INDEX_JS = REPO_ROOT / "dsh-cst-tools" / "index.js"
PACKAGE_JSON = REPO_ROOT / "dsh-cst-tools" / "package.json"
CORDIS_PATCH = REPO_ROOT / "dsh-cst-tools" / "cordis.patch.yml"
BRIDGE_PS1 = REPO_ROOT / "cst_ai_tool_bridge.ps1"

TOOLS = (
    "plan_cst_ai_task",
    "run_cst_ai_task",
    "get_cst_ai_run_status",
    "inspect_cst_ai_run",
    "resume_cst_ai_run",
)

#: bridges and scripts that belong to the private development tree and must never
#: be referenced by a published adapter
UNPUBLISHED_BRIDGES = (
    "cst_readonly_v16_bridge",
    "cst_production_bridge",
    "cst_generic_evaluator_bridge",
    "cst_solver_bridge",
    "cst_retuner_bridge",
    "cst_bridge.ps1",
)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


class TestToolAdapter(unittest.TestCase):
    def test_the_five_tools_are_registered(self):
        source = read(INDEX_JS)

        for tool in TOOLS:
            with self.subTest(tool=tool):
                self.assertIn(f"'{tool}'", source, f"{tool} is not registered")

    def test_the_plan_and_run_timeouts_are_present(self):
        source = read(INDEX_JS)

        self.assertIn("120000", source, "the short timeout is missing")
        self.assertIn("1800000", source, "the long (solving) timeout is missing")

    def test_inspect_section_enum_matches_the_adapter(self):
        source = read(INDEX_JS)

        for section in ("summary", "plan", "artifacts", "observables", "errors", "events", "all"):
            with self.subTest(section=section):
                self.assertIn(section, source)

    def test_no_unpublished_bridge_is_referenced(self):
        source = read(INDEX_JS)

        for bridge in UNPUBLISHED_BRIDGES:
            with self.subTest(bridge=bridge):
                self.assertNotIn(bridge, source, f"index.js references {bridge}")

    def test_bridge_path_is_resolved_not_hard_coded(self):
        source = read(INDEX_JS)

        self.assertIn("CST_AI_BRIDGE", source)
        self.assertIn("cst_ai_tool_bridge.ps1", source)

    def test_plugin_exports_the_dsh_shape(self):
        source = read(INDEX_JS)

        self.assertIn("export const name", source)
        self.assertIn("export const inject", source)
        self.assertIn("export function apply", source)

    def test_package_declares_the_plugin_bundle(self):
        import json

        package = json.loads(read(PACKAGE_JSON))

        self.assertEqual(package["name"], "dsh-cst-tools")
        self.assertEqual(package["type"], "module")
        self.assertIn("index.js", package["files"])
        self.assertEqual(package["dsh"]["bundle"]["patch"], "./cordis.patch.yml")
        self.assertTrue(CORDIS_PATCH.is_file())


class TestBridge(unittest.TestCase):
    def test_actions_are_validated(self):
        source = read(BRIDGE_PS1)

        for action in ("plan", "run", "status", "inspect", "resume"):
            with self.subTest(action=action):
                self.assertIn(f'"{action}"', source)

    def test_interpreter_is_overridable(self):
        source = read(BRIDGE_PS1)

        self.assertIn("CST_AI_PYTHON", source, "the interpreter must be overridable")
        self.assertIn("$PSScriptRoot", source, "the adapter path must be relative to the script")
        self.assertIn("PYTHONIOENCODING", source)

    def test_exit_code_is_propagated(self):
        self.assertIn("exit $LASTEXITCODE", read(BRIDGE_PS1))


class TestNoMachineSpecificPaths(unittest.TestCase):
    """No published file may name a personal directory or a user account."""

    def published_files(self) -> list[Path]:
        skip_dirs = {".git", "__pycache__"}
        return sorted(
            p for p in REPO_ROOT.rglob("*")
            if p.is_file() and not (set(p.parts) & skip_dirs)
        )

    def test_no_personal_absolute_paths(self):
        # assembled at runtime so this file does not match its own scan
        needles = ("C:" + "\\" + "Users" + "\\", "C:/" + "Users" + "/", "Alien" + "ware")

        offenders = []

        for path in self.published_files():
            try:
                text = path.read_text(encoding="utf-8-sig")
            except (UnicodeDecodeError, OSError):
                continue

            for needle in needles:
                if needle in text:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}: {needle}")

        self.assertEqual(offenders, [], f"machine-specific paths found: {offenders}")

    def test_no_internal_report_or_ledger_references(self):
        """No published *document* may point at an internal artefact directory.

        `.gitignore` is excluded: its whole job is to name directories that must
        never be committed, so listing them there is the mechanism, not a
        reference.  The needles are assembled at runtime for the same reason as
        above — this file must not match its own scan.
        """

        needles = ("reports" + "/", "ledger" + "/")
        offenders = []

        for path in self.published_files():
            if path.name == ".gitignore":
                continue

            try:
                text = path.read_text(encoding="utf-8-sig")
            except (UnicodeDecodeError, OSError):
                continue

            for needle in needles:
                if needle in text:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}: {needle}")

        self.assertEqual(offenders, [], f"internal artefacts referenced: {offenders}")

    def test_no_backup_or_editor_leftovers(self):
        bad = [
            str(p.relative_to(REPO_ROOT))
            for p in self.published_files()
            if re.search(r"\.(bak|orig|rej|swp|tmp)$", p.name)
        ]

        self.assertEqual(bad, [], f"leftover files: {bad}")


if __name__ == "__main__":
    unittest.main()
