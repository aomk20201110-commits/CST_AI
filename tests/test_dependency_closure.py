"""Dependency closure: the repository imports only the standard library, `cst`,
and its own modules.

Run from the repository root with the interpreter bundled with CST Studio Suite
(the one that provides the `cst` package):

    python -m unittest discover -s tests -v

This test exists because a hidden third-party import would fail on a user's machine
rather than here.  It parses every Python file in the tree, so adding a module is
enough for it to be checked.
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: this repository wins over anything else on the path, so the suite always tests
#: the tree it lives in and never a neighbouring checkout
if str(REPO_ROOT) in sys.path:
    sys.path.remove(str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT))

#: `cst` ships with CST Studio Suite, not with this repository; it is the API we
#: are written against, so it is an allowed import.
VENDOR_PACKAGES = {"cst"}


def python_files() -> list[Path]:
    return sorted(
        p for p in REPO_ROOT.rglob("*.py")
        if "__pycache__" not in p.parts and ".git" not in p.parts
    )


def local_module_names() -> set[str]:
    names = set()

    for p in REPO_ROOT.iterdir():
        if p.is_file() and p.suffix == ".py":
            names.add(p.stem)
        elif p.is_dir() and (p / "__init__.py").is_file():
            names.add(p.name)

    return names


def imported_top_levels(source: str) -> set[str]:
    tree = ast.parse(source)
    found: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import inside the package
                continue
            if node.module:
                found.add(node.module.split(".")[0])

    return found


class TestDependencyClosure(unittest.TestCase):
    def test_every_import_is_stdlib_vendor_or_local(self):
        allowed = set(sys.stdlib_module_names) | VENDOR_PACKAGES | local_module_names()
        offenders: dict[str, list[str]] = {}

        for path in python_files():
            source = path.read_text(encoding="utf-8-sig")
            external = sorted(imported_top_levels(source) - allowed)

            if external:
                offenders[str(path.relative_to(REPO_ROOT))] = external

        self.assertEqual(
            offenders, {},
            f"imports outside stdlib/{sorted(VENDOR_PACKAGES)}/repository: {offenders}",
        )

    def test_dynamic_imports_are_local(self):
        """`__import__` / `importlib` may only name repository modules."""

        allowed = local_module_names()
        offenders: dict[str, list[str]] = {}

        for path in python_files():
            source = path.read_text(encoding="utf-8-sig")
            tree = ast.parse(source)

            for node in ast.walk(tree):
                target = None

                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    if node.func.id == "__import__" and node.args:
                        target = node.args[0]
                elif (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "import_module"
                    and node.args
                ):
                    target = node.args[0]

                if isinstance(target, ast.Constant) and isinstance(target.value, str):
                    top = target.value.split(".")[0]

                    if top not in allowed and top not in sys.stdlib_module_names:
                        offenders.setdefault(
                            str(path.relative_to(REPO_ROOT)), []
                        ).append(target.value)

        self.assertEqual(offenders, {}, f"dynamic imports outside the repository: {offenders}")

    def test_all_modules_parse(self):
        """A syntax error in any module fails here, not at a user's first call.

        Files are read as utf-8-sig: two modules in this tree begin with a BOM.
        """

        broken = []

        for path in python_files():
            try:
                ast.parse(path.read_text(encoding="utf-8-sig"))
            except SyntaxError as exc:
                broken.append(f"{path.relative_to(REPO_ROOT)}: {exc}")

        self.assertEqual(broken, [], f"unparseable modules: {broken}")

    def test_tool_adapter_is_thin(self):
        """The adapter may reach the plugin's public API and nothing deeper.

        The check reads identifiers out of the AST rather than raw text, so the
        module may still *name* the internals it refuses to use in its docstring.
        """

        source = (REPO_ROOT / "cst_ai_tool.py").read_text(encoding="utf-8-sig")

        self.assertIn("from cst_ai_plugin import public", source)

        identifiers = {
            node.id for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Name)
        } | {
            node.attr for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Attribute)
        }

        for forbidden in (
            "cst_session_discovery_v19a",
            "cst_result_api_v19",
            "v19b_real_solve",
            "v110c_analysis",
            "Orchestrator",
            "runstore",
            "cst_cli",
        ):
            self.assertNotIn(
                forbidden, identifiers,
                f"cst_ai_tool.py must not use {forbidden}",
            )

    def test_adapter_never_imports_cst(self):
        source = (REPO_ROOT / "cst_ai_tool.py").read_text(encoding="utf-8-sig")
        tops = imported_top_levels(source)

        self.assertNotIn("cst", tops, "the adapter must not import cst directly")


if __name__ == "__main__":
    unittest.main()
